"""
event_layer/clustering.py — Event clustering via embedding cosine similarity.

For each selected candidate, finds cluster mates among the full candidate pool
using cosine similarity on crawler embeddings (BAAI/bge-small-en-v1.5, 384-dim).

Two-tier clustering decision (Option A upgrade):
  Layer 1 — candidate filter:  cosine >= COSINE_CLUSTER_THRESHOLD (0.75)
  Layer 2 — validation gate:   auto-merge if cosine >= COSINE_AUTO_MERGE (0.85),
                                otherwise require >= 1 shared keyword in titles.

This prevents topically-related-but-distinct events from merging incorrectly
while still catching same-event rephrases across locales and platforms.

Phase 3 (future): LLM classifier for borderline pairs where keyword check is
                  unreliable (e.g. very short titles, pure CJK vs pure EN).

Source role classification (platform-based):
  fact:     Reuters / AP / BBC / Guardian / NYT / WaPo / Al Jazeera / FT / Bloomberg
  reaction: Reddit / YouTube / Bilibili / Twitter / X / Weibo
  context:  everything else (mainstream news, regional outlets)

Key constants
-------------
COSINE_AUTO_MERGE = 0.85
    Auto-accept threshold: same-event rephrases on BGE-small-en-v1.5 score here.

COSINE_CLUSTER_THRESHOLD = 0.75
    Candidate filter: items in [0.75, 0.85) are accepted only with keyword overlap.
    Items below 0.75 fall into the event_graph band (related but distinct events).

MAX_CLUSTER_MEMBERS = 5
    Cap on corroborating sources per cluster to bound prompt size.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import timezone

from db.crawler_reader import get_embeddings
from engine.selector.schemas import NormalizedCandidate

logger = logging.getLogger(__name__)

# Two-tier clustering thresholds:
#   >= COSINE_AUTO_MERGE   → same event, merge immediately (high confidence)
#   >= COSINE_CLUSTER_THRESHOLD and keyword overlap >= 1 → merge after validation
#   <  COSINE_CLUSTER_THRESHOLD → reject (or fall into event_graph band)
COSINE_AUTO_MERGE        = 0.85   # auto-accept: no further validation needed
COSINE_CLUSTER_THRESHOLD = 0.75   # candidate filter: requires keyword validation
MAX_CLUSTER_MEMBERS = 5

FACT_PLATFORMS: frozenset[str] = frozenset({
    'reuters', 'ap', 'apnews', 'bbc', 'guardian', 'nytimes', 'wapo',
    'wsj', 'aljazeera', 'ft', 'bloomberg', 'nikkei',
})
REACTION_PLATFORMS: frozenset[str] = frozenset({
    'reddit', 'twitter', 'x', 'youtube', 'bilibili', 'weibo',
    'instagram', 'tiktok', 'mastodon',
})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class EventCluster:
    """A group of articles covering the same real-world event."""

    event_id:         str                            # sha256[:16] of representative URL
    representative:   NormalizedCandidate            # highest-hotness article
    fact_sources:     list[NormalizedCandidate] = field(default_factory=list)
    context_sources:  list[NormalizedCandidate] = field(default_factory=list)
    reaction_sources: list[NormalizedCandidate] = field(default_factory=list)
    embedding_center: list[float] | None = None      # mean of member embeddings
    event_hotness:    float = 0.0                    # set by hotness.compute_event_hotness()
    member_count:     int = 1
    novelty_score:    float = 1.0                    # 0.0–1.0; set by selector after memory check
                                                     # 1.0 = new event (or soft-penalised duplicate
                                                     #        whose penalty is in effective_hotness)
                                                     # 0.7 = new development (follow-up)
    timeline:         list[dict] = field(default_factory=list)
                                                     # [{timestamp, title, platform, role}]
                                                     # sorted ascending by freshness


# Stopwords for keyword overlap (mirrors memory.py — kept local to avoid coupling)
_STOPWORDS: frozenset[str] = frozenset({
    'a', 'an', 'the', 'this', 'that', 'these', 'those',
    'from', 'to', 'in', 'on', 'at', 'by', 'for', 'with', 'as', 'of',
    'about', 'into', 'through', 'over', 'under', 'after', 'before',
    'between', 'up', 'down', 'out', 'off', 'and', 'or', 'but', 'nor',
    'so', 'yet', 'both', 'either', 'neither', 'than', 'not', 'no',
    'it', 'its', 'he', 'she', 'they', 'we', 'you', 'i',
    'his', 'her', 'their', 'our', 'your', 'my',
    'me', 'him', 'them', 'us', 'who', 'which', 'what', 'where',
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did',
    'will', 'would', 'shall', 'should', 'may', 'might', 'can', 'could',
    'also', 'just', 'still', 'now', 'here', 'there', 'when', 'how', 'why',
    'more', 'most', 'much', 'many', 'some', 'any', 'all', 'each', 'every',
    'says', 'said', 'say',
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
    """Lowercase alphanumeric tokens (ASCII + CJK), stopwords removed."""
    tokens = set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text.lower()))
    return tokens - _STOPWORDS


def _keyword_overlap(a: NormalizedCandidate, b: NormalizedCandidate) -> bool:
    """
    Return True when the two candidates share at least one meaningful keyword.

    Used as the validation gate for borderline cosine pairs (0.75 <= sim < 0.85).

    Two-pass comparison to handle cross-locale pairs (e.g. ZH vs EN):
      Pass 1 — title_original: works when both posts are in the same language.
               A ZH post vs EN post will have zero token overlap here.
      Pass 2 — canonical_title: English-normalised title present for all items.
               Cross-locale pairs that share an event will overlap here even
               when their title_original tokens are disjoint.
    Returns True as soon as either pass finds overlap.
    """
    # Pass 1: title_original (same-locale pairs)
    orig_a = a.title_original or ''
    orig_b = b.title_original or ''
    if orig_a and orig_b and (_tokenize(orig_a) & _tokenize(orig_b)):
        return True

    # Pass 2: canonical_title (cross-locale pairs — both normalised to English)
    canon_a = a.canonical_title or ''
    canon_b = b.canonical_title or ''
    if canon_a and canon_b and (_tokenize(canon_a) & _tokenize(canon_b)):
        return True

    return False


def _source_role(platform: str) -> str:
    p = platform.lower()
    if p in FACT_PLATFORMS:
        return 'fact'
    if p in REACTION_PLATFORMS:
        return 'reaction'
    return 'context'


def _embedding_center(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    dim = len(vectors[0])
    center = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            center[i] += x
    n = len(vectors)
    return [x / n for x in center]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_clusters(
    selected: list[NormalizedCandidate],
    pool: list[NormalizedCandidate],
) -> dict[str, EventCluster]:
    """
    Build EventCluster objects for each selected candidate.

    For each selected candidate that has an embedding, scans the full pool
    for cluster mates using the two-tier decision:
      sim >= COSINE_AUTO_MERGE (0.85)        → auto-merge
      sim >= COSINE_CLUSTER_THRESHOLD (0.75) → merge only if keyword overlap >= 1
    Mates are classified by source role (fact / context / reaction).

    Candidates without embeddings become singleton clusters with the
    representative classified into the appropriate role bucket.

    Args:
        selected: Candidates chosen by Stage 3/4 (one per format slot).
        pool:     Full Stage 1 candidate list including non-selected items.

    Returns:
        {candidate_id: EventCluster} — one entry per selected candidate.
    """
    if not selected:
        return {}

    # Collect all item IDs needing embeddings in one DB round-trip
    selected_item_ids = [c.crawler_item_id for c in selected]
    pool_item_ids     = [c.crawler_item_id for c in pool]
    all_item_ids      = list(set(selected_item_ids + pool_item_ids))

    embeddings = get_embeddings(all_item_ids)
    logger.info(
        "Clustering: %d embeddings fetched for %d unique items (%d selected, %d pool)",
        len(embeddings), len(all_item_ids), len(selected), len(pool),
    )

    # Build pool index: (candidate, embedding) pairs — only embedded pool items
    pool_index: list[tuple[NormalizedCandidate, list[float]]] = [
        (c, embeddings[c.crawler_item_id])
        for c in pool
        if c.crawler_item_id in embeddings
    ]

    clusters: dict[str, EventCluster] = {}

    for sel_cand in selected:
        event_id = hashlib.sha256(sel_cand.url.encode()).hexdigest()[:16]
        sel_emb  = embeddings.get(sel_cand.crawler_item_id)

        if sel_emb is None:
            # No embedding yet — singleton cluster
            cluster = _singleton_cluster(event_id, sel_cand)
            clusters[sel_cand.candidate_id] = cluster
            continue

        # Score every pool candidate against the selected one.
        # Two-tier decision:
        #   sim >= COSINE_AUTO_MERGE (0.85)        → same event, auto-accept
        #   sim >= COSINE_CLUSTER_THRESHOLD (0.75) → candidate; accept only if
        #                                            titles share >= 1 keyword
        mates: list[tuple[float, NormalizedCandidate]] = []
        for pool_cand, pool_emb in pool_index:
            if pool_cand.candidate_id == sel_cand.candidate_id:
                continue
            sim = _cosine(sel_emb, pool_emb)
            if sim >= COSINE_AUTO_MERGE:
                mates.append((sim, pool_cand))                  # auto-merge
            elif sim >= COSINE_CLUSTER_THRESHOLD and _keyword_overlap(sel_cand, pool_cand):
                mates.append((sim, pool_cand))                  # validated merge

        # Keep top MAX_CLUSTER_MEMBERS by similarity
        mates.sort(key=lambda x: -x[0])
        mates = mates[:MAX_CLUSTER_MEMBERS]

        # Classify mates by source role
        fact_sources:     list[NormalizedCandidate] = []
        context_sources:  list[NormalizedCandidate] = []
        reaction_sources: list[NormalizedCandidate] = []
        member_embeddings = [sel_emb]

        for _, mate in mates:
            role = _source_role(mate.platform)
            if role == 'fact':
                fact_sources.append(mate)
            elif role == 'reaction':
                reaction_sources.append(mate)
            else:
                context_sources.append(mate)
            mate_emb = embeddings.get(mate.crawler_item_id)
            if mate_emb:
                member_embeddings.append(mate_emb)

        # Classify the representative itself
        sel_role = _source_role(sel_cand.platform)
        if sel_role == 'fact':
            fact_sources.insert(0, sel_cand)
        elif sel_role == 'reaction':
            reaction_sources.insert(0, sel_cand)
        else:
            context_sources.insert(0, sel_cand)

        all_members_sorted = sorted(
            fact_sources + context_sources + reaction_sources,
            key=lambda c: c.freshness,
        )
        timeline = [
            {
                'timestamp': c.freshness.isoformat(),
                'title':     c.title_original or c.canonical_title or '',
                'platform':  c.platform,
                'role':      _source_role(c.platform),
            }
            for c in all_members_sorted
        ]

        cluster = EventCluster(
            event_id         = event_id,
            representative   = sel_cand,
            fact_sources     = fact_sources,
            context_sources  = context_sources,
            reaction_sources = reaction_sources,
            embedding_center = _embedding_center(member_embeddings),
            member_count     = 1 + len(mates),
            timeline         = timeline,
        )
        clusters[sel_cand.candidate_id] = cluster

        if mates:
            auto_count      = sum(1 for sim, _ in mates if sim >= COSINE_AUTO_MERGE)
            validated_count = len(mates) - auto_count
            logger.debug(
                "Cluster %r: %d mates (auto=%d validated=%d | fact=%d ctx=%d react=%d)",
                (sel_cand.title_original or sel_cand.canonical_title or '')[:50],
                len(mates), auto_count, validated_count,
                len(fact_sources), len(context_sources), len(reaction_sources),
            )

    embedded_count = sum(1 for c in selected if c.crawler_item_id in embeddings)
    multi_count    = sum(1 for cl in clusters.values() if cl.member_count > 1)
    logger.info(
        "Clustering complete: %d/%d selected had embeddings, %d multi-source clusters formed",
        embedded_count, len(selected), multi_count,
    )
    return clusters


def _singleton_cluster(event_id: str, candidate: NormalizedCandidate) -> EventCluster:
    """Wrap a single candidate as a lone-member cluster."""
    role = _source_role(candidate.platform)
    timeline = [
        {
            'timestamp': candidate.freshness.isoformat(),
            'title':     candidate.title_original or candidate.canonical_title or '',
            'platform':  candidate.platform,
            'role':      role,
        }
    ]
    return EventCluster(
        event_id         = event_id,
        representative   = candidate,
        fact_sources     = [candidate] if role == 'fact'     else [],
        context_sources  = [candidate] if role == 'context'  else [],
        reaction_sources = [candidate] if role == 'reaction' else [],
        member_count     = 1,
        timeline         = timeline,
    )
