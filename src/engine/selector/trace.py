"""
Trace log writer for the batch selection pipeline.

All trace records (Stage 1 reuse exclusions, Stage 3 selections/rejections,
Stage 3 pass1 partial warnings) are written to a single JSONL file:
  logs/trace_{batch_ts}.jsonl

The file is opened ONCE in append mode ("a") and the handle is passed through
the pipeline from Stage 1 to Stage 4. Never open in write mode ("w") — that
would silently overwrite Stage 1's reuse exclusion records.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from io import TextIOBase
from pathlib import Path
from typing import IO

from engine.selector.schemas import TraceRecord

logger = logging.getLogger(__name__)


def open_trace(logs_dir: str, batch_ts: int, metadata: dict | None = None) -> IO:
    """
    Open the trace JSONL file for this batch in append mode.

    If `metadata` is provided (and the file is newly opened), writes a
    single "batch_metadata" event as the first line. This matches the
    existing `pass1_partial` event pattern and lets consumers that filter
    on `event` type see the profile_id and classifier state under which
    the batch ran, without touching the per-candidate TraceRecord schema.

    Args:
        logs_dir: Directory for log files (created if missing).
        batch_ts: UNIX milliseconds — used to name the file.
        metadata: Optional {profile_id, keyword_map_sha, ...}. Written
                  only when the file is empty (so multiple calls during
                  replay don't duplicate the header).

    Returns:
        An open file handle in text append mode.
        Caller is responsible for closing it after Stage 4.
    """
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, f"trace_{batch_ts}.jsonl")

    # Only write the header if the file is new/empty
    write_header = metadata is not None and (
        not os.path.exists(path) or os.path.getsize(path) == 0
    )

    handle = open(path, 'a', encoding='utf-8')

    if write_header:
        header = {
            "event":           "batch_metadata",
            "batch_ts":        batch_ts,
            "profile_id":      metadata.get("profile_id"),
            "keyword_map_sha": metadata.get("keyword_map_sha"),
        }
        handle.write(json.dumps(header, ensure_ascii=False) + '\n')
        handle.flush()

    return handle


def write_trace(handle: IO, record: TraceRecord) -> None:
    """Write one TraceRecord as a JSON line to the trace file."""
    d = asdict(record)
    # Convert frozenset fields if present (should not appear in TraceRecord
    # but guard defensively)
    for k, v in d.items():
        if isinstance(v, frozenset):
            d[k] = sorted(v)
        elif hasattr(v, 'isoformat'):
            d[k] = v.isoformat()
    handle.write(json.dumps(d, ensure_ascii=False) + '\n')


def emit_pass1_partial_warning(
    format_id: int,
    filled: int,
    required: int,
    batch_ts: int,
    handle: IO,
) -> None:
    """
    Emit a structured warning record when Stage 3 Pass 1 fails to fill
    a format's quota despite Stage 2 declaring it feasible.

    This is a diagnostic event record (not a per-candidate TraceRecord).
    Stage 4 uses these records to mark partial formats.

    Args:
        format_id: The format that came up short.
        filled:    How many items were actually reserved.
        required:  How many items were needed.
        batch_ts:  Current batch timestamp.
        handle:    Open trace file handle.
    """
    record = {
        "event":     "pass1_partial",
        "format_id": format_id,
        "filled":    filled,
        "required":  required,
        "batch_ts":  batch_ts,
    }
    handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    logger.warning(
        "Stage 3 Pass 1 partial: format %d filled %d/%d items",
        format_id, filled, required,
    )
