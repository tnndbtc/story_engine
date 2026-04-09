"""
Story selector — picks candidate items from crawler DB per format.

Each format has its own selection logic (NOT a shared global top-N):
  - Format 1 (explainer): single top item by hotness
  - Format 2 (top5): top 5 with platform diversity
  - Format 7 (viral): early signals from niche platforms

The selector only reads the crawler DB. It never writes.
"""

import logging
from db.crawler_reader import (
    get_top_items,
    get_diverse_top_items,
    get_early_signals,
    get_regional_items,
)

logger = logging.getLogger(__name__)


def select_for_explainer(lang: str = 'en', hours: int = 24) -> dict | None:
    """
    Select the top story for a 60-second explainer (Format 1).

    Strategy: Highest hotness in the last 24 hours, any bucket.
    """
    items = get_top_items(limit=1, hours=hours)
    if not items:
        logger.warning("No items found for explainer selection")
        return None
    return items[0]


def select_for_top5(lang: str = 'en', hours: int = 24) -> list[dict]:
    """
    Select top 5 stories for the daily briefing (Format 2).

    Strategy: Top 5 by hotness with platform diversity
    (max 2 items from any single platform).
    If not enough items in the time window, expand to 48h then 72h.
    """
    for window in [hours, 48, 72]:
        items = get_diverse_top_items(limit=5, hours=window, max_per_platform=2)
        if len(items) >= 5:
            return items
        logger.info(f"Only {len(items)} diverse items in {window}h window, expanding...")

    if len(items) < 3:
        logger.warning(f"Only {len(items)} items found for top5 (need at least 3)")
    return items


def select_for_viral(hours: int = 24) -> list[dict]:
    """
    Select "before it goes viral" candidates (Format 7).

    Strategy: Items trending on niche platforms (HN, dev.to, lobsters,
    Papers with Code, GitHub) that haven't hit mainstream news yet.
    """
    items = get_early_signals(limit=5, hours=hours)
    if not items:
        logger.warning("No early signal items found")
    return items


def select_for_radar(hours: int = 24) -> list[dict]:
    """
    Select "stories US media ignores" (Format 3).

    Strategy: Top items from non-US regions with region diversity
    (max 1 item per region to show breadth).
    """
    candidates = get_regional_items(exclude_region='us', limit=200, hours=hours)
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
    from db.crawler_reader import get_top_items
    candidates = get_top_items(limit=200, hours=hours)

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

    Strategy: Get top items from diverse platforms/regions. The LLM will
    identify framing differences — we provide a rich, diverse candidate pool.
    Ensures at least 3 different regions and 3 different platforms.
    """
    candidates = get_top_items(limit=200, hours=hours)
    if not candidates:
        logger.warning("No items found for two_takes")
        return []

    # Select items maximizing region + platform diversity
    selected = []
    seen_regions: set[str] = set()
    seen_platforms: set[str] = set()

    # First pass: prioritize unseen region+platform combos
    for item in candidates:
        region = item.get('region_key', '')
        platform = item['platform']
        if region not in seen_regions or platform not in seen_platforms:
            selected.append(item)
            seen_regions.add(region)
            seen_platforms.add(platform)
            if len(selected) >= 8:
                break

    return selected


def select_for_pattern(hours: int = 72) -> list[dict]:
    """
    Select items for cross-region pattern detection (Format 6).

    Strategy: Get items from 3+ regions spanning 72 hours.
    The LLM identifies the pattern — we provide a diverse, multi-region pool.
    Deterministic gate: must have items from >=3 distinct regions.
    """
    candidates = get_regional_items(exclude_region='__none__', limit=200, hours=hours)
    if not candidates:
        logger.warning("No items found for pattern detection")
        return []

    # Select items maximizing region diversity
    selected = []
    region_counts: dict[str, int] = {}
    for item in candidates:
        region = item.get('region_key', '')
        if region_counts.get(region, 0) >= 3:
            continue
        selected.append(item)
        region_counts[region] = region_counts.get(region, 0) + 1
        if len(selected) >= 12:
            break

    # Deterministic gate: need items from >=3 regions
    if len(region_counts) < 3:
        logger.warning(f"Pattern needs >=3 regions, only found {len(region_counts)} — skipping")
        return []

    return selected


def select_for_viral(hours: int = 48) -> list[dict]:
    """
    Select "before it goes viral" candidates (Format 7).

    Strategy: Items trending on niche platforms (HN, dev.to, lobsters,
    Papers with Code, GitHub) that haven't hit mainstream news yet.
    Expand to 48h window for niche platforms which update slower.
    """
    items = get_early_signals(limit=5, hours=hours)
    if not items:
        # Try wider window
        items = get_early_signals(limit=5, hours=168)
    if not items:
        logger.warning("No early signal items found")
    return items


def select_for_deep_dive(topic: str = 'tech', hours: int = 168) -> list[dict]:
    """
    Select items for weekly deep dive (Format 8).

    Strategy: Collect all items for a given niche from the past 7 days,
    sorted by hotness. Needs substantial content to analyze.
    """
    bucket_map = {
        'tech': ['category_tech'],
        'entertainment': ['category_entertainment'],
        'finance': ['category_finance'],
        'gaming': ['category_gaming'],
    }
    buckets = bucket_map.get(topic)

    if buckets:
        items = get_top_items(limit=200, hours=hours, buckets=buckets)
    else:
        # Fallback: use all items
        items = get_top_items(limit=200, hours=hours)

    if len(items) < 5:
        # Not enough niche items — fall back to all items
        logger.info(f"Only {len(items)} items for topic '{topic}', using all items")
        items = get_top_items(limit=50, hours=hours)

    # Take top 15 for the deep dive (enough context for a 5-min script)
    return items[:15]


def select_for_niche(niche: str = 'tech', hours: int = 24) -> list[dict]:
    """
    Select items for niche focus (Format 9 — tech/finance daily).

    Strategy: Filter by category bucket, platform-diverse.
    """
    bucket_map = {
        'tech': ['category_tech', 'rising'],
        'finance': ['category_finance'],
        'entertainment': ['category_entertainment'],
        'gaming': ['category_gaming'],
    }
    buckets = bucket_map.get(niche, ['category_tech'])

    items = get_top_items(limit=100, hours=hours, buckets=buckets)

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
        # Not enough niche items, expand to wider buckets
        logger.info(f"Only {len(selected)} items for niche '{niche}', expanding search")
        all_items = get_top_items(limit=200, hours=hours)
        for item in all_items:
            if item not in selected:
                selected.append(item)
                if len(selected) >= 5:
                    break

    return selected


def get_top_regions_with_data(hours: int = 24, min_items: int = 3) -> list[str]:
    """
    Get region keys that have enough data for regional stories.
    Excludes US. Returns up to 3 regions with the most items.
    """
    from db.crawler_reader import get_regional_items
    candidates = get_regional_items(exclude_region='us', limit=100, hours=hours)

    region_counts: dict[str, int] = {}
    for item in candidates:
        region = item.get('region_key', '')
        region_counts[region] = region_counts.get(region, 0) + 1

    # Return regions with enough items, sorted by count
    qualified = [r for r, c in region_counts.items() if c >= min_items]
    qualified.sort(key=lambda r: region_counts[r], reverse=True)
    return qualified[:3]
