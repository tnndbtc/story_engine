"""
Story selector — picks candidate items from crawler DB per format.

Each format has its own selection logic (NOT a shared global top-N):
  - Format 1 (explainer): single top item by hotness
  - Format 2 (top5): top 5 with platform diversity
  - Format 3 (radar): stories US media ignores
  - Format 4 (regional): what region X is saying
  - Format 5 (two_takes): framing contrast
  - Format 6 (pattern): cross-region pattern detection
  - Format 7 (viral): early signals from niche platforms
  - Format 8 (deep_dive): weekly deep dive
  - Format 9 (niche): niche focus (tech/finance)

The selector only reads the crawler DB. It never writes.
All selectors apply _filter_already_used() to avoid reusing items across story sets.
"""

import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

from db.crawler_reader import (
    get_top_items,
    get_diverse_top_items,
    get_early_signals,
    get_regional_items,
    get_known_surface_keys,
)
from db.models import get_used_urls_with_hotness

logger = logging.getLogger(__name__)

HOTNESS_REGAIN_FACTOR = 1.3  # item must be 30% hotter to be re-eligible

STORY_MIX_PATH = os.environ.get(
    'STORY_MIX_PATH',
    str(Path(__file__).resolve().parent.parent.parent / 'story_mix.json')
)

# ---------------------------------------------------------------------------
# Defaults — used when story_mix.json is absent or malformed.
# category_sources replaces the former hardcoded CATEGORY_MAPPING constant;
# it is now the authoritative config, read from story_mix.json at runtime.
# ---------------------------------------------------------------------------

DEFAULT_CATEGORY_MIX: dict[str, float] = {
    'tech':          0.20,
    'news':          0.15,
    'politics':      0.10,
    'finance':       0.08,
    'ai':            0.07,
    'regional':      0.15,
    'entertainment': 0.10,
    'social':        0.05,
    'science':       0.05,
    'business':      0.05,
}

DEFAULT_CATEGORY_SOURCES: dict[str, dict] = {
    'tech':          {'buckets': ['category_tech', 'rising'],
                      'platforms': ['hackernews', 'devto', 'lobsters', 'github',
                                    'paperswithcode', 'stackoverflow', 'v2ex']},
    'news':          {'buckets': ['news'], 'platforms': None},
    'politics':      {'buckets': ['category_politics', 'news'], 'platforms': None,
                      'prefer_topics': ['politics']},
    'finance':       {'buckets': ['category_finance', 'news'], 'platforms': None,
                      'prefer_topics': ['finance']},
    'ai':            {'buckets': ['category_tech', 'rising'],
                      'platforms': ['hackernews', 'paperswithcode', 'arxiv_ai_rss', 'devto'],
                      'prefer_topics': ['ai']},
    'regional':      {'buckets': None, 'platforms': None, 'exclude_region': 'us'},
    'entertainment': {'buckets': ['category_entertainment'],
                      'platforms': ['youtube', 'bilibili', 'nicovideo']},
    'social':        {'buckets': ['hot_now'], 'platforms': ['reddit', 'weibo', 'baidu']},
    'science':       {'buckets': None, 'platforms': ['paperswithcode', 'arxiv_ai_rss']},
    'business':      {'buckets': ['news'], 'platforms': None,
                      'prefer_topics': ['business']},
}

# One-time startup flag — surface key validation runs once per process lifetime
_surface_key_check_done = False

