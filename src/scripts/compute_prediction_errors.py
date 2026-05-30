#!/usr/bin/env python3
"""
src/scripts/compute_prediction_errors.py — Change 2: Prediction Error Feedback

Computes per-story-type prediction error (actual performance − predicted score)
and writes the result to config/prediction_errors.json.

Run weekly via cron:
    cd /home/tnnd/data/code/story_engine
    python src/scripts/compute_prediction_errors.py

attract_scorer.py loads prediction_errors.json at startup and applies a
±1.5-point bias correction per story_type to nudge attract_score toward
what the channel actually rewards.

Output format (config/prediction_errors.json):
    {
      "tech_ai":     +0.12,   # system under-predicts AI stories by 12 perf pts
      "celebrity":   -0.08,   # system over-predicts celebrity stories
      ...
    }

    Values are already clamped to [−0.15, +0.15].
    In attract_scorer.py: correction_points = value × 10 → max ±1.5 points.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# src/ is parents[1] from src/scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.models import get_story_type_prediction_error

_CONFIG_DIR  = Path(__file__).resolve().parents[2] / "config"
_OUTPUT_PATH = _CONFIG_DIR / "prediction_errors.json"


def main():
    print("compute_prediction_errors: loading analytics from DB ...")
    errors = get_story_type_prediction_error()

    if not errors:
        print("compute_prediction_errors: no data found — is youtube_publish_log populated?")
        print("  Need: avg_view_pct IS NOT NULL AND analytics_pulled_at > 0 AND views >= 100")
        print("  Run fetch_analytics.py first.")
        return

    print(f"compute_prediction_errors: {len(errors)} story type(s) with sufficient data:")
    for st, corr in sorted(errors.items(), key=lambda x: x[1], reverse=True):
        direction = "↑ UNDER-predicted" if corr > 0 else "↓ OVER-predicted"
        print(f"  {st:<20} correction={corr:+.4f}  {direction}")

    # Write to config
    _CONFIG_DIR.mkdir(exist_ok=True)
    with open(_OUTPUT_PATH, "w") as f:
        json.dump(errors, f, indent=2, ensure_ascii=False)
    print(f"\ncompute_prediction_errors: written to {_OUTPUT_PATH}")

    # Interpretation guide
    print("\nInterpretation:")
    print("  correction × 10 = attract_score adjustment (points)")
    print("  e.g. +0.12 → +1.2 points for under-predicted types")
    print("  e.g. -0.08 → -0.8 points for over-predicted types")
    print("  Max ±0.15 → ±1.5 points (gate band is 58–72 = 14 pts)")


if __name__ == "__main__":
    main()
