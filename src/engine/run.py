"""
story_engine generation runner — CLI entry point.

This is what the cron job calls. It:
  1. Creates a story set (batch)
  2. Selects candidates from the crawler DB (excluding previously used items)
  3. Generates scripts via Claude CLI
  4. Records used items for dedup in future runs
  5. Marks the story set complete/failed

Usage:
    python -m engine.run                     # Generate all formats
    python -m engine.run --lang zh           # All formats, Chinese
    python -m engine.run --format explainer  # Single format only
    python -m engine.run --dry-run           # Show selections without generating
"""

import argparse
import logging
import os
import sys

# Add src/ to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.models import (
    init_db,
    create_story_set,
    complete_story_set,
    record_used_items,
)
from engine.selector import (
    select_for_explainer,
    select_for_top5,
    select_for_radar,
    select_for_regional,
    select_for_two_takes,
    select_for_pattern,
    select_for_viral,
    select_for_deep_dive,
    select_for_niche,
    get_top_regions_with_data,
)
from engine.generator import (
    generate_explainer,
    generate_top5,
    generate_radar,
    generate_regional,
    generate_two_takes,
    generate_pattern,
    generate_viral,
    generate_deep_dive,
    generate_niche,
)

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(levelname)s %(asctime)s %(name)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, 'generate.log')),
    ],
)
logger = logging.getLogger(__name__)

# Region display names
REGION_NAMES = {
    'jp': 'Japan', 'kr': 'South Korea', 'cn': 'China', 'de': 'Germany',
    'fr': 'France', 'br': 'Brazil', 'es': 'Spain/Latin America',
    'in': 'India', 'ru': 'Russia', 'it': 'Italy', 'tr': 'Turkey',
    'ar': 'Arab World', 'id': 'Indonesia', 'pl': 'Poland',
    'nl': 'Netherlands', 'se': 'Sweden', 'ph': 'Philippines',
    'vn': 'Vietnam', 'th': 'Thailand', 'my': 'Malaysia',
    'pt': 'Portugal', 'ar_latam': 'Argentina',
}


def _log_items(label: str, items: list[dict]):
    """Log selected items."""
    for i, item in enumerate(items, 1):
        title = item.get('canonical_title') or item['title_original']
        region = item.get('region_key', '??')
        logger.info(f"  {label} #{i}: [{region}/{item['platform']}] {title[:60]} (hotness={item['hotness']:.1f})")


