#!/usr/bin/env python3
"""
event_layer/purity_calibrator.py — Auto-calibrate the purity gate threshold.

Reads purity_log from the story engine DB, computes the optimal threshold
using the distribution of blocked vs allowed merge decisions, and writes
the result back to clustering_config.json.

Run daily via cron. Safe to run multiple times — fully idempotent.

Algorithm
---------
Threshold = 75th percentile of BLOCKED decision purity scores.
Interpretation: "reject anything below the score where 75% of
known-bad merges fall." This is conservative — prefers fewer false
blocks over fewer false passes.

Auto-enable logic
-----------------
Gate starts disabled. Once purity_log accumulates >= min_samples_to_enable
rows (default 300), the gate is automatically switched on and the
calibrated threshold applied. From that point, the threshold is
recalibrated daily as new data arrives.

Safety rails
------------
- Threshold is clamped to [threshold_min, threshold_max] (default 0.45–0.70)
- Threshold only updates when the change exceeds change_min_delta (default 0.03)
  to prevent noisy micro-adjustments
- Uses only the last 90 days of data to stay current with recent news patterns
- All failures are logged and exit with code 1 so cron can detect them
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Add src/ to path so db.models is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db.models import DB_PATH, get_connection

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_PATH: Path = Path(__file__).resolve().parents[3] / 'config' / 'clustering_config.json'

# ---------------------------------------------------------------------------
# Defaults (used when config file is missing or a key is absent)
# ---------------------------------------------------------------------------

_DEFAULTS: dict = {
    'purity_gate_enabled':        False,
    'purity_gate_threshold':      0.55,
    'min_samples_to_enable':      300,
    'threshold_min':              0.45,
    'threshold_max':              0.70,
    'change_min_delta':           0.03,
    'last_calibrated_at':         None,
    'sample_count_at_calibration': 0,
}

# Only use data from the last N days to stay current with news patterns
_LOOKBACK_DAYS = 90


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------

def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        # Fill in any missing keys with defaults
        for k, v in _DEFAULTS.items():
            cfg.setdefault(k, v)
        return cfg
    except FileNotFoundError:
        print(f"  [warn] {CONFIG_PATH} not found — using defaults")
        return dict(_DEFAULTS)
    except Exception as e:
        print(f"  [warn] failed to read config: {e} — using defaults")
        return dict(_DEFAULTS)


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + '\n')


# ---------------------------------------------------------------------------
# Threshold computation
# ---------------------------------------------------------------------------

def compute_threshold(conn) -> tuple[float, int, int]:
    """
    Compute the optimal purity gate threshold from recent purity_log data.

    Returns:
        (threshold, total_sample_count, blocked_count)

    Strategy: 75th percentile of BLOCKED decision scores.
    If fewer than 10 blocked samples exist, returns the current default
    without changing anything.
    """
    cutoff = int(time.time()) - _LOOKBACK_DAYS * 86400

    rows = conn.execute(
        "SELECT purity, allowed FROM purity_log WHERE created_at >= ? ORDER BY purity",
        (cutoff,),
    ).fetchall()

    total         = len(rows)
    blocked       = sorted(r[0] for r in rows if r[1] == 0)
    blocked_count = len(blocked)

    if blocked_count < 10:
        # Not enough blocked samples to make a meaningful calibration
        return _DEFAULTS['purity_gate_threshold'], total, blocked_count

    # 75th percentile of blocked scores
    idx       = int(blocked_count * 0.75)
    threshold = blocked[min(idx, blocked_count - 1)]

    return threshold, total, blocked_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[purity_calibrator] {ts}")

    config = load_config()

    threshold_min   = float(config['threshold_min'])
    threshold_max   = float(config['threshold_max'])
    change_min_delta = float(config['change_min_delta'])
    min_samples     = int(config['min_samples_to_enable'])
    current_thresh  = float(config['purity_gate_threshold'])
    currently_enabled = bool(config['purity_gate_enabled'])

    try:
        conn = get_connection()
        new_threshold, sample_count, blocked_count = compute_threshold(conn)
        conn.close()
    except Exception as e:
        print(f"  [error] DB read failed: {e}")
        return 1

    print(f"  samples (last {_LOOKBACK_DAYS}d): {sample_count} total, {blocked_count} blocked")
    print(f"  current: enabled={currently_enabled}, threshold={current_thresh}")

    changed = False

    # --- Auto-enable when enough samples have accumulated ---
    if not currently_enabled and sample_count >= min_samples:
        config['purity_gate_enabled'] = True
        changed = True
        print(f"  AUTO-ENABLED purity gate ({sample_count} >= {min_samples} samples)")

    # --- Clamp new threshold to safe range ---
    new_threshold = max(threshold_min, min(threshold_max, round(new_threshold, 3)))

    # --- Update threshold only if change is meaningful ---
    delta = abs(new_threshold - current_thresh)
    if delta >= change_min_delta:
        config['purity_gate_threshold'] = new_threshold
        changed = True
        direction = '↑' if new_threshold > current_thresh else '↓'
        print(f"  threshold {direction} {current_thresh:.3f} → {new_threshold:.3f} (delta={delta:.3f})")
    else:
        print(f"  threshold unchanged at {current_thresh:.3f} (delta={delta:.3f} < {change_min_delta})")

    # Always update metadata so we know when calibration last ran
    config['last_calibrated_at']          = ts
    config['sample_count_at_calibration'] = sample_count

    save_config(config)
    print(f"  [OK] config written → {CONFIG_PATH}")
    if changed:
        print(f"  [OK] gate settings updated")

    return 0


if __name__ == '__main__':
    sys.exit(main())
