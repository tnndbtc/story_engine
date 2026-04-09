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
from pathlib import Path

from db.crawler_reader import (
    get_top_items,
    get_diverse_top_items,
    get_early_signals,
    get_regional_items,
)
from db.models import get_used_urls_with_hotness

logger = logging.getLogger(__name__)

HOTNESS_REGAIN_FACTOR = 1.3  # item must be 30% hotter to be re-eligible

# ---------------------------------------------------------------------------
# Category mix config — maps config keys to (buckets, platforms)
# None means no filter on that dimension
# ---------------------------------------------------------------------------

CATEGORY_MAPPING: dict[str, tuple[list[str] | None, list[str] | None]] = {
    'tech':          (['category_tech', 'rising'],
                      ['hackernews', 'devto', 'lobsters', 'github',
                       'paperswithcode', 'stackoverflow', 'v2ex']),
    'news':          (['news'], None),
    'entertainment': (['category_entertainment'],
                      ['youtube', 'bilibili', 'nicovideo']),
    'regional':      (None, None),  # special: region_key != 'us'
    'social':        (['hot_now'],
                      ['reddit', 'weibo', 'baidu']),
    'science':       (None, ['paperswithcode', 'arxiv_ai_rss']),
    'gaming':        (['category_gaming'], None),
    'finance':       (['news'], None),  # fallback to news bucket for now
}

DEFAULT_MIX = {
    'tech':          0.30,
    'news':          0.25,
    'entertainment': 0.15,
    'regional':      0.15,
    'social':        0.10,
    'science':       0.05,
}

STORY_MIX_PATH = os.environ.get(
    'STORY_MIX_PATH',
    str(Path(__file__).resolve().parent.parent.parent / 'story_mix.json')
)


def load_category_mix() -> dict[str, float]:
    """Load category mix ratios from story_mix.json, or use defaults."""
    if os.path.exists(STORY_MIX_PATH):
        try:
            with open(STORY_MIX_PATH) as f:
                data = json.load(f)
            mix = data.get('category_mix', DEFAULT_MIX)
            total = sum(mix.values())
            if abs(total - 1.0) > 0.01:
                logger.warning(f"category_mix ratios sum to {total:.2f}, not 1.0")
            return mix
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load story_mix.json: {e}, using defaults")
    return DEFAULT_MIX.copy()


def _select_by_mix(total_needed: int, hours: int = 24) -> list[dict]:
    """
    Select items across categories according to the configured mix ratios.

    Used by multi-item formats (top5, two_takes, pattern, deep_dive, niche).
    """
    mix = load_category_mix()
    selected = []
    seen_urls: set[str] = set()

    for category, ratio in mix.items():
        n = round(total_needed * ratio)
        if n == 0:
            continue

        mapping = CATEGORY_MAPPING.get(category)
        if not mapping:
            logger.warning(f"Unknown category '{category}' in mix, skipping")
            continue

        buckets, platforms = mapping

        # Special handling for 'regional' — uses region exclusion, not buckets
        if category == 'regional':
            items = get_regional_items(exclude_region='us', limit=n * 5, hours=hours)
        else:
            items = get_top_items(limit=n * 5, hours=hours, buckets=buckets, platforms=platforms)

        items = _filter_already_used(items)

        # Take up to n items, deduplicating by URL
        added = 0
        for item in items:
            if item['url'] not in seen_urls:
                selected.append(item)
                seen_urls.add(item['url'])
                added += 1
                if added >= n:
                    break

    # Fallback: fill remaining slots with top items by hotness
    if len(selected) < total_needed:
        fallback = get_top_items(limit=50, hours=hours)
        fallback = _filter_already_used(fallback)
        for item in fallback:
            if item['url'] not in seen_urls:
                selected.append(item)
                seen_urls.add(item['url'])
                if len(selected) >= total_needed:
                    break

    return selected[:total_needed]


