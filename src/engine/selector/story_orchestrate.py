"""
engine/selector/story_orchestrate.py — Stage 3: Ranking + Story Orchestration Layer.

Implements the story3.txt design plan (Phases 3.1 + 3.2).

Pipeline position:
    build_clusters() → story_orchestrate() → generate_story_batch()

INPUTS (from build_clusters()):
    cluster_map: dict[str, EventCluster]

OUTPUT:
    {
      "deep_story":         EventCluster,
      "supporting_stories": [EventCluster, ...],    # 1–4 items
      "excluded_clusters":  [EventCluster, ...],    # rejected by pre-filter or not selected
      "ranking_metadata":   {                       # keyed by event_id
          event_id: {
              "cluster_score":    float,
              "selection_rank":   int,     # 0 = deep, 1–4 = supporting, -1 = excluded
              "rejection_reason": str | None,
          }
      }
    }
    Returns None if cluster_map is empty or no cluster passes the quality floor.

STAGE 3 STEPS (from story3.txt):
    Step 0 — Quality-floor pre-filter  (guards against bad Stage 2 clustering)
    Step 1 — Normalization             (recency_decay, novelty_score fallback)
    Step 2 — Deep story selection      (max normalized_score + bonus slots)
    Step 3 — Supporting story selection (with under-supply fallback)
    Step 4 — Diversity enforcement     (entities, geographies, event types)
    Step 5 — Repeat control            (entity fingerprint, JSON history, graduated penalty)
    Step 6 — Output formatting
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Tuneable constants ─────────────────────────────────────────────────────────

# Step 0 — Quality-floor pre-filter
MIN_CLUSTER_SIZE       = 3      # minimum member_count to pass pre-filter
MIN_SOURCE_DIVERSITY   = 0.2    # minimum source_diversity to pass pre-filter
HIGH_HOTNESS_THRESHOLD = 50     # single-source exception: allow if event_hotness > this

# Step 1 — Normalization
RECENCY_LAMBDA = 0.1            # exp(-RECENCY_LAMBDA * hours) → ~10-hour half-life

# Step 3 — Supporting stories
MAX_SUPPORTING          = 4     # upper bound on supporting stories
MIN_SUPPORTING          = 2     # below this → invoke under-supply fallback
ENTITY_OVERLAP_HARD_CAP = 0.6   # hard skip if entity overlap with deep_story exceeds this

# Step 3 — Entity overlap interval scoring (Q3)
OVERLAP_OPTIMAL_MIN          = 0.10   # below this: weak link, no bonus
OVERLAP_OPTIMAL_MAX          = 0.40   # above this: soft penalty zone
OVERLAP_BONUS                = 0.12   # bonus for optimal narrative coupling (10%–40%)
OVERLAP_PENALTY              = 0.08   # penalty for soft repetition zone (40%–60%)

# Step 3 — Two-stage pipeline (Q7)
TOP_K_STAGE1                 = 12     # top candidates from Stage 1 passed to Stage 2

# Step 3 — Conflict relevance keywords (Q2)
CONFLICT_KEYWORDS: tuple = (
    "war", "attack", "sanction", "protest", "ban",
    "regulation", "crisis", "lawsuit", "strike", "violence", "security",
)
LLM_BOOST_VARIANCE_THRESHOLD = 0.05   # LLM boost triggered when top score variance < this

# Step 3 — New dimension quality gate (Q6)
NEW_DIMENSION_THRESHOLD      = 0.35   # minimum score for a story to add a new dimension

# Step 5 — Repeat control
REPEAT_WINDOW_BATCHES = 5       # number of previous batches to check for repeats
REPEAT_EVICT_HOURS    = 24      # also evict batches older than this many hours

# History file — written alongside other engine logs
_HISTORY_PATH: Path = (
    Path(__file__).resolve().parents[3] / 'logs' / 'ranking_history.json'
)


# ── Entity fingerprint ────────────────────────────────────────────────────────

def _entity_fingerprint(cluster) -> str:
    """
    Stable identity key for repeat control.

    Built from the top-3 entities (countries + orgs combined), sorted and
    joined with '|'. Stable across re-clustering because it is derived from
    content (entity names), not cluster-assignment artefacts (event_id).

    Returns 'UNKNOWN' when the cluster has no entity data.

    Example:
        cluster_countries = {"usa", "china"}
        cluster_orgs      = {"IMF"}
        → fingerprint = "IMF|china|usa"
    """
    countries  = set(getattr(cluster, 'cluster_countries', None) or set())
    orgs       = set(getattr(cluster, 'cluster_orgs', None) or set())
    all_ents   = sorted(countries | orgs)[:3]
    return '|'.join(all_ents) if all_ents else 'UNKNOWN'


# ── Batch history (JSON file) ─────────────────────────────────────────────────

def _load_history() -> list[dict]:
    """
    Load ranking history from JSON file.

    File schema (array of batch records, oldest-first):
        [
          {
            "batch_ts":      int,   # UNIX ms
            "timestamp_utc": str,   # ISO 8601
            "shown": [
              {"fingerprint": str, "event_id": str}
            ]
          },
          ...
        ]
    Returns [] on any read error (file missing, parse error, etc.).
    """
    try:
        with open(_HISTORY_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(
            "story_orchestrate: could not load ranking_history.json (%s) — starting fresh", e
        )
        return []


def _save_history(batches: list[dict]) -> None:
    """Persist ranking history to JSON file, creating directory if needed."""
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_HISTORY_PATH, 'w') as f:
            json.dump(batches, f, indent=2)
    except Exception as e:
        logger.warning("story_orchestrate: could not save ranking_history.json (%s)", e)


def _evict_history(batches: list[dict]) -> list[dict]:
    """
    Remove entries that fall outside the repeat window.

    Eviction: either condition triggers removal (whichever comes first):
      - batch count exceeds REPEAT_WINDOW_BATCHES (keep last N)
      - batch timestamp older than REPEAT_EVICT_HOURS hours
    """
    # Count-based: keep only the last N batches
    trimmed = batches[-REPEAT_WINDOW_BATCHES:] if len(batches) > REPEAT_WINDOW_BATCHES else batches

    # Time-based: drop batches older than REPEAT_EVICT_HOURS
    cutoff_ts_ms = (datetime.now(timezone.utc).timestamp() - REPEAT_EVICT_HOURS * 3600) * 1000
    trimmed = [b for b in trimmed if b.get('batch_ts', 0) >= cutoff_ts_ms]

    return trimmed


def _build_fingerprint_age_map(batches: list[dict]) -> dict[str, int]:
    """
    Build {fingerprint → batches_ago} from recent history.

    batches_ago = 1 means the fingerprint appeared in the most recent batch.
    When a fingerprint appears in multiple batches, only the most recent
    occurrence is recorded (smallest batches_ago = highest penalty).

    Iterates newest-first so the first occurrence found for each fingerprint
    is always the most recent one.
    """
    fp_map: dict[str, int] = {}
    for i, batch in enumerate(reversed(batches)):
        batches_ago = i + 1     # reversed: index 0 = most recent = 1 batch ago
        for entry in batch.get('shown', []):
            fp = entry.get('fingerprint', '')
            if fp and fp not in fp_map:
                fp_map[fp] = batches_ago
    return fp_map


def _repetition_penalty(batches_ago: int) -> float:
    """
    Graduated repetition penalty (story3.txt Step 5):

        penalty_multiplier = max(0.5, 1.0 - 0.15 * batches_ago)

    Examples:
        shown 1 batch ago  → max(0.5, 0.85) = 0.85
        shown 3 batches ago → max(0.5, 0.55) = 0.55
        shown 5 batches ago → max(0.5, 0.25) = 0.50
    """
    return max(0.5, 1.0 - 0.15 * batches_ago)


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _recency_decay(cluster) -> float:
    """
    Step 1: exp(-RECENCY_LAMBDA * hours_since_event)

    Uses the NEWEST article in the cluster timeline (sorted ascending by freshness,
    so timeline[-1] is the newest).
    Returns 0.5 on any parse error — neutral, not penalising.
    """
    if not cluster.timeline:
        return 0.5
    try:
        newest_ts = cluster.timeline[-1]['timestamp']
        newest_dt = datetime.fromisoformat(newest_ts)
        if newest_dt.tzinfo is None:
            newest_dt = newest_dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - newest_dt).total_seconds() / 3600
        return math.exp(-RECENCY_LAMBDA * age_hours)
    except Exception:
        return 0.5


def _get_novelty_score(cluster) -> float:
    """
    Step 1: novelty_score from cluster field, or binary fallback from is_new_development.

    When Stage 1 sets novelty_score explicitly (e.g. 0.7 for follow-ups), that
    calibrated value is used directly. Fallback applies only when the field is
    absent or None.

    Fallback: 1.0 if is_new_development else 0.0
    """
    score = getattr(cluster, 'novelty_score', None)
    if score is not None:
        return float(score)
    rep = getattr(cluster, 'representative', None)
    if rep is not None and getattr(rep, 'is_new_development', False):
        return 1.0
    return 0.0


def _normalized_score(cluster) -> float:
    """
    Step 1 — base normalized score:

        0.4 * log(1 + event_hotness)
      + 0.2 * source_diversity
      + 0.2 * novelty_score
      + 0.2 * recency_decay
    """
    return (
        0.4 * math.log(1.0 + cluster.event_hotness)
        + 0.2 * getattr(cluster, 'source_diversity', 0.0)
        + 0.2 * _get_novelty_score(cluster)
        + 0.2 * _recency_decay(cluster)
    )


def _entity_overlap(deep, cand) -> float:
    """
    Fraction of deep_story's countries that appear in the candidate cluster.
    Returns 0.0 when either cluster has no country data.
    """
    deep_c = getattr(deep, 'cluster_countries', set()) or set()
    cand_c = getattr(cand, 'cluster_countries', set()) or set()
    if not deep_c:
        return 0.0
    return len(deep_c & cand_c) / max(len(deep_c), 1)


def _event_type(cluster) -> str:
    """
    Read event_type from the representative candidate's entity dict.
    Falls back to 'UNKNOWN' if not available.
    """
    ents = getattr(cluster.representative, 'candidate_entities', None) or {}
    return ents.get('event_type', 'UNKNOWN')


def _cluster_category(cluster) -> str:
    """
    Read the normalized category from the representative candidate.
    Used by topic_diversity_bonus during supporting story selection.
    Falls back to 'unknown'.
    """
    rep = getattr(cluster, 'representative', None)
    if rep is None:
        return 'unknown'
    return getattr(rep, 'category', None) or 'unknown'


# ── Narrative alignment helpers ───────────────────────────────────────────────

def _overlap_curve(overlap: float) -> float:
    """
    Interval-based entity overlap bonus/penalty (Q3).
      0%–10%   → 0.0   (weak link, no signal)
      10%–40%  → +0.12 (optimal narrative coupling)
      40%–60%  → -0.08 (soft repetition suppression)
      >60%     → hard-filtered upstream; never reaches here
    """
    if overlap < OVERLAP_OPTIMAL_MIN:
        return 0.0
    if overlap <= OVERLAP_OPTIMAL_MAX:
        return OVERLAP_BONUS
    return -OVERLAP_PENALTY


def _cosine_similarity(vec_a: list, vec_b: list) -> float:
    """Cosine similarity between two embedding vectors."""
    dot   = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(a * a for a in vec_a))
    mag_b = math.sqrt(sum(b * b for b in vec_b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _tfidf_similarity(a, b) -> float:
    """
    Jaccard word-overlap similarity — lightweight fallback when embeddings
    are unavailable.
    """
    def _words(cluster) -> set:
        text = " ".join([
            getattr(cluster, 'canonical_title', '') or '',
            getattr(cluster, 'description',     '') or '',
        ]).lower()
        return set(text.split())

    set_a = _words(a)
    set_b = _words(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _semantic_similarity(a, b) -> float:
    """
    Cosine similarity if both clusters carry pre-computed embeddings (Q4);
    falls back to TF-IDF Jaccard similarity when embeddings are absent.
    """
    emb_a = getattr(a, 'embedding', None)
    emb_b = getattr(b, 'embedding', None)
    if emb_a is not None and emb_b is not None:
        return _cosine_similarity(emb_a, emb_b)
    return _tfidf_similarity(a, b)


def _keyword_conflict_score(cluster) -> float:
    """
    Normalized keyword match score against CONFLICT_KEYWORDS (Q2).
    Score = matched_keywords / total_keywords, capped at 1.0.
    Matches whole words only — avoids false positives like "ban" in "urban"
    or "war" in "award".
    Note: English-only in MVP; non-English articles return 0.0 silently.
    """
    text = " ".join([
        getattr(cluster, 'canonical_title', '') or '',
        getattr(cluster, 'description',     '') or '',
    ]).lower()
    words = set(text.split())
    matches = sum(1 for kw in CONFLICT_KEYWORDS if kw in words)
    return min(matches / len(CONFLICT_KEYWORDS), 1.0)


def _conflict_relevance(cluster, deep_cluster) -> float:
    """
    Hybrid conflict relevance score (Q2).
      0.5 * keyword_conflict_score
    + 0.3 * entity_conflict_overlap  (reuses existing entity_overlap — shared
                                      entities indicate conflict coupling)
    + 0.2 * LLM boost                (deferred; only when score variance < threshold)
    LLM boost is NOT implemented in MVP — weight absorbed into keyword + entity.
    """
    kw_score      = _keyword_conflict_score(cluster)
    entity_coupl  = _entity_overlap(deep_cluster, cluster)
    # MVP: no LLM boost; redistribute its 0.2 weight proportionally
    # Effective weights: keyword=0.625, entity=0.375 (normalized from 0.5+0.3)
    return 0.625 * kw_score + 0.375 * entity_coupl


def _support_alignment_score(cluster, deep_cluster) -> float:
    """
    Stage 2 narrative alignment score (Q7 / Phase 2).
      0.4 * semantic_similarity_to_deep_story
    + 0.3 * conflict_relevance
    + 0.2 * entity_connection_strength
    + 0.1 * novelty_bonus
    """
    semantic   = _semantic_similarity(cluster, deep_cluster)
    conflict   = _conflict_relevance(cluster, deep_cluster)
    entity_con = _entity_overlap(deep_cluster, cluster)
    novelty    = _get_novelty_score(cluster)
    return (
        0.4 * semantic
      + 0.3 * conflict
      + 0.2 * entity_con
      + 0.1 * novelty
    )


def _new_dimension_score(cluster, deep_cluster) -> float:
    """
    Measures whether a supporting story adds a new perspective to the deep story (Q6).
      0.4 * entity_set_difference     (1 - entity_overlap, inverted)
    + 0.3 * topic_embedding_distance  (1 - semantic_similarity, inverted)
    + 0.3 * event_type_difference     (1.0 if different types, 0.0 if same)
    Score >= NEW_DIMENSION_THRESHOLD → story adds a new dimension.
    Note: topic_embedding_distance uses TF-IDF proxy when embeddings are absent.
    """
    entity_diff   = 1.0 - _entity_overlap(deep_cluster, cluster)
    topic_dist    = 1.0 - _semantic_similarity(cluster, deep_cluster)
    deep_etype    = _event_type(deep_cluster)
    cand_etype    = _event_type(cluster)
    type_diff     = 0.0 if (deep_etype == cand_etype and deep_etype != 'UNKNOWN') else 1.0
    return 0.4 * entity_diff + 0.3 * topic_dist + 0.3 * type_diff


def _cross_platform_diversity_bonus(cluster) -> float:
    """
    Phase 3.2 MVP: +0.1 if the cluster spans >= 2 distinct source-role types.

    Source-role types (from clustering.py):
        fact     → mainstream wire services (Reuters, AP, BBC, …)
        context  → mainstream news and regional outlets
        reaction → social platforms (Reddit, YouTube, Twitter, …)

    Rationale: a story covered by both wire services AND social media signals
    broader real-world impact than one confined to a single platform type.
    """
    has_fact     = bool(getattr(cluster, 'fact_sources',     None))
    has_context  = bool(getattr(cluster, 'context_sources',  None))
    has_reaction = bool(getattr(cluster, 'reaction_sources', None))
    type_count   = sum([has_fact, has_context, has_reaction])
    return 0.1 if type_count >= 2 else 0.0


# ── Quality-floor pre-filter (Step 0) ────────────────────────────────────────

def _passes_quality_floor(cluster) -> tuple[bool, str | None]:
    """
    Step 0 — Quality-floor pre-filter.

    REJECT when BOTH conditions are true:
        member_count < MIN_CLUSTER_SIZE
        source_diversity < MIN_SOURCE_DIVERSITY

    EXCEPTION: allow single-source clusters when event_hotness > HIGH_HOTNESS_THRESHOLD.

    Returns (passes, rejection_reason_or_None).
    """
    mc  = cluster.member_count
    sd  = getattr(cluster, 'source_diversity', 0.0)
    hot = cluster.event_hotness

    if mc < MIN_CLUSTER_SIZE and sd < MIN_SOURCE_DIVERSITY:
        if hot > HIGH_HOTNESS_THRESHOLD:
            # Hot singleton exception
            return True, None
        return False, (
            f"quality_floor(member_count={mc}<{MIN_CLUSTER_SIZE}, "
            f"source_diversity={sd:.2f}<{MIN_SOURCE_DIVERSITY:.2f})"
        )
    return True, None


# ── Public API ────────────────────────────────────────────────────────────────

def story_orchestrate(
    cluster_map: dict,
    apply_repetition_penalty: bool = True,
) -> dict | None:
    """
    Stage 3: Select deep_story and supporting_stories from a cluster_map.

    Args:
        cluster_map: {candidate_id: EventCluster} produced by build_clusters().
                     Each cluster must have event_hotness set by compute_event_hotness().
        apply_repetition_penalty: When True (default), applies a graduated penalty
                     multiplier to clusters whose entity fingerprint appeared in any
                     of the last REPEAT_WINDOW_BATCHES batches. History is always
                     saved regardless of this flag.

    Returns:
        {
          "deep_story":         EventCluster,
          "supporting_stories": [EventCluster, ...],
          "excluded_clusters":  [EventCluster, ...],
          "ranking_metadata":   {event_id: {cluster_score, selection_rank, rejection_reason}}
        }
        Returns None if cluster_map is empty or no cluster passes the quality floor.
    """
    if not cluster_map:
        logger.warning("story_orchestrate: empty cluster_map — nothing to orchestrate")
        return None

    clusters              = list(cluster_map.values())
    excluded_clusters:list = []
    ranking_metadata:dict  = {}

    # ── Step 5 (load): read batch history for repetition penalty ──────────────
    batches    = _load_history()
    batches    = _evict_history(batches)
    fp_age_map: dict[str, int] = {}

    if apply_repetition_penalty:
        fp_age_map = _build_fingerprint_age_map(batches)
        if fp_age_map:
            logger.info(
                "story_orchestrate: repetition penalty ON — %d fingerprints in history "
                "from last %d batch(es)",
                len(fp_age_map), len(batches),
            )
        else:
            logger.info("story_orchestrate: repetition penalty ON — no prior history found")
    else:
        logger.info("story_orchestrate: repetition penalty OFF (--no-repetition-penalty)")

    # ── Step 0: Quality-floor pre-filter ──────────────────────────────────────
    passed: list = []
    for cluster in clusters:
        ok, reason = _passes_quality_floor(cluster)
        if not ok:
            excluded_clusters.append(cluster)
            ranking_metadata[cluster.event_id] = {
                'cluster_score':    0.0,
                'selection_rank':   -1,
                'rejection_reason': reason,
            }
            logger.debug(
                "story_orchestrate: Step 0 excluded cluster %s — %s",
                cluster.event_id, reason,
            )
        else:
            passed.append(cluster)

    if not passed:
        logger.warning(
            "story_orchestrate: all %d clusters failed quality floor — nothing to select",
            len(clusters),
        )
        return None

    logger.info(
        "story_orchestrate: Step 0 — %d/%d clusters passed quality floor, %d excluded",
        len(passed), len(clusters), len(excluded_clusters),
    )

    # ── Steps 1 + 5: Normalize scores + apply repetition penalty ──────────────
    scored: list[tuple] = []
    for cluster in passed:
        base_score = _normalized_score(cluster)

        # Step 5: graduated repetition penalty
        fp         = _entity_fingerprint(cluster)
        batches_ago = fp_age_map.get(fp)
        if batches_ago is not None:
            multiplier  = _repetition_penalty(batches_ago)
            final_score = base_score * multiplier
            logger.debug(
                "story_orchestrate: repetition penalty — cluster %s fingerprint=%r "
                "batches_ago=%d score %.4f → %.4f (×%.2f)",
                cluster.event_id, fp, batches_ago, base_score, final_score, multiplier,
            )
        else:
            final_score = base_score

        scored.append((cluster, final_score))

    # ── Step 2: Deep story selection ──────────────────────────────────────────
    # Phase 3.2: cross_platform_diversity_bonus implemented (MVP).
    # entity_centrality_bonus deferred to Stage 4+ (needs entity graph).
    def _deep_score(cluster, norm_score: float) -> float:
        cp_bonus               = _cross_platform_diversity_bonus(cluster)
        entity_centrality_bonus = 0.0   # Stage 4+: needs entity graph — deferred
        return (
            norm_score
            + 0.1 * cp_bonus
            + 0.1 * entity_centrality_bonus
        )

    # Prefer clusters meeting MIN_CLUSTER_SIZE; fall back if none qualify.
    eligible = [(c, s) for c, s in scored if c.member_count >= MIN_CLUSTER_SIZE]
    if not eligible:
        eligible = scored
        logger.warning(
            "story_orchestrate: Step 2 — no cluster with member_count >= %d; "
            "falling back to all %d passed clusters",
            MIN_CLUSTER_SIZE, len(scored),
        )

    # Sort: deep_score DESC, member_count DESC, event_id ASC (deterministic tiebreak)
    eligible.sort(key=lambda x: (-_deep_score(x[0], x[1]), -x[0].member_count, x[0].event_id))
    deep_cluster, deep_norm = eligible[0]
    deep_final              = _deep_score(deep_cluster, deep_norm)

    ranking_metadata[deep_cluster.event_id] = {
        'cluster_score':    round(deep_final, 4),
        'selection_rank':   0,
        'rejection_reason': None,
    }
    logger.info(
        "story_orchestrate: Step 2 — deep_story selected event_id=%s "
        "score=%.4f member_count=%d hotness=%.1f",
        deep_cluster.event_id, deep_final,
        deep_cluster.member_count, deep_cluster.event_hotness,
    )

    # ── Step 3: Supporting story selection ────────────────────────────────────
    # topic_diversity_bonus: applied dynamically during selection loop (Phase 3.2 MVP).
    # It is a selection-stage constraint, not a pre-computed score — the bonus/penalty
    # depends on which categories have already been chosen.
    remaining = [(c, s) for c, s in scored if c.event_id != deep_cluster.event_id]

    def _support_score(norm_score: float, overlap: float, topic_bonus: float = 0.0) -> float:
        # Q3: interval overlap curve replaces old linear penalty (-0.3 * overlap)
        return norm_score + _overlap_curve(overlap) + 0.1 * topic_bonus

    def _filter_and_score_candidates(
        pool: list[tuple],
        overlap_cap: float,
    ) -> list[tuple]:
        """Return [(cluster, base_support_score)] filtered by overlap_cap, sorted DESC.

        base_support_score does NOT include topic_diversity_bonus — that is applied
        dynamically in the selection loop below as categories are accumulated.
        """
        result = []
        for cluster, norm_score in pool:
            overlap = _entity_overlap(deep_cluster, cluster)
            if overlap > overlap_cap:
                continue
            result.append((cluster, _support_score(norm_score, overlap)))
        result.sort(key=lambda x: (-x[1], -x[0].member_count, x[0].event_id))
        return result

    support_candidates = _filter_and_score_candidates(remaining, overlap_cap=ENTITY_OVERLAP_HARD_CAP)

    # Under-supply fallback: relax entity overlap cap by 50% and re-run
    if len(support_candidates) < MIN_SUPPORTING:
        relaxed_cap        = ENTITY_OVERLAP_HARD_CAP * 1.5
        support_candidates = _filter_and_score_candidates(remaining, overlap_cap=relaxed_cap)
        logger.warning(
            "story_orchestrate: Step 3 under-supply — fewer than %d clusters passed "
            "filter; relaxed entity overlap cap to %.2f and re-ran "
            "(%d candidates now available)",
            MIN_SUPPORTING, relaxed_cap, len(support_candidates),
        )

    # ── Stage 1 → Stage 2 handoff (Q7) ──────────────────────────────────────────
    # Cap at TOP_K_STAGE1 before narrative refinement to control cost + latency.
    stage1_candidates = support_candidates[:TOP_K_STAGE1]

    # Phase 3 Q6 — new dimension quality gate
    # Filter out stories that don't add a new perspective vs. the deep story.
    stage2_candidates = [
        (c, s) for c, s in stage1_candidates
        if _new_dimension_score(c, deep_cluster) >= NEW_DIMENSION_THRESHOLD
    ]
    if len(stage2_candidates) < MIN_SUPPORTING:
        logger.warning(
            "story_orchestrate: new_dimension gate — only %d candidate(s) passed "
            "(threshold=%.2f); falling back to full Stage 1 set",
            len(stage2_candidates), NEW_DIMENSION_THRESHOLD,
        )
        stage2_candidates = stage1_candidates

    # Stage 2 — re-rank by narrative alignment score (Q7)
    support_candidates = sorted(
        stage2_candidates,
        key=lambda x: (-_support_alignment_score(x[0], deep_cluster), -x[1]),
    )
    logger.debug(
        "story_orchestrate: Stage 2 — %d candidates re-ranked by alignment score",
        len(support_candidates),
    )

    # Selection loop — applies event_type diversity cap + topic_diversity_bonus
    seen_event_types: dict[str, int] = {}
    seen_categories:  set[str]       = set()
    supporting:       list           = []
    support_scores:   list           = []

    for cluster, base_sup_score in support_candidates:
        if len(supporting) >= MAX_SUPPORTING:
            break

        etype = _event_type(cluster)
        if etype != 'UNKNOWN' and seen_event_types.get(etype, 0) >= 1:
            logger.debug(
                "story_orchestrate: skip cluster %s — event_type %r already represented",
                cluster.event_id, etype,
            )
            continue

        # topic_diversity_bonus (Phase 3.2 MVP):
        #   new category      → +0.1  (reward coverage breadth)
        #   repeated category → -0.1  (soft discourage repetition)
        # topic_bonus is already the final ±0.1 delta — add directly, no extra multiplier.
        cat             = _cluster_category(cluster)
        topic_bonus     = 0.1 if cat not in seen_categories else -0.1
        final_sup_score = base_sup_score + topic_bonus

        supporting.append(cluster)
        support_scores.append(final_sup_score)
        seen_event_types[etype] = seen_event_types.get(etype, 0) + 1
        seen_categories.add(cat)
        logger.debug(
            "story_orchestrate: supporting #%d — event_id=%s category=%r "
            "topic_bonus=%.1f final_score=%.4f",
            len(supporting), cluster.event_id, cat, topic_bonus, final_sup_score,
        )

    # Minimum-1 last-resort fallback
    if not supporting and remaining:
        fallback_cluster = sorted(remaining, key=lambda x: -x[1])[0][0]
        supporting.append(fallback_cluster)
        support_scores.append(0.0)
        logger.warning(
            "story_orchestrate: Step 3 thin_pool — zero candidates survived filter; "
            "using highest-scoring remaining cluster %s as emergency fallback",
            fallback_cluster.event_id,
        )
    elif len(supporting) < MIN_SUPPORTING:
        logger.warning(
            "story_orchestrate: Step 3 thin_pool — only %d supporting cluster(s) "
            "available (minimum desired: %d)",
            len(supporting), MIN_SUPPORTING,
        )

    # Record ranking_metadata for selected supporting stories
    for rank, (cluster, score) in enumerate(zip(supporting, support_scores), start=1):
        ranking_metadata[cluster.event_id] = {
            'cluster_score':    round(score, 4),
            'selection_rank':   rank,
            'rejection_reason': None,
        }

    # ── Step 4: Diversity enforcement ─────────────────────────────────────────
    # Hard constraint: supporting stories must not heavily overlap each other
    # on entities/geographies. If violation detected, replace the lower-ranked
    # story with the next best candidate not already selected.
    if len(supporting) > 1:
        selected_ids = {deep_cluster.event_id} | {c.event_id for c in supporting}
        for i in range(len(supporting)):
            for j in range(i + 1, len(supporting)):
                ci = supporting[i]
                cj = supporting[j]
                c_i = getattr(ci, 'cluster_countries', set()) or set()
                c_j = getattr(cj, 'cluster_countries', set()) or set()
                if c_i and c_j:
                    geo_overlap = len(c_i & c_j) / max(len(c_i), len(c_j))
                    if geo_overlap > ENTITY_OVERLAP_HARD_CAP:
                        logger.debug(
                            "story_orchestrate: Step 4 diversity violation — "
                            "clusters %s and %s share %.0f%% countries — "
                            "replacing rank %d",
                            ci.event_id, cj.event_id, geo_overlap * 100, j + 1,
                        )
                        # Replace the lower-ranked story (index j)
                        for alt_cluster, alt_score in support_candidates:
                            if alt_cluster.event_id not in selected_ids:
                                old = supporting[j]
                                ranking_metadata[old.event_id] = {
                                    'cluster_score':    ranking_metadata.get(
                                        old.event_id, {}
                                    ).get('cluster_score', 0.0),
                                    'selection_rank':   -1,
                                    'rejection_reason': 'diversity_enforcement',
                                }
                                supporting[j]     = alt_cluster
                                support_scores[j] = alt_score
                                selected_ids.add(alt_cluster.event_id)
                                ranking_metadata[alt_cluster.event_id] = {
                                    'cluster_score':    round(alt_score, 4),
                                    'selection_rank':   j + 1,
                                    'rejection_reason': None,
                                }
                                break
                        else:
                            logger.warning(
                                "story_orchestrate: Step 4 — diversity violation between "
                                "clusters %s and %s could not be resolved "
                                "(no unused alternative available)",
                                ci.event_id, cj.event_id,
                            )
                        break   # only fix one violation per pass (re-check not needed at this scale)

    # ── Step 6: Build excluded_clusters + ranking_metadata for non-selected ───
    selected_ids = {deep_cluster.event_id} | {c.event_id for c in supporting}
    for cluster, norm_score in remaining:
        if cluster.event_id not in selected_ids and cluster.event_id not in ranking_metadata:
            overlap     = _entity_overlap(deep_cluster, cluster)
            sup_score   = _support_score(norm_score, overlap)
            ranking_metadata[cluster.event_id] = {
                'cluster_score':    round(sup_score, 4),
                'selection_rank':   -1,
                'rejection_reason': 'not_selected',
            }
            excluded_clusters.append(cluster)

    # ── Step 5 (save): append this batch to history ───────────────────────────
    # Always saved regardless of apply_repetition_penalty, so future runs can
    # penalise what was shown today even when penalty was off this run.
    shown_entries = (
        [{'fingerprint': _entity_fingerprint(deep_cluster), 'event_id': deep_cluster.event_id}]
        + [{'fingerprint': _entity_fingerprint(c), 'event_id': c.event_id} for c in supporting]
    )
    new_batch = {
        'batch_ts':      int(datetime.now(timezone.utc).timestamp() * 1000),
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'shown':         shown_entries,
    }
    batches.append(new_batch)
    batches = _evict_history(batches)
    _save_history(batches)

    logger.info(
        "story_orchestrate: complete — 1 deep story + %d supporting + %d excluded "
        "| history: %d batch(es) saved",
        len(supporting), len(excluded_clusters), len(batches),
    )

    return {
        'deep_story':         deep_cluster,
        'supporting_stories': supporting,
        'excluded_clusters':  excluded_clusters,
        'ranking_metadata':   ranking_metadata,
    }
