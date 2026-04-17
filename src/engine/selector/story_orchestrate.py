"""
engine/selector/story_orchestrate.py — Deep story + supporting stories orchestration.

Slots in after clustering, before generation:
    clustering → story_orchestrate() → generate_story_batch()

Implements Phases 3.1 + 3.2 from the plan (story_deep.txt).

Inputs:
    cluster_map: dict[str, EventCluster] from build_clusters()

Output:
    {
      "deep_story":        EventCluster,
      "supporting_stories": [EventCluster, ...]   # 1–4 items
    }
    Returns None if cluster_map is empty.

Selection logic:
    Phase 3.1 — normalized_score per cluster:
        0.4 * log(1 + event_hotness)
      + 0.2 * source_diversity
      + 0.2 * novelty_score
      + 0.2 * recency_decay     (exp(-age_hours / 24) from newest article)

    deep_story  = argmax(normalized_score) where member_count >= 2
    supporting  = top 1–4 remaining clusters by support_score
                  support_score = normalized_score - 0.3 * entity_overlap_with_deep

    Constraints applied:
      - Hard skip if entity_overlap > 0.6  (RISK-1)
      - Max 1 supporting story per event_type category  (RISK-2)
      - Allow count to fall to 1 on thin pool + log warning  (RISK-3)
      - Tiebreak: member_count DESC, event_id ASC  (RISK-4)

    Phase 3.2 — Batch-level repetition penalty (default ON):
      - Reads the last REPETITION_LOOKBACK_BATCHES rows from hierarchical_stories
      - Any cluster whose event_id appeared in those batches gets its
        normalized_score multiplied by REPETITION_PENALTY_MULTIPLIER (0.5)
      - Disable via apply_repetition_penalty=False in story_orchestrate()
      - Disable at CLI level via --no-repetition-penalty flag in run.py
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Tuneable constants ─────────────────────────────────────────────────────────
MAX_SUPPORTING               = 4     # upper bound on supporting stories
MIN_SUPPORTING               = 1     # below this → log thin_pool warning
ENTITY_OVERLAP_HARD_CAP      = 0.6   # skip supporting cluster if overlap exceeds this
DEEP_MIN_MEMBER_COUNT        = 2     # deep_story must have at least this many sources
REPETITION_LOOKBACK_BATCHES  = 3     # how many recent batches to check for repeats (P2)
REPETITION_PENALTY_MULTIPLIER = 0.5  # score multiplier applied to recently shown clusters (P2)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load_recent_event_ids(lookback: int = REPETITION_LOOKBACK_BATCHES) -> set[str]:
    """
    Phase 3.2 — Read event_ids shown in the last N hierarchical story batches.

    Queries the hierarchical_stories table (already written by generate_story_batch)
    and extracts event_ids from deep_story and supporting_stories JSON columns.

    Returns an empty set on any DB error — penalty is silently skipped rather
    than blocking orchestration.
    """
    try:
        import sqlite3, os
        db_path = os.environ.get(
            'STORY_ENGINE_DB',
            str(__import__('pathlib').Path(__file__).resolve().parents[3] / 'db.sqlite3'),
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT deep_story, supporting_stories
            FROM hierarchical_stories
            WHERE status = 'ready'
            ORDER BY id DESC
            LIMIT ?
            """,
            (lookback,),
        ).fetchall()
        conn.close()

        seen: set[str] = set()
        for row in rows:
            # Extract event_id from deep_story JSON
            if row['deep_story']:
                try:
                    ds = json.loads(row['deep_story'])
                    if ds.get('event_id'):
                        seen.add(ds['event_id'])
                except (json.JSONDecodeError, TypeError):
                    pass
            # Extract event_ids from supporting_stories JSON array
            if row['supporting_stories']:
                try:
                    ss = json.loads(row['supporting_stories'])
                    for item in (ss or []):
                        if isinstance(item, dict) and item.get('event_id'):
                            seen.add(item['event_id'])
                except (json.JSONDecodeError, TypeError):
                    pass

        logger.debug(
            "story_orchestrate: repetition penalty loaded %d recent event_ids "
            "from last %d batches",
            len(seen), lookback,
        )
        return seen

    except Exception as _e:
        logger.warning(
            "story_orchestrate: could not load recent event_ids for repetition "
            "penalty (%s) — penalty skipped this run",
            _e,
        )
        return set()


