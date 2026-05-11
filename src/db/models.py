"""
story_engine storage — own SQLite database for generated stories.

The crawler DB is READ-ONLY to story_engine. This module manages
story_engine's own db.sqlite3 for persisting generated content.

All timestamps are stored as UNIX epoch integers (seconds since 1970-01-01).
Converted to ISO text strings at read time for API responses.
"""

import json
import sqlite3
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# story_engine's own database — NOT the crawler DB
DB_PATH = os.environ.get(
    'STORY_ENGINE_DB',
    str(Path(__file__).resolve().parent.parent.parent / 'db.sqlite3')
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    format          TEXT NOT NULL,
    channel         INTEGER NOT NULL DEFAULT 1,
    lang            TEXT NOT NULL DEFAULT 'en',
    status          TEXT NOT NULL DEFAULT 'generating',
    generated_at    INTEGER,            -- UNIX epoch seconds
    hook            TEXT,
    bullets         TEXT,               -- JSON array of strings
    twist           TEXT,
    sources         TEXT,               -- JSON array of {url, platform, hotness, title, role?}
                                        -- role is 'fact'|'context'|'reaction' for cluster members
    topic_clusters  TEXT,               -- NULL for single-item formats / singleton clusters
                                        -- JSON: [{event_id, representative,
                                        --   fact_sources, context_sources, reaction_sources}]
    comments_used   TEXT,               -- JSON array of {text, likes, platform}
    error_message   TEXT,
    created_at      INTEGER             -- UNIX epoch seconds
);

CREATE INDEX IF NOT EXISTS idx_stories_format ON stories(format);
CREATE INDEX IF NOT EXISTS idx_stories_lang ON stories(lang);
CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status);
CREATE INDEX IF NOT EXISTS idx_stories_generated_at ON stories(generated_at);
CREATE INDEX IF NOT EXISTS idx_stories_channel ON stories(channel);

CREATE TABLE IF NOT EXISTS story_sets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_ts    INTEGER NOT NULL UNIQUE,    -- UNIX epoch seconds
    lang        TEXT NOT NULL DEFAULT 'zh',
    channel     INTEGER NOT NULL DEFAULT 2,
    status      TEXT NOT NULL DEFAULT 'running',
    created_at  INTEGER                     -- UNIX epoch seconds
);

CREATE TABLE IF NOT EXISTS used_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    crawler_item_id INTEGER NOT NULL,
    crawler_url     TEXT NOT NULL,
    hotness_at_use  REAL NOT NULL,
    story_set_id    INTEGER NOT NULL REFERENCES story_sets(id),
    story_id        INTEGER REFERENCES stories(id),
    format          TEXT NOT NULL,
    used_at         INTEGER,                -- UNIX epoch seconds
    platform        TEXT,
    role            TEXT NOT NULL DEFAULT 'main'
);

CREATE INDEX IF NOT EXISTS idx_used_items_crawler_id ON used_items(crawler_item_id);
CREATE INDEX IF NOT EXISTS idx_used_items_url ON used_items(crawler_url);
CREATE INDEX IF NOT EXISTS idx_used_items_story_set ON used_items(story_set_id);

CREATE TABLE IF NOT EXISTS event_memory (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL,              -- sha256[:16] of story_title
    story_title      TEXT NOT NULL,              -- zh story title (debug)
    source_titles    TEXT NOT NULL DEFAULT '[]', -- JSON array of English source titles
    source_urls      TEXT NOT NULL DEFAULT '[]', -- JSON array of source URLs
    embedding_center TEXT,                       -- JSON float array (384-dim BGE); NULL for legacy rows
    entities         TEXT,                       -- JSON array of {text, type}; NULL for legacy rows
    story_id         INTEGER REFERENCES stories(id),
    story_set_id     INTEGER REFERENCES story_sets(id),
    created_at       INTEGER NOT NULL,           -- UNIX epoch seconds
    expires_at       INTEGER NOT NULL            -- created_at + window_days * 86400
);

CREATE INDEX IF NOT EXISTS idx_event_memory_expires  ON event_memory(expires_at);
CREATE INDEX IF NOT EXISTS idx_event_memory_event_id ON event_memory(event_id);

CREATE TABLE IF NOT EXISTS purity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sim         REAL    NOT NULL,       -- cosine similarity score (0.75–0.85 tier)
    purity      REAL    NOT NULL,       -- computed purity score
    allowed     INTEGER NOT NULL,       -- 1 = merged, 0 = blocked
    reason      TEXT    NOT NULL,       -- e.g. keyword_overlap, country_conflict, purity_gate
    created_at  INTEGER NOT NULL        -- UNIX epoch seconds
);

CREATE INDEX IF NOT EXISTS idx_purity_log_created ON purity_log(created_at);
CREATE INDEX IF NOT EXISTS idx_purity_log_allowed ON purity_log(allowed);

CREATE TABLE IF NOT EXISTS hierarchical_stories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    story_set_id        INTEGER,
    batch_ts            INTEGER,
    lang                TEXT,
    channel             INTEGER,
    status              TEXT NOT NULL DEFAULT 'ready',  -- ready / failed / partial
    deep_story          TEXT,                           -- JSON dict
    supporting_stories  TEXT,                           -- JSON array
    generated_at        INTEGER,
    created_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_hierarchical_stories_batch_ts ON hierarchical_stories(batch_ts);
CREATE INDEX IF NOT EXISTS idx_hierarchical_stories_status   ON hierarchical_stories(status);
CREATE INDEX IF NOT EXISTS idx_hierarchical_stories_set_id   ON hierarchical_stories(story_set_id);

