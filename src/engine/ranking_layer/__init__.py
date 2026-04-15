"""
ranking_layer/ — Single authoritative event scoring entry point.

This module is the ONLY place where event_score() is defined.
All ranking decisions flow through here.

Per story.txt architecture:
    ranking_layer/
        event_scoring(event)          ← event_score() below
        diversity scoring             ← selector/stage2_allocate.py (global caps)
        global cap allocator          ← selector/stage2_allocate.py (platform_budgets)
        novelty filter                ← selector/stage1_normalize.py (memory classification)

Public API
----------
event_score(cluster) -> float
    Unified score for a fully-built EventCluster. Incorporates:
      - base hotness (log-aggregated, top-K members)
      - diversity bonus (fact source, multi-platform, multi-member)
      - recency decay (age of representative article)
      - source quality factor (multi-source minimum enforcement)
      - cross-platform bonus (underrepresented platforms)
      - novelty bonus (new_development events promoted)

    novelty_bonus and repetition_penalty are applied PRE-SELECTION in
    stage1_normalize.py to effective_hotness so they influence stage3
    ranking. event_score() is the POST-SELECTION unified score passed
    to generators and used for observability/trace logging.

    Returns cluster.event_hotness (already computed by hotness.py)
    multiplied by novelty_bonus from cluster.novelty_score.
"""

from __future__ import annotations

from engine.event_layer.clustering import EventCluster
from engine.event_layer.hotness import compute_event_hotness

# Novelty multiplier tiers based on cluster.novelty_score.
# novelty_score = 1.0 → new event or soft-penalised duplicate → baseline (penalty
#                         already baked into effective_hotness by stage1_normalize)
# novelty_score = 0.7 → new_development (follow-up, fresh angle) → +20% boost
# novelty_score = 0.2 → reserved (hard duplicates are filtered in Stage 1 and
#                         never reach clustering — value kept for future use)
_NOVELTY_MULT: dict[float, float] = {
    1.0: 1.00,
    0.7: 1.20,
    0.2: 0.60,
}
_NOVELTY_DEFAULT = 1.00


def event_score(cluster: EventCluster) -> float:
    """
    Unified event score — single authoritative ranking function.

    Computes (or reuses) event_hotness via hotness.py, then applies
    a novelty multiplier based on cluster.novelty_score.

    Args:
        cluster: A fully-built EventCluster with novelty_score set
                 by selector/__init__.py after memory classification.

    Returns:
        Final score float. Also updates cluster.event_hotness in-place.
    """
    # Ensure event_hotness is computed (idempotent if already set)
    if cluster.event_hotness == 0.0:
        compute_event_hotness(cluster)

    novelty_mult = _NOVELTY_MULT.get(cluster.novelty_score, _NOVELTY_DEFAULT)
    return cluster.event_hotness * novelty_mult


__all__ = ['event_score']