def _recency_decay(cluster) -> float:
    """
    exp(-age_hours / 24) using the NEWEST article in the cluster timeline.

    timeline is sorted ascending by freshness, so timeline[-1] is the newest.
    Returns 0.5 on any parse error — neutral, not penalising.
    """
    if not cluster.timeline:
        return 0.5
    try:
        newest_ts = cluster.timeline[-1]['timestamp']
        newest_dt = datetime.fromisoformat(newest_ts)
        if newest_dt.tzinfo is None:
            newest_dt = newest_dt.replace(tzinfo=timezone.utc)
        now_utc   = datetime.now(timezone.utc)
        age_hours = (now_utc - newest_dt).total_seconds() / 3600
        return math.exp(-age_hours / 24)
    except Exception:
        return 0.5


def _normalized_score(cluster, recent_event_ids: set[str] | None = None) -> float:
    """
    Phase 3.1 Step A:
        0.4 * log(1 + event_hotness)
      + 0.2 * source_diversity
      + 0.2 * novelty_score
      + 0.2 * recency_decay

    Phase 3.2 (default ON): if recent_event_ids is provided and this cluster's
    event_id appears in it, multiply the final score by REPETITION_PENALTY_MULTIPLIER.
    """
    base = (
        0.4 * math.log(1.0 + cluster.event_hotness)
        + 0.2 * getattr(cluster, 'source_diversity', 0.0)
        + 0.2 * cluster.novelty_score
        + 0.2 * _recency_decay(cluster)
    )
    if recent_event_ids and cluster.event_id in recent_event_ids:
        logger.debug(
            "story_orchestrate: repetition penalty applied to cluster %s "
            "(score %.4f → %.4f)",
            cluster.event_id, base, base * REPETITION_PENALTY_MULTIPLIER,
        )
        return base * REPETITION_PENALTY_MULTIPLIER
    return base


def _entity_overlap(deep, cand) -> float:
    """
    Phase 3.1 Step C:
        |deep.cluster_countries ∩ cand.cluster_countries|
        / max(len(deep.cluster_countries), 1)

    Returns 0.0 when either cluster has no country data.
    """
    deep_countries = getattr(deep, 'cluster_countries', set()) or set()
    cand_countries = getattr(cand, 'cluster_countries', set()) or set()
    if not deep_countries:
        return 0.0
    return len(deep_countries & cand_countries) / max(len(deep_countries), 1)


def _event_type(cluster) -> str:
    """
    Read event_type from the representative candidate's entity dict.
    Falls back to 'UNKNOWN' if not available.
    """
    ents = getattr(cluster.representative, 'candidate_entities', None) or {}
    return ents.get('event_type', 'UNKNOWN')


# ── Public API ─────────────────────────────────────────────────────────────────

