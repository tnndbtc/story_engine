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
    platforms: list[str] | None = None,
    per_platform_k: int = 10,
    allowed_categories: list[str] | set[str] | None = None,
) -> list[dict]:
    """
    Get the top items by hotness from the last N hours.

    Uses a per-platform Top-K window query to guarantee that long-tail
    platforms (hackernews, reddit, bbc, aljazeera …) survive into the
    candidate pool before any cap filtering runs.  Without this, a global
    ORDER BY hotness DESC LIMIT N hands the entire pool to bilibili/youtube
    because their hotness scores are structurally higher.

    Category-aware fetch (Stage 1 Mode B, design.md): when
    ``allowed_categories`` is set, the fetch is executed SEPARATELY for
    each allowed category (top-N per category) and the results are merged
    and deduped. This prevents high-hotness categories (entertainment,
    politics) from saturating the global top-N and drowning out
    scarce-but-on-topic categories (business, ai, science, world) for
    focused profiles. Example: run4_business only accepts "business"; in
    a 48h window the crawler has 1,400+ business items but <30 of them
    sit above the global top-500 hotness cutoff. Without per-category
    fetch, focused profiles see a nearly-empty pool after the Step 4b
    allowlist filter.

    Architecture:
      - Unfocused (allowed_categories is None):
          ① per-platform Top-K  →  ② ORDER BY hotness DESC  →  ③ LIMIT
      - Focused   (allowed_categories is a set):
          For each cat:
            ① per-platform Top-K within cat → ② ORDER BY hotness → ③ LIMIT
          Merge + dedupe by URL + re-sort by hotness desc.

    Args:
        limit: Max items to return. In focused mode the limit is applied
            PER category, so total items returned may be up to
            limit × len(allowed_categories) before dedup.
        hours: How far back to look.
        buckets: Filter to specific buckets (e.g., ['hot_now', 'news']).
        lang_group: Filter to specific language group (e.g., 'en').
        exclude_platforms: Platforms to exclude.
        platforms: Only include these platforms (e.g., ['hackernews', 'devto']).
        per_platform_k: How many top items to keep per platform before the
            final global ranking.  Formula from bugs.txt:
            K = max(5, ceil(total_needed × 3 / active_platform_count)).
            Default 10 is a safe conservative value; callers that know
            total_needed can pass a computed K explicitly.
        allowed_categories: Optional set/list of story_category values.
            When provided, one fetch is issued PER category and results
            are merged + deduped. When None, a single global fetch is
            issued (legacy behavior).
    """
    # Focused-profile path: one fetch per allowed category, then merge.
    # This guarantees each on-topic category gets its own top-N slice
    # instead of competing for slots in a globally-hotness-sorted pool.
    if allowed_categories:
        merged: list[dict] = []
        seen_urls: set[str] = set()
        for cat in sorted(allowed_categories):
            for row in _get_top_items_single_pass(
                limit             = limit,
                hours             = hours,
                buckets           = buckets,
                lang_group        = lang_group,
                exclude_platforms = exclude_platforms,
                platforms         = platforms,
                per_platform_k    = per_platform_k,
                category_filter   = cat,
            ):
                url = row.get('url') or ''
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(row)
        # Re-sort by hotness desc for determinism + downstream ranking.
        merged.sort(key=lambda r: (r.get('hotness') or 0.0), reverse=True)
        return merged

    # Unfocused path: single global fetch (legacy behavior).
    return _get_top_items_single_pass(
        limit             = limit,
        hours             = hours,
        buckets           = buckets,
        lang_group        = lang_group,
        exclude_platforms = exclude_platforms,
        platforms         = platforms,
        per_platform_k    = per_platform_k,
        category_filter   = None,
    )


