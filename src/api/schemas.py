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
    subscriber_count:      Optional[int]   # Data API; authoritative unless hidden
    # True only when the owner hid the public count (Data API then omits it).
    # Defaults to False so rows predating the migration still parse.
    subscriber_count_hidden: Optional[bool] = False
    real_subscriber_count: Optional[int]   # Analytics fallback; lags ~2-3 days
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
    lang:              Optional[str]    # 'en' = KataGo playlist, 'zh' = Go Chinese playlist
    is_famous:         Optional[int]    # 1 = famous game, 0 = ai_selfplay
    ab_variant:        Optional[str]    # 'male-en' | 'female-en' | 'male-zh' | 'female-zh'
    fetched_at:        Optional[str]    # ISO datetime of last fetch
    comments:          list[GamesComment] = []


class GamesCountryRow(BaseModel):
    """Viewer-country breakdown for the KataGo channel (GET /api/games/audience-countries)."""
    country:    str            # ISO 3166-1 alpha-2, e.g. "US", "TW"
    views:      int
    fetched_at: Optional[str]  # ISO datetime of last refresh


class GamesSubtitleRow(BaseModel):
    """CC/subtitle language breakdown for the KataGo channel (GET /api/games/subtitle-langs)."""
    lang:       str            # e.g. "zh-Hans", "en", "" = subtitles off
    views:      int
    fetched_at: Optional[str]


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
    traffic_sources:     Optional[dict]     # {"YT_SEARCH": 42, "SUGGESTED_VIDEOS": 31, ...} — was fetched
                                             # since fetch_analytics.py's first version but never surfaced
    watch_time_hours:    Optional[float]    # estimatedMinutesWatched/60
    shares:              Optional[int]
    subscribers_gained:  Optional[int]
    dislikes:            Optional[int]      # Analytics API owner-only estimate
    has_retention_curve: bool = False       # true if youtube_video_retention_curve has rows for this video


class AudienceDimensionRow(BaseModel):
    """One breakdown row within a channel Audience-tab dimension."""
    dim_key:      str      # e.g. 'US', 'age25-34|male', 'MOBILE', 'ANDROID', 'WATCH'
    metric_value: float    # views (count) for most dimensions, viewer % for age_gender


class ChannelAudienceSnapshot(BaseModel):
    """Channel-level Audience-tab snapshot for one profile (en|zh), grouped by dimension."""
    upload_profile: str
    fetched_at:     Optional[str]                            # ISO datetime of the snapshot, None if never fetched
    country:            list[AudienceDimensionRow] = []
    age_gender:          list[AudienceDimensionRow] = []
    device:              list[AudienceDimensionRow] = []
    os:                  list[AudienceDimensionRow] = []
    playback_location:   list[AudienceDimensionRow] = []


class RetentionCurvePoint(BaseModel):
    """One point on a video's audience retention curve."""
    elapsed_video_time_pct: float             # 0.00-1.00, position in the video
    audience_watch_ratio:   Optional[float]   # fraction of viewers still watching
    relative_performance:   Optional[float]   # vs similar-length YouTube videos; >0 = better than typical


class VideoRetentionCurve(BaseModel):
    """Full retention curve for one video."""
    video_id: str
    points:   list[RetentionCurvePoint]


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


# ---------------------------------------------------------------------------
# Comment-questions review  (GET /api/games/comment-questions)
# ---------------------------------------------------------------------------

class WinrateStep(BaseModel):
    """One row of whatif.py output: a single move and the resulting winrate."""
    color:    str           # "Black" | "White"
    move:     str           # GTP coordinate, e.g. "Q8"
    winrate:  float         # Black win% after this move
    score:    float         # score lead (positive = Black ahead)


class WinrateResult(BaseModel):
    """Parsed result_json from comment_questions, produced by whatif.py."""
    fork_winrate: float             # Black win% at the fork (before hypothetical)
    fork_score:   float             # Score lead at fork
    steps:        list[WinrateStep] # Each hypothetical move in sequence


class LifeDeathResult(BaseModel):
    """Parsed result_json for a dead/live question (kind='life_death')."""
    status:           str                    # 'alive' | 'dead' | 'unsettled'
    target_color:     str                    # 'B' | 'W'
    group_anchor_gtp: str                    # a representative point of the group, e.g. "S2"
    group_size:       int                    # number of stones in the group
    ownership_avg:    float                  # avg ownership, group's-color perspective [-1,1]
    confidence:       float                  # 0..1
    at_move:          Optional[int]   = None # position analysed at
    whatif_moves:     Optional[str]   = ""   # hypothetical moves played before the read
    resolved_by:      Optional[str]   = None # 'anchor' | 'nearest_move' | 'region'


class CommentQuestion(BaseModel):
    """One analyzed comment question (whatif or life_death), ready for human review."""
    id:            int
    comment_id:    str
    comment_text:  str              # original viewer comment (from game_comments.text)
    author:        Optional[str]    # commenter name
    like_count:    int
    at_move:       Optional[int]    # whatif.py --at param (may be null for life_death)
    whatif_moves:  str              # whatif.py --moves param, e.g. "Q8 Q9"
    visits:        int
    kind:          str = "whatif"           # "whatif" | "life_death"
    result:        Optional[WinrateResult]    = None   # set when kind='whatif'
    life_death:    Optional[LifeDeathResult]  = None   # set when kind='life_death'
    reply_preview: Optional[str]    = None   # exact localized reply that will be posted
    status:        str              # 'analyzed' | 'approved' | 'skipped'


class VideoWithCommentQuestions(BaseModel):
    """A KataGo video together with its analyzed comment questions."""
    video_db_id:  int
    video_id:     str               # YouTube video ID
    title:        Optional[str]
    published_at: Optional[int]     # UNIX timestamp
    questions:    list[CommentQuestion]
