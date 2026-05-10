#!/usr/bin/env python3
"""
score_existing.py — Retroactively score all unscored hierarchical stories.

Run from story_engine/ directory:
  python3 score_existing.py [--dry-run] [--published-only] [--limit N]

  --dry-run         Print scores without writing to DB
  --published-only  Only score stories with YouTube analytics data
  --limit N         Process at most N stories (for daily NULL retry cron)

After running, compare attractiveness_score against avg_view_pct in
youtube_publish_log to validate the threshold.

This script also computes trend_bonus, final_score, produce_tier, and
story_type for each story.
"""

import argparse
import json
import sys

sys.path.insert(0, 'src')

from db.models import get_connection, save_attractiveness_score, save_trend_score
from engine.attract_scorer import score_story
from engine.trend_scorer import get_trend_bonus

parser = argparse.ArgumentParser(description="Retroactively score unscored hierarchical stories.")
parser.add_argument('--dry-run', action='store_true', help="Print scores without writing to DB")
parser.add_argument('--published-only', action='store_true',
                    help="Only score stories that have YouTube analytics data")
parser.add_argument('--limit', type=int, default=None,
                    help="Max stories to process (for daily NULL retry cron)")
args = parser.parse_args()

conn = get_connection()

if args.published_only:
    rows = conn.execute(
        """SELECT hs.id, hs.story_set_id, hs.lang, hs.deep_story,
                  p.views, p.avg_view_pct
           FROM hierarchical_stories hs
           JOIN youtube_publish_log p ON p.story_id = hs.id
           WHERE hs.attractiveness_score IS NULL
             AND hs.status = 'ready'
             AND p.analytics_pulled_at IS NOT NULL
           ORDER BY hs.id"""
    ).fetchall()
    mode_label = "published+analytics"
else:
    rows = conn.execute(
        """SELECT id, story_set_id, lang, deep_story, NULL, NULL
           FROM hierarchical_stories
           WHERE attractiveness_score IS NULL
             AND status = 'ready'
           ORDER BY id"""
    ).fetchall()
    mode_label = "all unscored"

conn.close()

if args.limit:
    rows = rows[:args.limit]

print(f"Scoring {len(rows)} {mode_label} stories"
      f"{'  [DRY RUN]' if args.dry_run else ''}"
      f"{'  [LIMIT ' + str(args.limit) + ']' if args.limit else ''}...")

for row in rows:
    hs_id, ss_id, lang, deep_story_json, views, avg_view_pct = row
    try:
        ds = json.loads(deep_story_json)
        title = ds.get('title', '')
        body  = ds.get('body', ds.get('hook', ''))
        if not title or not body:
            print(f"  hs={hs_id} ss={ss_id} — skip (empty title/body)")
            continue

        score, breakdown = score_story(title, body, lang)
        bonus = get_trend_bonus(ss_id)

        # Extract story_type from breakdown (not a scored dimension)
        story_type = breakdown.pop('story_type', None)
        if isinstance(story_type, str):
            story_type = story_type.strip() or None

        # Retention suffix (only present in --published-only mode)
        if avg_view_pct is not None:
            retention_str = f"  avp={avg_view_pct}%"
            views_str     = f"  views={views}"
        else:
            retention_str = ""
            views_str     = ""

        if score is None:
            # Scorer failed — do not write to DB, print NULL so it's visible
            print(
                f"  hs={hs_id:4d} ss={ss_id:4d} lang={lang}  "
                f"attract=NULL  trend={bonus:+3d}  final=NULL  SCORER_FAIL"
                f"{views_str}{retention_str}  {title[:45]}"
            )
            continue

        final = score + bonus

        # Derive produce_tier for display
        if final >= 72:
            tier = 'STRONG'
        elif final >= 58:
            tier = 'WEAK  '
        else:
            tier = 'SKIP  '

        type_str = f"  [{story_type}]" if story_type else ""
        print(
            f"  hs={hs_id:4d} ss={ss_id:4d} lang={lang}  "
            f"attract={score:3d}  trend={bonus:+3d}  final={final:3d}  {tier}"
            f"{views_str}{retention_str}{type_str}  {title[:40]}"
        )

        if not args.dry_run:
            save_attractiveness_score(ss_id, score, breakdown, story_type)
            save_trend_score(ss_id, bonus, final)

    except Exception as e:
        print(f"  hs={hs_id} ss={ss_id} — ERROR: {e}")

print("Done.")
