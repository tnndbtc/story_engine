"""
Stage 2 — Feasibility + Global Allocation  (stage2_allocate.py)

Validates that available candidate supply can satisfy the batch's hard
constraints. Computes a batch-level allocation envelope. Identifies and
records infeasible formats BEFORE any selection begins.

Key correctness points:
  - Uncapped platforms (not in platform_caps) get budget =
    floor(total_item_count × config.default_uncapped_platform_max_share).
    NOT total_item_count. No platform is fully uncapped.
  - Most-constrained-first ordering uses eligible_count from supply index.
  - Greedy rollback is count-based only — no individual IDs at this stage.
  - FormatFeasibility.item_count = 0 for infeasible formats (not required count).
  - category_budgets["unknown"] = floor(total_item_count * 0.10) (NOT 0).
  - AllocationEnvelope contains budgets and feasibility only — no item assignments.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import engine.format_registry as format_registry
from engine.selector.config import BatchConfig
from engine.selector.schemas import (
    AllocationEnvelope,
    ConflictRecord,
    FormatFeasibility,
    NormalizedCandidate,
)

logger = logging.getLogger(__name__)


def stage2_allocate(
    candidates: list[NormalizedCandidate],
    config: BatchConfig,
    format_ids: list[int],
    batch_ts: int,
) -> AllocationEnvelope:
    """
    Stage 2: feasibility scan + global allocation envelope.

    Args:
        candidates: Stage 1 output (used items excluded).
        config:     Loaded BatchConfig.
        format_ids: Integer format IDs for this batch run.
        batch_ts:   UNIX milliseconds.

    Returns:
        AllocationEnvelope with platform_budgets, category_budgets,
        total_item_count, and per_format_feasibility.
    """
    # Step 1 — Build supply index: format_id → list of eligible candidates
    supply: dict[int, list[NormalizedCandidate]] = {fid: [] for fid in format_ids}
    for candidate in candidates:
        for fid in candidate.eligible_format_ids:
            if fid in supply:
                supply[fid].append(candidate)

    # Step 2 — Per-format feasibility scan (most-constrained first)
    per_format_feasibility: dict[int, FormatFeasibility] = {}

    # Sort by eligible supply size ascending (fewest candidates = most constrained)
    sorted_format_ids = sorted(format_ids, key=lambda fid: len(supply[fid]))

    for fid in sorted_format_ids:
        required = format_registry.item_count(fid)
        rule = config.format_eligibility.get(fid, config.format_defaults)

        if rule.source_restricted_to == "comment_platforms":
            # Primary/secondary supply split for formats like 26, 31
            supply_primary   = [c for c in supply[fid]
                                 if c.platform in config.comment_platforms]
            supply_secondary = [c for c in supply[fid]
                                 if c.platform not in config.comment_platforms]
            eligible_primary   = len(supply_primary)
            eligible_secondary = len(supply_secondary)
            eligible_total     = eligible_primary + eligible_secondary

            if eligible_total < required:
                per_format_feasibility[fid] = FormatFeasibility(
                    format_id           = fid,
                    feasible            = False,
                    eligible_count      = eligible_total,
                    item_count          = 0,
                    skip_reason         = "insufficient_supply",
                    blocking_constraint = f"supply:{fid}",
                )
                logger.warning(
                    "Stage 2: format %d infeasible — need %d, have %d eligible "
                    "(primary=%d, secondary=%d)",
                    fid, required, eligible_total, eligible_primary, eligible_secondary,
                )
            else:
                needs_sec = eligible_primary < required
                per_format_feasibility[fid] = FormatFeasibility(
                    format_id      = fid,
                    feasible       = True,
                    eligible_count = eligible_total,
                    item_count     = required,
                    needs_secondary = needs_sec,
                )
                if needs_sec:
                    logger.info(
                        "Stage 2: format %d will use secondary fill "
                        "(primary=%d < required=%d)",
                        fid, eligible_primary, required,
                    )
        else:
            eligible = len(supply[fid])

            if eligible < required:
                per_format_feasibility[fid] = FormatFeasibility(
                    format_id           = fid,
                    feasible            = False,
                    eligible_count      = eligible,
                    item_count          = 0,   # MUST be 0 for infeasible formats
                    skip_reason         = "insufficient_supply",
                    blocking_constraint = f"supply:{fid}",
                )
                logger.warning(
                    "Stage 2: format %d infeasible — need %d, have %d eligible",
                    fid, required, eligible,
                )
            else:
                per_format_feasibility[fid] = FormatFeasibility(
                    format_id      = fid,
                    feasible       = True,
                    eligible_count = eligible,
                    item_count     = required,
                )

    # Step 3 — Compute total_item_count from feasible formats only
    total_item_count = sum(
        ff.item_count
        for ff in per_format_feasibility.values()
        if ff.feasible
    )

    # Step 4 — Compute platform_budgets
    # Capped platforms: floor(total × cap_fraction)
    # Uncapped platforms: total_item_count (unlimited — must appear in platform_caps to be restricted)
    platform_budgets: dict[str, int] = {}

    # Populate caps for all known platforms.
    # Minimum of 1 ensures small batches (e.g. 5-item English runs) can still
    # draw from any allowed platform — floor(5 × 0.10) = 0 would otherwise
    # block hackernews/reddit/twitter entirely.
    for platform, cap_fraction in config.platform_caps.items():
        platform_budgets[platform] = max(math.floor(total_item_count * cap_fraction), 1)

    # Any platform in supply but NOT in platform_caps → capped at default share
    # No platform is fully uncapped — default share is config.default_uncapped_platform_max_share
    _uncapped_budget = max(math.floor(total_item_count * config.default_uncapped_platform_max_share), 1)
    for candidate in candidates:
        p = candidate.platform
        if p not in platform_budgets:
            platform_budgets[p] = _uncapped_budget

    # Step 4b — Compute platform_soft_budgets
    # soft_budget = floor(hard_budget × target_ratio_of_cap) per config
    # Used by Stage 3 Pass 2 _over_target() for deprioritization only — never a hard cap.
    soft_ratio = config.platform_targets.target_ratio_of_cap
    platform_soft_budgets: dict[str, int] = {
        p: math.floor(budget * soft_ratio)
        for p, budget in platform_budgets.items()
    }

    # Step 5 — Compute category_budgets
    category_budgets: dict[str, int] = {}
    for cat, fraction in config.category_mix.items():
        category_budgets[cat] = math.floor(total_item_count * fraction)

    # Unknown category: 10% of batch size as default dominance guard (NOT 0)
    # Design section Step 5 says 0 — that is WRONG. This note overrides it.
    category_budgets["unknown"] = math.floor(total_item_count * 0.10)

    # Step 6 — Joint feasibility check (greedy rollback, count-based)
    conflict_records: list[ConflictRecord] = []
    platform_reserved: dict[str, int] = {p: 0 for p in platform_budgets}

    # Process feasible formats in most-constrained-first order
    feasible_sorted = sorted(
        [fid for fid, ff in per_format_feasibility.items() if ff.feasible],
        key=lambda fid: per_format_feasibility[fid].eligible_count,
    )

    for fid in feasible_sorted:
        ff = per_format_feasibility[fid]
        required = ff.item_count

        # Count how many of the eligible candidates fit within remaining platform budgets
        platform_counts: dict[str, int] = {}
        for candidate in supply[fid]:
            p = candidate.platform
            platform_counts[p] = platform_counts.get(p, 0) + 1

        # Tentative: can we fill required items without exhausting any platform budget?
        # Simple count-based check: sum up how many slots remain per platform
        available_for_format = 0
        for candidate in supply[fid]:
            p = candidate.platform
            remaining = platform_budgets.get(p, total_item_count) - platform_reserved.get(p, 0)
            if remaining > 0:
                available_for_format += 1

        if available_for_format < required:
            # Identify the blocking platform(s)
            blocking_platforms = []
            for p, cap in platform_budgets.items():
                reserved = platform_reserved.get(p, 0)
                if reserved >= cap:
                    blocking_platforms.append(p)

            blocking_str = ",".join(f"platform_cap:{p}" for p in blocking_platforms) or "platform_budget_exhausted"
            conflict = ConflictRecord(
                constraint_a        = blocking_str,
                constraint_b        = f"format_supply:{fid}",
                description         = (
                    f"Format {fid} needs {required} items but only {available_for_format} "
                    f"fit within remaining platform budgets after higher-priority formats reserved slots."
                ),
                affected_format_ids = [fid],
            )
            conflict_records.append(conflict)

            # Mark format infeasible (rollback this format's allocation)
            per_format_feasibility[fid] = FormatFeasibility(
                format_id           = fid,
                feasible            = False,
                eligible_count      = ff.eligible_count,
                item_count          = 0,
                skip_reason         = "platform_cap_conflict",
                blocking_constraint = blocking_str,
            )
            logger.warning(
                "Stage 2: format %d marked infeasible due to platform cap conflict "
                "(available=%d, required=%d)",
                fid, available_for_format, required,
            )
        else:
            # Tentatively reserve platform slots for this format (count-based only)
            slots_reserved = 0
            for candidate in supply[fid]:
                if slots_reserved >= required:
                    break
                p = candidate.platform
                remaining = platform_budgets.get(p, total_item_count) - platform_reserved.get(p, 0)
                if remaining > 0:
                    platform_reserved[p] = platform_reserved.get(p, 0) + 1
                    slots_reserved += 1

    # Recompute total_item_count after Step 6 (some formats may have become infeasible)
    total_item_count = sum(
        ff.item_count
        for ff in per_format_feasibility.values()
        if ff.feasible
    )

    # Recompute budgets with updated total (minimum 1 preserved here too)
    for platform, cap_fraction in config.platform_caps.items():
        platform_budgets[platform] = max(math.floor(total_item_count * cap_fraction), 1)
    _uncapped_budget_final = max(math.floor(total_item_count * config.default_uncapped_platform_max_share), 1)
    for candidate in candidates:
        p = candidate.platform
        if p not in config.platform_caps:
            platform_budgets[p] = _uncapped_budget_final

    for cat, fraction in config.category_mix.items():
        category_budgets[cat] = math.floor(total_item_count * fraction)
    category_budgets["unknown"] = math.floor(total_item_count * 0.10)

    # Recompute soft budgets with updated platform_budgets
    platform_soft_budgets = {
        p: math.floor(budget * soft_ratio)
        for p, budget in platform_budgets.items()
    }

    # Step 7 — Build and return AllocationEnvelope
    partial = any(not ff.feasible for ff in per_format_feasibility.values())

    envelope = AllocationEnvelope(
        batch_ts               = batch_ts,
        total_item_count       = total_item_count,
        platform_budgets       = platform_budgets,
        platform_soft_budgets  = platform_soft_budgets,
        category_budgets       = category_budgets,
        per_format_feasibility = per_format_feasibility,
        conflict_records       = conflict_records,
        partial                = partial,
    )

    feasible_count = sum(1 for ff in per_format_feasibility.values() if ff.feasible)
    logger.info(
        "Stage 2 complete: %d/%d formats feasible, total_item_count=%d, partial=%s",
        feasible_count, len(format_ids), total_item_count, partial,
    )
    if conflict_records:
        logger.warning("Stage 2: %d conflict record(s) emitted", len(conflict_records))

    return envelope
