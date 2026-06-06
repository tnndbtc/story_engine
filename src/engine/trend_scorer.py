"""
trend_scorer.py — Lightweight trend bonus for pipe gating (Phase 1.5).

Uses hotness_at_use from used_items (already in story_engine's own SQLite DB)
to detect whether a story's source articles had real engagement at crawl time.
No cross-database query — no PostgreSQL connection needed.

Three outcomes (no negative penalty — cold topics are slow-burn, not bad):
    0  Cold/Neutral: avg hotness < 380  (news_rss/baidu/weibo baseline range)
   +5  Hot topic: 380 <= avg hotness < 500
        (reddit/bilibili-heavy sources, strong engagement signal)
  +10  Very hot: avg hotness >= 500
        (bilibili/reddit dominant, trending or viral signal)

Thresholds are based on measured hotness_at_use distribution (2026-05-09):
  news_rss avg: 300   baidu avg: 297   hackernews avg: 309
  reddit avg: 407     bilibili avg: 524

final_score = attractiveness_score + trend_bonus
Gate in run_generate.sh uses COALESCE(final_score, attractiveness_score).
"""

from __future__ import annotations

import logging

from db.models import get_connection

logger = logging.getLogger(__name__)

# Thresholds derived from live hotness_at_use distribution
# No negative penalty — cold ≠ bad, deep-dive content is inherently slow-burn.
_HOT_THRESHOLD      = 380.0   # ≥ this → +5 (reddit/bilibili-heavy signal)
_VERY_HOT_THRESHOLD = 500.0   # ≥ this → +10 (bilibili/reddit dominant, viral)


def get_trend_bonus(story_set_id: int) -> int:
    """
    Return the trend bonus for a story set: -10, 0, or +5.

    Queries used_items for main-role source items, computes average
    hotness_at_use, and maps to a bonus value.

    Returns 0 on any error or missing data — fail neutral, never block
    due to trend scorer failure.
    """
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT hotness_at_use FROM used_items "
            "WHERE story_set_id = %s AND role = 'main'",
            (story_set_id,)
        ).fetchall()
        conn.close()

        if not rows:
            logger.debug(
                "trend_scorer: no main-role used_items for story_set_id=%d — returning 0",
                story_set_id,
            )
            return 0

        avg_h = sum(r[0] for r in rows) / len(rows)

        if avg_h >= _VERY_HOT_THRESHOLD:
            logger.debug(
                "trend_scorer: story_set_id=%d avg_hotness=%.1f >= %.0f → very hot → +10",
                story_set_id, avg_h, _VERY_HOT_THRESHOLD,
            )
            return 10

        if avg_h >= _HOT_THRESHOLD:
            logger.debug(
                "trend_scorer: story_set_id=%d avg_hotness=%.1f >= %.0f → hot → +5",
                story_set_id, avg_h, _HOT_THRESHOLD,
            )
            return 5

        logger.debug(
            "trend_scorer: story_set_id=%d avg_hotness=%.1f → cold/neutral → 0",
            story_set_id, avg_h,
        )
        return 0

    except Exception as exc:
        logger.warning(
            "trend_scorer: failed for story_set_id=%d — %s (returning 0)",
            story_set_id, exc,
        )
        return 0
