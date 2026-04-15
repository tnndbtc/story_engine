"""
event_layer/hotness.py — Event-level hotness model (Q2).

Replaces single-article hotness with an aggregate score that rewards
multi-source confirmation and penalises stale events.

Formula
-------
event_hotness = log(1 + Σ top-K hotness) × (1 + diversity_bonus) × recency_decay

Components
----------
log_base        log(1 + sum of top-3 member hotness scores).
                log prevents spam inflation while preserving multi-source signal.

diversity_bonus Additive reward for source quality / breadth:
                  +0.15  authoritative fact source present (Reuters/AP/BBC…)
                  +0.10  multi-platform coverage (≥ 2 distinct platforms)
                  +0.10  strong multi-source (≥ 3 members)
                Capped at MAX_DIVERSITY_BONUS (0.35).

recency_decay   exp(-age_hours / TAU_HOURS)
                TAU_HOURS = 48 → events older than 48h score ~37% of peak.
                Reference time: representative article's collected_at (freshness).

Calibration note
----------------
The log is applied to raw hotness values (range ~200–500 for current corpus).
log(1 + 450) ≈ 6.1, so a 0.35 bonus multiplier yields a 15% swing — meaningful
without drowning the base signal. Revisit if hotness scale changes significantly.
"""

from __future__ import annotations

import math
import time
from datetime import timezone

from engine.event_layer.clustering import EventCluster

TAU_HOURS           = 48.0   # recency decay time constant
TOP_K_MEMBERS       = 3      # how many member hotness scores to aggregate
MAX_DIVERSITY_BONUS = 0.35   # cap on diversity bonus

# Source quality factor (RULE 4 — multi-source minimum enforcement, soft version)
# Applied as a multiplier: multi-source events score full; singletons are penalised.
_SQF_MULTI_SOURCE    = 1.0   # ≥ 2 members: confirmed by multiple sources
_SQF_SINGLETON_FACT  = 0.8   # 1 member but it's a high-authority fact source
_SQF_SINGLETON_WEAK  = 0.6   # 1 member, no authoritative source

# Platforms treated as high-authority fact sources for SQF calculation.
# Subset of FACT_PLATFORMS from clustering.py — intentionally kept minimal.
_AUTHORITY_PLATFORMS: frozenset[str] = frozenset({
    'reuters', 'ap', 'apnews', 'bbc', 'nytimes', 'bloomberg', 'ft',
})

# Cross-platform bonus — reward events with coverage from underrepresented
# (long-tail) platforms that rarely dominate the pool.
_CROSS_PLATFORM_BONUS        = 0.15
_UNDERREPRESENTED_PLATFORMS: frozenset[str] = frozenset({
    'hackernews', 'lobsters', 'devto', 'paperswithcode', 'github',
    'v2ex', 'producthunt', 'stackoverflow', 'aljazeera', 'ft',
})


def compute_event_hotness(cluster: EventCluster) -> float:
    """
    Compute event_hotness for a cluster and store it on cluster.event_hotness.

    Formula (unified scoring):
        event_hotness =
            log(1 + Σ top-K member hotness)  ← base
            × (1 + diversity_bonus)           ← fact/multi-platform/multi-source
            × recency_decay                   ← age of representative article
            × source_quality_factor           ← RULE 4: penalise weak singletons
            × (1 + cross_platform_bonus)      ← reward underrepresented platforms

    Note: novelty_bonus and repetition_penalty are applied BEFORE selection
    (in stage1_normalize.py) to effective_hotness so they influence stage3
    ranking. source_quality_factor and cross_platform_bonus are applied here
    (post-selection) to enrich event_hotness passed to generators.

    Returns the computed value (also sets cluster.event_hotness in-place).
    Falls back to representative.effective_hotness when the cluster has no members.
    """
    all_members = (
        cluster.fact_sources
        + cluster.context_sources
        + cluster.reaction_sources
    )

    if not all_members:
        score = cluster.representative.effective_hotness
        cluster.event_hotness = score
        return score

    # 1. Log-aggregated base hotness (top-K members)
    hotness_scores = sorted(
        (m.hotness for m in all_members), reverse=True
    )[:TOP_K_MEMBERS]
    log_base = math.log1p(sum(hotness_scores))

    # 2. Diversity bonus
    bonus = 0.0
    platforms = {m.platform for m in all_members}
    if cluster.fact_sources:
        bonus += 0.15
    if len(platforms) > 1:
        bonus += 0.10
    if len(all_members) >= 3:
        bonus += 0.10
    bonus = min(bonus, MAX_DIVERSITY_BONUS)

    # 3. Recency decay relative to representative's freshness
    rep_freshness = cluster.representative.freshness
    if rep_freshness.tzinfo is None:
        # Treat naive datetimes as UTC (consistent with _parse_freshness)
        freshness_ts = rep_freshness.replace(tzinfo=timezone.utc).timestamp()
    else:
        freshness_ts = rep_freshness.timestamp()
    age_hours = max(0.0, (time.time() - freshness_ts) / 3600.0)
    decay = math.exp(-age_hours / TAU_HOURS)

    # 4. Source quality factor (RULE 4 — soft multi-source minimum)
    if cluster.member_count >= 2:
        sqf = _SQF_MULTI_SOURCE
    elif any(
        m.platform.lower() in _AUTHORITY_PLATFORMS
        for m in all_members
    ):
        sqf = _SQF_SINGLETON_FACT
    else:
        sqf = _SQF_SINGLETON_WEAK

    # 5. Cross-platform bonus — underrepresented platforms in this cluster
    underrepresented = platforms & _UNDERREPRESENTED_PLATFORMS
    cp_bonus = _CROSS_PLATFORM_BONUS if underrepresented else 0.0

    score = log_base * (1.0 + bonus) * decay * sqf * (1.0 + cp_bonus)
    cluster.event_hotness = score
    return score
