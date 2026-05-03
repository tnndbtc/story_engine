#!/usr/bin/env python3
"""
compute_weights.py — Dynamic surface weight computation for story_mix.json.

Run daily (e.g. via cron) BEFORE story generation to keep surface_weight_overrides
calibrated to actual crawl volume and hotness distribution.

Algorithm (per surface):
  1. Measure actual share: items from this surface / total items (7-day window)
  2. Measure hotness ratio: global_avg_hotness / surface_avg_hotness
  3. weight = clamp(desired_share/actual_share × hotness_ratio, 0.10, 3.0)

The 7-day window prevents oscillation from a single bad crawl day.
Surfaces not in platform_caps (purely editorial, e.g. bloomberg_news) skip the
volume factor and use hotness normalization only.

Writes updated surface_weight_overrides back to story_mix.json (in-place rewrite,
preserving all other keys). Prints a unified diff for audit.

Safety aborts:
  - Fewer than 5 000 total items in window → data too sparse, abort
  - Any platform in platform_caps returns 0 items → zero-division guard, skip

Usage:
  python compute_weights.py [--dry-run] [--days 7] [--db /path/to/db.sqlite3]
"""

import argparse
import difflib
import json
import os
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]  # src/scripts/ → src/ → story_engine/
STORY_MIX_PATH = os.environ.get('STORY_MIX_PATH', str(_REPO_ROOT / 'config' / 'story_mix.json'))
CRAWLER_DB_PATH = os.environ.get('CRAWLER_DB', '/home/tnnd/data/code/crawler/db.sqlite3')

# Weight bounds — same as selector.py effective_multiplier bounds
WEIGHT_MIN = 0.10
WEIGHT_MAX = 3.0

