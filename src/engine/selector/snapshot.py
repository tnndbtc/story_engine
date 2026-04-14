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


_SNAPSHOT_SCHEMA_VERSION = 2


def save_snapshot(
    candidates: list[NormalizedCandidate],
    db_path: str,
    batch_ts: int,
    metadata: dict | None = None,
) -> str:
    """
    Serialize list[NormalizedCandidate] to snapshots/{batch_ts}_stage1.json.

    File format (schema_version 2, introduced 2026-04-14):
        {
          "schema_version": 2,
          "metadata": {
            "profile_id":       "run2_ai" | null,
            "keyword_map_sha":  "abcdef12" | null,
            "batch_ts":         <int>
          },
          "candidates": [ { ... }, { ... }, ... ]
        }

    Legacy format (schema_version 1): bare JSON array of candidate dicts.
    load_snapshot() handles both on read.

    Args:
        candidates: Stage 1 output (used items already excluded).
        db_path:    Path to db.sqlite3 — used to locate snapshots/ directory.
        batch_ts:   UNIX milliseconds — used in file name.
        metadata:   Optional dict of batch metadata (profile_id,
                    keyword_map_sha, ...). When None, metadata block is
                    written with batch_ts only.

    Returns:
        Absolute path to the written snapshot file.
    """
    snap_dir = _snapshots_dir(db_path)
    os.makedirs(snap_dir, exist_ok=True)
    path = os.path.join(snap_dir, f"{batch_ts}_stage1.json")

    # Convert each NormalizedCandidate to dict
    candidates_data = [asdict(c) for c in candidates]

    envelope = {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "metadata": {
            "profile_id":      (metadata or {}).get("profile_id"),
            "keyword_map_sha": (metadata or {}).get("keyword_map_sha"),
            "batch_ts":        batch_ts,
        },
        "candidates": candidates_data,
    }

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(envelope, f, cls=_CandidateEncoder, ensure_ascii=False, indent=None)

    logger.info("Snapshot written: %s (%d candidates, profile=%s)",
                path, len(candidates), envelope["metadata"]["profile_id"] or '(base)')
    return path


def load_snapshot(snapshot_path: str) -> list[NormalizedCandidate]:
    """
    Deserialize a Stage 1 snapshot back to list[NormalizedCandidate].

    Supports both schema v2 (envelope with metadata + candidates) and
    legacy schema v1 (bare array). When loading a legacy v1 file, a
    "legacy-snapshot, replay validation degraded" warning is logged.

    Args:
        snapshot_path: Absolute path to the snapshot JSON file.

    Returns:
        List of NormalizedCandidate objects with all types restored.
    """
    metadata, candidates = load_snapshot_with_metadata(snapshot_path)
    return candidates


def load_snapshot_with_metadata(
    snapshot_path: str,
) -> tuple[dict, list[NormalizedCandidate]]:
    """
    Deserialize a Stage 1 snapshot and return both metadata and candidates.

    Used by replay tooling to detect non-canonical replays when upstream
    crawler classifier state has moved since the snapshot was taken.

    Returns:
        (metadata_dict, list[NormalizedCandidate])
        metadata_dict is {} for legacy v1 bare-array snapshots.
    """
    with open(snapshot_path, 'r', encoding='utf-8') as f:
        raw = json.load(f, object_hook=_candidate_decoder)

    # Detect schema: v2 is a dict with schema_version, v1 is a bare list
    if isinstance(raw, dict) and raw.get("schema_version") == _SNAPSHOT_SCHEMA_VERSION:
        metadata = raw.get("metadata", {})
        raw_list = raw.get("candidates", [])
    elif isinstance(raw, list):
        logger.warning(
            "Legacy snapshot format at %s — no metadata; replay validation "
            "degraded", snapshot_path
        )
        metadata = {}
        raw_list = raw
    else:
        raise ValueError(
            f"Unrecognized snapshot format at {snapshot_path} "
            f"(expected v2 envelope dict or v1 bare list)"
        )

    candidates = []
    for d in raw_list:
        # eligible_format_ids arrives as frozenset from the object hook
        candidates.append(NormalizedCandidate(**d))

    logger.info(
        "Snapshot loaded: %s (%d candidates, profile=%s)",
        snapshot_path, len(candidates), metadata.get("profile_id") or '(base)',
    )
    return metadata, candidates


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
