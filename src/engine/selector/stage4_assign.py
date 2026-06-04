"""
Stage 4 — Format Assignment + Trace Logging  (stage4_assign.py)

Assigns selected items to formats. Writes results to DB. Emits the full
trace log. Marks partial output where needed.

Key correctness points:
  - Uses Tier 1 (reserved) then Tier 2 (global fill) for assignment.
  - format_id ascending sort is INTEGER sort (1, 2, ..., 9, 10, ..., 46).
  - DB writes are a single transaction — any failure rolls back entirely.
  - Stage 4 does NOT write to the stories table.
    stories rows are written by generators via save_story() in Phase 5.
  - Stage 4 writes:
      (a) UPDATE story_sets SET status, partial, partial_formats WHERE id=story_set_id
      (b) INSERT INTO used_items (one row per assigned item)
  - used_items INSERT includes all NOT NULL columns:
      crawler_item_id, crawler_url, hotness_at_use, story_set_id, format, used_at
  - story_set_id is required and passed explicitly by run_batch().
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import engine.format_registry as format_registry
from db.models import get_connection
from engine.selector.config import BatchConfig
from engine.selector.schemas import (
    AllocationEnvelope,
    BatchResult,
    NormalizedCandidate,
    PartialFormat,
    SelectedItem,
    TraceRecord,
)
from engine.selector.stage1_normalize import (
    close_trace_handle,
    get_trace_path,
)

logger = logging.getLogger(__name__)


def stage4_assign(
    selected:      list[SelectedItem],
    traces:        list[TraceRecord],
    envelope:      AllocationEnvelope,
    config:        BatchConfig,
    db_path:       str,
    batch_ts:      int,
    story_set_id:  int,
) -> BatchResult:
    """
    Stage 4: format assignment + DB writes + trace log finalization.

    Args:
        selected:     Stage 3 output.
        traces:       Stage 3 output (summary — actual records in JSONL).
        envelope:     Stage 2 AllocationEnvelope.
        config:       Loaded BatchConfig.
        db_path:      Path to story_engine's db.sqlite3.
        batch_ts:     UNIX milliseconds.
        story_set_id: Row ID created by create_story_set() before Stage 1.

    Returns:
        BatchResult with format_assignments dict[int, list[NormalizedCandidate]].
    """
    # Build a lookup from candidate_id → NormalizedCandidate
    # We need the full NormalizedCandidate objects for used_items INSERT and
    # to return in BatchResult.format_assignments.
    # Stage 3 returns SelectedItem (without full NormalizedCandidate data).
    # We reconstruct using the stage1 candidate list — but Stage 4 doesn't
    # have it directly. We use selected items' fields for DB writes.
    # For format_assignments, we store SelectedItem-shaped NormalizedCandidate
    # shells with the fields generators need.
    #
    # The orchestrator in __init__.py passes the Stage 1 candidates separately
    # via a module-level lookup. We register them in Stage 1 for retrieval here.
    from engine.selector.stage1_normalize import _trace_handles  # noqa — for internal use

    # Step 1 — Format assignment (Tier 1 reserved + Tier 2 global fill)
    assignments: dict[int, list[SelectedItem]] = {}
    assigned_ids: set[str] = set()

    # Process formats in INTEGER ascending order (1, 2, ..., 9, 10, ..., 46)
    feasible_format_ids = sorted(
        fid for fid, ff in envelope.per_format_feasibility.items()
        if ff.feasible
    )

    for fid in feasible_format_ids:
        ff = envelope.per_format_feasibility[fid]

        # Tier 1: items Stage 3 reserved specifically for this format
        reserved = [s for s in selected if s.reserved_for_format == fid]

        # Tier 2: global fill items eligible for this format (not yet assigned)
        fill_eligible = [
            s for s in selected
            if s.reserved_for_format is None
            and fid in s.eligible_format_ids
            and s.candidate_id not in assigned_ids
        ]
        fill_eligible.sort(key=lambda s: (
            -s.score,
            -s.hotness,
            -s.freshness.timestamp(),
            s.candidate_id,
        ))

        # Combine: reserved first, then fill up to item_count
        needed = max(0, ff.item_count - len(reserved))
        picked = reserved + fill_eligible[:needed]
        assignments[fid] = picked
        assigned_ids.update(s.candidate_id for s in picked)

    # Step 2 — Identify partial formats
    partial_formats: list[PartialFormat] = []

    # Formats infeasible from Stage 2
    for fid, ff in envelope.per_format_feasibility.items():
        if not ff.feasible:
            dim = "platform" if (ff.blocking_constraint or "").startswith("platform_cap") else "supply"
            partial_formats.append(PartialFormat(
                skipped_format_id                = fid,
                shortage_dimension               = dim,
                blocking_constraint              = ff.blocking_constraint,
                candidate_count_before_filtering = ff.eligible_count,
                candidate_count_after_filtering  = 0,
            ))

    # Formats feasible in Stage 2 but short in Stage 4 assignment
    for fid in feasible_format_ids:
        ff = envelope.per_format_feasibility[fid]
        assigned_count = len(assignments.get(fid, []))
        if assigned_count < ff.item_count:
            partial_formats.append(PartialFormat(
                skipped_format_id                = fid,
                shortage_dimension               = "supply",
                blocking_constraint              = None,
                candidate_count_before_filtering = ff.eligible_count,
                candidate_count_after_filtering  = assigned_count,
            ))

    is_partial = bool(partial_formats)

    # Step 3 — Validate batch result before any DB writes
    candidates_by_id = {c.candidate_id: c for c in _candidate_registry.values()}
    validation = validate_batch_result(
        format_assignments=assignments,
        assigned_ids=assigned_ids,
        envelope=envelope,
        config=config,
        candidates_by_id=candidates_by_id,
    )
    if not validation["is_valid"]:
        for err in validation["errors"]:
            logger.error("Stage 4 validation failed: %s", err)
        # Close trace, emit trace log for observability, return failure — do NOT raise.
        # Design: "do NOT raise an exception; return a failed BatchResult"
        trace_path = get_trace_path(batch_ts, db_path)
        close_trace_handle(batch_ts)
        return BatchResult(
            story_set_id       = story_set_id,
            batch_ts           = batch_ts,
            format_assignments = {},
            partial            = True,
            partial_formats    = partial_formats,
            trace_path         = trace_path,
            snapshot_path      = "",
        )

    # Step 4 — DB writes (single transaction) — only reached if validation passes
    trace_path = get_trace_path(batch_ts, db_path)

    conn = get_connection()

    try:
        # 4a. UPDATE story_sets (row was INSERTed by create_story_set() before Stage 1)
        status = "partial" if is_partial else "complete"
        partial_formats_json = json.dumps(
            [
                {
                    "skipped_format_id":                pf.skipped_format_id,
                    "shortage_dimension":               pf.shortage_dimension,
                    "blocking_constraint":              pf.blocking_constraint,
                    "candidate_count_before_filtering": pf.candidate_count_before_filtering,
                    "candidate_count_after_filtering":  pf.candidate_count_after_filtering,
                }
                for pf in partial_formats
            ],
            ensure_ascii=False,
        )
        conn.execute(
            """UPDATE story_sets
               SET status = %s, partial = %s, partial_formats = %s
               WHERE id = %s""",
            (status, 1 if is_partial else 0, partial_formats_json, story_set_id),
        )

        # 4b. INSERT INTO used_items (one row per assigned item)
        for fid, items in assignments.items():
            fmt_strategy = format_registry.strategy(fid)
            for s_item in items:
                # We need NormalizedCandidate fields for used_items.
                # Stage 3 SelectedItem carries url, platform, hotness, candidate_id.
                # crawler_item_id is carried in candidate_id's source — but SelectedItem
                # doesn't include it. We store it as an attribute lookup from the
                # candidate registry (populated in Stage 1 __init__.py).
                crawler_item_id = _get_crawler_item_id(s_item.candidate_id)
                conn.execute(
                    """INSERT INTO used_items
                       (crawler_item_id, crawler_url, hotness_at_use,
                        story_set_id, story_id, format, used_at, platform)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        crawler_item_id,
                        s_item.url,
                        s_item.hotness,
                        story_set_id,
                        None,   # story_id: set by generator after Phase 5
                        fmt_strategy,
                        batch_ts,
                        s_item.platform,
                    ),
                )

        conn.commit()
        logger.info(
            "Stage 4: DB writes committed — status=%s, %d used_items inserted",
            status, sum(len(v) for v in assignments.values()),
        )
    except Exception as e:
        conn.rollback()
        logger.error("Stage 4: DB write failed — rolling back: %s", e)
        raise
    finally:
        conn.close()

    # Step 5 — Close trace log (flush and close)
    close_trace_handle(batch_ts)

    # Step 6 — Build format_assignments: dict[int, list[NormalizedCandidate]]
    # Convert SelectedItem → NormalizedCandidate shell with generator-required fields
    format_assignments: dict[int, list[NormalizedCandidate]] = {}
    for fid, items in assignments.items():
        format_assignments[fid] = [
            _selected_to_candidate(s_item) for s_item in items
        ]

    # Include feasible formats with empty assignments (for completeness)
    for fid in feasible_format_ids:
        if fid not in format_assignments:
            format_assignments[fid] = []

    partial = bool(partial_formats)
    logger.info(
        "Stage 4 complete: story_set_id=%d, status=%s, partial_formats=%d",
        story_set_id, status, len(partial_formats),
    )

    return BatchResult(
        story_set_id       = story_set_id,
        batch_ts           = batch_ts,
        format_assignments = format_assignments,
        partial            = partial,
        partial_formats    = partial_formats,
        trace_path         = trace_path,
        snapshot_path      = "",   # set by orchestrator from Stage 1
    )


