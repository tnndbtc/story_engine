"""
Stage 3 — Deterministic Constrained Selection  (stage3_select.py)

Selects the exact items to include in the batch, respecting all hard
constraints from the AllocationEnvelope, in a fully deterministic order.
Emits a TraceRecord for every candidate (selected + rejected).

Algorithm: two-pass (format reservation then global fill).
  Pass 1: reserve items per format (most-constrained first)
  Pass 2: fill remaining global budget slots from unreserved candidates

Key correctness points:
  - Platform cap is a hard reject in BOTH passes.
  - Category diversity is advisory only — dominance logged, items never blocked.
  - Every candidate gets exactly one TraceRecord (no doubles, no missing).
  - DOMINANCE_MULTIPLIER comes from config.category_dominance_multiplier.
  - reserved_for_format is set on SelectedItem for all Pass 1 items.
  - Pass 2 does not re-assign items reserved in Pass 1.
  - emit_pass1_partial_warning() written to trace file, not as a TraceRecord.
"""

from __future__ import annotations

import logging
from datetime import datetime

import engine.format_registry as format_registry
from engine.selector.config import BatchConfig
from engine.selector.schemas import (
    AllocationEnvelope,
    NormalizedCandidate,
    SelectedItem,
    TraceRecord,
)
from engine.selector.stage1_normalize import get_trace_handle
from engine.selector.trace import emit_pass1_partial_warning, write_trace

logger = logging.getLogger(__name__)