CREATE TABLE IF NOT EXISTS youtube_subscribers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       TEXT    NOT NULL UNIQUE,   -- YouTube channel ID (UCxxx)
    display_name     TEXT    NOT NULL,           -- subscriber's display name
    description      TEXT,                       -- their channel description (may be empty)
    country          TEXT,                       -- their declared country (may be null)
    account_created  TEXT,                       -- ISO datetime of their YouTube account creation
    subscriber_count INTEGER,                    -- their own channel's subscriber count
    video_count      INTEGER,                    -- their own channel's uploaded video count
    view_count       INTEGER,                    -- their own channel's total views
    subscribed_to    TEXT    NOT NULL DEFAULT '[]', -- JSON list of our profile keys e.g. ["en"]
    public_playlists TEXT,                       -- JSON [{id, title, item_count, created_at}]
    fetched_at       INTEGER NOT NULL            -- UNIX epoch seconds of last fetch
);

CREATE INDEX IF NOT EXISTS idx_yt_subscribers_channel
    ON youtube_subscribers(channel_id);

CREATE TABLE IF NOT EXISTS youtube_video_comments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id          TEXT    NOT NULL,         -- YouTube video ID
    comment_id        TEXT    NOT NULL UNIQUE,  -- YouTube comment ID
    author_name       TEXT,
    author_channel_id TEXT,
    text              TEXT    NOT NULL,
    like_count        INTEGER NOT NULL DEFAULT 0,
    published_at      TEXT,                     -- ISO datetime from YouTube
    fetched_at        INTEGER NOT NULL          -- UNIX epoch seconds
);

CREATE INDEX IF NOT EXISTS idx_yt_comments_video
    ON youtube_video_comments(video_id);

CREATE TABLE IF NOT EXISTS youtube_publish_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            TEXT    NOT NULL UNIQUE,
    story_set_id        INTEGER REFERENCES story_sets(id),
    story_id            INTEGER REFERENCES stories(id),
    channel_id          TEXT    NOT NULL,   -- YouTube channel ID (UCxxx)
    upload_profile      TEXT    NOT NULL,   -- 'en' or 'zh' (key in youtube_profiles.json)
    lang                TEXT    NOT NULL,   -- 'en' or 'zh' (matches stories.lang)
    locale              TEXT    NOT NULL,   -- 'en-US' or 'zh-Hans' (render locale)
    published_at        INTEGER,            -- UNIX epoch SECONDS; NULL until published
    ctr_pct             REAL,               -- impressionClickThroughRate (%)
    avg_view_duration   REAL,               -- averageViewDuration (seconds)
    avg_view_pct        REAL,               -- averageViewPercentage (%)
    views               INTEGER,            -- raw view count at time of analytics pull
    like_count          INTEGER,            -- YouTube like count at time of pull (Data API v3)
    comment_count       INTEGER,            -- YouTube comment count at time of pull (Data API v3)
    traffic_sources     TEXT,               -- JSON {source_type: views} from Analytics API
    analytics_pulled_at INTEGER,            -- UNIX epoch seconds of last pull; -1 = no data (gave up)
    created_at          INTEGER NOT NULL    -- UNIX epoch MILLISECONDS (matches _now() convention)
);

CREATE INDEX IF NOT EXISTS idx_yt_log_video_id
    ON youtube_publish_log(video_id);
CREATE INDEX IF NOT EXISTS idx_yt_log_story_set
    ON youtube_publish_log(story_set_id);
CREATE INDEX IF NOT EXISTS idx_yt_log_published_at
    ON youtube_publish_log(published_at);
CREATE INDEX IF NOT EXISTS idx_yt_log_analytics
    ON youtube_publish_log(analytics_pulled_at);
