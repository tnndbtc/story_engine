"""
Read-only access to the crawler's PostgreSQL database.

story_engine NEVER writes to the crawler DB. This module provides
read-only queries for selecting candidate items for story generation.

Connection is configured via the CRAWLER_DB_URL environment variable
(set by setup.sh after running option 7 "Configure .env").

Default URL: postgres://dbuser:dbpass@localhost:5432/crawler_db
"""

import json
import os
from pathlib import Path

import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# Load .env from story_engine root if env vars are not already set.
# This is a safety net for direct invocations (e.g. python engine/run.py).
# setup.sh sources .env before starting the service, so vars are normally
# already in the environment.
# ---------------------------------------------------------------------------

def _load_env_file() -> None:
    if "CRAWLER_DB_URL" not in os.environ:
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"
        if env_path.is_file():
            with open(env_path) as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if key and key not in os.environ:
                        os.environ[key] = val


_load_env_file()

# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------

_DEFAULT_CRAWLER_DB_URL = "postgres://dbuser:dbpass@localhost:5432/crawler_db"

CRAWLER_DB_URL: str = os.environ.get("CRAWLER_DB_URL", _DEFAULT_CRAWLER_DB_URL)

# Crawler root directory — used by stage1_normalize for auto_keywords.json.
# Defaults to the sibling crawler/ directory.
CRAWLER_ROOT: str = os.environ.get(
    "CRAWLER_ROOT",
    str(Path(__file__).resolve().parent.parent.parent.parent / "crawler"),
)

# Legacy alias kept so existing imports of CRAWLER_DB_PATH still work.
# Returns the connection URL so callers that only use it for display are OK.
CRAWLER_DB_PATH: str = CRAWLER_DB_URL


def get_crawler_connection() -> psycopg2.extensions.connection:
    """
    Get a read-only connection to the crawler PostgreSQL database.

    Uses the CRAWLER_DB_URL environment variable (set via setup.sh option 7
    or sourced from .env).
    """
    conn = psycopg2.connect(CRAWLER_DB_URL)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def test_connection() -> bool:
    """Return True if the crawler database is reachable, False otherwise."""
    try:
        conn = get_crawler_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


def _exec(conn: psycopg2.extensions.connection, sql: str, params=()) -> list[dict]:
    """Execute *sql* and return rows as a list of plain dicts."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------------


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
    if allowed_categories:
        merged: list[dict] = []
        seen_urls: set[str] = set()
        for cat in sorted(allowed_categories):
            for row in _get_top_items_single_pass(
                limit=limit,
                hours=hours,
                buckets=buckets,
                lang_group=lang_group,
                exclude_platforms=exclude_platforms,
                platforms=platforms,
                per_platform_k=per_platform_k,
                category_filter=cat,
            ):
                url = row.get("url") or ""
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(row)
        merged.sort(key=lambda r: (r.get("hotness") or 0.0), reverse=True)
        return merged

    # Unfocused path: single global fetch (legacy behavior).
    return _get_top_items_single_pass(
        limit=limit,
        hours=hours,
        buckets=buckets,
        lang_group=lang_group,
        exclude_platforms=exclude_platforms,
        platforms=platforms,
        per_platform_k=per_platform_k,
        category_filter=None,
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
        f"ti.collected_at >= NOW() - INTERVAL '{hours} hours'",
        "ti.classification_state NOT IN ('pending', 'failed')",
    ]
    params: list = []

    if category_filter is not None:
        conditions.append("ti.story_category = %s")
        params.append(category_filter)

    if buckets:
        placeholders = ",".join(["%s"] * len(buckets))
        conditions.append(f"ti.bucket IN ({placeholders})")
        params.extend(buckets)

    if lang_group:
        conditions.append("ti.lang_group = %s")
        params.append(lang_group)

    if exclude_platforms:
        placeholders = ",".join(["%s"] * len(exclude_platforms))
        conditions.append(f"ts.platform NOT IN ({placeholders})")
        params.extend(exclude_platforms)

    if platforms:
        placeholders = ",".join(["%s"] * len(platforms))
        conditions.append(f"ts.platform IN ({placeholders})")
        params.extend(platforms)

    where = " AND ".join(conditions)
    params.extend([per_platform_k, limit])

    rows = _exec(
        conn,
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
        WHERE rn <= %s
        ORDER BY hotness DESC
        LIMIT %s
        """,
        params,
    )

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
    candidates = get_top_items(limit=limit * 20, hours=hours)

    selected = []
    platform_counts: dict[str, int] = {}

    for item in candidates:
        platform = item["platform"]
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
    """
    conn = get_crawler_connection()

    rows = _exec(
        conn,
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
            ts.key AS surface_key,
            ts.selection_weight,
            r.key AS region_key,
            r.name AS region_name,
            COALESCE(ti.primary_region, r.key) AS effective_region
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        JOIN crawler_admin_region r ON ti.region_id = r.id
        WHERE ti.hotness IS NOT NULL
          AND ti.collected_at >= NOW() - INTERVAL '{hours} hours'
          AND ts.platform IN ('hackernews', 'lobsters', 'devto',
                              'paperswithcode', 'github', 'v2ex',
                              'producthunt', 'stackoverflow')
        ORDER BY ti.hotness DESC
        LIMIT %s
        """,
        (limit * 3,),
    )

    conn.close()
    return [_item_to_dict(row) for row in rows][:limit]


