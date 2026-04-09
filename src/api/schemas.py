"""
story_engine API contract — Pydantic schemas.

These schemas define the exact JSON shape returned by every endpoint.
trend_ui must be built against these types.
story_engine storage (models.py) must write fields that map to these types.
"""

from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, computed_field


# ---------------------------------------------------------------------------
# Sub-objects
# ---------------------------------------------------------------------------

class SourceItem(BaseModel):
    """One crawled item used as a source for this story."""
    url: str
    platform: str           # reddit / hackernews / youtube / ap_news / etc.
    hotness: float
    title: str              # title_original or canonical_title from crawler DB


class CommentItem(BaseModel):
    """One comment fetched on-demand during enrichment."""
    text: str
    likes: int
    platform: str           # reddit / hackernews / youtube


class Script(BaseModel):
    """
    The generated script body.
    hook + bullets + twist are stored in SQLite.
    full_text is assembled at read time — never stored separately.
    """
    hook: str
    bullets: list[str]
    twist: str

    @computed_field
    @property
    def full_text(self) -> str:
        """Assembled at read time: hook + bullets (joined) + twist."""
        parts = [self.hook] + self.bullets + [self.twist]
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Format and channel literals
# ---------------------------------------------------------------------------

FormatType = Literal[
    "explainer",    # Format A — 60-sec explainer
    "top5",         # Format B — Top 5 today
    "radar",        # Format C — stories US media ignores
    "regional",     # Format D — what region X is saying
    "two_takes",    # Format E — two completely different takes
    "pattern",      # Format F — pattern/trend analysis
    "viral",        # Format G — before it goes viral
    "deep_dive",    # Format H — weekly deep dive
    "niche",        # Format I — niche focus (tech/finance)
]

ChannelType = Literal[1, 2, 3]
LangType = Literal["en", "zh"]
StatusType = Literal["generating", "ready", "failed"]


# ---------------------------------------------------------------------------
# Story card (list view — no full script)
# ---------------------------------------------------------------------------

class StoryCard(BaseModel):
    """Returned in list endpoints. No script body — just metadata."""
    id: int
    title: str
    format: FormatType
    channel: ChannelType
    lang: LangType
    status: StatusType
    generated_at: Optional[datetime]
    sources_count: int


# ---------------------------------------------------------------------------
# Full story (detail view)
# ---------------------------------------------------------------------------

class Story(BaseModel):
    """Returned by GET /api/stories/{id}. Includes full script + sources."""
    id: int
    title: str
    format: FormatType
    channel: ChannelType
    lang: LangType
    status: StatusType
    generated_at: Optional[datetime]
    sources_count: int
    script: Script
    sources: list[SourceItem]
    comments_used: list[CommentItem]


# ---------------------------------------------------------------------------
# List response (GET /api/stories and GET /api/stories/today)
# ---------------------------------------------------------------------------

class StoriesListResponse(BaseModel):
    date: str               # YYYY-MM-DD
    generated_at: datetime  # when this batch was produced
    total: int
    stories: list[Story]


# ---------------------------------------------------------------------------
# Job status (POST /api/generate → GET /api/jobs/{job_id})
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    format: FormatType
    lang: LangType = "en"
    topic_hint: Optional[str] = None    # optional seed topic; engine may ignore


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "complete", "failed"]
    format: FormatType
    lang: LangType
    story_id: Optional[int] = None      # set when status == "complete"
    error: Optional[str] = None         # set when status == "failed"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Engine status (GET /api/status)
# ---------------------------------------------------------------------------

class EngineStatus(BaseModel):
    scheduler: Literal["cron"]          # always cron (shell script)
    last_run_at: Optional[datetime]
    last_run_status: Optional[Literal["success", "failed", "partial"]]
    stories_today: int
    crawler_db_path: str
    crawler_db_reachable: bool
