"""
story_engine generation runner — CLI entry point.

This is what the cron job calls. It:
  1. Runs the 4-stage batch selection pipeline (run_batch)
  2. Generates scripts via Claude CLI for each assigned format
  3. Marks the story set complete/failed

Usage:
    python -m engine.run                     # Generate all formats
    python -m engine.run --lang zh           # All formats, Chinese
    python -m engine.run --format explainer  # Single format only
    python -m engine.run --dry-run           # Show selections without generating
    python -m engine.run --hours 24          # 24-hour lookback window
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Add src/ to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.models import init_db, DB_PATH, complete_story_set
from engine import selector
from engine.format_registry import FORMAT_NAME_TO_ID, FORMAT_NAMES
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
    generate_by_format,
    generate_format_deep_en,
    generate_story_batch,
)
from engine.selector.story_orchestrate import story_orchestrate

# Config path — story_mix.json lives at the project root (two levels above src/)
CONFIG_PATH = str(Path(__file__).resolve().parent.parent.parent / 'story_mix.json')

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

# Region display names — used by _dispatch_legacy for generate_regional
REGION_NAMES = {
    'jp': 'Japan', 'kr': 'South Korea', 'cn': 'China', 'de': 'Germany',
    'fr': 'France', 'br': 'Brazil', 'es': 'Spain/Latin America',
    'in': 'India', 'ru': 'Russia', 'it': 'Italy', 'tr': 'Turkey',
    'ar': 'Arab World', 'id': 'Indonesia', 'pl': 'Poland',
    'nl': 'Netherlands', 'se': 'Sweden', 'ph': 'Philippines',
    'vn': 'Vietnam', 'th': 'Thailand', 'my': 'Malaysia',
    'pt': 'Portugal', 'ar_latam': 'Argentina',
}

# Legacy generators for formats 2, 3, 5, 6, 7, 8, 9
# Formats 1 and 4 are handled specially in _dispatch_legacy
LEGACY_GENERATORS = {
    2: generate_top5,
    3: generate_radar,
    5: generate_two_takes,
    6: generate_pattern,
    7: generate_viral,
    8: generate_deep_dive,
    9: generate_niche,
}


def _dispatch_legacy(format_id, item_dicts, *, lang, channel, batch_id, batch_ts):
    """
    Per-format dispatch for legacy formats 1–9.

    NOT uniform — two formats have non-standard signatures:
      format 1 (explainer): expects a single dict, not a list
      format 4 (regional):  requires region_name positional arg
    All other legacy formats: generate_*(items: list[dict], ...)
    """
    kw = dict(lang=lang, channel=channel, batch_id=batch_id, batch_ts=batch_ts)
    if format_id == 1:
        # generate_explainer takes a single dict, not a list
        return generate_explainer(item_dicts[0], **kw)
    if format_id == 4:
        region_key = item_dicts[0].get('region_key', 'unknown') if item_dicts else 'unknown'
        region_name = REGION_NAMES.get(region_key, region_key)
        return generate_regional(item_dicts, region_name=region_name, **kw)
    gen_fn = LEGACY_GENERATORS.get(format_id)
    if gen_fn is None:
        raise ValueError(f"No legacy generator for format_id={format_id}")
    if format_id == 8:
        return gen_fn(item_dicts, topic='tech', **kw)
    if format_id == 9:
        return gen_fn(item_dicts, niche='tech', **kw)
    return gen_fn(item_dicts, **kw)


def _candidate_to_source_dict(c) -> dict:
    """Minimal dict for cluster mate articles passed to the generator."""
    return {
        'url':                  c.url,
        'platform':             c.platform,
        'hotness':              c.hotness,
        'title_original':       c.title_original,
        'canonical_title':      c.canonical_title,
        'description_original': c.description_original,
        'title':                c.canonical_title or c.title_original,
        'id':                   c.crawler_item_id,
    }


def _candidates_to_dicts(candidates, cluster_map: dict | None = None) -> list[dict]:
    """
    Convert list[NormalizedCandidate] to the dict format generators expect.

    When cluster_map is provided (from event clustering), each dict is enriched
    with fact_sources, context_sources, reaction_sources, event_hotness, and
    cluster_size so generators can produce richer multi-source narratives.
    """
    result = []
    for c in candidates:
        d = {
            'url':                  c.url,
            'platform':             c.platform,
            'hotness':              c.hotness,
            'category':             c.category,
            'story_category':       c.category,
            'canonical_title':      c.canonical_title,
            'title_original':       c.title_original,
            'description_original': c.description_original,
            'region_key':           c.region_key,
            'region_name':          c.region_name,
            'engagement_signals':   c.engagement_signals,
            'raw_payload':          c.raw_payload,
            # Legacy field aliases expected by some generators
            'title':                c.canonical_title or c.title_original,
            'id':                   c.crawler_item_id,
            # New-development signal (Step 7)
            'is_new_development':   c.is_new_development,
            'prior_story_title':    c.prior_story_title,
        }
        if cluster_map and c.candidate_id in cluster_map:
            cluster = cluster_map[c.candidate_id]
            # Exclude the representative itself from the source lists
            d['fact_sources']     = [
                _candidate_to_source_dict(m) for m in cluster.fact_sources
                if m.candidate_id != c.candidate_id
            ]
            d['context_sources']  = [
                _candidate_to_source_dict(m) for m in cluster.context_sources
                if m.candidate_id != c.candidate_id
            ]
            d['reaction_sources'] = [
                _candidate_to_source_dict(m) for m in cluster.reaction_sources
                if m.candidate_id != c.candidate_id
            ]
            d['event_hotness']    = cluster.event_hotness
            d['cluster_size']     = cluster.member_count
            d['embedding_center'] = cluster.embedding_center  # Phase 2 dedup
            d['novelty_score']    = cluster.novelty_score
            d['timeline']         = cluster.timeline
        result.append(d)
    return result


def main():
    parser = argparse.ArgumentParser(description='Generate stories from crawled data')
    parser.add_argument('--lang', choices=['en', 'zh'], default='en', help='Output language')
    parser.add_argument('--channel', type=int, choices=[1, 2, 3], default=1, help='Output channel')
    parser.add_argument('--format', nargs='+', default=None,
                        help='Formats to generate (space-separated): all, all_extended, explainer, top5, format_10, ... '
                             'Omit entirely when using --deep-story-only.')
    parser.add_argument('--dry-run', action='store_true', help='Show selections without generating')
    parser.add_argument('--deep-story', action='store_true',
                        help='Generate a hierarchical deep story + supporting stories batch')
    parser.add_argument('--deep-story-only', action='store_true',
                        help='Run ONLY the deep story pipeline — skip all flat format generation. '
                             'Implies --deep-story. Use this to replace the formats 1-9 run with deep story.')
    parser.add_argument('--no-repetition-penalty', action='store_true',
                        help='(Phase 3.2) Disable the repetition penalty that down-scores '
                             'clusters seen in recent batches. Penalty is ON by default.')
    parser.add_argument('--hours', type=int, default=48,
                        help='Lookback window in hours for candidate fetch (default 48)')
    parser.add_argument('--config-profile', default=os.getenv('STORY_RUN_PROFILE'),
                        help='Per-run overlay profile id (e.g. run2_ai). Reads '
                             'config/story_mix_<profile>.json as a shallow overlay '
                             'on top of story_mix.json. Default: base only.')
    args = parser.parse_args()

    # --deep-story-only implies --deep-story
    if args.deep_story_only:
        args.deep_story = True

    logger.info("=== story_engine generation run ===")
    logger.info("  lang=%s  channel=%d  format=%s  dry_run=%s  hours=%d  profile=%s  deep_story=%s  deep_story_only=%s",
                args.lang, args.channel, args.format, args.dry_run, args.hours,
                args.config_profile or '(base)', args.deep_story, args.deep_story_only)

    # Initialize DB (creates tables + runs migrations)
    init_db()

    # Step 1 — Convert CLI format strings to int IDs
    # In --deep-story-only mode, we still run selection with formats 1-9 so that
    # Stage 4 assigns candidates and build_clusters() has representatives to work with.
    # Flat format GENERATION is skipped in Step 3, but the selection pipeline must
    # see real format_ids — otherwise all_selected=[] → cluster_map={} → nothing generated.
    format_ids: list[int] = []
    fmt_input = args.format or ['all']
    for f in fmt_input:
        if f == 'all':
            format_ids.extend(range(1, 10))        # legacy 1–9
        elif f == 'all_extended':
            format_ids.extend(range(1, 47))        # all 46
        elif f.startswith('format_') and f[7:].isdigit():
            format_ids.append(int(f[7:]))          # "format_12" → 12
        elif f in FORMAT_NAME_TO_ID:
            format_ids.append(FORMAT_NAME_TO_ID[f])  # "top5" → 2
        else:
            logger.error("Unknown format: %s — skipping", f)

    if not format_ids:
        logger.error("No valid format IDs to run")
        return 1

    # Remove duplicates while preserving order
    seen: set[int] = set()
    deduped: list[int] = []
    for fid in format_ids:
        if fid not in seen:
            seen.add(fid)
            deduped.append(fid)
    format_ids = deduped

    if args.deep_story_only:
        logger.info(
            "--deep-story-only: selection will use %d format(s) to seed clustering "
            "(flat generation skipped)",
            len(format_ids),
        )
    else:
        logger.info("Running %d format(s): %s", len(format_ids), format_ids)

    if args.dry_run:
        logger.info("[DRY RUN] Selection and generation skipped")
        return 0

    # Step 2 — Run selection (all 4 stages) — one call, returns full item data
    # In deep-story-only mode, format_ids is still populated (1–9 by default) so
    # Stage 4 assigns candidates and build_clusters() has representatives to cluster.
    # The flat format generation loop in Step 3 is the only thing skipped.
    try:
        batch_result = selector.run_batch(
            format_ids     = format_ids,
            db_path        = DB_PATH,
            config_path    = CONFIG_PATH,
            lang           = args.lang,
            channel        = args.channel,
            hours          = args.hours,
            config_profile = args.config_profile,
        )
    except Exception as e:
        logger.error("Batch selection failed: %s", e, exc_info=True)
        return 1

    set_id    = batch_result.story_set_id
    batch_ts  = batch_result.batch_ts

    logger.info(
        "Selection complete: story_set_id=%d, partial=%s, trace=%s",
        set_id, batch_result.partial, batch_result.trace_path,
    )
    if batch_result.partial_formats:
        for pf in batch_result.partial_formats:
            logger.warning(
                "  Partial format %d (%s): assigned %d/%d — %s",
                pf.skipped_format_id,
                FORMAT_NAMES.get(pf.skipped_format_id, str(pf.skipped_format_id)),
                pf.candidate_count_after_filtering,
                pf.candidate_count_before_filtering,
                pf.blocking_constraint or pf.shortage_dimension,
            )

    # Step 3 — Run generation per format using pre-selected items
    # Skipped entirely in --deep-story-only mode.
    results: dict[int, int | None] = {}

    if not args.deep_story_only:
        for format_id, candidates in batch_result.format_assignments.items():
            fmt_name = FORMAT_NAMES.get(format_id, f'format_{format_id}')

            if not candidates:
                logger.info("  %s (format %d): no items assigned — skipping", fmt_name, format_id)
                results[format_id] = None
                continue

            item_dicts = _candidates_to_dicts(candidates, cluster_map=batch_result.cluster_map)
            logger.info("  %s (format %d): generating with %d item(s)",
                        fmt_name, format_id, len(item_dicts))

            try:
                if format_id <= 9:
                    story_id = _dispatch_legacy(
                        format_id, item_dicts,
                        lang=args.lang, channel=args.channel,
                        batch_id=set_id, batch_ts=batch_ts,
                    )
                elif format_id >= 100:
                    # English deep-dive formats (101–105): Bash-enabled research via
                    # generate_format_deep_en(); uses deep_dive_en.txt with {topic},
                    # {seed_urls}, {serper_key} — not compatible with generate_by_format().
                    story_id = generate_format_deep_en(
                        format_id, item_dicts,
                        lang=args.lang, channel=args.channel,
                        batch_id=set_id, batch_ts=batch_ts,
                    )
                else:
                    story_id = generate_by_format(
                        format_id, item_dicts,
                        lang=args.lang, channel=args.channel,
                        batch_id=set_id, batch_ts=batch_ts,
                    )
                results[format_id] = story_id
            except Exception as e:
                logger.error("  %s (format %d) generation failed: %s", fmt_name, format_id, e)
                results[format_id] = None

    # Step 3b — Deep story generation (opt-in via --deep-story flag)
    if args.deep_story:
        cluster_map = batch_result.cluster_map
        if not cluster_map:
            logger.warning("--deep-story: no cluster_map available — skipping")
        elif len(cluster_map) < 2:
            logger.warning(
                "--deep-story: only %d cluster(s) available — need at least 2 "
                "(1 deep + 1 supporting). Skipping.",
                len(cluster_map),
            )
        else:
            logger.info("--deep-story: orchestrating %d clusters...", len(cluster_map))
            try:
                orchestration_result = story_orchestrate(
                    cluster_map,
                    apply_repetition_penalty=not args.no_repetition_penalty,
                )
                if orchestration_result is None:
                    logger.warning("--deep-story: story_orchestrate() returned None — skipping")
                else:
                    # Deep story generation: claude -p --tools Bash does search + write
                    # in one pass — no separate research_engine call needed.
                    generate_story_batch(
                        orchestration_result = orchestration_result,
                        lang     = args.lang,
                        channel  = args.channel,
                        batch_id = set_id,
                        batch_ts = batch_ts,
                    )
                    logger.info("--deep-story: hierarchical story batch saved successfully")
            except Exception as _deep_err:
                logger.error("--deep-story: generation failed — %s", _deep_err, exc_info=True)
                # Non-fatal: flat format generation is unaffected

    # Step 4 — Mark story set status (generation outcome)
    if args.deep_story_only:
        # No flat formats were attempted — status reflects deep story only
        gen_status = 'complete'
    else:
        succeeded = sum(1 for v in results.values() if v is not None)
        total = len(results)
        if succeeded == total and not batch_result.partial:
            gen_status = 'complete'
        elif succeeded > 0:
            gen_status = 'partial'
        else:
            gen_status = 'failed'

    # Update story_sets status to reflect generation outcome
    # (Stage 4 already set it to "complete"/"partial" for selection;
    #  here we refine it based on generation success)
    complete_story_set(set_id, gen_status)

    if args.deep_story_only:
        logger.info("Story set #%d marked as '%s' (deep-story-only mode)", set_id, gen_status)
    else:
        logger.info("Story set #%d marked as '%s' (%d/%d formats generated)",
                    set_id, gen_status, succeeded, total)

    # Step 5 — Summary
    logger.info("=== Generation complete ===")
    if args.deep_story_only:
        logger.info("  deep-story-only mode — see hierarchical_stories table for output")
    else:
        for fid, story_id in results.items():
            fmt_name = FORMAT_NAMES.get(fid, f'format_{fid}')
            if story_id:
                logger.info("  %s (format %d): story #%d", fmt_name, fid, story_id)
            else:
                logger.info("  %s (format %d): skipped or failed", fmt_name, fid)

        if all(v is None for v in results.values()):
            logger.warning("All formats failed or were skipped")
            return 1

    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