# ---------------------------------------------------------------------------
# Entertainment content filter — for formats that require real news events
# ---------------------------------------------------------------------------
_ENTERTAINMENT_PLATFORMS = frozenset({'bilibili', 'youtube', 'nicovideo'})
_ENTERTAINMENT_PATTERN = re.compile(
    r'动画|原创动画|概念PV|角色PV|角色短片|官方MV|主题曲|片头曲'
    r'|[Oo]fficial\s*[Vv]ideo|[Oo]fficial\s*[Mm][Vv]|[Tt]railer|[Cc]oncept\s*[Pp][Vv]'
    r'|\bPV\b|\bMV\b'
)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_story_mix_config() -> dict:
    """
    Load the full story_mix.json config.

    Returns a dict with keys:
      category_mix, category_sources, platform_caps,
      topic_boosts, surface_weight_overrides, platform_default_weights,
      min_slots_per_format.

    Falls back to defaults on missing file or parse error.
    On first call, validates surface_weight_overrides keys against DB and
    logs warnings for any that don't match a real TrendSurface.key.
    """
    global _surface_key_check_done

    defaults = {
        'category_mix':             DEFAULT_CATEGORY_MIX,
        'category_sources':         DEFAULT_CATEGORY_SOURCES,
        'platform_caps':            {},
        'topic_boosts':             {},
        'surface_weight_overrides': {},
        'platform_default_weights': {},
        'min_slots_per_format':     {},
    }

    if not os.path.exists(STORY_MIX_PATH):
        return defaults

    try:
        with open(STORY_MIX_PATH) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse story_mix.json: {e} — using defaults")
        return defaults

    config = {
        'category_mix':             data.get('category_mix', DEFAULT_CATEGORY_MIX),
        'category_sources':         data.get('category_sources', DEFAULT_CATEGORY_SOURCES),
        'platform_caps':            data.get('platform_caps', {}),
        'topic_boosts':             data.get('topic_boosts', {}),
        'surface_weight_overrides': data.get('surface_weight_overrides', {}),
        'platform_default_weights': data.get('platform_default_weights', {}),
        'min_slots_per_format':     data.get('min_slots_per_format', {}),
    }

    total = sum(config['category_mix'].values())
    if abs(total - 1.0) > 0.01:
        logger.warning(f"category_mix ratios sum to {total:.2f}, expected 1.0")

    # One-time validation: warn for surface keys not present in DB
    if not _surface_key_check_done and config['surface_weight_overrides']:
        _surface_key_check_done = True
        try:
            known = get_known_surface_keys()
            for key in config['surface_weight_overrides']:
                if key not in known:
                    logger.warning(
                        f"surface_weight_overrides key '{key}' has no matching "
                        f"TrendSurface.key in DB — override will be ignored. "
                        f"Run: SELECT DISTINCT key FROM crawler_admin_trendsurface "
                        f"WHERE enabled=1"
                    )
        except Exception as e:
            logger.warning(f"Could not verify surface_weight_overrides keys: {e}")

    return config


def load_category_mix() -> dict[str, float]:
    """Backward-compatible wrapper — returns just the category_mix ratios."""
    return load_story_mix_config()['category_mix']


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------

def _apply_topic_boost(
    items: list[dict],
    topic_boosts: dict[str, float],
    surface_weight_overrides: dict[str, float],
    platform_default_weights: dict[str, float] | None = None,
) -> list[dict]:
    """
    Apply surface weight and topic boost multipliers to item hotness scores.

    Both multipliers are combined into a single bounded effective_multiplier
    to prevent compounding instability. Bounds: [0.3, 3.0].

    - surface_weight_overrides: keyed by TrendSurface.key (surface_key field)
    - topic_boosts: keyed by topic label (e.g. "politics", "finance")
    - platform_default_weights: fallback tier — applied when a surface key is
      not in surface_weight_overrides but its platform has a default weight.
      Prevents new surfaces on high-volume platforms (reddit, bilibili) from
      defaulting to 1.0 until compute_weights.py adds them explicitly.
    - Items without topic_tags get topic_mult=1.0 — safe no-op.

    Surface weight lookup priority (three-tier):
      1. surface_weight_overrides[surface_key]  — explicit per-surface
      2. platform_default_weights[platform]     — platform-level fallback
      3. item['selection_weight']               — DB admin field (default 1.0)

    topic_mult is the PRODUCT of all matching tag boosts (not the max), so an
    item tagged ["politics", "finance"] gets 1.5 × 1.4 = 2.1× before clamping.

    Sorted by effective_hotness descending.
    Original item["hotness"] is preserved — UsedItem.hotness_at_use must use
    item["hotness"] (original), never item["effective_hotness"].
    """
    if platform_default_weights is None:
        platform_default_weights = {}

    if not topic_boosts and not surface_weight_overrides and not platform_default_weights:
        return items

    for item in items:
        tags = item.get('topic_tags') or []
        # Multiply boosts across all matching tags (not max) so multi-topic
        # items are rewarded proportionally.
        topic_mult = 1.0
        for tag in tags:
            topic_mult *= topic_boosts.get(tag, 1.0)

        # Three-tier surface weight lookup:
        # 1. explicit per-surface override (story_mix.json)
        # 2. platform-level fallback (platform_default_weights in story_mix.json)
        # 3. DB admin field (TrendSurface.selection_weight, default 1.0)
        surface_key = item.get('surface_key', '')
        platform = item.get('platform', '')
        if surface_key in surface_weight_overrides:
            surface_mult = surface_weight_overrides[surface_key]
        elif platform in platform_default_weights:
            surface_mult = platform_default_weights[platform]
        else:
            surface_mult = item.get('selection_weight', 1.0)

        # Bound the combined multiplier to prevent runaway stacking
        effective_multiplier = max(0.3, min(surface_mult * topic_mult, 3.0))
        item['effective_hotness'] = item['hotness'] * effective_multiplier

    return sorted(
        items,
        key=lambda x: x.get('effective_hotness', x.get('hotness', 0)),
        reverse=True,
    )