def get_regional_items(
    exclude_region: str = "us",
    limit: int = 10,
    hours: int = 24,
    exclude_platforms: list[str] | None = None,
) -> list[dict]:
    """
    Get top items from non-US regions for "stories US media ignores" (Format 3).

    Uses content-based primary_region if available, falls back to source region.
    """
    conn = get_crawler_connection()

    platform_clause = ""
    params: list = []
    if exclude_platforms:
        placeholders = ",".join(["%s"] * len(exclude_platforms))
        platform_clause = f"AND ts.platform NOT IN ({placeholders})"
        params.extend(exclude_platforms)
    params.append(exclude_region)
    params.append(limit)

    rows = _exec(
        conn,
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
            ts.key AS surface_key,
            ts.selection_weight,
            r.key AS region_key,
            r.name AS region_name,
            COALESCE(ti.primary_region, r.key) AS effective_region
        FROM crawler_admin_trenditem ti
        JOIN crawler_admin_trendsurface ts ON ti.surface_id = ts.id
        JOIN crawler_admin_region r ON ti.region_id = r.id
        WHERE ti.hotness IS NOT NULL
          AND ti.collected_at >= NOW() - INTERVAL '{hours} hours'
          {platform_clause}
          AND COALESCE(ti.primary_region, r.key) != %s
        ORDER BY ti.hotness DESC
        LIMIT %s
        """,
        params,
    )

    conn.close()
    return [_item_to_dict(row) for row in rows]


def get_embeddings(item_ids: list[int]) -> dict[int, list[float]]:
    """
    Fetch embeddings for a list of crawler item IDs.

    Returns {item_id: embedding_vector} for items that have a complete embedding.
    Items without embeddings are absent from the result.

    The embedding is stored in crawler_admin_itemderivation.content_body as a
    JSON float array (BAAI/bge-small-en-v1.5, 384 dimensions).
    """
    if not item_ids:
        return {}
    conn = get_crawler_connection()
    placeholders = ",".join(["%s"] * len(item_ids))
    rows = _exec(
        conn,
        f"""
        SELECT item_id, content_body
        FROM crawler_admin_itemderivation
        WHERE item_id IN ({placeholders})
          AND derivation_type = 'embedding'
          AND status = 'complete'
          AND content_body IS NOT NULL
        """,
        item_ids,
    )
    conn.close()
    result: dict[int, list[float]] = {}
    for row in rows:
        try:
            body = row["content_body"]
            # psycopg2 may auto-parse jsonb columns; handle both str and list
            vec = body if isinstance(body, list) else json.loads(body)
            if isinstance(vec, list) and vec:
                result[row["item_id"]] = vec
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
    return result


def get_item_count(hours: int = 24) -> int:
    """Get total items collected in the last N hours."""
    conn = get_crawler_connection()
    rows = _exec(
        conn,
        f"SELECT COUNT(*) AS cnt FROM crawler_admin_trenditem "
        f"WHERE collected_at >= NOW() - INTERVAL '{hours} hours'",
    )
    conn.close()
    return rows[0]["cnt"] if rows else 0


def get_known_surface_keys() -> set[str]:
    """
    Return the set of TrendSurface.key values for all enabled surfaces.

    Used by the selector to validate surface_weight_overrides config keys
    at startup and warn about any that don't match a real surface.
    """
    conn = get_crawler_connection()
    rows = _exec(
        conn,
        "SELECT DISTINCT key FROM crawler_admin_trendsurface WHERE enabled = true",
    )
    conn.close()
    return {row["key"] for row in rows}


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
        f"ti.collected_at >= NOW() - INTERVAL '{hours} hours'",
        "ti.classification_state NOT IN ('pending', 'failed')",
    ]
    params: list = []

    if category:
        conditions.append("ti.story_category = %s")
        params.append(category)

    if exclude_urls:
        placeholders = ",".join(["%s"] * len(exclude_urls))
        conditions.append(f"ti.url NOT IN ({placeholders})")
        params.extend(exclude_urls)

    where = " AND ".join(conditions)
    params.append(limit)

    rows = _exec(
        conn,
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
        LIMIT %s
        """,
        params,
    )

    conn.close()
    return [_item_to_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Row conversion
# ---------------------------------------------------------------------------


def _parse_json_or_default(val, default):
    """Parse a JSON value that may already be a Python object (psycopg2 jsonb)."""
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val  # already parsed by psycopg2 from a jsonb column
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default


def _item_to_dict(row: dict) -> dict:
    """Normalise a crawler item row (already a plain dict from _exec)."""
    d = dict(row)

    # Parse JSON fields — handle both text (SQLite legacy) and jsonb (PostgreSQL)
    d["engagement_signals"] = _parse_json_or_default(d.get("engagement_signals"), {})
    d["raw_payload"] = _parse_json_or_default(d.get("raw_payload"), {})
    d["content_regions"] = _parse_json_or_default(d.get("content_regions"), [])
    d["topic_tags"] = _parse_json_or_default(d.get("topic_tags"), [])

    # selection_weight default
    if d.get("selection_weight") is None:
        d["selection_weight"] = 1.0

    # story_category: string or None — no JSON parsing needed
    if "story_category" not in d:
        d["story_category"] = None

    # collected_at: psycopg2 returns datetime objects for timestamptz columns.
    # Downstream code that calls str() on it or uses isoformat() will be fine,
    # but some callers expect a plain string. Normalise to ISO string.
    if d.get("collected_at") is not None and not isinstance(d["collected_at"], str):
        try:
            d["collected_at"] = d["collected_at"].isoformat()
        except AttributeError:
            d["collected_at"] = str(d["collected_at"])

    return d
