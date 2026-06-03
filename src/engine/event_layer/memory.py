"""
event_layer/memory.py — Event dedup memory (Phase 1 + Phase 2).

Compares incoming candidates against recently told stories so the pipeline
does not re-tell the same event within the dedup window.

Three-way decision
------------------
duplicate       sim >= threshold
                Same event retold. Hard-filtered if seen < 1 day ago;
                soft repetition_penalty applied if 1–7 days ago.

new_development lower <= sim < threshold
                Clearly related (same person/topic/policy) but a different angle
                or genuine update (e.g. "Congress reacts to Swalwell resignation"
                after "Swalwell resigns"). Allow through; generators frame it as
                an update, not a retelling.

new_event       sim < lower
                Unrelated — allow through normally.

Similarity method (auto-selected per candidate)
-----------------------------------------------
Phase 2 (cosine):   Used when BOTH the candidate has a crawler embedding AND
                    the matched event has a stored embedding_center.
                    Thresholds: _DUPLICATE_THRESHOLD_COSINE = 0.85
                                _NEW_DEV_LOWER_COSINE       = 0.65
                    BGE-small-en-v1.5: same-event rephrases ~0.85+,
                    topically-related ~0.65–0.84, unrelated < 0.60.

Phase 1 (Jaccard):  Fallback when either side lacks an embedding.
                    Thresholds: _DUPLICATE_THRESHOLD_JACCARD = 0.35
                                _NEW_DEV_LOWER_JACCARD       = 0.10

Public API
----------
classify_candidates(candidates, window_days) -> dict[str, tuple[str, str, float]]
    Returns {url: (decision, prior_story_title, days_since_last_seen)} for
    candidates that score above the active lower threshold.
    Decision is 'duplicate' or 'new_development'.
    days_since_last_seen is the age of the matched event in days (float).
    URLs absent from the result are new events (allowed through).

store_event(...)
    Thin delegation to db.models.store_event(). Imported here so callers only
    need to import from event_layer.memory.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path

from db.crawler_reader import get_embeddings
from db.models import load_recent_events, store_event as _store_event  # noqa: F401

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Published-video dedup (topic-cluster cooldown)
# ---------------------------------------------------------------------------
# Jaccard threshold for matching candidates against recently PUBLISHED YouTube
# videos. Lower than the event-memory threshold (0.20 vs 0.35) because we want
# to catch "same event, different angle" pairs that slipped through event-memory
# (e.g., three Ebola stories published across consecutive batches).
_PUBLISHED_DUP_THRESHOLD_JACCARD = 0.20
_PUBLISHED_DUP_WINDOW_DAYS       = 14  # 14-day cooldown for published topics


def load_published_video_titles(window_days: int = 14) -> list[dict]:
    """
    Return recently published YouTube video titles from youtube_publish_log,
    joined with hierarchical_stories to get the generated story title.
    Used to enforce a topic-cluster cooldown across production runs.
    """
    try:
        from db.models import get_connection
        conn = get_connection()
        cutoff_sec = int(time.time()) - window_days * 86400
        rows = conn.execute(
            """
            SELECT
                ypl.video_id,
                ypl.published_at,
                COALESCE(
                    json_extract(hs.deep_story, '$.title'),
                    json_extract(hs.deep_story, '$.hook'),
                    ''
                ) AS story_title
            FROM youtube_publish_log ypl
            LEFT JOIN story_sets ss ON ss.id = ypl.story_set_id
            LEFT JOIN hierarchical_stories hs ON hs.story_set_id = ss.id
            WHERE ypl.published_at IS NOT NULL
              AND ypl.published_at >= ?
              AND story_title != ''
            ORDER BY ypl.published_at DESC
            """,
            (cutoff_sec,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("load_published_video_titles failed: %s", exc)
        return []


def check_against_published(
    candidates,  # list[NormalizedCandidate]
    window_days: int = _PUBLISHED_DUP_WINDOW_DAYS,
) -> set[str]:
    """
    Return a set of candidate URLs whose titles match a recently published
    YouTube video title above _PUBLISHED_DUP_THRESHOLD_JACCARD.
    Used to enforce topic-cluster cooldown (14-day window, lower threshold).
    """
    published = load_published_video_titles(window_days=window_days)
    if not published:
        return set()

    blocked_urls: set[str] = set()
    for candidate in candidates:
        cand_title = candidate.title_original or candidate.canonical_title or ''
        if not cand_title:
            continue
        cand_tokens = _tokenize(cand_title)
        if len(cand_tokens) < _MIN_TOKENS:
            continue

        for pub in published:
            pub_title = pub.get('story_title', '')
            if not pub_title:
                continue
            sim = _similarity(cand_title, pub_title)
            if sim >= _PUBLISHED_DUP_THRESHOLD_JACCARD:
                days_since = max(0.0, (time.time() - (pub.get('published_at') or 0)) / 86400.0)
                logger.debug(
                    "published_dedup: blocked url=%r sim=%.2f days_since=%.1f pub_title=%r",
                    candidate.url[:70], sim, days_since, pub_title[:60],
                )
                blocked_urls.add(candidate.url)
                break

    return blocked_urls


# ---------------------------------------------------------------------------
# Config loader for tunable thresholds
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "clustering_config.json"
# parents[3] = story_engine/  (this file is at story_engine/src/engine/event_layer/)
# Resolves to: story_engine/config/clustering_config.json


def _load_jaccard_threshold() -> float:
    """Load jaccard_duplicate_threshold from clustering_config.json.

    Cached at module import time — a process restart is required after
    changing the config value. Falls back to 0.35 if config is missing
    or malformed.
    """
    try:
        with open(_CONFIG_PATH) as f:
            value = float(json.load(f).get("jaccard_duplicate_threshold", 0.35))
        logger.info("[memory] Loaded jaccard_duplicate_threshold: %.2f from config", value)
        return value
    except Exception as exc:
        logger.warning(
            "[memory] Could not load jaccard_duplicate_threshold from config (%s); "
            "using fallback 0.35",
            exc,
        )
        return 0.35

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Phase 2 — cosine similarity (BGE-small-en-v1.5, 384-dim)
# Used when BOTH candidate and stored event have embeddings.
_DUPLICATE_THRESHOLD_COSINE = 0.85
_NEW_DEV_LOWER_COSINE       = 0.65

# Phase 1 — Jaccard similarity (stopword-filtered tokens)
# Fallback when embeddings are unavailable on either side.
#
# _DUPLICATE_THRESHOLD_JACCARD (configurable via clustering_config.json)
#   Default was 0.35; lowered to 0.20 to catch same-event different-framing pairs
#   (e.g. two Iran warship stories scored 0.27, slipping through at 0.35).
#   "Eric Swalwell resigns from Congress" vs "Rep Eric Swalwell resigns from
#   US House" → 0.50 (still caught at 0.20).
#   To tune: edit jaccard_duplicate_threshold in config/clustering_config.json
#   and restart the process (value is cached at import time).
#
# _NEW_DEV_LOWER_JACCARD = 0.10
#   Catches follow-up angles that share one or two key tokens.
_DUPLICATE_THRESHOLD_JACCARD = _load_jaccard_threshold()
_NEW_DEV_LOWER_JACCARD       = 0.10

# Minimum token count in a title (after stopword removal) to attempt matching.
_MIN_TOKENS = 2

# Common English words that appear across many headlines and carry no
# event-distinguishing signal. Filtering them makes Jaccard focus on
# the specific entities and nouns that actually identify an event.
# Intentionally kept modest — domain nouns (congress, president, market…)
# are NOT filtered because they do carry signal in some contexts.
_STOPWORDS: frozenset[str] = frozenset({
    # articles / determiners
    'a', 'an', 'the', 'this', 'that', 'these', 'those',
    # prepositions / conjunctions
    'from', 'to', 'in', 'on', 'at', 'by', 'for', 'with', 'as', 'of',
    'about', 'into', 'through', 'over', 'under', 'after', 'before',
    'between', 'up', 'down', 'out', 'off', 'and', 'or', 'but', 'nor',
    'so', 'yet', 'both', 'either', 'neither', 'than', 'not', 'no',
    # common pronouns / possessives
    'it', 'its', 'he', 'she', 'they', 'we', 'you', 'i',
    'his', 'her', 'their', 'our', 'your', 'my',
    'me', 'him', 'them', 'us', 'who', 'which', 'what', 'where',
    # auxiliary verbs
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did',
    'will', 'would', 'shall', 'should', 'may', 'might', 'can', 'could',
    # common adverbs / misc
    'also', 'just', 'still', 'now', 'here', 'there', 'when', 'how', 'why',
    'more', 'most', 'much', 'many', 'some', 'any', 'all', 'each', 'every',
    'says', 'said', 'say',   # attribution verbs that appear in nearly every headline
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in pure Python (no numpy dependency)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tokenize(text: str) -> set[str]:
    """
    Lowercase alphanumeric content tokens (ASCII + CJK), stopwords removed.

    Filtering common structural words means similarity is driven by entity
    tokens (names, specific nouns) rather than the news-template skeleton
    ('X resigns from Y' shares 'from' with every resignation story).
    """
    tokens = set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text.lower()))
    return tokens - _STOPWORDS


def _similarity(title_a: str, title_b: str) -> float:
    """
    Jaccard similarity between two title strings.

    Phase 2: replace body with:
        vec_a = embedding_from_crawler(title_a)
        vec_b = embedding_from_crawler(title_b)
        return cosine(vec_a, vec_b)
    """
    wa = _tokenize(title_a)
    wb = _tokenize(title_b)
    if len(wa) < _MIN_TOKENS or len(wb) < _MIN_TOKENS:
        return 0.0
    union = wa | wb
    if not union:
        return 0.0
    return len(wa & wb) / len(union)


def _best_sim_to_event(candidate_title: str, event: dict) -> float:
    """
    Max similarity between a candidate title and an event's source titles.

    We take max (not avg) because one strong title match is sufficient to
    identify the same real-world event across different phrasings.
    """
    best = 0.0
    for src_title in event.get('source_titles', []):
        if src_title:
            score = _similarity(candidate_title, src_title)
            if score > best:
                best = score
    # Also compare against the story's own title
    story_title = event.get('story_title', '')
    if story_title:
        score = _similarity(candidate_title, story_title)
        if score > best:
            best = score
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_candidates(
    candidates,          # list[NormalizedCandidate]
    window_days: int = 7,
) -> dict[str, tuple[str, str, float]]:
    """
    Three-way classification of candidates against recent event memory.

    Similarity method is auto-selected per candidate:
      - Phase 2 (cosine): when BOTH the candidate has a crawler embedding AND
        the best-matched event has a stored embedding_center. Uses cosine
        thresholds (_DUPLICATE_THRESHOLD_COSINE / _NEW_DEV_LOWER_COSINE).
      - Phase 1 (Jaccard): fallback when either side lacks an embedding. Uses
        Jaccard thresholds (_DUPLICATE_THRESHOLD_JACCARD / _NEW_DEV_LOWER_JACCARD).

    Fetches candidate embeddings in one batch DB call at the start.

    Returns:
        {url: (decision, prior_story_title, days_since_last_seen)} for
        candidates scoring above the active lower threshold.
        Decision is 'duplicate' or 'new_development'.
        days_since_last_seen is the age of the matched event in days (float).
        URLs absent from the result are new events — no entry added.
    """
    recent_events = load_recent_events(window_days=window_days)
    if not recent_events:
        return {}

    # Batch-fetch embeddings for all candidates in one DB round-trip.
    item_ids = [c.crawler_item_id for c in candidates if c.crawler_item_id]
    candidate_embeddings: dict[int, list[float]] = {}
    if item_ids:
        try:
            candidate_embeddings = get_embeddings(item_ids)
        except Exception as _emb_exc:
            logger.warning("event_memory: embedding fetch failed, using Jaccard only: %s", _emb_exc)

    cosine_count  = 0
    jaccard_count = 0
    now = time.time()
    decisions: dict[str, tuple[str, str, float]] = {}

    for candidate in candidates:
        title   = candidate.title_original or ''
        cand_emb = candidate_embeddings.get(candidate.crawler_item_id)

        best_sim   = 0.0
        best_event = None
        used_cosine = False

        for event in recent_events:
            event_emb = event.get('embedding_center')
            if cand_emb and event_emb:
                # Phase 2: cosine on stored embedding_center
                sim = _cosine(cand_emb, event_emb)
                _cosine_used = True
            else:
                # Phase 1 fallback: Jaccard on title tokens
                if not title:
                    continue
                sim = _best_sim_to_event(title, event)
                _cosine_used = False

            if sim > best_sim:
                best_sim    = sim
                best_event  = event
                used_cosine = _cosine_used

        if best_event is None:
            continue

        # Apply thresholds appropriate to the similarity method used
        dup_thresh = _DUPLICATE_THRESHOLD_COSINE if used_cosine else _DUPLICATE_THRESHOLD_JACCARD
        dev_lower  = _NEW_DEV_LOWER_COSINE       if used_cosine else _NEW_DEV_LOWER_JACCARD

        if best_sim < dev_lower:
            continue   # new_event — no entry

        prior_title = best_event.get('story_title') or ''
        created_at  = best_event.get('created_at') or 0
        days_since  = max(0.0, (now - created_at) / 86400.0) if created_at else 999.0
        method      = 'cosine' if used_cosine else 'jaccard'

        if used_cosine:
            cosine_count += 1
        else:
            jaccard_count += 1

        if best_sim >= dup_thresh:
            decisions[candidate.url] = ('duplicate', prior_title, days_since)
            logger.debug(
                "event_memory duplicate [%s]: url=%r sim=%.3f days_since=%.1f matched=%r",
                method, candidate.url[:70], best_sim, days_since, prior_title[:50],
            )
        else:
            decisions[candidate.url] = ('new_development', prior_title, days_since)
            logger.debug(
                "event_memory new_development [%s]: url=%r sim=%.3f days_since=%.1f prior=%r",
                method, candidate.url[:70], best_sim, days_since, prior_title[:50],
            )

    dup_count = sum(1 for d, _, _2 in decisions.values() if d == 'duplicate')
    dev_count = sum(1 for d, _, _2 in decisions.values() if d == 'new_development')
    if decisions or (cosine_count + jaccard_count) > 0:
        logger.info(
            "event_memory: %d duplicate, %d new_development out of %d candidates "
            "(cosine=%d jaccard=%d)",
            dup_count, dev_count, len(candidates), cosine_count, jaccard_count,
        )

    return decisions