def _enforce_platform_caps(
    items: list[dict],
    caps: dict[str, float],
    total_slots: int,
) -> list[dict]:
    """
    Enforce per-platform caps as a fraction of total_slots.

    Items from a platform that exceed their cap are moved to overflow and
    used only to backfill remaining slots after all caps are respected.
    Ensures no single high-volume platform dominates the final selection.

    Hard-excluded platforms (cap rounds to 0 for this total_slots value) are
    never used for backfill — they are excluded completely regardless of how
    many slots remain unfilled. This prevents platforms with cap=0.15 from
    sneaking back in when total_slots is small (e.g. 5 slots: 0.15×5=0.75→0).

    Min-supply guard: if hard exclusions leave fewer than min(3, total_slots)
    items, caps are relaxed and a warning is logged so the system stays
    functional even with very sparse data.

    Example: bilibili cap=0.15, total_slots=5 → int(0.75)=0 → hard excluded.
    Example: reddit cap=0.15, total_slots=10 → int(1.5)=1 → max 1 item.
    """
    if not caps:
        return items

    # Pre-compute hard-excluded platforms: those whose cap rounds to 0 slots.
    hard_excluded: set[str] = {
        platform for platform, frac in caps.items()
        if int(total_slots * frac) == 0
    }

    platform_counts: dict[str, int] = defaultdict(int)
    result = []
    overflow = []

    for item in items:
        platform = item.get('platform', '')
        cap_frac = caps.get(platform)
        if cap_frac is not None:
            max_allowed = int(total_slots * cap_frac)
            if platform_counts[platform] >= max_allowed:
                overflow.append(item)
                continue
        platform_counts[platform] += 1
        result.append(item)

    # Backfill: only non-hard-excluded platforms may fill remaining slots.
    for item in overflow:
        if len(result) >= total_slots:
            break
        if item.get('platform', '') not in hard_excluded:
            result.append(item)

    # Min-supply guard: if strict caps produce too few items, relax and warn.
    min_supply = min(3, total_slots)
    if len(result) < min_supply:
        logger.warning(
            f"_enforce_platform_caps: only {len(result)} items after caps "
            f"(hard_excluded={hard_excluded}), need {min_supply} — relaxing caps"
        )
        seen_urls = {i['url'] for i in result}
        for item in overflow:
            if len(result) >= min_supply:
                break
            if item['url'] not in seen_urls:
                result.append(item)
                seen_urls.add(item['url'])

    return result[:total_slots]


def _fill_min_slots(
    candidates_by_category: dict[str, list[dict]],
    min_slots: dict[str, int],
    seen_urls: set[str],
) -> list[dict]:
    """
    Guarantee at least N items from each required category (Phase 3).

    Pulls items from candidates_by_category in hotness order; skips any
    URL already in seen_urls (dedup). Adds guaranteed items to seen_urls
    so the caller's proportional fill does not double-count them.

    Logs a warning if a category pool is too small to satisfy its quota.
    """
    guaranteed = []
    got: dict[str, int] = {}

    for category, n_needed in min_slots.items():
        pool = candidates_by_category.get(category, [])
        added = 0
        for item in pool:
            if item['url'] not in seen_urls and added < n_needed:
                guaranteed.append(item)
                seen_urls.add(item['url'])
                added += 1
        got[category] = added

    for category, n_needed in min_slots.items():
        if got.get(category, 0) < n_needed:
            logger.warning(
                f"min_slots not fully satisfied: '{category}' "
                f"wanted {n_needed}, got {got.get(category, 0)} "
                f"— sparse data or all items already used"
            )

    return guaranteed