def story_orchestrate(
    cluster_map: dict,
    apply_repetition_penalty: bool = True,
) -> dict | None:
    """
    Select deep_story and supporting_stories from a cluster_map.

    Args:
        cluster_map: {candidate_id: EventCluster} produced by build_clusters().
                     Each cluster must have event_hotness set by compute_event_hotness().
        apply_repetition_penalty: Phase 3.2 — when True, clusters whose event_id
                     appeared in the last REPETITION_LOOKBACK_BATCHES batches have
                     their normalized_score halved. Default True (on).

    Returns:
        {
          "deep_story":        EventCluster,
          "supporting_stories": [EventCluster, ...]
        }
        Returns None if cluster_map is empty or no cluster is selectable.
    """
    if not cluster_map:
        logger.warning("story_orchestrate: empty cluster_map — nothing to orchestrate")
        return None

    clusters = list(cluster_map.values())

    # ── Phase 3.2: load recent event_ids for repetition penalty ───────────────
    recent_event_ids: set[str] | None = None
    if apply_repetition_penalty:
        recent_event_ids = _load_recent_event_ids()
        if recent_event_ids:
            logger.info(
                "story_orchestrate: repetition penalty ON — %d recent event_ids loaded",
                len(recent_event_ids),
            )
        else:
            logger.info("story_orchestrate: repetition penalty ON — no recent batches found")

    # ── Step A: score all clusters ─────────────────────────────────────────────
    scored: list[tuple] = [
        (cluster, _normalized_score(cluster, recent_event_ids))
        for cluster in clusters
    ]

    # ── Step B: pick deep_story ────────────────────────────────────────────────
    # Prefer clusters with member_count >= DEEP_MIN_MEMBER_COUNT.
    # Tiebreak: member_count DESC, event_id ASC (deterministic — RISK-4).
    eligible = [
        (c, s) for c, s in scored
        if c.member_count >= DEEP_MIN_MEMBER_COUNT
    ]
    if not eligible:
        eligible = scored
        logger.warning(
            "story_orchestrate: no cluster with member_count >= %d — "
            "falling back to singletons",
            DEEP_MIN_MEMBER_COUNT,
        )

    eligible.sort(key=lambda x: (-x[1], -x[0].member_count, x[0].event_id))
    deep_cluster, deep_score = eligible[0]

    logger.info(
        "story_orchestrate: deep_story selected — event_id=%s score=%.4f "
        "member_count=%d hotness=%.1f",
        deep_cluster.event_id,
        deep_score,
        deep_cluster.member_count,
        deep_cluster.event_hotness,
    )

    # ── Step C: pick supporting_stories ───────────────────────────────────────
    remaining = [
        (c, s) for c, s in scored
        if c.event_id != deep_cluster.event_id
    ]

    support_candidates: list[tuple] = []
    for cluster, norm_score in remaining:
        overlap = _entity_overlap(deep_cluster, cluster)

        # Hard skip: entity overlap > cap (RISK-1)
        if overlap > ENTITY_OVERLAP_HARD_CAP:
            logger.debug(
                "story_orchestrate: skip cluster %s — "
                "entity_overlap=%.2f > cap %.2f",
                cluster.event_id, overlap, ENTITY_OVERLAP_HARD_CAP,
            )
            continue

        support_score = norm_score - 0.3 * overlap
        support_candidates.append((cluster, support_score))

    # Sort: support_score DESC, then member_count DESC, event_id ASC (RISK-4)
    support_candidates.sort(
        key=lambda x: (-x[1], -x[0].member_count, x[0].event_id)
    )

    # event_type diversity cap: max 1 per event_type category (RISK-2)
    seen_event_types: dict[str, int] = {}
    supporting: list = []

    for cluster, support_score in support_candidates:
        if len(supporting) >= MAX_SUPPORTING:
            break

        etype = _event_type(cluster)
        if etype != 'UNKNOWN' and seen_event_types.get(etype, 0) >= 1:
            logger.debug(
                "story_orchestrate: skip cluster %s — "
                "event_type %r already represented (RISK-2)",
                cluster.event_id, etype,
            )
            continue

        supporting.append(cluster)
        seen_event_types[etype] = seen_event_types.get(etype, 0) + 1
        logger.debug(
            "story_orchestrate: supporting #%d — event_id=%s score=%.4f overlap=%.2f",
            len(supporting), cluster.event_id, support_score,
            _entity_overlap(deep_cluster, cluster),
        )

    # Thin pool warning (RISK-3)
    if len(supporting) < MIN_SUPPORTING:
        logger.warning(
            "story_orchestrate: thin_pool — only %d supporting clusters available "
            "(minimum desired: %d). Proceeding with what is available.",
            len(supporting), MIN_SUPPORTING,
        )

    logger.info(
        "story_orchestrate: complete — 1 deep story + %d supporting stories",
        len(supporting),
    )

    return {
        "deep_story":        deep_cluster,
        "supporting_stories": supporting,
    }
