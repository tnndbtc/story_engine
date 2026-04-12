"""
selector package — batch selection pipeline.

Public API:
    run_batch(format_ids, db_path, config_path, lang, channel,
              hours=48, snapshot_path=None) -> BatchResult

Stages:
    Stage 1: stage1_normalize  — candidate normalization + eligibility tagging
    Stage 2: stage2_allocate   — feasibility check + global allocation envelope
    Stage 3: stage3_select     — deterministic constrained selection (two-pass)
    Stage 4: stage4_assign     — format assignment + DB writes + trace log
"""

from engine.selector.schemas import BatchResult


def run_batch(
    format_ids:    list[int],
    db_path:       str,
    config_path:   str,
    lang:          str,
    channel:       int,
    hours:         int = 48,
    snapshot_path: str | None = None,
) -> BatchResult:
    """
    Run the full 4-stage batch selection pipeline.

    Args:
        format_ids:    List of integer format IDs to include in this batch.
        db_path:       Path to story_engine's own db.sqlite3.
        config_path:   Path to story_mix.json.
        lang:          Output language ("en" or "zh").
        channel:       Output channel (1, 2, or 3).
        hours:         Lookback window in hours for candidate fetch (default 48).
        snapshot_path: If set, replay candidates from this snapshot file instead
                       of querying the live DB (debug/determinism testing only).

    Returns:
        BatchResult with format_assignments dict[int, list[NormalizedCandidate]].
        The generators in run.py receive these candidates directly — no re-query.
    """
    from engine.selector.config import load_config
    from engine.selector.stage1_normalize import stage1_normalize, get_trace_path
    from engine.selector.stage2_allocate import stage2_allocate
    from engine.selector.stage3_select import stage3_select
    from engine.selector.stage4_assign import stage4_assign, register_candidates
    from engine.selector.snapshot import save_snapshot
    from db.models import create_story_set

    # 1. Load config
    config = load_config(config_path)

    # 2. Create story_sets row immediately — stage4_assign() will UPDATE it.
    #    batch_ts is int (UNIX ms) from create_story_set().
    story_set_id, batch_ts = create_story_set(lang, channel)

    # 3. Stage 1 — normalize candidates
    snap_path = ""
    if snapshot_path:
        from engine.selector.snapshot import load_snapshot
        from engine.selector.trace import open_trace
        from engine.selector.stage1_normalize import _store_trace_handle
        import os
        candidates = load_snapshot(snapshot_path)
        # Open trace handle for replay mode
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'logs')
        trace_handle = open_trace(logs_dir, batch_ts)
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