def _select_by_mix(
    total_needed: int,
    hours: int = 24,
    format_name: str | None = None,
) -> list[dict]:
    """
    Select items across categories according to story_mix.json ratios.

    Category sources (bucket + platform filters) are read from story_mix.json
    category_sources — no longer hardcoded. Surface weights and topic boosts
    are applied after fetching to re-rank candidates within each category.

    Constraint priority order (Phase 3):
      1. min_slots (guaranteed category slots — inserted first)
      2. platform_caps (hard ceiling — applied after all categories assembled)
      3. category_mix  (soft target — proportional slot allocation per category)

    Args:
        format_name: Format key for looking up min_slots_per_format in story_mix.json
                     (e.g. 'top5', 'two_takes', 'pattern', 'deep_dive').
                     Pass None for formats with no min_slot requirements.
    """
    config = load_story_mix_config()
    mix = config['category_mix']
    category_sources = config['category_sources']
    platform_caps = config.get('platform_caps', {})
    topic_boosts = config.get('topic_boosts', {})
    surface_weight_overrides = config.get('surface_weight_overrides', {})
    platform_default_weights = config.get('platform_default_weights', {})
    min_slots_per_format = config.get('min_slots_per_format', {})

    # --- Step 1: Fetch and score candidates per category ---
    candidates_by_category: dict[str, list[dict]] = {}
    for category in mix:
        src = category_sources.get(category)
        if not src:
            logger.warning(f"No category_sources entry for '{category}', skipping")
            candidates_by_category[category] = []
            continue

        buckets = src.get('buckets')
        platforms = src.get('platforms')
        exclude_region = src.get('exclude_region')
        exclude_platforms = src.get('exclude_platforms')
        prefer_topics = src.get('prefer_topics')

        if exclude_region:
            items = get_regional_items(
                exclude_region=exclude_region, limit=total_needed * 5, hours=hours,
                exclude_platforms=exclude_platforms,
            )
        else:
            items = get_top_items(
                limit=total_needed * 5, hours=hours, buckets=buckets, platforms=platforms,
                exclude_platforms=exclude_platforms,
            )

        items = _filter_already_used(items)
        items = _apply_topic_boost(
            items, topic_boosts, surface_weight_overrides, platform_default_weights
        )

        # BUG 3 fix: promote items whose topic_tags intersect prefer_topics to the
        # front of the candidate list (within-category priority before proportional fill).
        # Relative hotness order is preserved within each partition.
        if prefer_topics:
            prefer_set = set(prefer_topics)
            items = sorted(
                items,
                key=lambda x: (
                    0 if prefer_set.intersection(x.get('topic_tags') or []) else 1,
                    -x.get('effective_hotness', x.get('hotness', 0)),
                ),
            )

        candidates_by_category[category] = items

    # --- Step 2: Guarantee min_slots for this format (Phase 3) ---
    min_slots: dict[str, int] = (
        min_slots_per_format.get(format_name, {}) if format_name else {}
    )
    seen_urls: set[str] = set()
    selected: list[dict] = []

    if min_slots:
        guaranteed = _fill_min_slots(candidates_by_category, min_slots, seen_urls)
        selected.extend(guaranteed)
        logger.info(
            f"min_slots for '{format_name}': "
            f"guaranteed {len(guaranteed)} items from {list(min_slots.keys())}"
        )

    # --- Step 3: Proportional fill for remaining slots ---
    for category, ratio in mix.items():
        if len(selected) >= total_needed:
            break
        n = round(total_needed * ratio)
        if n == 0:
            continue
        added = 0
        for item in candidates_by_category.get(category, []):
            if len(selected) >= total_needed:
                break
            if item['url'] not in seen_urls:
                selected.append(item)
                seen_urls.add(item['url'])
                added += 1
                if added >= n:
                    break

    # --- Step 4: Apply platform caps across the full assembled list ---
    selected = _enforce_platform_caps(selected, platform_caps, total_needed)

    # --- Step 5: Fallback — fill remaining slots with top items by hotness ---
    if len(selected) < total_needed:
        fallback = get_top_items(limit=50, hours=hours)
        fallback = _filter_already_used(fallback)
        fallback = _apply_topic_boost(
            fallback, topic_boosts, surface_weight_overrides, platform_default_weights
        )
        seen_urls_current = {item['url'] for item in selected}
        for item in fallback:
            if item['url'] not in seen_urls_current:
                selected.append(item)
                seen_urls_current.add(item['url'])
                if len(selected) >= total_needed:
                    break

        # BUG 2 fix: re-enforce platform caps after fallback additions.
        # Fallback items are drawn from an unfiltered hotness pool; caps must
        # run again so reddit/bilibili cannot fill remaining slots unchecked.
        selected = _enforce_platform_caps(selected, platform_caps, total_needed)

    return selected[:total_needed]


