"""
Stage 1 — Candidate Normalization  (stage1_normalize.py)

Transforms raw crawler DB rows into a clean, typed, enriched candidate list
ready for allocation and selection. Emits a snapshot and a summary metrics log.

Key correctness points:
  - Uses crawler_reader.get_top_items() directly (NOT a custom query).
    The design section Stage 1 Step 1 SQL is wrong — do not use it.
  - _is_entertainment() reads from config.normalization — not hardcoded.
  - Reuse exclusion TraceRecords written before used items are filtered out.
  - Snapshot written after normalization, not from raw crawler output.
  - Trace JSONL opened in append mode ("a") — never "w".
  - frozenset and datetime serialized via snapshot.py custom encoder.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from db.crawler_reader import get_top_items, CRAWLER_DB_PATH
from db.models import get_used_urls_with_hotness, DB_PATH
from engine.selector.config import BatchConfig
from engine.selector.schemas import NormalizedCandidate, TraceRecord
from engine.selector.snapshot import cleanup_old_snapshots, save_snapshot
from engine.selector.trace import open_trace, write_trace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unified scoring constants (event_layer signal integration)
# ---------------------------------------------------------------------------

# Novelty bonus for 'new_development' events (follow-up on a known story).
# Applied multiplicatively to effective_hotness so these evolving stories
# rank above stale same-score events.
_NOVELTY_BONUS = 0.25       # +25% for a fresh angle on a known story

# Soft repetition penalty based on how recently the same event was told.
# Replaces the previous binary is_used=True filter for 'duplicate' events
# that are older than 1 day — allows re-surfacing if the pool is thin while
# suppressing recent spam.
_PENALTY_VERY_RECENT = 0.30   # < 1 day: essentially suppressed
_PENALTY_RECENT      = 0.60   # 1–3 days
_PENALTY_AGING       = 0.80   # 3–7 days
_PENALTY_NONE        = 1.00   # ≥ 7 days: no penalty (dedup window expired)


def _repetition_penalty(days_since: float) -> float:
    """
    Soft score multiplier for DUPLICATE events based on recency.
    Used only when decision == 'duplicate' and days_since >= 1.0.
    """
    if days_since < 1.0:
        return _PENALTY_VERY_RECENT
    elif days_since < 3.0:
        return _PENALTY_RECENT
    elif days_since < 7.0:
        return _PENALTY_AGING
    return _PENALTY_NONE


def _new_dev_repetition_factor(days_since: float) -> float:
    """
    Softer repetition factor for NEW_DEVELOPMENT events.

    new_development events are genuine new angles on a known story — they
    should always score AT LEAST as high as a fresh unrelated event.
    Net multiplier with novelty bonus: (1 + 0.25) × factor >= 1.0 for all tiers.

    Tiers (combined with _NOVELTY_BONUS = 0.25):
      < 1 day:   1.25 × 0.80 = 1.00  (no net penalty for same-day follow-up)
      1–3 days:  1.25 × 0.88 = 1.10  (slight lift — update is still fresh)
      3–7 days:  1.25 × 0.96 = 1.20  (stronger lift — story is maturing)
      ≥ 7 days:  1.25 × 1.00 = 1.25  (max novelty boost, dedup window almost done)
    """
    if days_since < 1.0:
        return 0.80
    elif days_since < 3.0:
        return 0.88
    elif days_since < 7.0:
        return 0.96
    return 1.00


# Path to the crawler's auto_keywords.json (used for keyword_map_sha).
# Derived relative to CRAWLER_DB_PATH so story_engine stays independent of
# the crawler's Python package structure.
_AUTO_KEYWORDS_PATH = (
    Path(CRAWLER_DB_PATH).parent / 'config' / 'auto_keywords.json'
    if CRAWLER_DB_PATH else None
)


def _compute_batch_metadata(config: BatchConfig) -> dict:
    """
    Build the (profile_id, keyword_map_sha) metadata block used by the
    snapshot envelope and the trace batch_metadata event.

    keyword_map_sha is the first 8 hex chars of the SHA-256 of the
    crawler's auto_keywords.json, matching the crawler worker's
    classification_version formula. On missing file, returns None for
    the sha — replay validation will degrade to a no-op for that field.
    """
    sha: str | None = None
    if _AUTO_KEYWORDS_PATH is not None and _AUTO_KEYWORDS_PATH.exists():
        try:
            content = _AUTO_KEYWORDS_PATH.read_bytes()
            sha = hashlib.sha256(content).hexdigest()[:8]
        except OSError as e:
            logger.warning(
                "Could not read auto_keywords.json for batch metadata: %s", e
            )
    return {
        "profile_id":      config.profile_id,
        "keyword_map_sha": sha,
    }

# ---------------------------------------------------------------------------
# Entertainment media filter — config-driven (reads from config.normalization)
# ---------------------------------------------------------------------------

_entertainment_pattern_cache: dict[str, re.Pattern] = {}


def _get_entertainment_pattern(regex: str) -> re.Pattern:
    """Compile and cache the entertainment detection regex."""
    if regex not in _entertainment_pattern_cache:
        _entertainment_pattern_cache[regex] = re.compile(regex, re.IGNORECASE)
    return _entertainment_pattern_cache[regex]

# Region display names (copied from run.py REGION_NAMES)
_REGION_NAMES: dict[str, str] = {
    'jp': 'Japan', 'kr': 'South Korea', 'cn': 'China', 'de': 'Germany',
    'fr': 'France', 'br': 'Brazil', 'es': 'Spain/Latin America',
    'in': 'India', 'ru': 'Russia', 'it': 'Italy', 'tr': 'Turkey',
    'ar': 'Arab World', 'id': 'Indonesia', 'pl': 'Poland',
    'nl': 'Netherlands', 'se': 'Sweden', 'ph': 'Philippines',
    'vn': 'Vietnam', 'th': 'Thailand', 'my': 'Malaysia',
    'pt': 'Portugal', 'ar_latam': 'Argentina',
}


def _derive_category(
    bucket: str,
    platform: str,
    topic_tags: list[str] | None,
) -> str:
    """
    Emergency fallback: derive a category from raw crawler fields when
    story_category is absent. This path should never fire in normal operation
    (crawler classifies all items before they reach the story engine).
    Monitored via emergency_derivation_count in Stage 1 metrics.

    Returns 'unknown' — downstream consumers must handle unknown gracefully.
    The category_derivation config block has been removed (dead code);
    this function is retained as a circuit-breaker only.
    """
    return 'unknown'


def _is_entertainment(title: str | None, platform: str, config) -> bool:
    """
    Return True if this item is entertainment media (anime PV, MV, Trailer, etc.)
    Reads from config.normalization.news_event_detection — not hardcoded.
    """
    ned = config.normalization.news_event_detection
    if platform not in ned.video_platforms:
        return False
    if not title:
        return False
    pattern = _get_entertainment_pattern(ned.title_block_regex)
    return bool(pattern.search(title))


def _parse_freshness(collected_at: str | None) -> datetime:
    """
    Parse collected_at string to UTC datetime.
    Falls back to epoch if unparseable.
    """
    if not collected_at:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        # SQLite datetime format: 'YYYY-MM-DD HH:MM:SS[.ffffff]'
        dt_str = collected_at.replace(' ', 'T')
        if '+' not in dt_str and 'Z' not in dt_str:
            dt_str += '+00:00'
        return datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def stage1_normalize(
    db_path: str,
    config: BatchConfig,
    format_ids: list[int],
    hours: int,
    batch_ts: int,
) -> list[NormalizedCandidate]:
    """
    Stage 1: normalize raw crawler items into typed NormalizedCandidate objects.

    Args:
        db_path:    Path to story_engine's db.sqlite3 (for used_urls lookup).
        config:     Loaded BatchConfig from story_mix.json.
        format_ids: List of integer format IDs included in this batch.
        hours:      Lookback window in hours.
        batch_ts:   UNIX milliseconds — used for trace file name and snapshot name.

    Returns:
        List of NormalizedCandidate objects with is_used==False only.
        Side effects: snapshot written, trace JSONL opened and partially written.
    """
    # Clean up old snapshots at run start
    cleanup_old_snapshots(db_path)

    # Determine logs directory alongside db.sqlite3
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'logs')

    # Compute batch metadata (profile_id + keyword_map_sha) for snapshot
    # envelope and trace header. This lets replay tooling detect when
    # upstream classifier state has moved since the batch was taken.
    _batch_metadata = _compute_batch_metadata(config)

    # Open trace handle in APPEND mode — Stage 3/4 will reuse the same file.
    # The metadata is written as a batch_metadata event on first open.
    trace_handle = open_trace(logs_dir, batch_ts, metadata=_batch_metadata)

    # Step 1 — Fetch raw items from crawler DB (category-aware)
    #
    # Derive the category allowlist from config.category_mix: any
    # category explicitly set to 0 is excluded from the fetch, any
    # category with target > 0 is included. If all categories are
    # non-zero (base profile / run1_legacy), no allowlist is passed and
    # the legacy single-global-fetch path is used.
    #
    # This is Step 1 Mode B (category-aware fetch) per design.md. Without
    # per-category fetch, focused profiles only see <30 business items
    # out of 1,400+ in a 48h window because high-hotness categories
    # (entertainment, politics) saturate the global top-500.
    fetch_allowed_categories: list[str] | None
    if config.category_mix and any(v == 0 for v in config.category_mix.values()):
        fetch_allowed_categories = sorted(
            c for c, v in config.category_mix.items() if v > 0
        )
    else:
        fetch_allowed_categories = None

    # Per-platform K is set to a large value to prevent low-hotness platforms
    # being excluded before cap filtering. In focused mode, LIMIT is applied
    # per category, so total fetched may be up to limit × N_categories.
    raw_items = get_top_items(
        limit=500,
        hours=hours,
        per_platform_k=50,
        allowed_categories=fetch_allowed_categories,
    )
    logger.info(
        "Stage 1: fetched %d raw items from crawler (hours=%d, allowlist=%s)",
        len(raw_items), hours,
        fetch_allowed_categories if fetch_allowed_categories else 'none',
    )

    # Step 2 — Pre-fetch used URLs for binary dedup
    used_urls: dict[str, float] = get_used_urls_with_hotness()
    used_url_set: set[str] = set(used_urls.keys())

    # Step 3 — Normalize each item
    all_candidates: list[NormalizedCandidate] = []
    known_categories = set(config.category_mix.keys())

    _emergency_derivation_count = 0

    for row in raw_items:
        # a. platform normalization — 4-step sequence per implementation plan
        # Step 1: lowercase + strip
        platform = (row.get('platform') or '').lower().strip()
        # Step 2: alias resolution
        platform = config.platform_aliases.get(platform, platform)
        # Step 3: hard excluded platforms
        if platform in config.hard_excluded_platforms:
            _url = row.get('url') or ''
            _trace_rec = TraceRecord(
                candidate_id      = _url,
                url               = _url,
                platform          = platform,
                category          = 'unknown',
                language          = (row.get('lang_group') or 'unknown').strip(),
                format_considered = None,
                selection_status  = 'excluded',
                rejection_reasons = ['hard_excluded_platform'],
                constraint_hits   = ['hard_excluded_platforms'],
                score             = 0.0,
                hotness           = 0.0,
                rank_inputs       = {},
                final_assignment  = None,
                batch_ts          = batch_ts,
            )
            write_trace(trace_handle, _trace_rec)
            continue
        # Step 4: group resolution — replace platform with group name if member
        for _group_name, _members in config.platform_groups.items():
            if platform in _members:
                platform = _group_name
                break

        # b. category — use story_category if crawler has classified the item.
        #    Fall back to _derive_category() only as emergency path (< 5% target).
        raw_bucket = (row.get('bucket') or '').lower().strip()
        story_category = row.get('story_category') or None

        if story_category:
            # Crawler owns classification — use it directly. No remapping.
            category = story_category
        else:
            # Emergency path: derive from topic_tags + bucket + platform.
            # This runs for items that slipped through (pending/failed items
            # that passed the classification_state filter in an edge case,
            # or items collected before the new schema was deployed).
            raw_topic_tags = row.get('topic_tags') or []
            if isinstance(raw_topic_tags, str):
                try:
                    import json as _json
                    raw_topic_tags = _json.loads(raw_topic_tags)
                except Exception:
                    raw_topic_tags = []
            category = _derive_category(raw_bucket, platform, raw_topic_tags)
            _emergency_derivation_count += 1

        # c. hotness
        hotness = float(row.get('hotness') or 0.0)

        # d. effective_hotness = hotness × topic_boost × platform_weight (3 factors)
        boost           = config.topic_boosts.get(category, 1.0)
        platform_weight = config.surface_weight_overrides.get(platform, 1.0)
        effective_hotness = hotness * boost * platform_weight

        # e. is_used — binary URL dedup
        url = row.get('url') or ''
        is_used = url in used_url_set

        # f. freshness — ti.collected_at
        freshness = _parse_freshness(row.get('collected_at'))

        # g. language — ti.lang_group
        language = (row.get('lang_group') or 'unknown').strip()

        # h. region fields
        region_key = row.get('region_key') or None
        region_name = (
            row.get('region_name')
            or _REGION_NAMES.get(region_key, region_key)
            if region_key else None
        )

        # i. engagement_signals — JSON string or dict
        raw_signals = row.get('engagement_signals')
        if isinstance(raw_signals, str):
            try:
                engagement_signals = json.loads(raw_signals)
            except (json.JSONDecodeError, TypeError):
                engagement_signals = {}
        elif isinstance(raw_signals, dict):
            engagement_signals = raw_signals
        else:
            engagement_signals = {}

        candidate = NormalizedCandidate(
            candidate_id         = url,   # URL is the stable unique key
            url                  = url,
            platform             = platform,
            category             = category,
            language             = language,
            hotness              = hotness,
            effective_hotness    = effective_hotness,
            freshness            = freshness,
            eligible_format_ids  = frozenset(),  # filled in Step 4
            crawler_item_id      = int(row.get('id') or 0),
            title_original       = row.get('title_original') or '',
            canonical_title      = row.get('canonical_title') or None,
            description_original = row.get('description_original') or None,
            region_key           = region_key,
            region_name          = region_name,
            engagement_signals   = engagement_signals,
            raw_payload          = row.get('raw_payload') or None,
            is_used              = is_used,
        )
        all_candidates.append(candidate)

    # Step 3b — URL deduplication (same URL can appear from multiple surfaces)
    # Keep the first occurrence per URL (get_top_items() returns ORDER BY hotness DESC
    # so the first occurrence is already the highest-hotness one for that URL).
    _seen_urls: set[str] = set()
    _deduped: list[NormalizedCandidate] = []
    for c in all_candidates:
        if c.candidate_id not in _seen_urls:
            _seen_urls.add(c.candidate_id)
            _deduped.append(c)
    if len(_deduped) < len(all_candidates):
        logger.info(
            "Stage 1: deduped %d → %d candidates (%d duplicate URLs removed)",
            len(all_candidates), len(_deduped), len(all_candidates) - len(_deduped),
        )
    all_candidates = _deduped

    # Step 3c — Event memory classification (three-way: duplicate / new_development / new_event)
    #
    # All signals from memory now flow into effective_hotness (unified scoring).
    #
    # duplicate (sim >= 0.35):
    #   Very recent (< 1 day) → hard filter (is_used=True). Same story, no time gap.
    #   Older (1–7 days)       → soft repetition_penalty on effective_hotness.
    #                            Allows re-surfacing only if pool is thin.
    #
    # new_development (0.10 <= sim < 0.35):
    #   novelty_bonus (+25%) rewards fresh angles on known stories.
    #   Mild repetition_penalty (0.8–1.0) avoids hammering the same angle daily.
    #
    # new_event (sim < 0.10):
    #   No action — full score, no penalty.
    #
    # Phase 1 uses Jaccard on titles; Phase 2 will use cosine on crawler embeddings.
    _emem_decisions: dict = {}
    try:
        from engine.event_layer.memory import classify_candidates
        _emem_decisions = classify_candidates(all_candidates, window_days=7)
        for c in all_candidates:
            decision_entry = _emem_decisions.get(c.url)
            if decision_entry is None:
                continue
            decision, prior_title, days_since = decision_entry
            if decision == 'duplicate':
                if days_since < 1.0 and not c.is_used:
                    # Hard filter: told < 1 day ago — never retell verbatim
                    c.is_used = True
                elif not c.is_used:
                    # Soft penalty: allow re-surfacing but ranked well below fresh events
                    penalty = _repetition_penalty(days_since)
                    c.effective_hotness *= penalty
                    logger.debug(
                        "event_memory duplicate (soft): url=%r days_since=%.1f penalty=%.2f",
                        c.url[:70], days_since, penalty,
                    )
            elif decision == 'new_development':
                c.is_new_development = True
                c.prior_story_title  = prior_title
                # Novelty bonus: promote follow-up angles above stale same-hotness events
                c.effective_hotness *= (1.0 + _NOVELTY_BONUS)
                # Softer repetition factor (never < 0.80): new angle, not a retelling.
                # Net multiplier always >= 1.0 so new developments outrank same-score
                # fresh events at every age tier. Uses _new_dev_repetition_factor,
                # NOT _repetition_penalty (which goes as low as 0.30 for duplicates).
                c.effective_hotness *= _new_dev_repetition_factor(days_since)
                logger.debug(
                    "event_memory new_development: url=%r days_since=%.1f "
                    "effective_hotness→%.1f",
                    c.url[:70], days_since, c.effective_hotness,
                )
    except Exception as _emem_exc:
        logger.warning("event_memory classification skipped (error): %s", _emem_exc)

    # Step 4 — Format eligibility tagging
    for candidate in all_candidates:
        eligible: set[int] = set()
        for format_id in format_ids:
            rule = config.format_eligibility.get(format_id)
            if rule is None:
                eligible.add(format_id)   # no restriction → eligible
                continue
            if rule.excluded_categories and candidate.category in rule.excluded_categories:
                continue                  # blocked by category exclusion
            if rule.requires_news_event and _is_entertainment(
                candidate.title_original or candidate.canonical_title,
                candidate.platform,
                config,
            ):
                continue                  # blocked by news-event requirement
            if rule.source_restricted_to == "comment_platforms":
                if candidate.platform not in config.comment_platforms:
                    continue              # blocked by source restriction
            eligible.add(format_id)
        candidate.eligible_format_ids = frozenset(eligible)

    # Step 4b — Profile category allowlist (hard filter)
    #
    # The profile's category_mix is used as an implicit allowlist: any
    # category with target > 0 is allowed, any category with target == 0
    # is HARD EXCLUDED. This is the enforcement mechanism for "focused"
    # profiles (business/finance tab, politics tab) so that when on-topic
    # candidates run out, the batch shrinks instead of filling with
    # unrelated content.
    #
    # The filter is a no-op when every category in category_mix has a
    # non-zero target (base profile / run1_legacy). When category_mix is
    # empty or missing, no filter is applied.
    allowed_categories: set[str] | None
    if config.category_mix:
        _zero_cats = {c for c, v in config.category_mix.items() if v == 0}
        if _zero_cats:
            allowed_categories = {c for c, v in config.category_mix.items() if v > 0}
        else:
            allowed_categories = None  # all non-zero → no filter
    else:
        allowed_categories = None      # missing → no filter

    if allowed_categories is not None:
        logger.info(
            "Stage 1: profile category allowlist active: %s (excluded: %s)",
            sorted(allowed_categories),
            sorted({c for c, v in config.category_mix.items() if v == 0}),
        )

    # Step 5 — Filter used items and category-disallowed items
    #          + emit exclusion traces
    available: list[NormalizedCandidate] = []
    excluded_count = 0
    excluded_by_category_count = 0

    for candidate in all_candidates:
        # 5a. Reuse / used-item exclusion (emitted first — highest priority)
        if candidate.is_used:
            excluded_count += 1
            # Distinguish URL reuse from event-memory semantic dedup in trace
            is_event_dup = (
                _emem_decisions.get(candidate.url, (None,))[0] == 'duplicate'
            )
            trace = TraceRecord(
                candidate_id      = candidate.candidate_id,
                url               = candidate.url,
                platform          = candidate.platform,
                category          = candidate.category,
                language          = candidate.language,
                format_considered = None,
                selection_status  = "excluded",
                rejection_reasons = ["event_memory_duplicate" if is_event_dup else "used_item"],
                constraint_hits   = ["reuse_policy:event_memory" if is_event_dup else "reuse_policy:binary_url"],
                score             = candidate.effective_hotness,
                hotness           = candidate.hotness,
                rank_inputs       = {},
                final_assignment  = None,
                batch_ts          = batch_ts,
            )
            write_trace(trace_handle, trace)
            continue

        # 5b. Profile category allowlist exclusion
        if allowed_categories is not None and candidate.category not in allowed_categories:
            excluded_by_category_count += 1
            trace = TraceRecord(
                candidate_id      = candidate.candidate_id,
                url               = candidate.url,
                platform          = candidate.platform,
                category          = candidate.category,
                language          = candidate.language,
                format_considered = None,
                selection_status  = "excluded",
                rejection_reasons = ["category_not_in_profile_allowlist"],
                constraint_hits   = ["soft_targets.category_mix:zero_target"],
                score             = candidate.effective_hotness,
                hotness           = candidate.hotness,
                rank_inputs       = {},
                final_assignment  = None,
                batch_ts          = batch_ts,
            )
            write_trace(trace_handle, trace)
            continue

        available.append(candidate)

    if excluded_by_category_count:
        logger.info(
            "Stage 1: category allowlist excluded %d candidates",
            excluded_by_category_count,
        )

    # Step 6 — Emit Stage 1 summary metrics
    by_platform: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_language: dict[str, int] = {}
    eligible_per_format: dict[int, int] = {fid: 0 for fid in format_ids}

    for c in available:
        by_platform[c.platform] = by_platform.get(c.platform, 0) + 1
        by_category[c.category] = by_category.get(c.category, 0) + 1
        by_language[c.language] = by_language.get(c.language, 0) + 1
        for fid in c.eligible_format_ids:
            if fid in eligible_per_format:
                eligible_per_format[fid] += 1

    total_ingested = len(all_candidates)
    emergency_rate = _emergency_derivation_count / total_ingested if total_ingested else 0.0

    metrics = {
        "stage":                      1,
        "batch_ts":                   batch_ts,
        "total_ingested":             len(all_candidates),
        "excluded_by_reuse":          excluded_count,
        "excluded_by_category":       excluded_by_category_count,
        "available":                  len(available),
        "by_platform":                by_platform,
        "by_category":                by_category,
        "by_language":                by_language,
        "eligible_per_format":        eligible_per_format,
        "emergency_derivation_count": _emergency_derivation_count,
        "emergency_derivation_rate":  round(emergency_rate, 4),
        "profile_allowed_categories": (
            sorted(allowed_categories) if allowed_categories is not None else None
        ),
    }
    logger.info("Stage 1 summary: %s", json.dumps(metrics, ensure_ascii=False))
    if emergency_rate > 0.05:
        logger.warning(
            "Stage 1: emergency category derivation rate %.1f%% exceeds 5%% threshold — "
            "crawler classification pipeline may be degraded",
            emergency_rate * 100,
        )
    else:
        logger.info(
            "Stage 1: emergency category derivation rate %.1f%% (within 5%% threshold)",
            emergency_rate * 100,
        )

    # Step 7 — Write snapshot (available candidates only, used excluded)
    snap_path = save_snapshot(available, db_path, batch_ts, metadata=_batch_metadata)

    # Store trace handle on module level for Stage 3/4 to retrieve
    # (passed via the returned candidates list is not possible cleanly;
    #  we store it on the module so the orchestrator can pass it to Stage 4)
    # NOTE: the orchestrator (__init__.py) must close the handle after Stage 4.
    _store_trace_handle(batch_ts, trace_handle, logs_dir)

    logger.info(
        "Stage 1 complete: %d available candidates, %d excluded by reuse, snapshot=%s",
        len(available), excluded_count, snap_path,
    )
    return available


# ---------------------------------------------------------------------------
# Trace handle registry — allows Stage 3 and Stage 4 to append to the same file
# ---------------------------------------------------------------------------

_trace_handles: dict[int, tuple] = {}  # batch_ts → (handle, logs_dir)


def _store_trace_handle(batch_ts: int, handle, logs_dir: str) -> None:
    _trace_handles[batch_ts] = (handle, logs_dir)


def get_trace_handle(batch_ts: int):
    """Retrieve the open trace handle for a given batch. Used by Stage 3 and 4."""
    entry = _trace_handles.get(batch_ts)
    if entry is None:
        raise RuntimeError(
            f"No trace handle registered for batch_ts={batch_ts}. "
            "Ensure stage1_normalize() was called before accessing the trace handle."
        )
    return entry[0]


def get_trace_path(batch_ts: int, db_path: str) -> str:
    """Return the path to the trace JSONL file for this batch."""
    entry = _trace_handles.get(batch_ts)
    if entry:
        logs_dir = entry[1]
    else:
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'logs')
    return os.path.join(logs_dir, f"trace_{batch_ts}.jsonl")


def close_trace_handle(batch_ts: int) -> None:
    """Close and remove the trace handle after Stage 4 completes."""
    entry = _trace_handles.pop(batch_ts, None)
    if entry:
        try:
            entry[0].close()
        except Exception:
            pass
