#!/usr/bin/env python3
"""
distribution_watch.py — Daily story_type distribution report.

Monitors story type diversity to prevent pattern collapse (the scoring system
gravitating toward a narrow set of story types over time).

Output:
  1. story_type distribution for the last N days
  2. Warnings if any type is over-represented (>40%) or absent (>5 days)
  3. Retention by story_type (for stories with YouTube analytics)

Run from story_engine/ directory:
  python3 distribution_watch.py [--days 30] [--min-count 3]

This script is observation-only — it does NOT modify scores or thresholds.
Human review is required before acting on any warning.
"""

import argparse
import sys
import time
from collections import defaultdict

sys.path.insert(0, 'src')

from db.models import get_connection

# ── Config ────────────────────────────────────────────────────────────────────
WARN_DOMINANT_RATIO = 0.40    # warn if any type exceeds this share
WARN_ABSENT_DAYS    = 5       # warn if a known type hasn't appeared in N days
KNOWN_TYPES = [
    'health_science', 'tech_ai', 'crime', 'finance', 'geopolitics',
    'sports', 'celebrity', 'accident', 'social_tech', 'political',
    'environment', 'other',
]


def ts_to_days_ago(ts_ms, now_ms: int) -> float:
    return (now_ms - int(ts_ms)) / 86_400_000


def main(days: int, min_count: int) -> None:
    conn = get_connection()
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - days * 86_400_000

    # ── 1. Story type distribution ────────────────────────────────────────────
    rows = conn.execute(
        """SELECT hs.story_type, COUNT(*) as cnt,
                  MAX(ss.batch_ts) as last_seen_ts
           FROM hierarchical_stories hs
           JOIN story_sets ss ON ss.id = hs.story_set_id
           WHERE ss.batch_ts >= %s
             AND hs.status = 'ready'
             AND hs.produce_tier IN ('strong', 'weak')
           GROUP BY hs.story_type
           ORDER BY cnt DESC""",
        (cutoff_ms,)
    ).fetchall()

    total = sum(r['cnt'] for r in rows)
    type_counts = {r['story_type']: r['cnt'] for r in rows}
    type_last_seen = {r['story_type']: r['last_seen_ts'] for r in rows}

    print(f"\n{'='*60}")
    print(f"  Distribution Watch — last {days} days")
    print(f"  {total} produced stories  (strong + weak tiers)")
    print(f"{'='*60}")

    if total == 0:
        print("  No produced stories in window — nothing to report.")
        conn.close()
        return

    print(f"\n{'Story Type':<22} {'Count':>5}  {'Ratio':>6}  {'Last seen':>10}")
    print(f"{'─'*22}  {'─'*5}  {'─'*6}  {'─'*10}")

    warnings = []
    for stype, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
        ratio = cnt / total
        last_ts = type_last_seen.get(stype)
        days_ago = ts_to_days_ago(last_ts, now_ms) if last_ts else float('inf')
        last_str = f"{days_ago:.0f}d ago" if days_ago < 999 else "never"
        flag = "  ⚠ DOMINANT" if ratio > WARN_DOMINANT_RATIO else ""
        label = stype or "(unclassified)"
        print(f"  {label:<20}  {cnt:>5}  {ratio:>5.0%}  {last_str:>10}{flag}")
        if ratio > WARN_DOMINANT_RATIO:
            warnings.append(f"DOMINANT: '{label}' = {ratio:.0%} of produced stories (>{WARN_DOMINANT_RATIO:.0%})")

    # Check for known types absent > WARN_ABSENT_DAYS
    for stype in KNOWN_TYPES:
        if stype not in type_counts:
            days_ago = float('inf')
        else:
            last_ts = type_last_seen.get(stype)
            days_ago = ts_to_days_ago(last_ts, now_ms) if last_ts else float('inf')

        if days_ago > WARN_ABSENT_DAYS:
            warnings.append(
                f"ABSENT: '{stype}' not produced in {days_ago:.0f}+ days"
                if days_ago < 9999 else
                f"ABSENT: '{stype}' never produced"
            )

    # Note: NULL story_type rows appear as "(unclassified)" in the table above.

    # ── 2. Warnings ───────────────────────────────────────────────────────────
    print()
    if warnings:
        print("WARNINGS (observation only — no automatic action taken):")
        for w in warnings:
            print(f"  ⚠  {w}")
    else:
        print("  ✓ No distribution warnings.")

    # ── 3. Retention by story_type ────────────────────────────────────────────
    ret_rows = conn.execute(
        """SELECT hs.story_type,
                  COUNT(*) as cnt,
                  AVG(p.avg_view_pct) as avg_avp,
                  AVG(p.views) as avg_views
           FROM hierarchical_stories hs
           JOIN youtube_publish_log p ON p.story_id = hs.id
           WHERE p.analytics_pulled_at IS NOT NULL
             AND hs.produce_tier IN ('strong', 'weak')
           GROUP BY hs.story_type
           HAVING COUNT(*) >= %s
           ORDER BY avg_avp DESC""",
        (min_count,)
    ).fetchall()

    if ret_rows:
        print(f"\nRetention by story_type  (≥{min_count} videos with analytics):")
        print(f"{'Story Type':<22} {'n':>4}  {'avg_avp':>8}  {'avg_views':>10}")
        print(f"{'─'*22}  {'─'*4}  {'─'*8}  {'─'*10}")
        for r in ret_rows:
            label = r['story_type'] or "(unclassified)"
            avg_avp = r['avg_avp']
            avg_views = r['avg_views']
            cnt = r['cnt']
            print(f"  {label:<20}  {cnt:>4}  {avg_avp or 0:>7.1f}%  {avg_views or 0:>10.0f}")
    else:
        print(f"\n  (No retention data yet with ≥{min_count} videos per type)")

    # ── 4. Tier breakdown ─────────────────────────────────────────────────────
    tier_rows = conn.execute(
        """SELECT produce_tier, COUNT(*) as cnt
           FROM hierarchical_stories hs
           JOIN story_sets ss ON ss.id = hs.story_set_id
           WHERE ss.batch_ts >= %s
             AND hs.status = 'ready'
             AND hs.produce_tier IS NOT NULL
           GROUP BY produce_tier""",
        (cutoff_ms,)
    ).fetchall()

    if tier_rows:
        tier_map = {r['produce_tier']: r['cnt'] for r in tier_rows}
        t_strong = tier_map.get('strong', 0)
        t_weak   = tier_map.get('weak',   0)
        t_skip   = tier_map.get('skip',   0)
        t_prod   = t_strong + t_weak
        print(f"\nTier breakdown (last {days} days):")
        print(f"  STRONG  (≥72): {t_strong:>4}  ({t_strong/max(t_prod,1):.0%} of produced)")
        print(f"  WEAK  (58–71): {t_weak:>4}  ({t_weak/max(t_prod,1):.0%} of produced)")
        print(f"  SKIP    (<58): {t_skip:>4}")
        if t_prod > 0 and t_weak / t_prod > 0.30:
            print("  ⚠  WEAK ratio >30% — consider raising the weak threshold")

    print(f"\n{'='*60}\n")
    conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Story type distribution report.")
    parser.add_argument('--days', type=int, default=30,
                        help="Lookback window in days (default: 30)")
    parser.add_argument('--min-count', type=int, default=3,
                        help="Min videos per type for retention table (default: 3)")
    args = parser.parse_args()
    main(args.days, args.min_count)
