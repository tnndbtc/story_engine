#!/usr/bin/env python3
"""
src/scripts/calibrate_attract_weights.py — Change 4: Attract Scorer Weight Calibration

Correlates each attract_scorer dimension score with actual video performance
(retention + CTR + views) to learn which dimensions actually predict viewer
engagement. Re-weights accordingly.

Run weekly via cron after sufficient data accumulates (~80+ published videos):
    cd /home/tnnd/data/code/story_engine
    python src/scripts/calibrate_attract_weights.py

Output format (config/attract_weights.json):
    {
      "curiosity_gap":             22,
      "hidden_mechanism":          24,
      "ordinary_people_stakes":    18,
      "consequence_clarity":       15,
      "audience_fit":              13,
      "title_retention_alignment":  5,
      "low_context_accessibility":  3
    }

    Values sum to 100. attract_scorer.py loads this at startup and rescales
    raw dimension scores: effective = raw × (new_weight / old_max).

Safeguards:
  - Correlations computed WITHIN story_type groups to control for topic confounds
    (e.g. crime stories naturally score high on stakes AND retain viewers).
  - Each weight is clamped to ±15% of its current value per run (no oscillation).
  - Types with n < MIN_PER_TYPE excluded from the correlation (not enough data).
  - Requires MIN_TOTAL_VIDEOS total before any update is written.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

# src/ is parents[1] from src/scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.models import get_connection

_CONFIG_DIR   = Path(__file__).resolve().parents[2] / "config"
_OUTPUT_PATH  = _CONFIG_DIR / "attract_weights.json"
_MIN_PER_TYPE = 10    # minimum videos per story_type for within-type correlation
_MIN_TOTAL    = 30    # minimum total videos before any update is written
_MAX_CHANGE   = 0.15  # maximum weight change per dimension per run (±15%)
_MIN_VIEWS    = 100   # ignore micro-view videos

# Current hardcoded maxes (must match attract_scorer._DIMS_MAX)
_DIMS_MAX: dict = {
    "curiosity_gap":             20,
    "ordinary_people_stakes":    20,
    "hidden_mechanism":          20,
    "consequence_clarity":       15,
    "audience_fit":              15,
    "title_retention_alignment":  5,
    "low_context_accessibility":  5,
}


def _pearson(xs: list, ys: list) -> float:
    """Pearson correlation coefficient between two lists."""
    n = len(xs)
    if n < 3:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num    = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x  = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y  = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def _performance_score(avg_view_pct, ctr_pct, views, max_views):
    """Composite performance score 0–1."""
    retention  = (avg_view_pct or 0) / 100.0
    ctr        = (ctr_pct or 0) / 100.0
    view_score = math.log10((views or 1) + 1) / math.log10(max_views + 1)
    return 0.5 * retention + 0.3 * ctr + 0.2 * view_score


def _load_current_weights() -> dict:
    """Load existing attract_weights.json or fall back to _DIMS_MAX."""
    try:
        with open(_OUTPUT_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return dict(_DIMS_MAX)
    except Exception as e:
        print(f"calibrate_attract_weights: could not load existing weights — {e}")
        return dict(_DIMS_MAX)


def main():
    print("calibrate_attract_weights: loading data from DB ...")

    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT h.story_type, h.attractiveness_breakdown,
                      y.avg_view_pct, y.ctr_pct, y.views
               FROM hierarchical_stories h
               JOIN youtube_publish_log y ON y.story_id = h.id
               WHERE y.avg_view_pct IS NOT NULL
                 AND y.analytics_pulled_at > 0
                 AND y.views >= ?
                 AND h.story_type IS NOT NULL
                 AND h.attractiveness_breakdown IS NOT NULL""",
            (_MIN_VIEWS,)
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < _MIN_TOTAL:
        print(
            f"calibrate_attract_weights: only {len(rows)} videos with data "
            f"(need {_MIN_TOTAL}) — skipping update."
        )
        return

    all_views = [r['views'] for r in rows if r['views'] and r['views'] > 0]
    max_views  = max(all_views) if all_views else 1

    # Group data by story_type
    # Each entry: {dim: raw_score, ..., '_perf': performance_score}
    by_type: dict = defaultdict(list)
    parse_errors  = 0

    for r in rows:
        try:
            bd = json.loads(r['attractiveness_breakdown']) if r['attractiveness_breakdown'] else {}
        except json.JSONDecodeError:
            parse_errors += 1
            continue

        entry = {}
        for dim in _DIMS_MAX:
            raw = bd.get(dim)
            if isinstance(raw, dict):
                entry[dim] = int(raw.get("score", 0))
            elif isinstance(raw, (int, float)):
                entry[dim] = int(raw)
            else:
                entry[dim] = 0

        entry['_perf'] = _performance_score(
            r['avg_view_pct'], r['ctr_pct'], r['views'], max_views
        )
        by_type[r['story_type']].append(entry)

    if parse_errors > 0:
        print(f"  Warning: {parse_errors} rows had unparseable attractiveness_breakdown (skipped)")

    # Compute within-type Pearson correlation for each dimension
    dim_correlations: dict = defaultdict(list)  # dim → [(correlation, n), ...]

    for story_type, entries in by_type.items():
        n = len(entries)
        if n < _MIN_PER_TYPE:
            print(f"  {story_type}: {n} videos < {_MIN_PER_TYPE} — excluded from calibration")
            continue

        print(f"  {story_type}: {n} videos — computing correlations ...")

        perf_vals = [e['_perf'] for e in entries]
        for dim in _DIMS_MAX:
            dim_vals = [e[dim] for e in entries]
            corr     = _pearson(dim_vals, perf_vals)
            dim_correlations[dim].append((corr, n))
            print(f"    {dim:<30} r={corr:+.3f} (n={n})")

    if not any(dim_correlations.values()):
        print("\ncalibrate_attract_weights: no story_type has enough data — skipping update.")
        return

    # Aggregate correlations (weighted by n) across qualifying types
    print("\nAggregated (n-weighted) correlations:")
    dim_agg_corr: dict = {}
    for dim in _DIMS_MAX:
        corr_list = dim_correlations[dim]
        if not corr_list:
            dim_agg_corr[dim] = 0.0
            continue
        total_n   = sum(n for _, n in corr_list)
        weighted  = sum(c * n for c, n in corr_list) / total_n if total_n > 0 else 0.0
        dim_agg_corr[dim] = weighted
        print(f"  {dim:<30} r_agg={weighted:+.3f}")

    # Shift correlations to positive range (min → 0) then normalise to sum=100
    min_corr = min(dim_agg_corr.values())
    shifted  = {d: max(0.0, c - min_corr) for d, c in dim_agg_corr.items()}
    total_shifted = sum(shifted.values())

    if total_shifted == 0:
        print("\ncalibrate_attract_weights: all correlations identical — skipping update.")
        return

    new_weights_raw = {d: (v / total_shifted) * 100 for d, v in shifted.items()}

    # Load current weights and apply ±MAX_CHANGE damping
    current_weights = _load_current_weights()
    final_weights   = {}
    print("\nApplying ±15% damping to prevent oscillation:")
    for dim in _DIMS_MAX:
        current = current_weights.get(dim, _DIMS_MAX[dim])
        target  = new_weights_raw[dim]
        # Clamp change to ±MAX_CHANGE of current
        max_delta    = current * _MAX_CHANGE
        clamped_new  = max(current - max_delta, min(current + max_delta, target))
        final_weights[dim] = round(clamped_new, 1)
        print(
            f"  {dim:<30} current={current:.1f}  target={target:.1f}  "
            f"final={clamped_new:.1f}"
        )

    # Renormalise final weights to exactly 100
    total_final = sum(final_weights.values())
    if total_final > 0:
        scale = 100.0 / total_final
        final_weights = {d: round(v * scale, 1) for d, v in final_weights.items()}

    # Write output
    _CONFIG_DIR.mkdir(exist_ok=True)
    with open(_OUTPUT_PATH, "w") as f:
        json.dump(final_weights, f, indent=2, ensure_ascii=False)
    print(f"\ncalibrate_attract_weights: written to {_OUTPUT_PATH}")
    print(f"Total weight: {sum(final_weights.values()):.1f} (should be ~100)")
    print("\nattract_scorer.py will pick up new weights on next process restart.")


if __name__ == "__main__":
    main()
