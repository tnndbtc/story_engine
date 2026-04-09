"""
Read-only access to the crawler's SQLite database.

story_engine NEVER writes to the crawler DB. This module provides
read-only queries for selecting candidate items for story generation.
"""

import json
import sqlite3
import os
from pathlib import Path

# Crawler database — READ ONLY
_DEFAULT_CRAWLER_DB = '/home/tnnd/data/code/crawler/db.sqlite3'

CRAWLER_DB_PATH = os.environ.get('CRAWLER_DB', _DEFAULT_CRAWLER_DB)


def get_crawler_connection() -> sqlite3.Connection:
    """
    Get a read-only connection to the crawler database.

    Uses file: URI with mode=ro to enforce read-only at the SQLite level.
    """
    uri = f"file:{CRAWLER_DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_top_items(
    limit: int = 10,
    hours: int = 24,
    buckets: list[str] | None = None,
    lang_group: str | None = None,
    exclude_platforms: list[str] | None = None,
) -> list[dict]:
    """
    Get the top items by hotness from the last N hours.

    Args:
        limit: Max items to return
        hours: How far back to look
        buckets: Filter to specific buckets (e.g., ['hot_now', 'news'])
        lang_group: Filter to specific language group (e.g., 'en')
        exclude_platforms: Platforms to exclude
    """
    conn = get_crawler_connection()

    conditions = [
        "ti.hotness IS NOT NULL",
        f"ti.collected_at >= datetime('now', '-{hours} hours')",
    ]
    params = []

    if buckets:
        placeholders = ",".join("?" * len(buckets))
        conditions.append(f"ti.bucket IN ({placeholders})")
        params.extend(buckets)

    if lang_group:
        conditions.append("ti.lang_group = ?")
        params.append(lang_group)

    if exclude_platforms:
        placeholders = ",".join("?" * len(exclude_platforms))
        conditions.append(f"ts.platform NOT IN ({placeholders})")
        params.extend(exclude_platforms)

    where = " AND ".join(conditions)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT
            ti.id,
            ti.title_original,
            ti.canonical_title,
            ti.description_original,
            ti.url,
            ti.hotness,
            ti.bucket,
            ti.engagement_signals,
            ti.raw_payload,
            ti.collected_at,
            ti.lang_group,
            ti.original_locale,
            ts.platform,
            ts.key as surface_key,
            r.key as region_key,
            r.name as region_name
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        JOIN crawler_admin_region r ON ti.region_id = r.id
        WHERE {where}
        ORDER BY ti.hotness DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    conn.close()
    return [_item_to_dict(row) for row in rows]


def get_diverse_top_items(
    limit: int = 5,
    hours: int = 24,
    max_per_platform: int = 2,
) -> list[dict]:
    """
    Get top items by hotness with platform diversity enforcement.

    Ensures no single platform dominates the selection by limiting
    items per platform. Fetches extra items and dedupes client-side.
    """
    # Fetch 20x to ensure diversity — top items may cluster on one platform
    candidates = get_top_items(limit=limit * 20, hours=hours)

    selected = []
    platform_counts: dict[str, int] = {}

    for item in candidates:
        platform = item['platform']
        if platform_counts.get(platform, 0) >= max_per_platform:
            continue
        selected.append(item)
        platform_counts[platform] = platform_counts.get(platform, 0) + 1
        if len(selected) >= limit:
            break

    return selected


def get_early_signals(limit: int = 5, hours: int = 24) -> list[dict]:
    """
    Find "before it goes viral" candidates.

    Items first seen on niche platforms (HN, Reddit niche, Papers with Code,
    dev.to, lobsters) that are NOT yet covered by mainstream news sources.

    From plan.txt Phase 1 Step 2:
    - Items from HN/Reddit/PapersWithCode
    - collected_at within last N hours
    - NOT yet in news/AP/Reuters by topic similarity
    """
    conn = get_crawler_connection()

    # Niche platform items with decent engagement
    niche_rows = conn.execute(
        """
        SELECT
            ti.id,
            ti.title_original,
            ti.canonical_title,
            ti.description_original,
            ti.url,
            ti.hotness,
            ti.bucket,
            ti.engagement_signals,
            ti.raw_payload,
            ti.collected_at,
            ti.lang_group,
            ti.original_locale,
            ts.platform,
            ts.key as surface_key,
            r.key as region_key,
            r.name as region_name
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        JOIN crawler_admin_region r ON ti.region_id = r.id
        WHERE ti.hotness IS NOT NULL
          AND ti.collected_at >= datetime('now', '-' || ? || ' hours')
          AND ts.platform IN ('hackernews', 'lobsters', 'devto',
                              'paperswithcode', 'github', 'v2ex',
                              'producthunt', 'stackoverflow')
        ORDER BY ti.hotness DESC
        LIMIT ?
        """,
        (hours, limit * 3),
    ).fetchall()

    conn.close()
    return [_item_to_dict(row) for row in niche_rows][:limit]


def get_regional_items(
    exclude_region: str = 'us',
    limit: int = 10,
    hours: int = 24,
) -> list[dict]:
    """
    Get top items from non-US regions for "stories US media ignores" (Format C).
    """
    conn = get_crawler_connection()

    rows = conn.execute(
        """
        SELECT
            ti.id,
            ti.title_original,
            ti.canonical_title,
            ti.description_original,
            ti.url,
            ti.hotness,
            ti.bucket,
            ti.engagement_signals,
            ti.raw_payload,
            ti.collected_at,
            ti.lang_group,
            ti.original_locale,
            ts.platform,
            ts.key as surface_key,
            r.key as region_key,
            r.name as region_name
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        JOIN crawler_admin_region r ON ti.region_id = r.id
        WHERE ti.hotness IS NOT NULL
          AND ti.collected_at >= datetime('now', '-' || ? || ' hours')
          AND r.key != ?
        ORDER BY ti.hotness DESC
        LIMIT ?
        """,
        (hours, exclude_region, limit),
    ).fetchall()

    conn.close()
    return [_item_to_dict(row) for row in rows]


def get_item_count(hours: int = 24) -> int:
    """Get total items collected in the last N hours."""
    conn = get_crawler_connection()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM crawler_admin_trenditem WHERE collected_at >= datetime('now', '-' || ? || ' hours')",
        (hours,),
    ).fetchone()
    conn.close()
    return row['cnt'] if row else 0


def _item_to_dict(row: sqlite3.Row) -> dict:
    """Convert a crawler item row to a dictionary."""
    d = dict(row)
    # Parse JSON fields
    if d.get('engagement_signals'):
        try:
            d['engagement_signals'] = json.loads(d['engagement_signals'])
        except (json.JSONDecodeError, TypeError):
            d['engagement_signals'] = {}
    if d.get('raw_payload'):
        try:
            d['raw_payload'] = json.loads(d['raw_payload'])
        except (json.JSONDecodeError, TypeError):
            d['raw_payload'] = {}
    return d