def _is_entertainment(item: dict) -> bool:
    """Return True if item appears to be entertainment media rather than a news event.

    Used to filter unsuitable items for formats that require real-world news events
    (e.g., 角色代入 needs real people making choices, not anime game trailers).
    Falls back to False for non-video platforms.
    """
    if item.get('platform') not in _ENTERTAINMENT_PLATFORMS:
        return False
    title = item.get('canonical_title') or item.get('title_original') or ''
    return bool(_ENTERTAINMENT_PATTERN.search(title))


def _filter_already_used(items: list[dict]) -> list[dict]:
    """
    Remove items that were already used in a previous story set.

    Dedup is by URL (not crawler_item_id) because the crawler creates
    multiple rows for the same URL across crawl cycles.

    Exception (Requirement #3): If the item's CURRENT hotness exceeds
    the hotness at the time it was last used by >= HOTNESS_REGAIN_FACTOR,
    it is allowed back in (the topic has new momentum).

    NOTE: Uses item["hotness"] (original, pre-boost) for the regain check —
    never item["effective_hotness"] — so editorial boosts don't distort dedup.
    """
    used = get_used_urls_with_hotness()  # {crawler_url: max_hotness_at_use}
    if not used:
        return items

    filtered = []
    for item in items:
        url = item['url']
        if url not in used:
            filtered.append(item)
        else:
            prev_hotness = used[url]
            current_hotness = item.get('hotness', 0)  # original, not effective
            if current_hotness >= prev_hotness * HOTNESS_REGAIN_FACTOR:
                filtered.append(item)
                logger.info(
                    f"  Re-admitting {url[:60]}: hotness {current_hotness:.1f} "
                    f"> {prev_hotness:.1f} * {HOTNESS_REGAIN_FACTOR}"
                )
    return filtered


# ---------------------------------------------------------------------------
# Format-specific selectors
# ---------------------------------------------------------------------------

def select_for_explainer(lang: str = 'en', hours: int = 24) -> dict | None:
    """
    Select the top story for a 60-second explainer (Format 1).

    Strategy: Highest effective hotness (topic boosts + surface weights applied)
    in the last 24 hours, with platform caps respected so a single high-volume
    platform cannot monopolise the slot.
    """
    config = load_story_mix_config()
    topic_boosts = config.get('topic_boosts', {})
    surface_weight_overrides = config.get('surface_weight_overrides', {})
    platform_default_weights = config.get('platform_default_weights', {})
    platform_caps = config.get('platform_caps', {})

    items = get_top_items(limit=20, hours=hours)
    items = _filter_already_used(items)
    if not items:
        logger.warning("No items found for explainer selection")
        return None

    items = _apply_topic_boost(
        items, topic_boosts, surface_weight_overrides, platform_default_weights
    )
    # BUG 6 fix: enforce caps before picking items[0] so a capped platform
    # (bilibili, reddit) cannot win purely by raw hotness volume.
    items = _enforce_platform_caps(items, platform_caps, total_slots=1)
    if not items:
        logger.warning("No items remain for explainer after platform cap enforcement")
        return None
    return items[0]


def select_for_top5(lang: str = 'en', hours: int = 24) -> list[dict]:
    """
    Select top 5 stories for the daily briefing (Format 2).

    Uses category mix ratios to ensure diverse topic coverage.
    Falls back to wider time windows if not enough items.
    """
    for window in [hours, 48, 72]:
        items = _select_by_mix(total_needed=5, hours=window, format_name='top5')
        if len(items) >= 5:
            return items[:5]
        logger.info(f"Only {len(items)} mix items in {window}h window, expanding...")

    if len(items) < 3:
        logger.warning(f"Only {len(items)} items found for top5 (need at least 3)")
    return items


