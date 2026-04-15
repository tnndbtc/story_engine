"""
Data schemas for the batch selection pipeline.

All dataclasses here are pure data — no logic.
format_id fields use int throughout (not str).
batch_ts fields use int (UNIX milliseconds from _now()).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class NormalizedCandidate:
    """A single crawler item after Stage 1 normalization and eligibility tagging."""

    # Core identification
    candidate_id:        str           # stable unique key — crawler_url
    url:                 str
    platform:            str
    category:            str           # normalized; "unknown" if not in category_mix
    language:            str           # ti.lang_group from crawler DB

    # Ranking signals
    hotness:             float
    effective_hotness:   float         # hotness × topic_boost
    freshness:           datetime      # ti.collected_at, UTC

    # Eligibility
    eligible_format_ids: frozenset[int]  # which formats this item can serve

    # Generator-required fields (verified against generator.py usages)
    crawler_item_id:      int           # ti.id — required for used_items INSERT
    title_original:       str           # raw crawler title
    canonical_title:      str | None    # pre-computed display title (may be None)
    description_original: str | None    # raw crawler description
    region_key:           str | None    # e.g. "tw", "jp"
    region_name:          str | None    # human label e.g. "Taiwan" — derived at Stage 1
    engagement_signals:   dict          # e.g. {"comments": N, "shares": N}; may be {}
    raw_payload:          str | None    # raw JSON from crawler — used by _extract_comments()

    # Reuse state (set by Stage 1)
    is_used:             bool = False

    # New-development flag (set by Stage 1 event_memory classifier)
    # True when the item is a follow-up on a recently told story —
    # it is NOT excluded, but generators use it to frame the story as an update.
    is_new_development:  bool = False
    prior_story_title:   str | None = None   # title of the matched past story


@dataclass
class FormatFeasibility:
    """Stage 2 assessment of one format's feasibility."""

    format_id:           int
    feasible:            bool
    eligible_count:      int           # how many candidates are eligible for this format
    item_count:          int           # required item count; 0 if not feasible
    skip_reason:         str | None = None
    blocking_constraint: str | None = None
    needs_secondary:     bool = False   # True for formats 26/31 when primary
                                        # (comment_platform) supply is insufficient;
                                        # secondary fill is expected behavior, not partial.


@dataclass
class ConflictRecord:
    """Records a constraint conflict detected during Stage 2 allocation."""

    constraint_a:        str           # e.g. "platform_cap:bilibili"
    constraint_b:        str           # e.g. "format_supply:11"
    description:         str
    affected_format_ids: list[int]


@dataclass
class AllocationEnvelope:
    """Stage 2 output: batch-level budget envelope (NOT per-format item assignments)."""

    batch_ts:               int              # UNIX milliseconds
    total_item_count:       int              # sum of item_count for all feasible formats
    platform_budgets:       dict[str, int]   # platform → hard max items allowed
    platform_soft_budgets:  dict[str, int]   # platform → soft target slots
                                             # = floor(hard_budget × target_ratio_of_cap)
                                             # deprioritization only — never a hard cap
    category_budgets:       dict[str, int]   # category → soft target item count
    per_format_feasibility: dict[int, FormatFeasibility]
    conflict_records:       list[ConflictRecord] = field(default_factory=list)
    partial:                bool = False     # True if any format is infeasible


@dataclass
class TraceRecord:
    """One trace entry per candidate per batch run — emitted to JSONL trace log."""

    candidate_id:      str
    url:               str
    platform:          str
    category:          str
    language:          str
    format_considered: int | None    # None for exclusions at Stage 1
    selection_status:  str           # "selected", "rejected", "excluded"
    rejection_reasons: list[str]
    constraint_hits:   list[str]
    score:             float
    hotness:           float
    rank_inputs:       dict[str, Any]
    final_assignment:  int | None    # format_id this item was assigned to; None if not assigned
    batch_ts:          int | None = None


@dataclass
class SelectedItem:
    """An item chosen during Stage 3 for inclusion in the batch."""

    candidate_id:        str
    url:                 str
    platform:            str
    category:            str
    language:            str
    hotness:             float
    effective_hotness:   float
    freshness:           datetime
    score:               float
    eligible_format_ids: frozenset[int]
    reserved_for_format: int | None    # set by Stage 3 Pass 1; None for global fill items


@dataclass
class PartialFormat:
    """Records a format that could not be fully fulfilled."""

    skipped_format_id:                int
    shortage_dimension:               str         # "platform" | "category" | "supply" | "fidelity"
    blocking_constraint:              str | None
    candidate_count_before_filtering: int         # eligible_count from FormatFeasibility
    candidate_count_after_filtering:  int         # actual items assigned (0 if infeasible)


@dataclass
class BatchResult:
    """Final output of run_batch() — selection results only. Generation is separate."""

    story_set_id:       int
    batch_ts:           int                            # UNIX milliseconds
    format_assignments: dict[int, list[NormalizedCandidate]]  # format_id → candidates
    partial:            bool
    partial_formats:    list[PartialFormat]
    trace_path:         str                            # path to JSONL trace log
    snapshot_path:      str                            # path to Stage 1 snapshot JSON
    cluster_map:        dict[str, Any] = field(default_factory=dict)
    # cluster_map: {candidate_id → EventCluster}
    # Typed as dict[str, Any] to avoid a circular import with event_layer.clustering.
    # Callers that need the full type can import EventCluster directly.
    event_graph:        Any = None
    # event_graph: EventGraph | None
    # Graph linking related but distinct events via embedding cosine + entity overlap.
    # None if clustering failed or produced < 2 clusters.