def stage3_select(
    candidates: list[NormalizedCandidate],
    envelope: AllocationEnvelope,
    config: BatchConfig,
    batch_ts: int,
) -> tuple[list[SelectedItem], list[TraceRecord]]:
    """
    Stage 3: two-pass deterministic constrained selection.

    Args:
        candidates: Stage 1 output (used items excluded).
        envelope:   Stage 2 AllocationEnvelope.
        config:     Loaded BatchConfig (for category_dominance_multiplier).
        batch_ts:   UNIX milliseconds.

    Returns:
        (selected, traces) — all traces include both selected and rejected candidates.
    """
    trace_handle = get_trace_handle(batch_ts)
    dominance_multiplier = config.category_dominance_multiplier

    # Step 1 — Initialize shared state
    platform_used: dict[str, int] = {p: 0 for p in envelope.platform_budgets}
    category_used: dict[str, int] = {}
    reserved: dict[int, list[str]] = {}      # format_id → [candidate_ids]
    reserved_ids: set[str] = set()

    feasible_formats: set[int] = {
        fid for fid, ff in envelope.per_format_feasibility.items()
        if ff.feasible
    }

    # Step 2 — Sort candidates (deterministic — applied once, used in both passes)
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (
            -c.effective_hotness,
            -c.hotness,
            -c.freshness.timestamp(),
            c.candidate_id,   # stable string tiebreak
        )
    )

    # Tracks which candidates have received a TraceRecord (to avoid doubles)
    traced_ids: set[str] = set()

    def _emit_trace(
        candidate: NormalizedCandidate,
        status: str,
        reasons: list[str],
        constraint_hits: list[str],
        final_assignment: int | None = None,
    ) -> TraceRecord:
        record = TraceRecord(
            candidate_id      = candidate.candidate_id,
            url               = candidate.url,
            platform          = candidate.platform,
            category          = candidate.category,
            language          = candidate.language,
            format_considered = None,
            selection_status  = status,
            rejection_reasons = reasons,
            constraint_hits   = constraint_hits,
            score             = candidate.effective_hotness,
            hotness           = candidate.hotness,
            rank_inputs       = {
                "effective_hotness": candidate.effective_hotness,
                "hotness":           candidate.hotness,
                "freshness_ts":      candidate.freshness.timestamp(),
            },
            final_assignment  = final_assignment,
            batch_ts          = batch_ts,
        )
        write_trace(trace_handle, record)
        traced_ids.add(candidate.candidate_id)
        return record

    # ==========================================================================
    # PASS 1 — Per-format quota reservation (most-constrained first)
    # ==========================================================================

    # Step 3 — Sort feasible formats by eligible_count ascending
    feasible_sorted = sorted(
        feasible_formats,
        key=lambda fid: envelope.per_format_feasibility[fid].eligible_count,
    )

    # Step 4 — Reserve items per format
    # Pick one-by-one with running platform budget check.
    # Building the filtered list first then slicing [:N] is WRONG — it can
    # include multiple same-platform items that together exceed the budget.
    for fid in feasible_sorted:
        ff = envelope.per_format_feasibility[fid]
        rule = config.format_eligibility.get(fid, config.format_defaults)
        picked: list[NormalizedCandidate] = []

        if rule.source_restricted_to == "comment_platforms":
            # Phase A — primary pass: comment_platforms only
            for c in sorted_candidates:
                if len(picked) >= ff.item_count:
                    break
                if fid not in c.eligible_format_ids:
                    continue
                if c.candidate_id in reserved_ids:
                    continue
                if c.platform not in config.comment_platforms:
                    continue
                if not _within_platform_budget(c.platform, platform_used, envelope.platform_budgets):
                    continue
                picked.append(c)
                platform_used[c.platform] = platform_used.get(c.platform, 0) + 1
                category_used[c.category] = category_used.get(c.category, 0) + 1

            # Phase B — secondary pass: general pool if primary insufficient
            # secondary_is_relaxation=False: does NOT trigger partial warning
            if len(picked) < ff.item_count:
                picked_ids = {p.candidate_id for p in picked}
                for c in sorted_candidates:
                    if len(picked) >= ff.item_count:
                        break
                    if fid not in c.eligible_format_ids:
                        continue
                    if c.candidate_id in reserved_ids or c.candidate_id in picked_ids:
                        continue
                    if c.platform in config.comment_platforms:
                        continue  # already tried primary
                    if not _within_platform_budget(c.platform, platform_used, envelope.platform_budgets):
                        continue
                    picked.append(c)
                    platform_used[c.platform] = platform_used.get(c.platform, 0) + 1
                    category_used[c.category] = category_used.get(c.category, 0) + 1
                    picked_ids.add(c.candidate_id)
        else:
            # Normal format — single pass
            for c in sorted_candidates:
                if len(picked) >= ff.item_count:
                    break
                if fid not in c.eligible_format_ids:
                    continue
                if c.candidate_id in reserved_ids:
                    continue
                if not _within_platform_budget(c.platform, platform_used, envelope.platform_budgets):
                    continue
                picked.append(c)
                platform_used[c.platform] = platform_used.get(c.platform, 0) + 1
                category_used[c.category] = category_used.get(c.category, 0) + 1

        reserved[fid] = [c.candidate_id for c in picked]
        reserved_ids.update(reserved[fid])

        if len(picked) < ff.item_count:
            emit_pass1_partial_warning(fid, len(picked), ff.item_count, batch_ts, trace_handle)

    # ==========================================================================
    # PASS 2 — Global fill (remaining budget slots)
    # ==========================================================================

    # Step 5 — Fill remaining slots from unreserved candidates
    # Pre-sort into group_a (under or at category/platform soft target) and group_b (over target).
    # group_a is processed first — implements deprioritize_when_over_target soft target.
    # Within each group, ordering is preserved from sorted_candidates (hotness-desc).
    def _over_target(c: NormalizedCandidate) -> bool:
        """True if candidate's category OR platform is over its soft target."""
        cat_target = envelope.category_budgets.get(c.category, 0)
        cat_over   = cat_target > 0 and category_used.get(c.category, 0) >= cat_target
        plat_soft  = envelope.platform_soft_budgets.get(c.platform)
        plat_over  = plat_soft is not None and platform_used.get(c.platform, 0) >= plat_soft
        return cat_over or plat_over

    unreserved = [c for c in sorted_candidates if c.candidate_id not in reserved_ids]
    group_a = [c for c in unreserved if not _over_target(c)]
    group_b = [c for c in unreserved if     _over_target(c)]
    pass2_candidates = group_a + group_b
    remaining = envelope.total_item_count - len(reserved_ids)
    global_fill_ids: list[str] = []

    for candidate in pass2_candidates:
        if remaining <= 0:
            break
        if candidate.candidate_id in reserved_ids:
            continue   # already reserved in Pass 1

        # Hard reject: platform cap
        if not _within_platform_budget(candidate.platform, platform_used, envelope.platform_budgets):
            _emit_trace(
                candidate,
                status="rejected",
                reasons=[f"platform_cap:{candidate.platform}"],
                constraint_hits=[f"platform_cap:{candidate.platform}"],
            )
            continue

        # Hard reject: no eligible feasible format (item can't serve any format in this batch)
        if not (candidate.eligible_format_ids & feasible_formats):
            _emit_trace(
                candidate,
                status="excluded",
                reasons=["no_eligible_feasible_format"],
                constraint_hits=["format_eligibility"],
            )
            continue

        # Category diversity advisory check — log dominance but do NOT block
        cat_target = envelope.category_budgets.get(candidate.category, 0)
        cat_used_n = category_used.get(candidate.category, 0)
        if cat_target > 0 and cat_used_n >= cat_target * dominance_multiplier:
            logger.debug(
                "Category dominance: %s at %d/%d (multiplier=%.1f) — item still included",
                candidate.category, cat_used_n, cat_target, dominance_multiplier,
            )

        platform_used[candidate.platform] = platform_used.get(candidate.platform, 0) + 1
        category_used[candidate.category] = category_used.get(candidate.category, 0) + 1
        global_fill_ids.append(candidate.candidate_id)
        remaining -= 1

        _emit_trace(
            candidate,
            status="selected",
            reasons=[],
            constraint_hits=[],
            final_assignment=None,  # format assigned in Stage 4
        )

    # Step 6 — Build SelectedItem list
    all_selected_ids = reserved_ids | set(global_fill_ids)
    selected: list[SelectedItem] = []
    _step6_seen: set[str] = set()  # guard against duplicate URLs in candidates list

    for candidate in candidates:
        if candidate.candidate_id not in all_selected_ids:
            continue
        if candidate.candidate_id in _step6_seen:
            continue  # skip duplicate URL (same URL from multiple crawl surfaces)
        _step6_seen.add(candidate.candidate_id)
        reserved_fmt = next(
            (fid for fid, ids in reserved.items() if candidate.candidate_id in ids),
            None
        )
        selected.append(SelectedItem(
            candidate_id        = candidate.candidate_id,
            url                 = candidate.url,
            platform            = candidate.platform,
            category            = candidate.category,
            language            = candidate.language,
            hotness             = candidate.hotness,
            effective_hotness   = candidate.effective_hotness,
            freshness           = candidate.freshness,
            score               = candidate.effective_hotness,
            eligible_format_ids = candidate.eligible_format_ids,
            reserved_for_format = reserved_fmt,
        ))

        # Emit trace for Pass 1 reserved items (not yet traced)
        if candidate.candidate_id not in traced_ids:
            _emit_trace(
                candidate,
                status="selected",
                reasons=[],
                constraint_hits=[],
                final_assignment=reserved_fmt,
            )

    # Step 7 — Emit trace for all candidates not yet traced (not reached or over budget)
    for candidate in sorted_candidates:
        if candidate.candidate_id not in traced_ids:
            _emit_trace(
                candidate,
                status="rejected",
                reasons=["not_reached_or_over_budget"],
                constraint_hits=[],
            )

    # Collect all traces written (we don't keep them in memory, but return summary list)
    # For API compatibility, return an empty list — actual records are in the JSONL file.
    # Callers that need traces should read the trace file.
    traces: list[TraceRecord] = []

    logger.info(
        "Stage 3 complete: %d selected (%d reserved in Pass 1, %d global fill)",
        len(selected), len(reserved_ids), len(global_fill_ids),
    )
    for fid, ids in reserved.items():
        logger.debug("  format %d: %d items reserved", fid, len(ids))

    return selected, traces


def _within_platform_budget(
    platform: str,
    platform_used: dict[str, int],
    platform_budgets: dict[str, int],
) -> bool:
    """Return True if this platform has remaining budget."""
    budget = platform_budgets.get(platform)
    if budget is None:
        return True   # uncapped platform not in budgets dict
    used = platform_used.get(platform, 0)
    return used < budget