def select_for_radar(hours: int = 24) -> list[dict]:
    """
    Select "stories US media ignores" (Format 3).

    Strategy: Top items from non-US regions with region diversity
    (max 1 item per region to show breadth).
    """
    candidates = get_regional_items(exclude_region='us', limit=200, hours=hours)
    candidates = _filter_already_used(candidates)
    if not candidates:
        logger.warning("No regional items found for radar")
        return []

    # Enforce region diversity — max 1 per effective_region (content-based)
    selected = []
    seen_regions: set[str] = set()
    for item in candidates:
        region = item.get('effective_region') or item.get('region_key', '')
        if region in seen_regions:
            continue
        selected.append(item)
        seen_regions.add(region)
        if len(selected) >= 5:
            break

    return selected


def select_for_regional(region: str, hours: int = 24) -> list[dict]:
    """
    Select top items from a specific region (Format 4 — regional perspectives).

    Strategy: Top items where content is ABOUT the given region (primary_region
    or content_regions), with source-region fallback for unclassified items.
    Platform-diverse selection.
    """
    candidates = get_top_items(limit=500, hours=hours)
    candidates = _filter_already_used(candidates)

    # Filter to items where content is about the target region
    selected = []
    platform_counts: dict[str, int] = {}
    for item in candidates:
        # Match on effective_region (primary_region with source fallback)
        # OR if region appears in content_regions multi-label list
        effective = item.get('effective_region') or item.get('region_key', '')
        content_regions = item.get('content_regions') or []
        if effective != region and region not in content_regions:
            continue
        platform = item['platform']
        if platform_counts.get(platform, 0) >= 2:
            continue
        selected.append(item)
        platform_counts[platform] = platform_counts.get(platform, 0) + 1
        if len(selected) >= 5:
            break

    if not selected:
        logger.warning(f"No items found for region '{region}'")
    return selected


def select_for_two_takes(hours: int = 24) -> list[dict]:
    """
    Select items for framing contrast (Format 5 — two takes).

    Uses category mix to get diverse items, then the LLM identifies
    framing differences.
    """
    items = _select_by_mix(total_needed=8, hours=hours, format_name='two_takes')
    if not items:
        logger.warning("No items found for two_takes")
    return items


def select_for_pattern(hours: int = 72) -> list[dict]:
    """
    Select items for cross-region pattern detection (Format 6).

    Uses category mix for diverse topics, then verifies >=3 regions present.
    """
    items = _select_by_mix(total_needed=12, hours=hours, format_name='pattern')

    # Deterministic gate: need items from >=3 regions
    regions = set(i.get('region_key', '') for i in items)
    if len(regions) < 3:
        logger.warning(f"Pattern needs >=3 regions, only found {len(regions)} — skipping")
        return []

    return items


def select_for_viral(hours: int = 48) -> list[dict]:
    """
    Select "before it goes viral" candidates (Format 7).

    Strategy: Items trending on niche platforms (HN, dev.to, lobsters,
    Papers with Code, GitHub) that haven't hit mainstream news yet.
    Expand to 48h window for niche platforms which update slower.
    """
    items = get_early_signals(limit=15, hours=hours)
    items = _filter_already_used(items)
    if not items:
        # Try wider window
        items = get_early_signals(limit=15, hours=168)
        items = _filter_already_used(items)
    if not items:
        logger.warning("No early signal items found")
    return items[:5] if items else []


def select_for_deep_dive(topic: str = 'tech', hours: int = 168) -> list[dict]:
    """
    Select items for weekly deep dive (Format 8).

    Uses category mix for diverse content over 7-day window.
    """
    items = _select_by_mix(total_needed=15, hours=hours, format_name='deep_dive')

    if len(items) < 5:
        logger.warning(f"Only {len(items)} items for deep_dive, need at least 5")

    return items


def select_for_niche(niche: str = 'tech', hours: int = 24) -> list[dict]:
    """
    Select items for niche focus (Format 9 — tech/finance daily).

    Uses the specific niche's bucket/platform mapping from story_mix.json
    category_sources (no longer hardcoded).
    """
    config = load_story_mix_config()
    category_sources = config['category_sources']
    topic_boosts = config.get('topic_boosts', {})
    surface_weight_overrides = config.get('surface_weight_overrides', {})
    platform_default_weights = config.get('platform_default_weights', {})

    src = category_sources.get(niche, category_sources.get('tech', {}))
    buckets = src.get('buckets')
    platforms = src.get('platforms')

    items = get_top_items(limit=100, hours=hours, buckets=buckets, platforms=platforms)
    items = _filter_already_used(items)
    items = _apply_topic_boost(
        items, topic_boosts, surface_weight_overrides, platform_default_weights
    )

    # Platform diversity — max 2 per platform
    selected = []
    platform_counts: dict[str, int] = defaultdict(int)
    for item in items:
        platform = item.get('platform', '')
        if platform_counts[platform] >= 2:
            continue
        selected.append(item)
        platform_counts[platform] += 1
        if len(selected) >= 5:
            break

    if len(selected) < 3:
        logger.info(f"Only {len(selected)} items for niche '{niche}', expanding search")
        all_items = get_top_items(limit=200, hours=hours)
        all_items = _filter_already_used(all_items)
        all_items = _apply_topic_boost(
            all_items, topic_boosts, surface_weight_overrides, platform_default_weights
        )
        seen_urls = {s['url'] for s in selected}
        for item in all_items:
            if item['url'] not in seen_urls:
                selected.append(item)
                seen_urls.add(item['url'])
                if len(selected) >= 5:
                    break

    return selected


