"""
event_layer/clustering.py — Event clustering via embedding cosine similarity.

For each selected candidate, finds cluster mates among the full candidate pool
using cosine similarity on crawler embeddings (BAAI/bge-small-en-v1.5, 384-dim).

Phase 1 (current): cosine threshold only (fast, no LLM cost).
Phase 2 (future):  add LLM classifier for borderline pairs (0.60–0.75 range).

Source role classification (platform-based):
  fact:     Reuters / AP / BBC / Guardian / NYT / WaPo / Al Jazeera / FT / Bloomberg
  reaction: Reddit / YouTube / Bilibili / Twitter / X / Weibo
  context:  everything else (mainstream news, regional outlets)

Key constants
-------------
COSINE_CLUSTER_THRESHOLD = 0.75
    Two articles scoring >= this share the same real-world event. Calibrated
    against BGE-small-en-v1.5: same-event rephrases typically score 0.85+,
    topically-related-but-different score 0.60-0.74, unrelated < 0.50.

MAX_CLUSTER_MEMBERS = 5
    Cap on corroborating sources per cluster to bound prompt size.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import timezone

from db.crawler_reader import get_embeddings
from engine.selector.schemas import NormalizedCandidate

logger = logging.getLogger(__name__)

COSINE_CLUSTER_THRESHOLD = 0.75
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
    for cluster mates above COSINE_CLUSTER_THRESHOLD and classifies them by
    source role (fact / context / reaction).

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

        # Score every pool candidate against the selected one
        mates: list[tuple[float, NormalizedCandidate]] = []
        for pool_cand, pool_emb in pool_index:
            if pool_cand.candidate_id == sel_cand.candidate_id:
                continue
            sim = _cosine(sel_emb, pool_emb)
            if sim >= COSINE_CLUSTER_THRESHOLD:
                mates.append((sim, pool_cand))

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
            logger.debug(
                "Cluster %r: %d mates (fact=%d ctx=%d react=%d)",
                sel_cand.title_original[:50],
                len(mates), len(fact_sources), len(context_sources), len(reaction_sources),
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
