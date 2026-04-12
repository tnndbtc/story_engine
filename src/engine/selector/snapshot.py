"""
Snapshot serialization for the batch selection pipeline.

Snapshots are taken after Stage 1 normalization (post-dedup, post-eligibility-
tagging). They enable deterministic replay for debugging and regression testing.

File format: snapshots/{batch_ts}_stage1.json
Retention: deleted if older than 48 hours at batch start.

Custom encoder/decoder handles two non-JSON-serializable types:
  (a) frozenset[int] — NormalizedCandidate.eligible_format_ids
        write: sorted(list(value))    read: frozenset(value)
  (b) datetime — NormalizedCandidate.freshness
        write: datetime.isoformat()   read: datetime.fromisoformat()

Both types must be round-tripped correctly. Without (a) the batch crashes at
Stage 1 Step 6 with TypeError. Without (b) it crashes on the first datetime field.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.selector.schemas import NormalizedCandidate

logger = logging.getLogger(__name__)

# Retention window for snapshots (48 hours in seconds)
_SNAPSHOT_RETENTION_SECONDS = 48 * 3600


class _CandidateEncoder(json.JSONEncoder):
    """JSON encoder that handles frozenset and datetime fields."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, frozenset):
            return {"__frozenset__": sorted(obj)}
        if isinstance(obj, datetime):
            return {"__datetime__": obj.isoformat()}
        return super().default(obj)


def _candidate_decoder(obj: dict) -> Any:
    """JSON object hook that reconstructs frozenset and datetime fields."""
    if "__frozenset__" in obj:
        return frozenset(obj["__frozenset__"])
    if "__datetime__" in obj:
        return datetime.fromisoformat(obj["__datetime__"])
    return obj


def _snapshots_dir(db_path: str) -> str:
    """Return the snapshots/ directory alongside db.sqlite3."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "snapshots")


def save_snapshot(candidates: list[NormalizedCandidate], db_path: str, batch_ts: int) -> str:
    """
    Serialize list[NormalizedCandidate] to snapshots/{batch_ts}_stage1.json.

    Args:
        candidates: Stage 1 output (used items already excluded).
        db_path:    Path to db.sqlite3 — used to locate snapshots/ directory.
        batch_ts:   UNIX milliseconds — used in file name.

    Returns:
        Absolute path to the written snapshot file.
    """
    snap_dir = _snapshots_dir(db_path)
    os.makedirs(snap_dir, exist_ok=True)
    path = os.path.join(snap_dir, f"{batch_ts}_stage1.json")

    # Convert each NormalizedCandidate to dict
    data = [asdict(c) for c in candidates]

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, cls=_CandidateEncoder, ensure_ascii=False, indent=None)

    logger.info("Snapshot written: %s (%d candidates)", path, len(candidates))
    return path


def load_snapshot(snapshot_path: str) -> list[NormalizedCandidate]:
    """
    Deserialize a Stage 1 snapshot back to list[NormalizedCandidate].

    Args:
        snapshot_path: Absolute path to the snapshot JSON file.

    Returns:
        List of NormalizedCandidate objects with all types restored.
    """
    with open(snapshot_path, 'r', encoding='utf-8') as f:
        raw_list = json.load(f, object_hook=_candidate_decoder)

    candidates = []
    for d in raw_list:
        # eligible_format_ids arrives as frozenset from the object hook
        # (because it was encoded as {"__frozenset__": [...]})
        candidates.append(NormalizedCandidate(**d))

    logger.info("Snapshot loaded: %s (%d candidates)", snapshot_path, len(candidates))
    return candidates


def cleanup_old_snapshots(db_path: str) -> None:
    """
    Delete snapshot files older than 48 hours.

    Called at the start of each batch run before Stage 1 begins.
    """
    snap_dir = _snapshots_dir(db_path)
    if not os.path.isdir(snap_dir):
        return

    cutoff = time.time() - _SNAPSHOT_RETENTION_SECONDS
    removed = 0
    for fname in os.listdir(snap_dir):
        fpath = os.path.join(snap_dir, fname)
        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
            try:
                os.remove(fpath)
                removed += 1
            except OSError as e:
                logger.warning("Failed to remove old snapshot %s: %s", fpath, e)

    if removed:
        logger.info("Cleaned up %d snapshot(s) older than 48h from %s", removed, snap_dir)