def select_for_format(format_id: int, hours: int = 24) -> list[dict] | None:
    """
    Generic selector for formats 10-46.
    Uses FORMAT_REGISTRY to determine strategy and item count.
    """
    from engine.format_registry import FORMAT_REGISTRY

    if format_id not in FORMAT_REGISTRY:
        logger.warning(f"Unknown format_id {format_id}")
        return None

    strategy, _, item_count = FORMAT_REGISTRY[format_id]

    if strategy == 'single':
        from engine.format_registry import FORMAT_REQUIRES_NEWS
        requires_news = format_id in FORMAT_REQUIRES_NEWS
        best_items = None
        for window in [hours, 48, 72, 168]:
            candidates = get_top_items(limit=50, hours=window)
            candidates = _filter_already_used(candidates)
            if not candidates:
                continue
            if requires_news:
                news_only = [i for i in candidates if not _is_entertainment(i)]
                if news_only:
                    best_items = news_only
                    break
                # No news items in this window — keep as fallback, try wider
                if best_items is None:
                    best_items = candidates
                logger.info(
                    f"  format_{format_id}: top items are entertainment in {window}h window, expanding..."
                )
            else:
                best_items = candidates
                break
        return [best_items[0]] if best_items else None

    elif strategy == 'mix':
        items = _select_by_mix(total_needed=item_count, hours=hours)
        return items if items else None

    elif strategy == 'comment':
        # BUG 5 fix: comment_platforms moved to story_mix.json (configurable).
        # Topic boosts and platform caps are now applied before selection so
        # reddit cannot win every slot purely by raw engagement volume.
        comment_config = load_story_mix_config()
        comment_platforms = comment_config.get(
            'comment_platforms', ['hackernews', 'youtube', 'reddit']
        )
        topic_boosts = comment_config.get('topic_boosts', {})
        surface_weight_overrides = comment_config.get('surface_weight_overrides', {})
        platform_default_weights = comment_config.get('platform_default_weights', {})
        platform_caps = comment_config.get('platform_caps', {})

        items = get_top_items(limit=item_count * 5, hours=hours, platforms=comment_platforms)
        items = _filter_already_used(items)
        if not items:
            items = _select_by_mix(total_needed=item_count, hours=hours)
            return items[:item_count] if items else None

        items = _apply_topic_boost(
            items, topic_boosts, surface_weight_overrides, platform_default_weights
        )
        items = _enforce_platform_caps(items, platform_caps, total_slots=item_count)
        return items[:item_count] if items else None

    elif strategy == 'topic_match':
        # Use mix selection — LLM will identify topic overlaps from diverse items
        items = _select_by_mix(total_needed=item_count, hours=hours)
        return items if items else None

    return None


def get_top_regions_with_data(hours: int = 24, min_items: int = 3) -> list[str]:
    """
    Get region keys that have enough data for regional stories.
    Excludes US. Returns up to 3 regions with the most items.
    """
    candidates = get_regional_items(exclude_region='us', limit=100, hours=hours)
    candidates = _filter_already_used(candidates)

    region_counts: dict[str, int] = {}
    for item in candidates:
        # Use effective_region (content-based with fallback) for accurate counts
        region = item.get('effective_region') or item.get('region_key', '')
        if region == 'us':
            continue  # still exclude US
        region_counts[region] = region_counts.get(region, 0) + 1

    # Return regions with enough items, sorted by count
    qualified = [r for r, c in region_counts.items() if c >= min_items]
    qualified.sort(key=lambda r: region_counts[r], reverse=True)
    return qualified[:3]
