"""
story_engine storage — own SQLite database for generated stories.

The crawler DB is READ-ONLY to story_engine. This module manages
story_engine's own db.sqlite3 for persisting generated content.
"""

import json
import sqlite3
import os
from datetime import datetime
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
    generated_at    TIMESTAMP,
    hook            TEXT,
    bullets         TEXT,           -- JSON array of strings
    twist           TEXT,
    sources         TEXT,           -- JSON array of {url, platform, hotness, title}
    comments_used   TEXT,           -- JSON array of {text, likes, platform}
    error_message   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_stories_format ON stories(format);
CREATE INDEX IF NOT EXISTS idx_stories_lang ON stories(lang);
CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status);
CREATE INDEX IF NOT EXISTS idx_stories_generated_at ON stories(generated_at);
CREATE INDEX IF NOT EXISTS idx_stories_channel ON stories(channel);
"""


def get_connection() -> sqlite3.Connection:
    """Get a connection to story_engine's own database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


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
) -> int:
    """
    Save a generated story to the database.

    Returns the story ID.
    """
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO stories (title, format, channel, lang, status, generated_at,
                             hook, bullets, twist, sources, comments_used)
        VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            format,
            channel,
            lang,
            datetime.utcnow().isoformat(),
            hook,
            json.dumps(bullets, ensure_ascii=False),
            twist,
            json.dumps(sources, ensure_ascii=False),
            json.dumps(comments_used or [], ensure_ascii=False),
        ),
    )
    story_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return story_id


def save_failed_story(title: str, format: str, lang: str, error: str) -> int:
    """Save a failed generation attempt for observability."""
    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO stories (title, format, channel, lang, status, generated_at, error_message)
        VALUES (?, ?, 1, ?, 'failed', ?, ?)
        """,
        (title, format, lang, datetime.utcnow().isoformat(), error),
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
    """Get all stories generated today."""
    today = datetime.utcnow().strftime('%Y-%m-%d')
    conn = get_connection()
    if lang:
        rows = conn.execute(
            "SELECT * FROM stories WHERE date(generated_at) = ? AND lang = ? ORDER BY id",
            (today, lang),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM stories WHERE date(generated_at) = ? ORDER BY id",
            (today,),
        ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_stories(
    date: str | None = None,
    format: str | None = None,
    channel: int | None = None,
    lang: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get stories with optional filters."""
    conditions = []
    params = []

    if date:
        conditions.append("date(generated_at) = ?")
        params.append(date)
    if format:
        conditions.append("format = ?")
        params.append(format)
    if channel:
        conditions.append("channel = ?")
        params.append(channel)
    if lang:
        conditions.append("lang = ?")
        params.append(lang)

    where = " AND ".join(conditions) if conditions else "1=1"
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM stories WHERE {where} ORDER BY generated_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a database row to a dictionary with parsed JSON fields."""
    d = dict(row)
    # Parse JSON fields
    d['bullets'] = json.loads(d['bullets']) if d.get('bullets') else []
    d['sources'] = json.loads(d['sources']) if d.get('sources') else []
    d['comments_used'] = json.loads(d['comments_used']) if d.get('comments_used') else []
    return d