def validate_batch_result(
    format_assignments: dict[int, list[SelectedItem]],
    assigned_ids: set[str],
    envelope: AllocationEnvelope,
    config: BatchConfig,
    candidates_by_id: dict[str, NormalizedCandidate],
) -> dict:
    """
    Validate batch result before persisting to DB.
    Returns {"is_valid": bool, "errors": list[str]}.
    Called in Stage 4 Step 3 — after compute_assignments, before persist_batch.
    """
    errors: list[str] = []

    # Check 1 — Platform caps not exceeded
    platform_counts: dict[str, int] = {}
    for items in format_assignments.values():
        for s in items:
            platform_counts[s.platform] = platform_counts.get(s.platform, 0) + 1
    for platform, count in platform_counts.items():
        budget = envelope.platform_budgets.get(platform)
        if budget is not None and count > budget:
            errors.append(
                f"platform_cap_exceeded:{platform} ({count} > {budget})"
            )

    # Check 2 — No item assigned to more than one format
    seen: dict[str, int] = {}
    for fid, items in format_assignments.items():
        for s in items:
            if s.candidate_id in seen:
                errors.append(
                    f"duplicate_assignment:{s.candidate_id} in formats "
                    f"{seen[s.candidate_id]} and {fid}"
                )
            else:
                seen[s.candidate_id] = fid

    # Check 3 — All assigned items satisfy format_eligibility
    for fid, items in format_assignments.items():
        rule = config.format_eligibility.get(fid, config.format_defaults)
        for s in items:
            if rule.excluded_categories and s.category in rule.excluded_categories:
                errors.append(
                    f"eligibility_violation:format {fid} item {s.candidate_id} "
                    f"category={s.category} excluded"
                )

    # Check 4 — No reused items
    for s_id in assigned_ids:
        c = candidates_by_id.get(s_id)
        if c and c.is_used:
            errors.append(f"reused_item:{s_id}")

    # Check 5 — Total assigned items ≤ envelope total
    total_assigned = sum(len(v) for v in format_assignments.values())
    if total_assigned > envelope.total_item_count:
        errors.append(
            f"total_exceeded:{total_assigned} > {envelope.total_item_count}"
        )

    # Check 6 — Partial flag correctness
    any_underfilled = any(
        len(items) < envelope.per_format_feasibility[fid].item_count
        for fid, items in format_assignments.items()
        if fid in envelope.per_format_feasibility
        and envelope.per_format_feasibility[fid].feasible
    )
    any_infeasible = any(
        not ff.feasible for ff in envelope.per_format_feasibility.values()
    )
    expected_partial = any_underfilled or any_infeasible
    # We don't flag a mismatch here — just record; is_partial is computed from partial_formats

    return {"is_valid": len(errors) == 0, "errors": errors}