# Safety threshold: abort if total items across all surfaces is below this
MIN_TOTAL_ITEMS = 5_000


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn(db_path: str) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_surface_stats(conn: sqlite3.Connection, days: int) -> list[dict]:
    """
    Return per-surface item counts and average hotness for the last N days.

    Returns a list of dicts with keys:
      surface_key, platform, item_count, avg_hotness
    """
    rows = conn.execute(
        """
        SELECT
            ts.key          AS surface_key,
            ts.platform     AS platform,
            COUNT(*)        AS item_count,
            AVG(ti.hotness) AS avg_hotness
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        WHERE ti.hotness IS NOT NULL
          AND ti.collected_at >= datetime('now', '-' || ? || ' days')
          AND ts.enabled = 1
        GROUP BY ts.key, ts.platform
        ORDER BY item_count DESC
        """,
        (days,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_global_avg_hotness(conn: sqlite3.Connection, days: int) -> float:
    """Return the global average hotness across all surfaces for the last N days."""
    row = conn.execute(
        """
        SELECT AVG(ti.hotness) AS global_avg
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        WHERE ti.hotness IS NOT NULL
          AND ti.collected_at >= datetime('now', '-' || ? || ' days')
          AND ts.enabled = 1
        """,
        (days,),
    ).fetchone()
    return float(row['global_avg'] or 1.0)


# ---------------------------------------------------------------------------
# Weight computation
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_new_weights(
    surface_stats: list[dict],
    global_avg_h: float,
    platform_caps: dict[str, float],
    current_overrides: dict[str, float],
    days: int,
) -> dict[str, float]:
    """
    Compute updated surface_weight_overrides.

    Only surfaces that are ALREADY in current_overrides are updated — we do
    not add new entries automatically (that would require operator review).
    Surfaces not in current_overrides are left unchanged (they will use the
    platform_default_weights fallback in selector.py).

    For surfaces whose platform is in platform_caps (volume-capped platforms):
      weight = clamp(desired_share/actual_share × global_avg_h/surface_avg_h, min, max)

    For surfaces NOT in platform_caps (editorial/news sources):
      weight = clamp(global_avg_h/surface_avg_h, min, max)
      (hotness normalization only — no volume penalty)
    """
    total_items = sum(s['item_count'] for s in surface_stats)
    if total_items < MIN_TOTAL_ITEMS:
        print(
            f"ABORT: only {total_items} items in last {days} days "
            f"(threshold: {MIN_TOTAL_ITEMS}). Data too sparse — weights unchanged.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build lookup: surface_key → stats
    stats_by_key: dict[str, dict] = {s['surface_key']: s for s in surface_stats}

    # Compute desired share per platform (uniform across surfaces of same platform)
    # desired_share = platform_cap / number_of_surfaces_for_that_platform
    platform_surface_counts: dict[str, int] = {}
    for key, stats in stats_by_key.items():
        if key in current_overrides:
            p = stats['platform']
            platform_surface_counts[p] = platform_surface_counts.get(p, 0) + 1

    new_weights: dict[str, float] = {}

    for surface_key, current_weight in current_overrides.items():
        stats = stats_by_key.get(surface_key)
        if stats is None:
            # Surface not seen in this window (may have been disabled or renamed)
            print(
                f"  WARN: '{surface_key}' not found in DB for last {days} days "
                f"— keeping current weight {current_weight:.2f}"
            )
            new_weights[surface_key] = current_weight
            continue

        platform = stats['platform']
        item_count = stats['item_count']
        avg_h = stats['avg_hotness'] or 1.0  # guard against NULL

        if item_count == 0:
            # Zero items — platform may be down; keep current weight
            print(
                f"  WARN: '{surface_key}' has 0 items in last {days} days "
                f"— keeping current weight {current_weight:.2f}"
            )
            new_weights[surface_key] = current_weight
            continue

        # Hotness normalization factor: elevate low-hotness surfaces, dampen high-hotness
        hotness_factor = global_avg_h / avg_h

        if platform in platform_caps:
            # Volume-capped platform: also penalise/reward by share
            cap_frac = platform_caps[platform]
            n_surfaces = platform_surface_counts.get(platform, 1)
            # Desired share is split equally across all surfaces of this platform
            desired_share = cap_frac / n_surfaces
            actual_share = item_count / total_items
            if actual_share == 0:
                volume_factor = 1.0
            else:
                volume_factor = desired_share / actual_share
            raw_weight = volume_factor * hotness_factor
        else:
            # Editorial/news source: hotness normalization only
            raw_weight = hotness_factor

        new_weight = _clamp(raw_weight, WEIGHT_MIN, WEIGHT_MAX)
        new_weights[surface_key] = round(new_weight, 2)

        delta = new_weight - current_weight
        sign = '+' if delta >= 0 else ''
        print(
            f"  {surface_key:<30} {current_weight:.2f} → {new_weight:.2f} "
            f"({sign}{delta:.2f})  items={item_count}  avg_h={avg_h:.1f}"
        )

    return new_weights


# ---------------------------------------------------------------------------
# story_mix.json read / write
# ---------------------------------------------------------------------------

def load_story_mix(path: str) -> dict:
    if not os.path.exists(path):
        print(f"ERROR: story_mix.json not found at: {path}", file=sys.stderr)
        print("Set STORY_MIX_PATH env var or run from the story_engine repo root.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def write_story_mix(path: str, data: dict) -> None:
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')


def unified_diff(old_text: str, new_text: str, path: str) -> str:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f'a/{path}',
        tofile=f'b/{path}',
    )
    return ''.join(diff)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='Compute weights but do not write story_mix.json')
    parser.add_argument('--days', type=int, default=7,
                        help='Lookback window in days (default: 7)')
    parser.add_argument('--db', default=CRAWLER_DB_PATH,
                        help='Path to crawler SQLite DB')
    args = parser.parse_args()

    print(f"compute_weights.py  days={args.days}  db={args.db}  dry_run={args.dry_run}")
    print(f"story_mix: {STORY_MIX_PATH}")
    print()

    # Load current config
    mix = load_story_mix(STORY_MIX_PATH)
    current_overrides: dict[str, float] = mix.get('surface_weight_overrides', {})
    platform_caps: dict[str, float] = mix.get('platform_caps', {})

    if not current_overrides:
        print("No surface_weight_overrides in story_mix.json — nothing to update.")
        sys.exit(0)

    # Fetch stats from crawler DB
    if not os.path.exists(args.db):
        print(f"ERROR: crawler DB not found at: {args.db}", file=sys.stderr)
        print("Set CRAWLER_DB env var or pass --db /path/to/db.sqlite3", file=sys.stderr)
        sys.exit(1)
    conn = _get_conn(args.db)
    surface_stats = fetch_surface_stats(conn, args.days)
    global_avg_h = fetch_global_avg_hotness(conn, args.days)
    conn.close()

    total_items = sum(s['item_count'] for s in surface_stats)
    print(f"Total items in last {args.days} days: {total_items}")
    print(f"Global average hotness: {global_avg_h:.1f}")
    print()
    print("Surface weight updates:")

    new_weights = compute_new_weights(
        surface_stats=surface_stats,
        global_avg_h=global_avg_h,
        platform_caps=platform_caps,
        current_overrides=current_overrides,
        days=args.days,
    )

    # Build updated story_mix
    old_text = json.dumps(mix, indent=2, ensure_ascii=False) + '\n'
    mix['surface_weight_overrides'] = new_weights
    new_text = json.dumps(mix, indent=2, ensure_ascii=False) + '\n'

    diff = unified_diff(old_text, new_text, STORY_MIX_PATH)
    print()
    print("Diff:")
    print(diff if diff else "  (no changes)")

    if args.dry_run:
        print("Dry run — story_mix.json NOT written.")
    else:
        write_story_mix(STORY_MIX_PATH, mix)
        print(f"Written: {STORY_MIX_PATH}")


if __name__ == '__main__':
    main()
