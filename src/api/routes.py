"""
story_engine REST API routes.

Endpoints:
  GET  /api/stories          — list stories with filters
  GET  /api/stories/today    — today's stories
  GET  /api/stories/{id}     — single story detail
  GET  /api/status           — engine health check
"""

import json
import sqlite3 as _sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.schemas import (
    Story,
    StoryCard,
    StoriesListResponse,
    StorySetSummary,
    Script,
    SourceItem,
    CommentItem,
    EngineStatus,
    YoutubeAnalyticRow,
    YoutubeSubscriber,
    YoutubeSubscriberPlaylist,
    YoutubeSubscribedChannel,
    VideoComment,
    StoryWithComments,
    FormatType,
    ChannelType,
    LangType,
    GamesChannelStats,
    GamesComment,
    GamesCountryRow,
    GamesSubtitleRow,
    GamesVideoRow,
    ChannelVideoRow,
    CommentQuestion,
    VideoWithCommentQuestions,
    WinrateResult,
    WinrateStep,
)
from db.models import get_story, get_stories_today, get_stories, get_story_sets, get_stories_by_set, get_youtube_analytics, get_subscribers, get_stories_with_comments, get_channel_videos
from db.crawler_reader import get_item_count, test_connection, CRAWLER_DB_URL

router = APIRouter(prefix="/api")


def _dict_to_story(d: dict) -> Story:
    """Convert a database row dict to a Story schema object."""
    # Normalise sources — deep story searched sources may lack platform/hotness
    raw_sources = d.get('sources', [])
    norm_sources = []
    for s in raw_sources:
        norm_sources.append(SourceItem(
            url=s.get('url', ''),
            platform=s.get('platform', 'web'),
            hotness=float(s.get('hotness', 0.0)),
            title=s.get('title', ''),
        ))
    return Story(
        id=d['id'],
        title=d['title'],
        format=d['format'],
        channel=d['channel'],
        lang=d['lang'],
        status=d['status'],
        generated_at=d.get('generated_at'),
        sources_count=len(norm_sources),
        token_estimate=d.get('token_estimate'),
        script=Script(
            hook=d.get('hook') or '',
            bullets=d.get('bullets') or [],
            twist=d.get('twist') or '',
        ),
        sources=norm_sources,
        comments_used=[CommentItem(**c) for c in d.get('comments_used', [])],
    )


def _dict_to_card(d: dict) -> StoryCard:
    """Convert a database row dict to a StoryCard (list view, no script)."""
    return StoryCard(
        id=d['id'],
        title=d['title'],
        format=d['format'],
        channel=d['channel'],
        lang=d['lang'],
        status=d['status'],
        generated_at=d.get('generated_at'),
        sources_count=len(d.get('sources', [])),
        token_estimate=d.get('token_estimate'),
    )


@router.get("/stories/today", response_model=StoriesListResponse)
def list_stories_today(lang: Optional[LangType] = None):
    """Get all stories generated today."""
    stories = get_stories_today(lang=lang)
    today = datetime.utcnow().strftime('%Y-%m-%d')

    return StoriesListResponse(
        date=today,
        generated_at=datetime.utcnow(),
        total=len(stories),
        stories=[_dict_to_story(s) for s in stories],
    )