def _get_top_items_single_pass(
    limit: int,
    hours: int,
    buckets: list[str] | None,
    lang_group: str | None,
    exclude_platforms: list[str] | None,
    platforms: list[str] | None,
    per_platform_k: int,
    category_filter: str | None,
) -> list[dict]:
    """
    Internal: one per-platform Top-K fetch, optionally restricted to a
    single story_category. Used by both the legacy unfocused path and
    the new per-category focused path in get_top_items().
    """
    conn = get_crawler_connection()

    conditions = [
        "ti.hotness IS NOT NULL",
        f"ti.collected_at >= datetime('now', '-{hours} hours')",
        "ti.classification_state NOT IN ('pending', 'failed')",
    ]
    params: list = []

    if category_filter is not None:
        conditions.append("ti.story_category = ?")
        params.append(category_filter)

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

    if platforms:
        placeholders = ",".join("?" * len(platforms))
        conditions.append(f"ts.platform IN ({placeholders})")
        params.extend(platforms)

    where = " AND ".join(conditions)
    # per_platform_k bound first, then the final LIMIT
    params.extend([per_platform_k, limit])

    rows = conn.execute(
        f"""
        WITH ranked AS (
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
                ti.content_regions,
                ti.primary_region,
                ti.topic_tags,
                ti.story_category,
                ts.platform,
                ts.key AS surface_key,
                ts.selection_weight,
                r.key AS region_key,
                r.name AS region_name,
                COALESCE(ti.primary_region, r.key) AS effective_region,
                ROW_NUMBER() OVER (
                    PARTITION BY ts.platform
                    ORDER BY ti.hotness DESC
                ) AS rn
            FROM crawler_admin_trenditem ti
            JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
            JOIN crawler_admin_region r ON ti.region_id = r.id
            WHERE {where}
        )
        SELECT
            id, title_original, canonical_title, description_original,
            url, hotness, bucket, engagement_signals, raw_payload,
            collected_at, lang_group, original_locale, content_regions,
            primary_region, topic_tags, story_category, platform, surface_key,
            selection_weight, region_key, region_name, effective_region
        FROM ranked
        WHERE rn <= ?
        ORDER BY hotness DESC
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
            ti.content_regions,
            ti.primary_region,
            ti.topic_tags,
            ts.platform,
            ts.key as surface_key,
            ts.selection_weight,
            r.key as region_key,
            r.name as region_name,
            COALESCE(ti.primary_region, r.key) as effective_region
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
    exclude_platforms: list[str] | None = None,
) -> list[dict]:
    """
    Get top items from non-US regions for "stories US media ignores" (Format 3).

    Uses content-based primary_region if available, falls back to source region.

    Args:
        exclude_platforms: Optional list of platform names to exclude (e.g.
                           ["bilibili", "reddit"]).  Prevents high-volume
                           platforms from dominating via raw hotness.
    """
    conn = get_crawler_connection()

    platform_clause = ''
    params: list = [hours, exclude_region]
    if exclude_platforms:
        placeholders = ','.join('?' * len(exclude_platforms))
        platform_clause = f'AND ts.platform NOT IN ({placeholders})'
        params.extend(exclude_platforms)
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
            ti.content_regions,
            ti.primary_region,
            ti.topic_tags,
            ts.platform,
            ts.key as surface_key,
            ts.selection_weight,
            r.key as region_key,
            r.name as region_name,
            COALESCE(ti.primary_region, r.key) as effective_region
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        JOIN crawler_admin_region r ON ti.region_id = r.id
        WHERE ti.hotness IS NOT NULL
          AND ti.collected_at >= datetime('now', '-' || ? || ' hours')
          AND COALESCE(ti.primary_region, r.key) != ?
          {platform_clause}
        ORDER BY ti.hotness DESC
        LIMIT ?
        """,
        params,
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


def get_known_surface_keys() -> set[str]:
    """
    Return the set of TrendSurface.key values for all enabled surfaces.

    Used by the selector to validate surface_weight_overrides config keys
    at startup and warn about any that don't match a real surface.
    """
    conn = get_crawler_connection()
    rows = conn.execute(
        "SELECT DISTINCT key FROM crawler_admin_trendsurface WHERE enabled = 1"
    ).fetchall()
    conn.close()
    return {row['key'] for row in rows}


def get_background_items(
    category: str | None,
    exclude_urls: list[str],
    limit: int,
    hours: int = 168,
) -> list[dict]:
    """
    Fetch background context articles for story enrichment.

    Does NOT filter by used_items — these articles can be freely reused
    as background context regardless of prior use.
    Does NOT apply per-platform Top-K windowing — category-scoped queries
    benefit from simple hotness ordering.

    Args:
        category:     story_category to match (e.g. 'technology').
                      If None, no category filter is applied.
        exclude_urls: URLs of already-selected main source articles for
                      this batch. Excluded so the same article does not
                      appear as both main and background in one story.
        limit:        Maximum number of items to return.
        hours:        Lookback window. Default 168 (7 days) for rich
                      historical context pool.

    Returns:
        List of item dicts in the same format as get_top_items().
    """
    conn = get_crawler_connection()

    conditions = [
        "ti.hotness IS NOT NULL",
        f"ti.collected_at >= datetime('now', '-{hours} hours')",
        "ti.classification_state NOT IN ('pending', 'failed')",
    ]
    params: list = []

    if category:
        conditions.append("ti.story_category = ?")
        params.append(category)

    if exclude_urls:
        placeholders = ",".join("?" * len(exclude_urls))
        conditions.append(f"ti.url NOT IN ({placeholders})")
        params.extend(exclude_urls)

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
            ti.content_regions,
            ti.primary_region,
            ti.topic_tags,
            ti.story_category,
            ts.platform,
            ts.key AS surface_key,
            ts.selection_weight,
            r.key AS region_key,
            r.name AS region_name,
            COALESCE(ti.primary_region, r.key) AS effective_region
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
    if d.get('content_regions'):
        try:
            d['content_regions'] = json.loads(d['content_regions'])
        except (json.JSONDecodeError, TypeError):
            d['content_regions'] = []
    else:
        d['content_regions'] = []
    # Phase 4: parse topic_tags JSON array ([] if absent or malformed)
    if d.get('topic_tags'):
        try:
            d['topic_tags'] = json.loads(d['topic_tags'])
        except (json.JSONDecodeError, TypeError):
            d['topic_tags'] = []
    else:
        d['topic_tags'] = []
    # Phase 4: selection_weight is a float column — default to 1.0 if missing
    if d.get('selection_weight') is None:
        d['selection_weight'] = 1.0
    # story_category: string or None — no JSON parsing needed
    if 'story_category' not in d:
        d['story_category'] = None
    return d