def run_explainer(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate a 60-second explainer (Format 1)."""
    item = select_for_explainer(lang=lang)
    if not item:
        logger.warning("No item found for explainer — skipping")
        return None

    _log_items("Explainer", [item])

    if dry_run:
        logger.info("[DRY RUN] Would generate explainer — skipping")
        return None

    story_id = generate_explainer(item, lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'explainer', [item])
    return story_id


def run_top5(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate a Top 5 Today script (Format 2)."""
    items = select_for_top5(lang=lang)
    if len(items) < 3:
        logger.warning(f"Only {len(items)} items for top5 — need at least 3, skipping")
        return None

    _log_items("Top5", items)

    if dry_run:
        logger.info("[DRY RUN] Would generate top5 — skipping")
        return None

    story_id = generate_top5(items, lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'top5', items)
    return story_id


def run_radar(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate 'stories US media ignores' (Format 3)."""
    items = select_for_radar()
    if len(items) < 3:
        logger.warning(f"Only {len(items)} items for radar — need at least 3, skipping")
        return None

    _log_items("Radar", items)

    if dry_run:
        logger.info("[DRY RUN] Would generate radar — skipping")
        return None

    story_id = generate_radar(items, lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'radar', items)
    return story_id


def run_regional(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate regional perspective (Format 4) for the top region with data."""
    regions = get_top_regions_with_data()
    if not regions:
        logger.warning("No regions with enough data for regional — skipping")
        return None

    region_key = regions[0]
    region_name = REGION_NAMES.get(region_key, region_key)
    items = select_for_regional(region=region_key)
    if len(items) < 3:
        logger.warning(f"Only {len(items)} items for region '{region_key}' — skipping")
        return None

    logger.info(f"Regional target: {region_name} ({region_key})")
    _log_items("Regional", items)

    if dry_run:
        logger.info(f"[DRY RUN] Would generate regional ({region_name}) — skipping")
        return None

    story_id = generate_regional(items, region_name=region_name, lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'regional', items)
    return story_id


def run_two_takes(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate framing contrast (Format 5)."""
    items = select_for_two_takes()
    if len(items) < 4:
        logger.warning(f"Only {len(items)} items for two_takes — need at least 4, skipping")
        return None

    _log_items("TwoTakes", items)

    if dry_run:
        logger.info("[DRY RUN] Would generate two_takes — skipping")
        return None

    story_id = generate_two_takes(items, lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'two_takes', items)
    return story_id


def run_pattern(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate cross-region pattern analysis (Format 6)."""
    items = select_for_pattern()
    if len(items) < 6:
        logger.warning(f"Only {len(items)} items for pattern — need at least 6, skipping")
        return None

    regions = set(i.get('region_key', '') for i in items)
    logger.info(f"Pattern candidates: {len(items)} items across {len(regions)} regions")
    _log_items("Pattern", items[:8])

    if dry_run:
        logger.info("[DRY RUN] Would generate pattern — skipping")
        return None

    story_id = generate_pattern(items, lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'pattern', items)
    return story_id


def run_viral(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate 'before it goes viral' (Format 7)."""
    items = select_for_viral()
    if len(items) < 2:
        logger.warning(f"Only {len(items)} items for viral — need at least 2, skipping")
        return None

    _log_items("Viral", items)

    if dry_run:
        logger.info("[DRY RUN] Would generate viral — skipping")
        return None

    story_id = generate_viral(items, lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'viral', items)
    return story_id


def run_deep_dive(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate weekly deep dive (Format 8)."""
    items = select_for_deep_dive(topic='tech')
    if len(items) < 5:
        logger.warning(f"Only {len(items)} items for deep_dive — need at least 5, skipping")
        return None

    logger.info(f"Deep dive: {len(items)} items for topic 'tech'")
    _log_items("DeepDive", items[:5])

    if dry_run:
        logger.info("[DRY RUN] Would generate deep_dive — skipping")
        return None

    story_id = generate_deep_dive(items, topic='tech', lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'deep_dive', items)
    return story_id


def run_niche(lang: str, channel: int, dry_run: bool, set_id: int | None, batch_ts: int | None = None) -> int | None:
    """Generate niche focus (Format 9)."""
    items = select_for_niche(niche='tech')
    if len(items) < 3:
        logger.warning(f"Only {len(items)} items for niche — need at least 3, skipping")
        return None

    logger.info(f"Niche focus: {len(items)} items for 'tech'")
    _log_items("Niche", items)

    if dry_run:
        logger.info("[DRY RUN] Would generate niche — skipping")
        return None

    story_id = generate_niche(items, niche='tech', lang=lang, channel=channel, batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, 'niche', items)
    return story_id


LEGACY_FORMATS = ['explainer', 'top5', 'radar', 'regional', 'two_takes', 'pattern', 'viral', 'deep_dive', 'niche']

FORMAT_RUNNERS = {
    'explainer': run_explainer,
    'top5': run_top5,
    'radar': run_radar,
    'regional': run_regional,
    'two_takes': run_two_takes,
    'pattern': run_pattern,
    'viral': run_viral,
    'deep_dive': run_deep_dive,
    'niche': run_niche,
}

# All valid format strings
ALL_FORMATS = LEGACY_FORMATS + [f'format_{i}' for i in range(10, 47)]


def _run_generic_format(format_id: int, lang: str, channel: int, dry_run: bool,
                        set_id: int | None, batch_ts: int | None) -> int | None:
    """Generic runner for formats 10-46."""
    from engine.selector import select_for_format
    from engine.generator import generate_by_format
    from engine.format_registry import FORMAT_NAMES

    format_name = FORMAT_NAMES.get(format_id, f'format_{format_id}')

    items = select_for_format(format_id, set_id=set_id)
    if not items:
        logger.warning(f"No items found for {format_name} — skipping")
        return None

    _log_items(format_name, items if isinstance(items, list) else [items])

    if dry_run:
        logger.info(f"[DRY RUN] Would generate {format_name} — skipping")
        return None

    story_id = generate_by_format(format_id, items, lang=lang, channel=channel,
                                  batch_id=set_id, batch_ts=batch_ts)
    if set_id:
        record_used_items(set_id, story_id, f'format_{format_id}', items)
    return story_id


def main():
    parser = argparse.ArgumentParser(description='Generate stories from crawled data')
    parser.add_argument('--lang', choices=['en', 'zh'], default='en', help='Output language')
    parser.add_argument('--channel', type=int, choices=[1, 2, 3], default=1, help='Output channel')
    parser.add_argument('--format', nargs='+', default=['all'],
                        help='Formats to generate (space-separated): all, all_extended, explainer, top5, format_10, ...')
    parser.add_argument('--dry-run', action='store_true', help='Show selections without generating')
    args = parser.parse_args()

    logger.info(f"=== story_engine generation run ===")
    logger.info(f"  lang={args.lang}  channel={args.channel}  format={args.format}  dry_run={args.dry_run}")

    # Initialize DB (creates tables + migration)
    init_db()

    # Create story set (unless dry run)
    set_id = None
    batch_ts = None
    if not args.dry_run:
        set_id, batch_ts = create_story_set(lang=args.lang, channel=args.channel)
        logger.info(f"Created story set #{set_id} (batch_ts={batch_ts})")

    # Determine which formats to run
    # args.format is now a list (nargs='+')
    formats = []
    for f in args.format:
        if f == 'all':
            formats.extend(LEGACY_FORMATS)
        elif f == 'all_extended':
            formats.extend(ALL_FORMATS)
        else:
            formats.append(f)
    results = {}

    for fmt in formats:
        # Check if it's a generic format (10-46)
        if fmt.startswith('format_') and fmt[7:].isdigit():
            format_id = int(fmt[7:])
            try:
                results[fmt] = _run_generic_format(format_id, args.lang, args.channel,
                                                   args.dry_run, set_id, batch_ts)
            except Exception as e:
                logger.error(f"{fmt} generation failed: {e}")
                results[fmt] = None
            continue

        # Legacy format (1-9)
        runner = FORMAT_RUNNERS.get(fmt)
        if not runner:
            logger.error(f"Unknown format: {fmt}")
            results[fmt] = None
            continue
        try:
            results[fmt] = runner(args.lang, args.channel, args.dry_run, set_id, batch_ts)
        except Exception as e:
            logger.error(f"{fmt} generation failed: {e}")
            results[fmt] = None

    # Mark story set complete / partial / failed
    if set_id:
        succeeded = sum(1 for v in results.values() if v is not None)
        total = len(results)
        if succeeded == total:
            status = 'complete'
        elif succeeded > 0:
            status = 'partial'
        else:
            status = 'failed'
        complete_story_set(set_id, status)
        logger.info(f"Story set #{set_id} marked as '{status}' ({succeeded}/{total} formats succeeded)")

    # Summary
    logger.info("=== Generation complete ===")
    for fmt, story_id in results.items():
        if story_id:
            logger.info(f"  {fmt}: story #{story_id}")
        else:
            logger.info(f"  {fmt}: skipped or failed")

    if all(v is None for v in results.values()):
        logger.warning("All formats failed or were skipped")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