"""


def _now() -> int:
    """Current time as UNIX epoch milliseconds. Avoids UNIQUE collisions within the same second."""
    return int(time.time() * 1000)


def _ts_to_iso(ts) -> str | None:
    """Convert a UNIX timestamp (ms or seconds) to ISO string for API display."""
    if ts is None:
        return None
    if isinstance(ts, str) and ts.isdigit():
        ts = int(ts)
    if isinstance(ts, (int, float)):
        # If value > year 2100 in seconds, it's milliseconds
        if ts > 4102444800:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return str(ts)


def _iso_to_unix(iso_str: str) -> int:
    """Convert an ISO string to UNIX epoch seconds. For migration."""
    try:
        dt = datetime.fromisoformat(iso_str.replace(' ', 'T'))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return _now()


def get_connection() -> sqlite3.Connection:
    """Get a connection to story_engine's own database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist. Migrate existing tables."""
    conn = get_connection()
    conn.executescript(SCHEMA)
    # Migration: add batch_id column to stories if not present
    try:
        conn.execute("ALTER TABLE stories ADD COLUMN batch_id INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add platform column to used_items if not present
    try:
        conn.execute("ALTER TABLE used_items ADD COLUMN platform TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add partial columns to story_sets if not present
    try:
        conn.execute("ALTER TABLE story_sets ADD COLUMN partial INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE story_sets ADD COLUMN partial_formats TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add canonical_story_id to used_items if not present (v2 dedup)
    try:
        conn.execute("ALTER TABLE used_items ADD COLUMN canonical_story_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add role column to used_items if not present
    try:
        conn.execute(
            "ALTER TABLE used_items ADD COLUMN role TEXT NOT NULL DEFAULT 'main'"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add profile_id column to story_sets (2026-04-14 channel
    # validation — per-run config profile tracking for trend_ui tabs)
    try:
        conn.execute(
            "ALTER TABLE story_sets ADD COLUMN profile_id TEXT DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: create event_memory table if not present (2026-04-15 event dedup)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_memory (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id      TEXT NOT NULL,
            story_title   TEXT NOT NULL,
            source_titles TEXT NOT NULL DEFAULT '[]',
            source_urls   TEXT NOT NULL DEFAULT '[]',
            story_id      INTEGER REFERENCES stories(id),
            story_set_id  INTEGER REFERENCES story_sets(id),
            created_at    INTEGER NOT NULL,
            expires_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_event_memory_expires  ON event_memory(expires_at);
        CREATE INDEX IF NOT EXISTS idx_event_memory_event_id ON event_memory(event_id);
    """)

    # Migration: add embedding_center column to event_memory (Phase 2 cosine dedup)
    try:
        conn.execute("ALTER TABLE event_memory ADD COLUMN embedding_center TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add entities column to event_memory (LLM entity extraction)
    try:
        conn.execute("ALTER TABLE event_memory ADD COLUMN entities TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add topic_clusters column to stories if not present
    try:
        conn.execute("ALTER TABLE stories ADD COLUMN topic_clusters TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: attractiveness scoring columns (Phase 1 pipe gate)
    try:
        conn.execute(
            "ALTER TABLE hierarchical_stories "
            "ADD COLUMN attractiveness_score INTEGER DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        conn.execute(
            "ALTER TABLE hierarchical_stories "
            "ADD COLUMN attractiveness_breakdown TEXT DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: trend scoring columns (Phase 1.5 lightweight trend assist)
    try:
        conn.execute(
            "ALTER TABLE hierarchical_stories "
            "ADD COLUMN trend_bonus INTEGER DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        conn.execute(
            "ALTER TABLE hierarchical_stories "
            "ADD COLUMN final_score INTEGER DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: story classification + soft tier columns
    try:
        conn.execute(
            "ALTER TABLE hierarchical_stories "
            "ADD COLUMN story_type TEXT DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        conn.execute(
            "ALTER TABLE hierarchical_stories "
            "ADD COLUMN produce_tier TEXT DEFAULT NULL"
            # values: 'strong' (>=72), 'weak' (58–71), 'skip' (<58), NULL (unscored)
        )
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add purity_log table if not present (Sprint 3 auto-calibration)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS purity_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sim         REAL    NOT NULL,
                purity      REAL    NOT NULL,
                allowed     INTEGER NOT NULL,
                reason      TEXT    NOT NULL,
                created_at  INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_purity_log_created ON purity_log(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_purity_log_allowed ON purity_log(allowed)")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # table already exists

    # Migration: create hierarchical_stories table if not present (deep story architecture)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hierarchical_stories (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            story_set_id        INTEGER,
            batch_ts            INTEGER,
            lang                TEXT,
            channel             INTEGER,
            status              TEXT NOT NULL DEFAULT 'ready',
            deep_story          TEXT,
            supporting_stories  TEXT,
            generated_at        INTEGER,
            created_at          INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_hierarchical_stories_batch_ts
            ON hierarchical_stories(batch_ts);
        CREATE INDEX IF NOT EXISTS idx_hierarchical_stories_status
            ON hierarchical_stories(status);
        CREATE INDEX IF NOT EXISTS idx_hierarchical_stories_set_id
            ON hierarchical_stories(story_set_id);
    """)

    # Migration: create youtube_publish_log table if not present (2026-05-03 analytics feedback)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS youtube_publish_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id            TEXT    NOT NULL UNIQUE,
            story_set_id        INTEGER REFERENCES story_sets(id),
            story_id            INTEGER REFERENCES stories(id),
            channel_id          TEXT    NOT NULL,
            upload_profile      TEXT    NOT NULL,
            lang                TEXT    NOT NULL,
            locale              TEXT    NOT NULL,
            published_at        INTEGER,
            ctr_pct             REAL,
            avg_view_duration   REAL,
            avg_view_pct        REAL,
            views               INTEGER,
            like_count          INTEGER,
            comment_count       INTEGER,
            traffic_sources     TEXT,
            analytics_pulled_at INTEGER,
            created_at          INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_yt_log_video_id
            ON youtube_publish_log(video_id);
        CREATE INDEX IF NOT EXISTS idx_yt_log_story_set
            ON youtube_publish_log(story_set_id);
        CREATE INDEX IF NOT EXISTS idx_yt_log_published_at
            ON youtube_publish_log(published_at);
        CREATE INDEX IF NOT EXISTS idx_yt_log_analytics
            ON youtube_publish_log(analytics_pulled_at);
    """)

    # Migration: create youtube_video_comments table if not present
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS youtube_video_comments (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id          TEXT    NOT NULL,
            comment_id        TEXT    NOT NULL UNIQUE,
            author_name       TEXT,
            author_channel_id TEXT,
            text              TEXT    NOT NULL,
            like_count        INTEGER NOT NULL DEFAULT 0,
            published_at      TEXT,
            fetched_at        INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_yt_comments_video
            ON youtube_video_comments(video_id);
    """)

    # Migration: create youtube_subscribers table if not present
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS youtube_subscribers (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id       TEXT    NOT NULL UNIQUE,
            display_name     TEXT    NOT NULL,
            description      TEXT,
            country          TEXT,
            account_created  TEXT,
            subscriber_count INTEGER,
            video_count      INTEGER,
            view_count       INTEGER,
            subscribed_to    TEXT    NOT NULL DEFAULT '[]',
            public_playlists TEXT,
            fetched_at       INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_yt_subscribers_channel
            ON youtube_subscribers(channel_id);
    """)

    # Migration: add like_count, comment_count, traffic_sources to youtube_publish_log
    for _col, _coltype in [
        ("like_count",      "INTEGER"),
        ("comment_count",   "INTEGER"),
        ("traffic_sources", "TEXT"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE youtube_publish_log ADD COLUMN {_col} {_coltype}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migration: convert text timestamps to UNIX integers
    _migrate_timestamps(conn)

    conn.commit()
    conn.close()


def _migrate_timestamps(conn):
    """Convert any text timestamps to UNIX integers in all tables."""
    # stories.generated_at — only migrate ISO strings (contain '-')
    rows = conn.execute(
        "SELECT id, generated_at FROM stories WHERE generated_at LIKE '%-%'"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE stories SET generated_at = ? WHERE id = ?",
                     (_iso_to_unix(r['generated_at']), r['id']))

    # stories.created_at
    rows = conn.execute(
        "SELECT id, created_at FROM stories WHERE created_at LIKE '%-%'"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE stories SET created_at = ? WHERE id = ?",
                     (_iso_to_unix(r['created_at']), r['id']))

    # story_sets.batch_ts — only migrate ISO strings, skip numeric strings
    rows = conn.execute(
        "SELECT id, batch_ts FROM story_sets WHERE batch_ts LIKE '%-%'"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE story_sets SET batch_ts = ? WHERE id = ?",
                     (_iso_to_unix(r['batch_ts']), r['id']))

    # story_sets.created_at
    rows = conn.execute(
        "SELECT id, created_at FROM story_sets WHERE created_at LIKE '%-%'"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE story_sets SET created_at = ? WHERE id = ?",
                     (_iso_to_unix(r['created_at']), r['id']))

    # used_items.used_at
    rows = conn.execute(
        "SELECT id, used_at FROM used_items WHERE used_at LIKE '%-%'"
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE used_items SET used_at = ? WHERE id = ?",
                     (_iso_to_unix(r['used_at']), r['id']))


# ---------------------------------------------------------------------------
# Story Set functions
# ---------------------------------------------------------------------------

def create_story_set(
    lang: str,
    channel: int,
    profile_id: str | None = None,
) -> tuple[int, int]:
    """
    Create a new story set. Returns (set_id, batch_ts) where batch_ts is UNIX epoch.

    Args:
        lang:       Output language ("en" or "zh").
        channel:    Output channel (1, 2, or 3).
        profile_id: Per-run overlay profile id (e.g. "run2_ai") or None for base.
                    Used by trend_ui to group stories by themed channel.
    """
    batch_ts = _now()
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO story_sets (batch_ts, lang, channel, profile_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (batch_ts, lang, channel, profile_id, batch_ts)
    )
    set_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return set_id, batch_ts


def complete_story_set(set_id: int, status: str = 'complete'):
    """Mark a story set as complete or failed."""
    conn = get_connection()
    conn.execute(
        "UPDATE story_sets SET status = ? WHERE id = ?",
        (status, set_id)
    )
    conn.commit()
    conn.close()


def record_used_items(story_set_id: int, story_id: int, format: str,
                      items: list[dict], role: str = 'main'):
    """Record which crawler items were consumed by a story."""
    now = _now()
    conn = get_connection()
    for item in items:
        conn.execute(
            """INSERT INTO used_items
               (crawler_item_id, crawler_url, hotness_at_use,
                story_set_id, story_id, format, used_at, platform, role)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item['id'], item['url'], item['hotness'],
             story_set_id, story_id, format, now, item.get('platform'), role)
        )
    conn.commit()
    conn.close()


def get_used_urls_with_hotness() -> dict[str, float]:
    """Return {crawler_url: max_hotness_at_use} for all used items.

    Keyed by URL (not crawler_item_id) because the crawler creates
    multiple rows for the same URL across crawl cycles.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT crawler_url, MAX(hotness_at_use) as max_hotness "
        "FROM used_items WHERE role = 'main' OR role IS NULL GROUP BY crawler_url"
    ).fetchall()
    conn.close()
    return {row['crawler_url']: row['max_hotness'] for row in rows}


def get_platform_counts_for_set(story_set_id: int) -> dict[str, int]:
    """Return {platform: count} of items already used in the given story set.

    Used by select_for_format (single strategy) to enforce cross-format
    platform caps within a single generation run.
    Requires used_items.platform column (added via migration in init_db).
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT platform, COUNT(*) as cnt
           FROM used_items
           WHERE story_set_id = ? AND platform IS NOT NULL AND (role = 'main' OR role IS NULL)
           GROUP BY platform""",
        (story_set_id,)
    ).fetchall()
    conn.close()
    return {row['platform']: row['cnt'] for row in rows}


def get_story_sets(
    limit: int = 20,
    profile_id: str | None = None,
    lang: str | None = None,
) -> list[dict]:
    """
    Get story sets with story counts (ready only).

    Args:
        limit:      Max number of sets to return (newest first).
        profile_id: Optional filter — if provided, only return sets whose
                    profile_id matches exactly. Used by trend_ui channel tabs.
                    Pass None (default) to get all sets regardless of profile.
        lang:       Optional filter — 'en' or 'zh'. Filters by story_sets.lang.
                    Pass None (default) to return sets of any language.
    """
    conn = get_connection()

    # Build WHERE clause dynamically based on filters
    conditions = []
    params: list = []
    if profile_id is not None:
        conditions.append('ss.profile_id = ?')
        params.append(profile_id)
    if lang is not None:
        conditions.append('ss.lang = ?')
        params.append(lang)

    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    params.append(limit)

    rows = conn.execute(
        f"""SELECT ss.*,
              COUNT(CASE WHEN s.status = 'ready' THEN 1 END) as story_count,
              COUNT(DISTINCT CASE WHEN hs.status = 'ready' THEN hs.id END) as hier_count
           FROM story_sets ss
           LEFT JOIN stories s ON s.batch_id = ss.id
           LEFT JOIN hierarchical_stories hs ON hs.story_set_id = ss.id
           {where_clause}
           GROUP BY ss.id
           HAVING story_count > 0 OR hier_count > 0
           ORDER BY ss.id DESC
           LIMIT ?""",
        params
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d['batch_ts'] = _ts_to_iso(d['batch_ts'])
        d['created_at'] = _ts_to_iso(d['created_at'])
        result.append(d)
    return result


def get_stories_by_set(set_id: int) -> list[dict]:
    """Get all stories in a specific story set.

    For deep-story-only sets (no flat stories), converts the hierarchical story
    and its supporting stories into Story-compatible dicts so the existing
    /story-sets/{id} endpoint and trend_ui StoryCard can render them without
    any frontend changes.
    """
    import json as _json

    conn = get_connection()

    # --- flat stories (legacy / flat-format runs) ---
    rows = conn.execute(
        "SELECT * FROM stories WHERE batch_id = ? AND status = 'ready' ORDER BY id",
        (set_id,)
    ).fetchall()
    flat = [_row_to_dict(r) for r in rows]

    if flat:
        conn.close()
        return flat

    # --- hierarchical (deep-story-only) sets ---
    # Convert the deep story + supporting stories to Story-compatible dicts so
    # the same API endpoint and frontend component can render them.
    hier_rows = conn.execute(
        "SELECT * FROM hierarchical_stories WHERE story_set_id = ? AND status = 'ready' ORDER BY id",
        (set_id,)
    ).fetchall()
    conn.close()

    result: list[dict] = []
    for hr in hier_rows:
        hr = dict(hr)
        ts = _ts_to_iso(hr['generated_at']) if hr.get('generated_at') else None
        channel = hr.get('channel', 2)
        lang = hr.get('lang', 'zh')

        def _norm_sources(raw_sources: list) -> list:
            """Ensure every source has platform and hotness (required by SourceItem schema)."""
            result_srcs = []
            for src in raw_sources:
                result_srcs.append({
                    'url':      src.get('url', ''),
                    'title':    src.get('title', ''),
                    'platform': src.get('platform', 'news_rss'),
                    'hotness':  src.get('hotness', 0.0),
                })
            return result_srcs

        # Deep story → one Story entry (format='deep_story')
        ds = _json.loads(hr['deep_story']) if hr.get('deep_story') else {}
        if ds:
            # New format stores narrative in 'body'; legacy format uses hook+bullets+twist.
            # 'body' takes priority — if present, hook = body and bullets/twist are empty.
            hook_text = ds.get('body') or ds.get('hook', '')
            bullets   = [] if ds.get('body') else ds.get('bullets', [])
            twist     = '' if ds.get('body') else ds.get('twist', '')
            result.append({
                'id':             hr['id'] * 10000,        # synthetic id — no collision with flat stories
                'title':          ds.get('title', ''),
                'format':         'deep_story',
                'channel':        channel,
                'lang':           lang,
                'status':         'ready',
                'generated_at':   ts,
                'hook':           hook_text,
                'bullets':        bullets,
                'twist':          twist,
                'sources':        _norm_sources(ds.get('sources', [])),
                'comments_used':  [],
                'token_estimate': ds.get('token_estimate'),
            })

        # Supporting stories → one Story entry each (format='supporting')
        supporting = _json.loads(hr['supporting_stories']) if hr.get('supporting_stories') else []
        for idx, ss in enumerate(supporting):
            result.append({
                'id':             hr['id'] * 10000 + idx + 1,
                'title':          ss.get('title', ''),
                'format':         'supporting',
                'channel':        channel,
                'lang':           lang,
                'status':         'ready',
                'generated_at':   ts,
                'hook':           ss.get('summary', ''),
                'bullets':        [],
                'twist':          ss.get('why_it_matters', ''),
                'sources':        _norm_sources(ss.get('sources', [])),
                'comments_used':  [],
                'token_estimate': None,
            })

    return result


# ---------------------------------------------------------------------------
# Story CRUD functions
# ---------------------------------------------------------------------------

def save_story(
    title: str,
    format: str,
    channel: int,
    lang: str,
    hook: str,
    bullets: list[str],
    twist: str,
    sources: list[dict],
    comments_used: list[dict] | None = None,
    batch_id: int | None = None,
    batch_ts: int | None = None,
    topic_clusters: list[dict] | None = None,
) -> int:
    """
    Save a generated story to the database.

    If batch_ts is provided (UNIX epoch), all stories in the same batch share
    the same generated_at timestamp. Otherwise falls back to current time.

    Returns the story ID.
    """
    now = batch_ts or _now()
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO stories (title, format, channel, lang, status, generated_at,
                             hook, bullets, twist, sources, topic_clusters,
                             comments_used, batch_id, created_at)
        VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            format,
            channel,
            lang,
            now,
            hook,
            json.dumps(bullets, ensure_ascii=False),
            twist,
            json.dumps(sources, ensure_ascii=False),
            json.dumps(topic_clusters, ensure_ascii=False) if topic_clusters else None,
            json.dumps(comments_used or [], ensure_ascii=False),
            batch_id,
            now,
        ),
    )
    story_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return story_id


def save_failed_story(title: str, format: str, lang: str, error: str,
                      batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """Save a failed generation attempt for observability."""
    now = batch_ts or _now()
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO stories (title, format, channel, lang, status, generated_at,
                             error_message, batch_id, created_at)
        VALUES (?, ?, 1, ?, 'failed', ?, ?, ?, ?)
        """,
        (title, format, lang, now, error, batch_id, now),
    )
    story_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return story_id


def save_hierarchical_story(
    story_set_id:       int,
    batch_ts:           int,
    lang:               str,
    channel:            int,
    deep_story:         dict,
    supporting_stories: list[dict],
    status:             str = 'ready',
) -> int:
    """
    Save a hierarchical (deep story + supporting stories) batch to the database.

    Mirrors save_story() pattern exactly.
    Returns the inserted row id.
    """
    now = batch_ts or _now()
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO hierarchical_stories
            (story_set_id, batch_ts, lang, channel, status,
             deep_story, supporting_stories, generated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            story_set_id,
            batch_ts,
            lang,
            channel,
            status,
            json.dumps(deep_story,         ensure_ascii=False),
            json.dumps(supporting_stories, ensure_ascii=False),
            now,
            now,
        ),
    )
    story_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return story_id


def save_attractiveness_score(
    story_set_id: int,
    score:        int,
    breakdown:    dict,
    story_type:   str | None = None,
) -> None:
    """
    Write attractiveness score + JSON breakdown + story_type to hierarchical_stories.

    Called after generate_story_batch() saves the story — non-fatal if it fails.
    score: int 0–100 (sum of seven dimension scores, computed by attract_scorer)
    breakdown: per-dimension {score, reason} dict (may include story_type key)
    story_type: one of health_science | tech_ai | crime | finance | geopolitics |
                sports | celebrity | accident | social_tech | political |
                environment | other  (NULL if scorer did not return one)
    """
    conn = get_connection()
    conn.execute(
        """UPDATE hierarchical_stories
           SET attractiveness_score = ?, attractiveness_breakdown = ?,
               story_type = COALESCE(?, story_type)
           WHERE story_set_id = ?""",
        (score, json.dumps(breakdown, ensure_ascii=False), story_type, story_set_id),
    )
    conn.commit()
    conn.close()


def save_trend_score(
    story_set_id: int,
    trend_bonus:  int,
    final_score:  int,
) -> None:
    """
    Write trend_bonus, final_score, and produce_tier to hierarchical_stories.

    Called after get_trend_bonus() in generate_story_batch() — non-fatal if fails.
    final_score = attractiveness_score + trend_bonus

    produce_tier values:
      'strong' — final_score >= 72  (normal production)
      'weak'   — final_score 58–71  (exploration band, produced but tagged)
      'skip'   — final_score < 58   (not produced)
    """
    if final_score >= 72:
        tier = 'strong'
    elif final_score >= 58:
        tier = 'weak'
    else:
        tier = 'skip'

    conn = get_connection()
    conn.execute(
        """UPDATE hierarchical_stories
           SET trend_bonus = ?, final_score = ?, produce_tier = ?
           WHERE story_set_id = ?""",
        (trend_bonus, final_score, tier, story_set_id),
    )
    conn.commit()
    conn.close()


def save_failed_hierarchical_story(
    story_set_id: int,
    batch_ts:     int,
    lang:         str,
    channel:      int,
    error:        str,
) -> int:
    """
    Record a failed hierarchical story generation attempt for observability.
    Mirrors save_failed_story() pattern.
    """
    now = batch_ts or _now()
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO hierarchical_stories
            (story_set_id, batch_ts, lang, channel, status,
             deep_story, supporting_stories, generated_at, created_at)
        VALUES (?, ?, ?, ?, 'failed', NULL, NULL, ?, ?)
        """,
        (story_set_id, batch_ts, lang, channel, now, now),
    )
    story_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return story_id


def get_story(story_id: int) -> dict | None:
    """Get a single story by ID."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_dict(row)


def get_stories_today(lang: str | None = None) -> list[dict]:
    """Get all stories generated today (based on UNIX timestamp)."""
    # Today's start as UNIX epoch
    today_start = int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp())
    conn = get_connection()
    if lang:
        rows = conn.execute(
            "SELECT * FROM stories WHERE generated_at >= ? AND lang = ? ORDER BY id",
            (today_start, lang),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM stories WHERE generated_at >= ? ORDER BY id",
            (today_start,),
        ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stories(
    date: str | None = None,
    format: str | None = None,
    channel: int | None = None,
    lang: str | None = None,
    set_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get stories with optional filters."""
    conditions = []
    params = []

    if date:
        # Convert YYYY-MM-DD to UNIX range
        dt = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        day_start = int(dt.timestamp())
        day_end = day_start + 86400
        conditions.append("generated_at >= ? AND generated_at < ?")
        params.extend([day_start, day_end])
    if format:
        conditions.append("format = ?")
        params.append(format)
    if channel:
        conditions.append("channel = ?")
        params.append(channel)
    if lang:
        conditions.append("lang = ?")
        params.append(lang)
    if set_id:
        conditions.append("batch_id = ?")
        params.append(set_id)

    where = " AND ".join(conditions) if conditions else "1=1"
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM stories WHERE {where} ORDER BY generated_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Event memory functions
# ---------------------------------------------------------------------------

def store_event(
    story_id: int,
    story_set_id: int | None,
    story_title: str,
    sources: list[dict],
    window_days: int = 7,
    embedding_center: list[float] | None = None,
    entities: list[dict] | None = None,
) -> None:
    """
    Store a generated story's event fingerprint in event_memory.

    Called by generator.py after every successful save_story(). Allows the
    event_layer/memory.py dedup module to compare future candidates against
    recently told stories.

    Args:
        story_id:          ID of the saved story.
        story_set_id:      batch_id of the story set.
        story_title:       zh story title — used as the event key and debug label.
        sources:           List of source dicts — must contain 'url' and 'title' keys.
        window_days:       How many days to retain this event in memory (default 7).
        embedding_center:  Mean embedding vector of the event's source articles
                           (BAAI/bge-small-en-v1.5, 384-dim). When provided,
                           memory.py uses cosine similarity (Phase 2) instead of
                           Jaccard (Phase 1) for future dedup comparisons.
                           None for multi-item format stories or missing embeddings.
        entities:          List of {text, type} dicts extracted by LLM (Haiku).
                           e.g. [{"text": "Eric Swalwell", "type": "PERSON"}, ...]
                           None if extraction failed or was not attempted.
    """
    import hashlib as _hashlib
    source_titles    = [s.get('title') or '' for s in sources if s.get('title')]
    source_urls      = [s.get('url')   or '' for s in sources if s.get('url')]
    event_id         = _hashlib.sha256(story_title.encode('utf-8')).hexdigest()[:16]
    now              = int(time.time())
    expires_at       = now + window_days * 86400
    emb_json         = json.dumps(embedding_center) if embedding_center else None
    ent_json         = json.dumps(entities, ensure_ascii=False) if entities else None
    conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO event_memory
           (event_id, story_title, source_titles, source_urls,
            embedding_center, entities, story_id, story_set_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            story_title,
            json.dumps(source_titles, ensure_ascii=False),
            json.dumps(source_urls),
            emb_json,
            ent_json,
            story_id,
            story_set_id,
            now,
            expires_at,
        ),
    )
    conn.commit()
    conn.close()


def load_recent_events(window_days: int = 7) -> list[dict]:
    """
    Load non-expired event memory entries (within window_days).

    Returns list of dicts with keys:
        event_id, story_title, source_titles (list), source_urls (list),
        story_id, story_set_id, created_at, expires_at
    """
    now = int(time.time())
    cutoff = now - window_days * 86400
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM event_memory WHERE expires_at > ? ORDER BY created_at DESC",
        (now,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d['source_titles'] = json.loads(d['source_titles']) if d['source_titles'] else []
        except (json.JSONDecodeError, TypeError):
            d['source_titles'] = []
        try:
            d['source_urls'] = json.loads(d['source_urls']) if d['source_urls'] else []
        except (json.JSONDecodeError, TypeError):
            d['source_urls'] = []
        # Phase 2: parse embedding_center — None for legacy rows without embeddings
        try:
            raw_emb = d.get('embedding_center')
            d['embedding_center'] = json.loads(raw_emb) if raw_emb else None
        except (json.JSONDecodeError, TypeError):
            d['embedding_center'] = None
        # Entities: parse JSON array — None for legacy rows
        try:
            raw_ent = d.get('entities')
            d['entities'] = json.loads(raw_ent) if raw_ent else None
        except (json.JSONDecodeError, TypeError):
            d['entities'] = None
        result.append(d)
    return result


def log_purity_decision(
    sim: float,
    purity: float,
    allowed: bool,
    reason: str,
) -> None:
    """
    Log a clustering purity gate decision to purity_log.

    Called for every borderline pair (0.75 <= cosine < 0.85) during clustering.
    Used by the purity_calibrator.py script to auto-calibrate the gate threshold.

    Never raises — clustering must never be blocked by a logging failure.
    """
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO purity_log (sim, purity, allowed, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (float(sim), float(purity), int(allowed), str(reason), int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # never block clustering


# ---------------------------------------------------------------------------
# YouTube Analytics functions
# ---------------------------------------------------------------------------

def register_youtube_publish(
    video_id:       str,
    story_set_id:   int | None,
    story_id:       int | None,
    channel_id:     str,
    upload_profile: str,
    lang:           str,
    locale:         str,
    published_at:   int | None = None,
) -> int:
    """
    Record a published YouTube video and its link to a story/story_set.
    Returns the new youtube_publish_log row id (or 0 on INSERT OR IGNORE no-op).

    Called by publish_episode.py immediately after the video goes public or is
    confirmed scheduled. Must be called in ALL exit paths of publish_episode.py
    — including the "already scheduled" and "already public" early returns.

    Non-blocking contract: publish_episode.py wraps this in try/except and must
    never abort if this write fails (the video is already live).

    INSERT OR IGNORE: first write wins. If publish_episode.py is retried after
    analytics have already been fetched, the existing row (with ctr_pct etc.)
    is preserved unchanged. INSERT OR REPLACE must NOT be used — it deletes the
    old row on conflict, wiping all analytics columns back to NULL.

    Timestamp units:
      created_at   — UNIX epoch MILLISECONDS (matches _now() convention)
      published_at — UNIX epoch SECONDS      (arithmetic used in fetch_analytics.py)
    """
    now_ms  = int(time.time() * 1000)          # milliseconds — matches _now()
    pub_sec = published_at or int(time.time()) # seconds — for 72h cutoff arithmetic
    conn = get_connection()

    # Auto-populate story_id from hierarchical_stories when not provided.
    # youtube_publish_log.story_id stores hierarchical_stories.id (NOT stories.id).
    # Join: youtube_publish_log.story_id = hierarchical_stories.id
    if story_id is None and story_set_id is not None:
        hs_row = conn.execute(
            "SELECT id FROM hierarchical_stories WHERE story_set_id = ? LIMIT 1",
            (story_set_id,),
        ).fetchone()
        if hs_row:
            story_id = hs_row[0]

    cursor = conn.execute(
        """INSERT OR IGNORE INTO youtube_publish_log
           (video_id, story_set_id, story_id, channel_id, upload_profile,
            lang, locale, published_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, story_set_id, story_id, channel_id, upload_profile,
         lang, locale, pub_sec, now_ms),
    )
    row_id = cursor.lastrowid or 0
    conn.commit()
    conn.close()
    return row_id


def get_youtube_analytics(story_set_id: int) -> list[dict]:
    """
    Return all youtube_publish_log rows for a given story_set_id.

    Used by the /api/analytics/story-set/{story_set_id} endpoint.
    Returns list of dicts with published_at as ISO string for API display.
    analytics_pulled_at == -1 means data was never available (gave up after 14d).
    analytics_pulled_at == None means still pending (video < 72h old or retry pending).
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, video_id, story_set_id, story_id, channel_id,
                  upload_profile, lang, locale, published_at,
                  ctr_pct, avg_view_duration, avg_view_pct, views,
                  like_count, comment_count, traffic_sources,
                  analytics_pulled_at, created_at
           FROM youtube_publish_log
           WHERE story_set_id = ?
           ORDER BY published_at ASC""",
        (story_set_id,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d['published_at']       = _ts_to_iso(d.get('published_at'))
        d['analytics_pulled_at'] = (
            None if d['analytics_pulled_at'] is None
            else 'no_data' if d['analytics_pulled_at'] == -1
            else _ts_to_iso(d['analytics_pulled_at'])
        )
        d['created_at'] = _ts_to_iso(d.get('created_at'))
        try:
            d['traffic_sources'] = json.loads(d['traffic_sources']) if d.get('traffic_sources') else None
        except (json.JSONDecodeError, TypeError):
            d['traffic_sources'] = None
        result.append(d)
    return result


def upsert_subscriber(
    channel_id:       str,
    display_name:     str,
    description:      str | None,
    country:          str | None,
    account_created:  str | None,
    subscriber_count: int | None,
    video_count:      int | None,
    view_count:       int | None,
    subscribed_to:    list[str],
    public_playlists: list[dict],
) -> None:
    """
    Insert or update a subscriber row in youtube_subscribers.

    Called by fetch_subscribers.py after pulling subscriber info from YouTube API.
    INSERT OR REPLACE: always overwrites with latest data on re-fetch.
    """
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO youtube_subscribers
           (channel_id, display_name, description, country, account_created,
            subscriber_count, video_count, view_count,
            subscribed_to, public_playlists, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            channel_id,
            display_name,
            description,
            country,
            account_created,
            subscriber_count,
            video_count,
            view_count,
            json.dumps(subscribed_to, ensure_ascii=False),
            json.dumps(public_playlists, ensure_ascii=False),
            int(time.time()),
        ),
    )
    conn.commit()
    conn.close()


def upsert_video_comment(
    video_id:          str,
    comment_id:        str,
    author_name:       str | None,
    author_channel_id: str | None,
    text:              str,
    like_count:        int,
    published_at:      str | None,
) -> None:
    """
    Insert or update a YouTube comment row.
    Called by fetch_video_comments.py. INSERT OR REPLACE overwrites on re-fetch.
    """
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO youtube_video_comments
           (video_id, comment_id, author_name, author_channel_id,
            text, like_count, published_at, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, comment_id, author_name, author_channel_id,
         text, like_count, published_at, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_stories_with_comments() -> list[dict]:
    """
    Return stories that have at least one fetched YouTube comment.

    Each item contains story metadata + a list of comment dicts.
    Joins: youtube_video_comments → youtube_publish_log → hierarchical_stories.
    Only returns stories where comments have actually been fetched (not just counted).
    """
    conn = get_connection()

    # Get all unique videos that have stored comments
    video_rows = conn.execute(
        """SELECT DISTINCT ypl.video_id, ypl.lang, ypl.upload_profile,
                  ypl.story_set_id, ypl.published_at,
                  JSON_EXTRACT(hs.deep_story, '$.title') as story_title
           FROM youtube_video_comments yvc
           JOIN youtube_publish_log ypl ON ypl.video_id = yvc.video_id
           LEFT JOIN hierarchical_stories hs ON hs.story_set_id = ypl.story_set_id
           ORDER BY ypl.published_at DESC"""
    ).fetchall()

    result = []
    for vr in video_rows:
        # Fetch comments for this video
        comments = conn.execute(
            """SELECT comment_id, author_name, author_channel_id,
                      text, like_count, published_at
               FROM youtube_video_comments
               WHERE video_id = ?
               ORDER BY like_count DESC, published_at ASC""",
            (vr["video_id"],),
        ).fetchall()

        result.append({
            "video_id":     vr["video_id"],
            "lang":         vr["lang"],
            "upload_profile": vr["upload_profile"],
            "story_set_id": vr["story_set_id"],
            "story_title":  vr["story_title"],
            "published_at": _ts_to_iso(vr["published_at"]) if vr["published_at"] else None,
            "comments": [
                {
                    "comment_id":        c["comment_id"],
                    "author_name":       c["author_name"],
                    "author_channel_id": c["author_channel_id"],
                    "text":              c["text"],
                    "like_count":        c["like_count"],
                    "published_at":      c["published_at"],
                }
                for c in comments
            ],
        })

    conn.close()
    return result


def get_subscribers() -> list[dict]:
    """
    Return all rows from youtube_subscribers, newest fetch first.

    public_playlists and subscribed_to are returned as parsed Python objects.
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT channel_id, display_name, description, country, account_created,
                  subscriber_count, video_count, view_count,
                  subscribed_to, public_playlists, fetched_at
           FROM youtube_subscribers
           ORDER BY fetched_at DESC"""
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d['subscribed_to']    = json.loads(d['subscribed_to'])    if d.get('subscribed_to')    else []
        except (json.JSONDecodeError, TypeError):
            d['subscribed_to'] = []
        try:
            d['public_playlists'] = json.loads(d['public_playlists']) if d.get('public_playlists') else []
        except (json.JSONDecodeError, TypeError):
            d['public_playlists'] = []
        d['fetched_at'] = _ts_to_iso(d['fetched_at'])
        result.append(d)
    return result


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to a dictionary with parsed JSON fields.
    Converts UNIX timestamps to ISO strings for API display.
    """
    d = dict(row)
    # Parse JSON fields
    d['bullets'] = json.loads(d['bullets']) if d.get('bullets') else []
    d['sources'] = json.loads(d['sources']) if d.get('sources') else []
    d['comments_used'] = json.loads(d['comments_used']) if d.get('comments_used') else []
    # Convert UNIX timestamps to ISO strings
    d['generated_at'] = _ts_to_iso(d.get('generated_at'))
    d['created_at'] = _ts_to_iso(d.get('created_at'))
    return d
