"""
story_engine REST API routes.

Endpoints:
  GET  /api/stories          — list stories with filters
  GET  /api/stories/today    — today's stories
  GET  /api/stories/{id}     — single story detail
  GET  /api/status           — engine health check
"""

from datetime import datetime
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
    FormatType,
    ChannelType,
    LangType,
)
from db.models import get_story, get_stories_today, get_stories, get_story_sets, get_stories_by_set
from db.crawler_reader import get_item_count, test_connection, CRAWLER_DB_URL

router = APIRouter(prefix="/api")


def _dict_to_story(d: dict) -> Story:
    """Convert a database row dict to a Story schema object."""
    return Story(
        id=d['id'],
        title=d['title'],
        format=d['format'],
        channel=d['channel'],
        lang=d['lang'],
        status=d['status'],
        generated_at=d.get('generated_at'),
        sources_count=len(d.get('sources', [])),
        script=Script(
            hook=d.get('hook') or '',
            bullets=d.get('bullets') or [],
            twist=d.get('twist') or '',
        ),
        sources=[SourceItem(**s) for s in d.get('sources', [])],
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
):
    """
    List story sets with story counts.

    If `profile` is provided, only return sets whose profile_id matches
    (used by trend_ui channel tabs). Default (no profile) returns all sets.
    """
    sets = get_story_sets(limit=limit, profile_id=profile)
    return [StorySetSummary(**s) for s in sets]


@router.get("/story-sets/{set_id}", response_model=StoriesListResponse)
def get_story_set_detail(set_id: int):
    """Get all stories in a specific story set."""
    stories = get_stories_by_set(set_id)
    if not stories:
        raise HTTPException(status_code=404, detail=f"Story set {set_id} not found or empty")

    return StoriesListResponse(
        date=stories[0].get('generated_at', '')[:10] if stories else '',
        generated_at=datetime.utcnow(),
        total=len(stories),
        stories=[_dict_to_story(s) for s in stories],
    )


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
