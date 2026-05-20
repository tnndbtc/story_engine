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
    likes: int = 0          # optional — not all platforms provide like counts
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

# Formats 1-9 (legacy names) + formats 10-46 (format_N)
FormatType = str  # accepts any format string: 'explainer', 'top5', ..., 'format_10', ..., 'format_46'

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
    token_estimate: Optional[int] = None   # chars/4 proxy; only set for deep_story format


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
    token_estimate: Optional[int] = None   # chars/4 proxy; only set for deep_story format
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
# Story set (GET /api/story-sets)
# ---------------------------------------------------------------------------

class StorySetSummary(BaseModel):
    id: int
    batch_ts: str
    lang: str
    channel: int
    status: str
    story_count: int
    profile_id: Optional[str] = None  # per-run overlay id, e.g. "run2_ai"


# ---------------------------------------------------------------------------
# YouTube Analytics (GET /api/analytics/story-set/{story_set_id})
# ---------------------------------------------------------------------------

class YoutubeAnalyticRow(BaseModel):
    """One row from youtube_publish_log for a published video."""
    video_id:            str
    lang:                str                 # 'en' or 'zh'
    locale:              str                 # 'en-US' or 'zh-Hans'
    views:               Optional[int]       # None = not yet fetched
    avg_view_duration:   Optional[float]     # seconds; None = not yet fetched
    avg_view_pct:        Optional[float]     # %; None = not yet fetched
    ctr_pct:             Optional[float]     # %; None = not monetized or pending
    published_at:        Optional[str]       # ISO datetime string
    analytics_pulled_at: Optional[str]       # ISO string | 'no_data' | None (pending)


# ---------------------------------------------------------------------------
# YouTube Subscribers (GET /api/subscribers)
# ---------------------------------------------------------------------------

class YoutubeSubscriberPlaylist(BaseModel):
    """One public playlist belonging to a subscriber."""
    id:         str
    title:      str
    item_count: int
    created_at: Optional[str]   # ISO datetime string from YouTube


class YoutubeSubscribedChannel(BaseModel):
    """One of our channels that a subscriber follows."""
    profile:       str            # profile key, e.g. "en" or "zh"
    channel_id:    str            # our YouTube channel ID (UCxxx)
    channel_name:  str            # our channel's display name
    subscribed_at: Optional[str]  # ISO datetime when they subscribed


class YoutubeSubscriber(BaseModel):
    """One row from youtube_subscribers — a public subscriber to one of our channels."""
    channel_id:       str
    display_name:     str
    description:      Optional[str]
    country:          Optional[str]
    account_created:  Optional[str]   # ISO datetime string
    subscriber_count: Optional[int]   # their own channel's subscriber count
    video_count:      Optional[int]   # their own channel's uploaded video count
    view_count:       Optional[int]   # their own channel's total view count
    subscribed_to:    list[YoutubeSubscribedChannel]  # our channels they follow
    public_playlists: list[YoutubeSubscriberPlaylist]
    fetched_at:       Optional[str]   # ISO datetime of last refresh


# ---------------------------------------------------------------------------
# YouTube Video Comments (GET /api/comments)
# ---------------------------------------------------------------------------

class VideoComment(BaseModel):
    """One viewer comment on a published YouTube video."""
    comment_id:        str
    author_name:       Optional[str]
    author_channel_id: Optional[str]
    text:              str
    like_count:        int
    published_at:      Optional[str]   # ISO datetime string from YouTube


class StoryWithComments(BaseModel):
    """A published story with its viewer comments."""
    video_id:       str
    lang:           str                 # 'en' | 'zh'
    upload_profile: str
    story_set_id:   Optional[int]
    story_title:    Optional[str]
    published_at:   Optional[str]       # ISO datetime
    comments:       list[VideoComment]


# ---------------------------------------------------------------------------
# Games / KataGo channel analytics (GET /api/games/channel-stats, GET /api/games/videos)
# ---------------------------------------------------------------------------

class GamesChannelStats(BaseModel):
    """Channel-level stats for the KataGo/games YouTube channel."""
    channel_id:            str
    channel_name:          Optional[str]
    subscriber_count:      Optional[int]   # 0 when hidden by YouTube (<1K)
    real_subscriber_count: Optional[int]   # exact count via Analytics API
    video_count:           Optional[int]
    view_count:            Optional[int]
    fetched_at:            Optional[str]   # ISO datetime of last refresh


class GamesComment(BaseModel):
    """One viewer comment on a KataGo video."""
    comment_id:        str
    author_name:       Optional[str]
    author_channel_id: Optional[str]
    text:              str
    like_count:        int
    published_at:      Optional[str]   # ISO datetime


class GamesVideoRow(BaseModel):
    """One published KataGo video with its YouTube stats."""
    video_id:          str
    title:             Optional[str]
    published_at:      Optional[str]    # ISO datetime
    views:             Optional[int]
    likes:             Optional[int]
    comment_count:     Optional[int]
    avg_view_duration: Optional[float]  # seconds; None until Analytics API data arrives
    avg_view_pct:      Optional[float]  # %; None until Analytics API data arrives
    fetched_at:        Optional[str]    # ISO datetime of last fetch
    comments:          list[GamesComment] = []


# ---------------------------------------------------------------------------
# Story-engine channel analytics  (GET /api/analytics/channel?lang=en|zh)
# ---------------------------------------------------------------------------

class ChannelVideoRow(BaseModel):
    """One published deep-story video for the EN or ZH channel."""
    video_id:            str
    lang:                str                # 'en' | 'zh'
    story_set_id:        Optional[int]
    title:               Optional[str]      # from hierarchical_stories.deep_story $.title
    profile_id:          Optional[str]      # run2_ai | run3_world | … (category)
    published_at:        Optional[str]      # ISO datetime
    views:               Optional[int]
    avg_view_duration:   Optional[float]    # seconds
    avg_view_pct:        Optional[float]    # %
    like_count:          Optional[int]
    comment_count:       Optional[int]
    analytics_pulled_at: Optional[str]      # None=pending, 'no_data'=gave up, ISO=fetched


# ---------------------------------------------------------------------------
# Engine status (GET /api/status)
# ---------------------------------------------------------------------------

class EngineStatus(BaseModel):
    scheduler: Literal["cron"]          # always cron (shell script)
    last_run_at: Optional[datetime]
    last_run_status: Optional[Literal["success", "failed", "partial"]]
    stories_today: int
    crawler_db_url: str          # password redacted
    crawler_db_reachable: bool
