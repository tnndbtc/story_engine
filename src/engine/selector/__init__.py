"""
selector package — batch selection pipeline.

Public API:
    run_batch(format_ids, db_path, config_path, lang, channel,
              hours=48, snapshot_path=None,
              config_profile=None) -> BatchResult

    config_profile (optional) loads config/story_mix_<profile>.json as a
    shallow atomic overlay on top of the base story_mix.json, enabling
    per-run channel specialization without splitting the global pool.

Stages:
    Stage 1: stage1_normalize  — candidate normalization + eligibility tagging
    Stage 2: stage2_allocate   — feasibility check + global allocation envelope
    Stage 3: stage3_select     — deterministic constrained selection (two-pass)
    Stage 4: stage4_assign     — format assignment + DB writes + trace log
"""

import logging

from engine.selector.schemas import BatchResult

_logger = logging.getLogger(__name__)


def _validate_replay_metadata(
    snapshot_metadata: dict,
    current_metadata: dict,
) -> None:
    """
    Compare snapshot-time classifier/profile state against current state.
    On mismatch, emit a "non-canonical replay" warning. The replay still
    runs; the result is just not treated as a bit-exact reproduction.
    """
    snap_profile = snapshot_metadata.get("profile_id")
    curr_profile = current_metadata.get("profile_id")
    snap_sha     = snapshot_metadata.get("keyword_map_sha")
    curr_sha     = current_metadata.get("keyword_map_sha")

    issues = []
    if snap_profile != curr_profile:
        issues.append(f"profile_id {snap_profile!r} → {curr_profile!r}")
    # Skip sha comparison when snapshot was legacy (no metadata at all)
    if snap_sha is not None and snap_sha != curr_sha:
        issues.append(f"keyword_map_sha {snap_sha!r} → {curr_sha!r}")

    if issues:
        _logger.warning(
            "Non-canonical replay: upstream state has moved since snapshot "
            "was taken (%s). Results may diverge from the original batch. "
            "Not a reproduction.",
            ", ".join(issues),
        )
    elif not snapshot_metadata:
        _logger.warning(
            "Legacy snapshot format — no metadata to validate replay "
            "canonicity against. Results cannot be validated as a "
            "bit-exact reproduction."
        )


def run_batch(
    format_ids:     list[int],
    db_path:        str,
    config_path:    str,
    lang:           str,
    channel:        int,
    hours:          int = 48,
    snapshot_path:  str | None = None,
    config_profile: str | None = None,
) -> BatchResult:
    """
    Run the full 4-stage batch selection pipeline.

    Args:
        format_ids:     List of integer format IDs to include in this batch.
        db_path:        Path to story_engine's own db.sqlite3.
        config_path:    Path to story_mix.json (the BASE file).
        lang:           Output language ("en" or "zh").
        channel:        Output channel (1, 2, or 3).
        hours:          Lookback window in hours for candidate fetch (default 48).
        snapshot_path:  If set, replay candidates from this snapshot file instead
                        of querying the live DB (debug/determinism testing only).
        config_profile: Optional per-run overlay profile id (e.g. "run2_ai").
                        When set, loads config/story_mix_<profile>.json as a
                        shallow atomic overlay on top of the base file. When
                        None, uses the base file directly (backward compat).

    Returns:
        BatchResult with format_assignments dict[int, list[NormalizedCandidate]].
        The generators in run.py receive these candidates directly — no re-query.
    """
    from engine.selector.config import load_with_profile
    from engine.selector.stage1_normalize import stage1_normalize, get_trace_path
    from engine.selector.stage2_allocate import stage2_allocate
    from engine.selector.stage3_select import stage3_select
    from engine.selector.stage4_assign import stage4_assign, register_candidates
    from engine.selector.snapshot import save_snapshot
    from db.models import create_story_set

    # 1. Load config (with optional per-run overlay)
    config = load_with_profile(config_path, config_profile)

    # 2. Create story_sets row immediately — stage4_assign() will UPDATE it.
    #    batch_ts is int (UNIX ms) from create_story_set().
    #    profile_id is persisted on the row so trend_ui can group story sets
    #    by themed channel (run2_ai / run3_world / run4_business).
    story_set_id, batch_ts = create_story_set(lang, channel, profile_id=config_profile)

    # 3. Stage 1 — normalize candidates
    snap_path = ""
    if snapshot_path:
        from engine.selector.snapshot import load_snapshot_with_metadata
        from engine.selector.trace import open_trace
        from engine.selector.stage1_normalize import (
            _store_trace_handle, _compute_batch_metadata,
        )
        import os
        snap_metadata, candidates = load_snapshot_with_metadata(snapshot_path)

        # Validate replay consistency: compare snapshot metadata against
        # current config + classifier state. On mismatch, the replay is
        # marked non-canonical (still runs, but not a bit-exact reproduction).
        current_metadata = _compute_batch_metadata(config)
        _validate_replay_metadata(snap_metadata, current_metadata)

        # Open trace handle for replay mode — use current metadata so the
        # replay's trace shows the state under which replay ran.
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'logs')
        trace_handle = open_trace(logs_dir, batch_ts, metadata=current_metadata)
        _store_trace_handle(batch_ts, trace_handle, logs_dir)
        snap_path = snapshot_path
    else:
        candidates = stage1_normalize(db_path, config, format_ids, hours, batch_ts)
        # snapshot path is inferred from batch_ts by save_snapshot
        import os
        snap_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "snapshots")
        snap_path = os.path.join(snap_dir, f"{batch_ts}_stage1.json")

    # 4. Register candidates with Stage 4 (so it can build full NormalizedCandidate objects)
    register_candidates(candidates)

    # 5. Stage 2 — feasibility + global allocation
    envelope = stage2_allocate(candidates, config, format_ids, batch_ts)

    # 6. Stage 3 — deterministic constrained selection
    selected, traces = stage3_select(candidates, envelope, config, batch_ts)

    # 7. Stage 4 — format assignment + DB writes + trace log
    result = stage4_assign(
        selected, traces, envelope, config, db_path, batch_ts, story_set_id
    )

    # Attach snapshot path to result
    result.snapshot_path = snap_path

    return result


__all__ = ['run_batch', 'BatchResult']