@router.get("/stories/{story_id}", response_model=Story)
def get_story_detail(story_id: int):
    """Get a single story with full script and sources."""
    d = get_story(story_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Story {story_id} not found")
    return _dict_to_story(d)


@router.get("/stories", response_model=list[StoryCard])
def list_stories(
    date: Optional[str] = None,
    format: Optional[FormatType] = None,
    channel: Optional[ChannelType] = None,
    lang: Optional[LangType] = None,
    set_id: Optional[int] = None,
    limit: int = Query(default=50, le=200),
):
    """List stories with optional filters. Returns cards (no full script)."""
    stories = get_stories(
        date=date,
        format=format,
        channel=channel,
        lang=lang,
        set_id=set_id,
        limit=limit,
    )
    return [_dict_to_card(s) for s in stories]


@router.get("/story-sets", response_model=list[StorySetSummary])
def list_story_sets(
    limit: int = Query(default=20, le=100),
    profile: str | None = Query(default=None, description="Filter by overlay profile id, e.g. 'run2_ai'"),
    lang: str | None = Query(default=None, description="Filter by language: 'en' or 'zh'"),
):
    """
    List story sets with story counts.

    If `profile` is provided, only return sets whose profile_id matches
    (used by trend_ui channel tabs). Default (no profile) returns all sets.
    If `lang` is provided ('en' or 'zh'), only return sets in that language.
    """
    sets = get_story_sets(limit=limit, profile_id=profile, lang=lang)
    return [StorySetSummary(**s) for s in sets]


@router.get("/story-sets/{set_id}", response_model=StoriesListResponse)
def get_story_set_detail(set_id: int):
    """Get all stories in a specific story set."""
    stories = get_stories_by_set(set_id)
    if not stories:
        raise HTTPException(status_code=404, detail=f"Story set {set_id} not found or empty")

    first_ts = (stories[0].get('generated_at') or '') if stories else ''
    return StoriesListResponse(
        date=first_ts[:10],
        generated_at=datetime.utcnow(),
        total=len(stories),
        stories=[_dict_to_story(s) for s in stories],
    )


@router.get("/analytics/story-set/{story_set_id}", response_model=list[YoutubeAnalyticRow])
def get_story_set_analytics(story_set_id: int):
    """
    Return YouTube Analytics rows for all videos in a story set.

    Each row corresponds to one published video (one per locale: en-US, zh-Hans).
    analytics_pulled_at values:
      null      → analytics not yet fetched (video < 72h old or pending retry)
      'no_data' → no data available after 14 days (gave up)
      ISO str   → analytics successfully fetched at this time
    """
    rows = get_youtube_analytics(story_set_id)
    return [YoutubeAnalyticRow(**r) for r in rows]


@router.get("/analytics/channel", response_model=list[ChannelVideoRow])
def get_channel_analytics(lang: str = "en"):
    """
    Return all published deep-story videos for the given channel language (en|zh),
    newest first, with analytics data and story title.

    analytics_pulled_at values:
      null      → pending (video < 72h old or not yet fetched)
      'no_data' → gave up after 14 days
      ISO str   → analytics successfully fetched
    """
    rows = get_channel_videos(lang)
    return [ChannelVideoRow(**r) for r in rows]


_STRATEGY_CHANGES_PATH       = Path("/home/tnnd/data/code/story_engine/strategy_changes.json")
_GAMES_STRATEGY_CHANGES_PATH = Path("/home/tnnd/data/code/games/strategy_changes.json")

@router.get("/analytics/strategy-changes")
def get_strategy_changes():
    """
    Return the list of strategy periods from strategy_changes.json.
    Sorted newest-first. Each entry: { date: "YYYY-MM-DD", label: "策略X…" }
    """
    try:
        return json.loads(_STRATEGY_CHANGES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


@router.get("/games/strategy-changes")
def get_games_strategy_changes():
    """
    Return the list of KataGo title/upload strategy periods from
    games/strategy_changes.json.  Same format as /analytics/strategy-changes.
    Sorted newest-first. Each entry: { date: "YYYY-MM-DD", label: "策略X…" }
    """
    try:
        return json.loads(_GAMES_STRATEGY_CHANGES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


_PIPE_PYTHON  = "/home/tnnd/.virtualenvs/pipe/bin/python"
_SCRIPTS_DIR  = Path("/home/tnnd/data/code/pipe/code/deploy/youtube")
_PIPE_CWD     = "/home/tnnd/data/code/pipe"

_GAMES_ROOT   = Path("/home/tnnd/data/code/games")
_GAMES_DB     = _GAMES_ROOT / "games.db"
_GAMES_PYTHON = "/home/tnnd/.virtualenvs/games/bin/python3"

_GAMES_CHANNEL_ID = "UCLeNQ9jLgctQzOhjYseIlFQ"


@router.post("/subscribers/refresh")
def refresh_subscribers():
    """
    Spawn fetch_subscribers.py in the background and return immediately.
    The script writes to the DB; poll GET /api/subscribers after ~10 s.
    """
    subprocess.Popen(
        [_PIPE_PYTHON, str(_SCRIPTS_DIR / "fetch_subscribers.py")],
        cwd=_PIPE_CWD,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"status": "started"}


@router.post("/comments/refresh")
def refresh_comments():
    """
    Spawn fetch_video_comments.py --refetch in the background and return immediately.
    The script writes to the DB; poll GET /api/comments after ~15 s.
    """
    subprocess.Popen(
        [_PIPE_PYTHON, str(_SCRIPTS_DIR / "fetch_video_comments.py"), "--refetch"],
        cwd=_PIPE_CWD,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"status": "started"}


@router.post("/analytics/refresh")
def refresh_channel_analytics():
    """
    Spawn fetch_analytics.py in the background and return immediately.
    Pulls YouTube Analytics for all eligible published deep-story videos (EN + ZH).
    Poll GET /api/analytics/channel after ~20 s.
    """
    subprocess.Popen(
        [_PIPE_PYTHON, str(_SCRIPTS_DIR / "fetch_analytics.py")],
        cwd=_PIPE_CWD,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"status": "started"}


@router.get("/subscribers", response_model=list[YoutubeSubscriber])
def list_subscribers():
    """
    Return all known public subscribers across all channel profiles.

    Data is populated by fetch_subscribers.py (run manually or via cron).
    Only subscribers with public subscriptions are visible — subscribers who
    set their YouTube subscriptions to private will not appear here.
    """
    rows = get_subscribers()
    result = []
    for r in rows:
        playlists = [
            YoutubeSubscriberPlaylist(
                id         = pl.get("id", ""),
                title      = pl.get("title", ""),
                item_count = pl.get("item_count", 0),
                created_at = pl.get("created_at"),
            )
            for pl in r.get("public_playlists", [])
        ]
        # subscribed_to may be old format (list[str]) or new format (list[dict])
        raw_subscribed = r.get("subscribed_to", [])
        subscribed_channels: list[YoutubeSubscribedChannel] = []
        for item in raw_subscribed:
            if isinstance(item, str):
                # Legacy format — just a profile key string, no channel details yet
                subscribed_channels.append(YoutubeSubscribedChannel(
                    profile=item, channel_id="", channel_name=item.upper(), subscribed_at=None
                ))
            elif isinstance(item, dict):
                subscribed_channels.append(YoutubeSubscribedChannel(
                    profile=item.get("profile", ""),
                    channel_id=item.get("channel_id", ""),
                    channel_name=item.get("channel_name", item.get("profile", "").upper()),
                    subscribed_at=item.get("subscribed_at"),
                ))

        result.append(YoutubeSubscriber(
            channel_id       = r["channel_id"],
            display_name     = r["display_name"],
            description      = r.get("description"),
            country          = r.get("country"),
            account_created  = r.get("account_created"),
            subscriber_count = r.get("subscriber_count"),
            video_count      = r.get("video_count"),
            view_count       = r.get("view_count"),
            subscribed_to    = subscribed_channels,
            public_playlists = playlists,
            fetched_at       = r.get("fetched_at"),
        ))
    return result


@router.get("/comments", response_model=list[StoryWithComments])
def list_story_comments():
    """
    Return all published stories that have at least one fetched viewer comment.

    Only includes stories where comments have been fetched (fetch_video_comments.py).
    Comments are ordered by like_count DESC within each video.
    Stories are ordered by published_at DESC (newest first).
    """
    rows = get_stories_with_comments()
    result = []
    for r in rows:
        comments = [
            VideoComment(
                comment_id        = c["comment_id"],
                author_name       = c.get("author_name"),
                author_channel_id = c.get("author_channel_id"),
                text              = c["text"],
                like_count        = c["like_count"],
                published_at      = c.get("published_at"),
            )
            for c in r.get("comments", [])
        ]
        result.append(StoryWithComments(
            video_id       = r["video_id"],
            lang           = r["lang"],
            upload_profile = r["upload_profile"],
            story_set_id   = r.get("story_set_id"),
            story_title    = r.get("story_title"),
            published_at   = r.get("published_at"),
            comments       = comments,
        ))
    return result


@router.get("/games/channel-stats", response_model=GamesChannelStats)
def get_games_channel_stats():
    """Return cached channel-level stats for the KataGo YouTube channel."""
    _empty = GamesChannelStats(
        channel_id=_GAMES_CHANNEL_ID,
        channel_name=None,
        subscriber_count=None,
        real_subscriber_count=None,
        video_count=None,
        view_count=None,
        fetched_at=None,
    )
    try:
        conn = _sqlite3.connect(str(_GAMES_DB))
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT * FROM channel_stats WHERE channel_id = ? LIMIT 1",
            (_GAMES_CHANNEL_ID,),
        ).fetchone()
        conn.close()
        if row is None:
            return _empty
        return GamesChannelStats(**dict(row))
    except Exception:
        return _empty


@router.get("/games/videos", response_model=list[GamesVideoRow])
def list_games_videos():
    """
    Return all published KataGo videos with their YouTube stats and comments, newest first.
    Video stats come from SQLite (games.db / video_analytics).
    Comments come from PostgreSQL go_db (game_comments) — migrated from SQLite.
    """
    try:
        import psycopg2
        import psycopg2.extras
        from collections import defaultdict

        # ── video stats from SQLite ───────────────────────────────────────────
        sqlite_conn = _sqlite3.connect(str(_GAMES_DB))
        sqlite_conn.row_factory = _sqlite3.Row
        videos = sqlite_conn.execute(
            "SELECT * FROM video_analytics ORDER BY published_at DESC"
        ).fetchall()
        sqlite_conn.close()

        # ── comments from PostgreSQL go_db ────────────────────────────────────
        comment_map: dict[str, list[GamesComment]] = defaultdict(list)
        try:
            pg_conn = psycopg2.connect(_GO_DB_URL)
            pg_conn.row_factory = None
            cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT gc.comment_id,
                       gc.author            AS author_name,
                       gc.author_channel_id,
                       gc.text,
                       gc.like_count,
                       gc.published_at,
                       gv.video_id
                FROM game_comments gc
                JOIN game_videos gv ON gv.video_db_id = gc.video_db_id
                ORDER BY gc.like_count DESC NULLS LAST, gc.published_at DESC
            """)
            for c in cur.fetchall():
                comment_map[c["video_id"]].append(GamesComment(
                    comment_id        = c["comment_id"],
                    author_name       = c["author_name"],
                    author_channel_id = c["author_channel_id"],
                    text              = c["text"],
                    like_count        = c["like_count"] or 0,
                    published_at      = str(c["published_at"]) if c["published_at"] else None,
                ))
            pg_conn.close()
        except Exception:
            pass  # go_db unreachable or table not yet populated — return videos without comments

        result = []
        for row in videos:
            d = dict(row)
            result.append(GamesVideoRow(
                **{k: v for k, v in d.items() if k != "comments"},
                comments=comment_map.get(d["video_id"], []),
            ))
        return result
    except Exception:
        return []


@router.get("/games/audience-countries", response_model=list[GamesCountryRow])
def get_games_audience_countries():
    """
    Return lifetime viewer counts by country for the KataGo channel, sorted by views DESC.
    Populated by fetch_games_analytics.py (requires yt-analytics.readonly scope).
    Returns [] if no data has been fetched yet.
    """
    try:
        conn = _sqlite3.connect(str(_GAMES_DB))
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT country, views, fetched_at FROM channel_country_views ORDER BY views DESC"
        ).fetchall()
        conn.close()
        return [GamesCountryRow(**dict(r)) for r in rows]
    except Exception:
        return []


@router.get("/games/subtitle-langs", response_model=list[GamesSubtitleRow])
def get_games_subtitle_langs():
    """
    Return lifetime views by CC/subtitle language for the KataGo channel, sorted by views DESC.
    Empty string lang means subtitles were off.
    Populated by fetch_games_analytics.py.
    """
    try:
        conn = _sqlite3.connect(str(_GAMES_DB))
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT lang, views, fetched_at FROM channel_subtitle_lang ORDER BY views DESC"
        ).fetchall()
        conn.close()
        return [GamesSubtitleRow(**dict(r)) for r in rows]
    except Exception:
        return []


@router.post("/games/refresh")
def refresh_games_analytics():
    """Spawn fetch_games_analytics.py in background. Poll GET /api/games/channel-stats after ~15s."""
    subprocess.Popen(
        [_GAMES_PYTHON, str(_GAMES_ROOT / "fetch_games_analytics.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"status": "started"}


_GO_DB_URL = "postgres://dbuser:dbpass@localhost:5432/go_db"


def _go_db_conn():
    """Return a new psycopg2 connection to go_db (the games PostgreSQL database)."""
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(_GO_DB_URL)


@router.get("/games/comment-questions", response_model=list[VideoWithCommentQuestions])
def get_comment_questions():
    """
    Return all KataGo videos that have at least one comment_question in
    status='analyzed' or status='approved', newest video first.
    Each video embeds its questions with the original comment text and winrate result.
    Returns [] if the comment_questions table does not exist yet.
    """
    import psycopg2
    import psycopg2.extras
    try:
        conn = _go_db_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Fetch videos that have qualifying questions, joined with comment text
        cur.execute("""
            SELECT DISTINCT
                gv.video_db_id,
                gv.video_id,
                gv.title,
                gv.published_at
            FROM comment_questions cq
            JOIN game_videos gv ON gv.video_db_id = cq.video_db_id
            WHERE cq.status IN ('analyzed', 'approved')
            ORDER BY gv.published_at DESC
        """)
        video_rows = cur.fetchall()

        if not video_rows:
            conn.close()
            return []

        video_db_ids = [r["video_db_id"] for r in video_rows]

        # Fetch all qualifying questions for those videos in one query
        # comment_text and author are stored directly on comment_questions
        # (populated by fetch_and_parse_comments.py at parse time)
        cur.execute("""
            SELECT
                cq.id,
                cq.comment_id,
                cq.video_db_id,
                COALESCE(cq.comment_text, '') AS comment_text,
                cq.author,
                COALESCE(cq.like_count, 0)   AS like_count,
                cq.at_move,
                cq.whatif_moves,
                COALESCE(cq.visits, 1600)    AS visits,
                cq.result_json,
                cq.status
            FROM comment_questions cq
            WHERE cq.video_db_id = ANY(%s)
              AND cq.status IN ('analyzed', 'approved')
            ORDER BY cq.like_count DESC NULLS LAST, cq.id ASC
        """, (video_db_ids,))
        question_rows = cur.fetchall()
        conn.close()

        # Group questions by video_db_id
        from collections import defaultdict
        q_map: dict = defaultdict(list)
        for q in question_rows:
            raw = q["result_json"]
            if raw is None:
                continue
            if isinstance(raw, str):
                import json as _json
                raw = _json.loads(raw)
            steps = [
                WinrateStep(
                    color=s["color"],
                    move=s["move"],
                    winrate=s["winrate"],
                    score=s["score"],
                )
                for s in raw.get("steps", [])
            ]
            result = WinrateResult(
                fork_winrate=raw.get("fork_winrate", 0.0),
                fork_score=raw.get("fork_score", 0.0),
                steps=steps,
            )
            q_map[q["video_db_id"]].append(CommentQuestion(
                id=q["id"],
                comment_id=q["comment_id"],
                comment_text=q["comment_text"] or "",
                author=q["author"],
                like_count=q["like_count"],
                at_move=q["at_move"],
                whatif_moves=q["whatif_moves"] or "",
                visits=q["visits"],
                result=result,
                status=q["status"],
            ))

        result_list = []
        for vr in video_rows:
            qs = q_map.get(vr["video_db_id"], [])
            if not qs:
                continue
            result_list.append(VideoWithCommentQuestions(
                video_db_id=vr["video_db_id"],
                video_id=vr["video_id"],
                title=vr["title"],
                published_at=vr["published_at"],
                questions=qs,
            ))
        return result_list

    except Exception as exc:
        # Table may not exist yet; return empty list rather than 500
        import logging
        logging.getLogger(__name__).warning("comment-questions fetch failed: %s", exc)
        return []


@router.post("/games/comment-questions/{question_id}/approve")
def approve_comment_question(question_id: int):
    """
    Approve a comment question and immediately post the KataGo reply to YouTube.
    Delegates to post_comment_replies.py --post-id (games venv) via subprocess
    to avoid cross-virtualenv import conflicts.
    Returns {"status": "posted", "reply_id": "...", "id": ...} on success.
    Raises HTTP 400/500 with a human-readable detail on failure.
    """
    script = str(_GAMES_ROOT / "go" / "post_comment_replies.py")
    try:
        result = subprocess.run(
            [_GAMES_PYTHON, script, "--post-id", str(question_id)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="YouTube post timed out after 30s")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Subprocess failed: {exc}")

    # Parse JSON printed to stdout by run_post_id()
    stdout = result.stdout.strip()
    if not stdout:
        detail = result.stderr.strip() or f"post_comment_replies.py exited with code {result.returncode}"
        raise HTTPException(status_code=500, detail=detail)

    try:
        data = json.loads(stdout)
    except Exception:
        raise HTTPException(status_code=500, detail=f"Unexpected output: {stdout[:200]}")

    if data.get("status") == "posted":
        return {"status": "posted", "id": question_id, "reply_id": data.get("reply_id")}

    # Script returned an error dict
    raise HTTPException(status_code=400, detail=data.get("detail", "post failed"))


@router.post("/games/comment-questions/{question_id}/skip")
def skip_comment_question(question_id: int):
    """Set comment_questions.status = 'skipped' with reason='rejected_by_reviewer'."""
    import psycopg2
    try:
        conn = _go_db_conn()
        cur  = conn.cursor()
        cur.execute(
            """UPDATE comment_questions
               SET status = 'skipped', error_message = 'rejected_by_reviewer'
               WHERE id = %s AND status IN ('analyzed', 'approved')""",
            (question_id,),
        )
        affected = cur.rowcount
        conn.commit()
        conn.close()
        if affected == 0:
            raise HTTPException(status_code=404, detail="Question not found or not in reviewable state")
        return {"status": "skipped", "id": question_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _redact_url(url: str) -> str:
    """Hide the password in a postgres://user:pass@host/db URL for safe display."""
    import re
    return re.sub(r"(postgres://[^:]+:)[^@]+(@)", r"\1***\2", url)


@router.get("/status", response_model=EngineStatus)
def engine_status():
    """Health check — shows scheduler status and crawler DB connectivity."""
    crawler_reachable = test_connection()
    items_today = 0

    if crawler_reachable:
        try:
            items_today = get_item_count(hours=24)
        except Exception:
            crawler_reachable = False

    # Count today's stories
    stories = get_stories_today()

    return EngineStatus(
        scheduler="cron",
        last_run_at=None,  # TODO: track from a metadata table
        last_run_status=None,
        stories_today=len(stories),
        crawler_db_url=_redact_url(CRAWLER_DB_URL),
        crawler_db_reachable=crawler_reachable,
    )