def _filter_already_used(items: list[dict]) -> list[dict]:
    """
    Remove items that were already used in a previous story set.

    Dedup is by URL (not crawler_item_id) because the crawler creates
    multiple rows for the same URL across crawl cycles.

    Exception (Requirement #3): If the item's CURRENT hotness exceeds
    the hotness at the time it was last used by >= HOTNESS_REGAIN_FACTOR,
    it is allowed back in (the topic has new momentum).
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
            current_hotness = item.get('hotness', 0)
            if current_hotness >= prev_hotness * HOTNESS_REGAIN_FACTOR:
                filtered.append(item)
                logger.info(
                    f"  Re-admitting {url[:60]}: hotness {current_hotness:.1f} "
                    f"> {prev_hotness:.1f} * {HOTNESS_REGAIN_FACTOR}"
                )
    return filtered


def select_for_explainer(lang: str = 'en', hours: int = 24) -> dict | None:
    """
    Select the top story for a 60-second explainer (Format 1).

    Strategy: Highest hotness in the last 24 hours, any bucket.
    """
    items = get_top_items(limit=10, hours=hours)
    items = _filter_already_used(items)
    if not items:
        logger.warning("No items found for explainer selection")
        return None
    return items[0]


def select_for_top5(lang: str = 'en', hours: int = 24) -> list[dict]:
    """
    Select top 5 stories for the daily briefing (Format 2).

    Uses category mix ratios to ensure diverse topic coverage.
    Falls back to wider time windows if not enough items.
    """
    for window in [hours, 48, 72]:
        items = _select_by_mix(total_needed=5, hours=window)
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

    # Enforce region diversity — max 1 per region
    selected = []
    seen_regions: set[str] = set()
    for item in candidates:
        region = item.get('region_key', '')
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

    Strategy: Top items by hotness from the given region, platform-diverse.
    """
    candidates = get_top_items(limit=200, hours=hours)
    candidates = _filter_already_used(candidates)

    # Filter to target region and enforce platform diversity
    selected = []
    platform_counts: dict[str, int] = {}
    for item in candidates:
        if item.get('region_key') != region:
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
    items = _select_by_mix(total_needed=8, hours=hours)
    if not items:
        logger.warning("No items found for two_takes")
    return items


def select_for_pattern(hours: int = 72) -> list[dict]:
    """
    Select items for cross-region pattern detection (Format 6).

    Uses category mix for diverse topics, then verifies >=3 regions present.
    """
    items = _select_by_mix(total_needed=12, hours=hours)

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
    items = _select_by_mix(total_needed=15, hours=hours)

    if len(items) < 5:
        logger.warning(f"Only {len(items)} items for deep_dive, need at least 5")

    return items


def select_for_niche(niche: str = 'tech', hours: int = 24) -> list[dict]:
    """
    Select items for niche focus (Format 9 — tech/finance daily).

    Uses the specific niche's bucket/platform mapping, not the full category mix.
    """
    mapping = CATEGORY_MAPPING.get(niche, CATEGORY_MAPPING['tech'])
    buckets, platforms = mapping

    items = get_top_items(limit=100, hours=hours, buckets=buckets, platforms=platforms)
    items = _filter_already_used(items)

    # Platform diversity
    selected = []
    platform_counts: dict[str, int] = {}
    for item in items:
        platform = item['platform']
        if platform_counts.get(platform, 0) >= 2:
            continue
        selected.append(item)
        platform_counts[platform] = platform_counts.get(platform, 0) + 1
        if len(selected) >= 5:
            break

    if len(selected) < 3:
        logger.info(f"Only {len(selected)} items for niche '{niche}', expanding search")
        all_items = get_top_items(limit=200, hours=hours)
        all_items = _filter_already_used(all_items)
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
        items = get_top_items(limit=10, hours=hours)
        items = _filter_already_used(items)
        return [items[0]] if items else None

    elif strategy == 'mix':
        items = _select_by_mix(total_needed=item_count, hours=hours)
        return items if items else None

    elif strategy == 'comment':
        # Prefer platforms with comments (reddit, hackernews, youtube)
        comment_platforms = ['reddit', 'hackernews', 'youtube']
        items = get_top_items(limit=item_count * 5, hours=hours, platforms=comment_platforms)
        items = _filter_already_used(items)
        if not items:
            items = _select_by_mix(total_needed=item_count, hours=hours)
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
        region = item.get('region_key', '')
        region_counts[region] = region_counts.get(region, 0) + 1

    # Return regions with enough items, sorted by count
    qualified = [r for r, c in region_counts.items() if c >= min_items]
    qualified.sort(key=lambda r: region_counts[r], reverse=True)
    return qualified[:3]
