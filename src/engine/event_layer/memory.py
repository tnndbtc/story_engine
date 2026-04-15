"""
event_layer/memory.py — Event dedup memory (Phase 1).

Compares incoming candidates against recently told stories so the pipeline
does not re-tell the same event within the dedup window.

Three-way decision
------------------
duplicate       sim >= _DUPLICATE_THRESHOLD (0.35)
                Same event retold. Hard-filtered if seen < 1 day ago;
                soft repetition_penalty applied if 1–7 days ago.

new_development _NEW_DEV_LOWER (0.10) <= sim < _DUPLICATE_THRESHOLD
                Clearly related (same person/topic/policy) but a different angle
                or genuine update (e.g. "Congress reacts to Swalwell resignation"
                after "Swalwell resigns"). Allow through; generators frame it as
                an update, not a retelling.

new_event       sim < _NEW_DEV_LOWER
                Unrelated — allow through normally.

Thresholds (Jaccard, stopword-filtered tokens, calibrated on English news headlines)
-------------------------------------------------------------------------------------
_DUPLICATE_THRESHOLD = 0.35
    "Eric Swalwell resigns from Congress" vs "Rep Eric Swalwell resigns from
    US House" → 0.50 (same event, caught correctly).
    "Tony Gonzales resigns from Congress" vs Swalwell stored event → 0.33
    (new_development — correct: same political moment, different person).

_NEW_DEV_LOWER = 0.10
    "Swalwell successor named to fill vacated seat" vs Swalwell stored event
    → 0.11 (shares "swalwell" token → correctly flagged as new_development).

Public API
----------
classify_candidates(candidates, window_days) -> dict[str, tuple[str, str, float]]
    Returns {url: (decision, prior_story_title, days_since_last_seen)} for
    candidates that score above _NEW_DEV_LOWER. Decision is 'duplicate' or
    'new_development'. days_since_last_seen is the age of the matched event
    in days. URLs absent from the result are new events (allowed through).

store_event(...)
    Thin delegation to db.models.store_event(). Imported here so callers only
    need to import from event_layer.memory.

Phase 2 upgrade path
--------------------
1. Crawler adds `embedding` (float[]) to trenditem rows.
2. store_event() saves embedding_center (mean of source embeddings) to event_memory.
3. Replace _similarity() below with cosine(candidate.embedding, event.embedding_center).
4. Update thresholds: _DUPLICATE_THRESHOLD ≈ 0.85, _NEW_DEV_LOWER ≈ 0.65 (cosine).
"""

from __future__ import annotations

import logging
import re
import time

from db.models import load_recent_events, store_event as _store_event  # noqa: F401

logger = logging.getLogger(__name__)

# Jaccard thresholds (Phase 1, stopword-filtered tokens).
#
# _DUPLICATE_THRESHOLD = 0.35
#   "Eric Swalwell resigns from Congress" vs "Rep Eric Swalwell resigns from
#   US House" → 0.50 (caught). Set at 0.35 to exclude the 0.30–0.34 band that
#   caused false positives on same-template-different-entity pairs.
#
# _NEW_DEV_LOWER = 0.10
#   Catches follow-up angles that share one or two key tokens (a name, a topic
#   keyword). "Swalwell successor named to fill vacated seat" shares only
#   "swalwell" → Jaccard ≈ 0.11 (above threshold → new_development ✓).
#   "Tony Gonzales resigns from Congress" on the day Swalwell also resigned
#   shares "congress" + "resigns" → ≈ 0.33 (new_development — correct, they
#   ARE related events from the same political moment).
_DUPLICATE_THRESHOLD = 0.35   # >= this → same event retold, exclude
_NEW_DEV_LOWER       = 0.10   # >= this (and < DUPLICATE) → new development, flag

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

    For each candidate, computes max Jaccard similarity against all source
    titles and the story title of events told within window_days.

    Returns:
        {url: (decision, prior_story_title, days_since_last_seen)} for
        candidates scoring above _NEW_DEV_LOWER.
        Decision is 'duplicate' or 'new_development'.
        days_since_last_seen is the age of the matched event in days (float).
        URLs absent from the result are new events — no entry added.
    """
    recent_events = load_recent_events(window_days=window_days)
    if not recent_events:
        return {}

    now = time.time()
    decisions: dict[str, tuple[str, str, float]] = {}

    for candidate in candidates:
        title = candidate.title_original or ''
        if not title:
            continue

        best_sim   = 0.0
        best_event = None

        for event in recent_events:
            sim = _best_sim_to_event(title, event)
            if sim > best_sim:
                best_sim   = sim
                best_event = event

        if best_sim < _NEW_DEV_LOWER or best_event is None:
            continue   # new_event — no entry

        prior_title = best_event.get('story_title', '')
        created_at  = best_event.get('created_at') or 0
        days_since  = max(0.0, (now - created_at) / 86400.0) if created_at else 999.0

        if best_sim >= _DUPLICATE_THRESHOLD:
            decisions[candidate.url] = ('duplicate', prior_title, days_since)
            logger.debug(
                "event_memory duplicate: url=%r sim=%.2f days_since=%.1f matched=%r",
                candidate.url[:70], best_sim, days_since, prior_title[:50],
            )
        else:
            decisions[candidate.url] = ('new_development', prior_title, days_since)
            logger.debug(
                "event_memory new_development: url=%r sim=%.2f days_since=%.1f prior=%r",
                candidate.url[:70], best_sim, days_since, prior_title[:50],
            )

    dup_count = sum(1 for d, _, _2 in decisions.values() if d == 'duplicate')
    dev_count = sum(1 for d, _, _2 in decisions.values() if d == 'new_development')
    if decisions:
        logger.info(
            "event_memory: %d duplicate, %d new_development out of %d candidates",
            dup_count, dev_count, len(candidates),
        )

    return decisions