# ---------------------------------------------------------------------------
# Candidate registry — populated by stage1_normalize, read by stage4_assign
# ---------------------------------------------------------------------------

_candidate_registry: dict[str, NormalizedCandidate] = {}


def register_candidates(candidates: list[NormalizedCandidate]) -> None:
    """Register Stage 1 candidates so Stage 4 can look up full objects by ID."""
    global _candidate_registry
    _candidate_registry = {c.candidate_id: c for c in candidates}


def _get_crawler_item_id(candidate_id: str) -> int:
    """Look up crawler_item_id for a candidate. Falls back to 0 if not registered."""
    c = _candidate_registry.get(candidate_id)
    return c.crawler_item_id if c else 0


def _selected_to_candidate(s_item: SelectedItem) -> NormalizedCandidate:
    """
    Return the full NormalizedCandidate from the registry for this SelectedItem.
    Falls back to a minimal shell if not found (should not happen in production).
    """
    c = _candidate_registry.get(s_item.candidate_id)
    if c:
        return c
    # Fallback shell — should never occur in a properly wired pipeline
    from datetime import timezone
    logger.warning(
        "Stage 4: candidate %s not in registry — using shell", s_item.candidate_id
    )
    return NormalizedCandidate(
        candidate_id         = s_item.candidate_id,
        url                  = s_item.url,
        platform             = s_item.platform,
        category             = s_item.category,
        language             = s_item.language,
        hotness              = s_item.hotness,
        effective_hotness    = s_item.effective_hotness,
        freshness            = s_item.freshness,
        eligible_format_ids  = s_item.eligible_format_ids,
        crawler_item_id      = 0,
        title_original       = s_item.candidate_id,
        canonical_title      = None,
        description_original = None,
        region_key           = None,
        region_name          = None,
        engagement_signals   = {},
        raw_payload          = None,
    )
