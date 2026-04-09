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
    sources         TEXT,               -- JSON array of {url, platform, hotness, title}
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
    used_at         INTEGER                 -- UNIX epoch seconds
);

CREATE INDEX IF NOT EXISTS idx_used_items_crawler_id ON used_items(crawler_item_id);
CREATE INDEX IF NOT EXISTS idx_used_items_url ON used_items(crawler_url);
CREATE INDEX IF NOT EXISTS idx_used_items_story_set ON used_items(story_set_id);
"""


def _now() -> int:
    """Current time as UNIX epoch seconds."""
    return int(time.time())


def _ts_to_iso(ts) -> str | None:
    """Convert a UNIX timestamp (int/str) or ISO string to ISO string for API display."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    # String — could be numeric string from SQLite TEXT column or ISO string
    if isinstance(ts, str) and ts.isdigit():
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
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

def create_story_set(lang: str, channel: int) -> tuple[int, int]:
    """Create a new story set. Returns (set_id, batch_ts) where batch_ts is UNIX epoch."""
    batch_ts = _now()
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO story_sets (batch_ts, lang, channel, created_at) VALUES (?, ?, ?, ?)",
        (batch_ts, lang, channel, batch_ts)
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
                      items: list[dict]):
    """Record which crawler items were consumed by a story."""
    now = _now()
    conn = get_connection()
    for item in items:
        conn.execute(
            """INSERT INTO used_items
               (crawler_item_id, crawler_url, hotness_at_use,
                story_set_id, story_id, format, used_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item['id'], item['url'], item['hotness'],
             story_set_id, story_id, format, now)
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
        "FROM used_items GROUP BY crawler_url"
    ).fetchall()
    conn.close()
    return {row['crawler_url']: row['max_hotness'] for row in rows}


def get_story_sets(limit: int = 20) -> list[dict]:
    """Get story sets with story counts (ready only)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT ss.*, COUNT(CASE WHEN s.status = 'ready' THEN 1 END) as story_count
           FROM story_sets ss
           LEFT JOIN stories s ON s.batch_id = ss.id
           GROUP BY ss.id
           ORDER BY ss.id DESC
           LIMIT ?""",
        (limit,)
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
    """Get all stories in a specific story set."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM stories WHERE batch_id = ? ORDER BY id",
        (set_id,)
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


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
                             hook, bullets, twist, sources, comments_used, batch_id,
                             created_at)
        VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?)
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
