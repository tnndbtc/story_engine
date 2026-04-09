"""
Story generator — uses Claude CLI to produce scripts from selected items.

Calls `claude -p` as a subprocess (no API key needed).
Each format has its own prompt template and generation logic.

Output is parsed as JSON and saved to story_engine's own SQLite database.
"""

import json
import logging
import os
import subprocess
from pathlib import Path

from db.models import save_story, save_failed_story

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'
CLAUDE_TIMEOUT = 120  # seconds
CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', 'opus')  # opus / sonnet / haiku


def _call_claude(prompt: str) -> str:
    """
    Call Claude CLI and return the raw response text.

    Uses `claude -p` (pipe mode) which reads from stdin.
    No API key needed — uses the local Claude Code installation.
    """
    try:
        result = subprocess.run(
            ['claude', '-p', '--model', CLAUDE_MODEL],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude -p failed (exit {result.returncode}): {result.stderr}")
        return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found. Is Claude Code installed?")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude -p timed out after {CLAUDE_TIMEOUT}s")


def _parse_json_response(text: str) -> dict:
    """
    Parse JSON from Claude's response.

    Handles common issues:
    - Markdown code fences (```json ... ```)
    - Extra text before/after JSON
    - Logging raw response on failure for debugging
    """
    cleaned = text.strip()

    # Strip markdown code fences if present
    if cleaned.startswith('```'):
        lines = cleaned.split('\n')
        lines = [l for l in lines if not l.strip().startswith('```')]
        cleaned = '\n'.join(lines)

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response (between first { and last })
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Try to fix truncated JSON — close open strings, remove trailing commas, close brackets
    truncated = cleaned[start:] if start != -1 else cleaned
    # Close any unclosed string
    if truncated.count('"') % 2 == 1:
        truncated += '"'
    # Close arrays and objects
    open_brackets = truncated.count('[') - truncated.count(']')
    open_braces = truncated.count('{') - truncated.count('}')
    truncated += ']' * open_brackets + '}' * open_braces
    # Remove trailing commas before ] or } (invalid JSON)
    import re
    truncated = re.sub(r',\s*(\]|\})', r'\1', truncated)
    try:
        result = json.loads(truncated)
        logger.warning("Recovered truncated JSON response — some content may be missing")
        return result
    except json.JSONDecodeError:
        pass

    # All parsing failed — log raw response for debugging
    logger.error(f"Failed to parse JSON from Claude response:\n{text[:500]}")
    raise ValueError(f"Could not parse JSON from Claude response (length={len(text)})")


def _lang_instruction(lang: str) -> str:
    """Get language instruction for prompts."""
    if lang == 'zh':
        return "Write the ENTIRE script in Chinese (Simplified, zh-Hans). Read sources in any language, output in Chinese only."
    return "Write the script in English."


def _format_source(item: dict) -> dict:
    """Format a crawler item as a source reference for the story."""
    return {
        'url': item['url'],
        'platform': item['platform'],
        'hotness': round(item['hotness'], 1),
        'title': item.get('canonical_title') or item['title_original'],
    }


def generate_explainer(item: dict, lang: str = 'en', channel: int = 1) -> int:
    """
    Generate a 60-second explainer script (Format A).

    Args:
        item: Selected crawler item (from selector.select_for_explainer)
        lang: Output language ('en' or 'zh')
        channel: Output channel (1, 2, or 3)

    Returns:
        story_id from the story_engine database
    """
    template = (PROMPTS_DIR / 'explainer.txt').read_text()

    # Build engagement summary
    signals = item.get('engagement_signals', {})
    engagement_parts = []
    for key in ['score', 'upvotes', 'views', 'likes', 'comments', 'num_comments']:
        if key in signals and signals[key]:
            engagement_parts.append(f"{key}: {signals[key]:,}" if isinstance(signals[key], (int, float)) else f"{key}: {signals[key]}")
    engagement_str = ', '.join(engagement_parts) if engagement_parts else 'N/A'

    prompt = template.format(
        title=item.get('canonical_title') or item['title_original'],
        platform=item['platform'],
        region=item.get('region_name', item.get('region_key', 'unknown')),
        description=item.get('description_original') or 'No description available',
        engagement=engagement_str,
        url=item['url'],
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating explainer: {item['title_original'][:60]}...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='explainer',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item)],
        )

        logger.info(f"Explainer saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate explainer: {e}")
        title = item.get('canonical_title') or item['title_original']
        save_failed_story(title=title[:200], format='explainer', lang=lang, error=str(e))
        raise


def generate_top5(items: list[dict], lang: str = 'en', channel: int = 1) -> int:
    """
    Generate a Top 5 Today script (Format B).

    Args:
        items: List of 5 selected crawler items (from selector.select_for_top5)
        lang: Output language
        channel: Output channel

    Returns:
        story_id from the story_engine database
    """
    template = (PROMPTS_DIR / 'top5.txt').read_text()

    # Build the stories block
    stories_lines = []
    for i, item in enumerate(items, 1):
        signals = item.get('engagement_signals', {})
        engagement_parts = []
        for key in ['score', 'upvotes', 'views', 'likes', 'comments', 'num_comments']:
            if key in signals and signals[key]:
                engagement_parts.append(f"{key}: {signals[key]:,}" if isinstance(signals[key], (int, float)) else f"{key}: {signals[key]}")
        engagement_str = ', '.join(engagement_parts) if engagement_parts else 'N/A'

        title = item.get('canonical_title') or item['title_original']
        desc = item.get('description_original') or 'No description available'
        stories_lines.append(
            f"Story {i}:\n"
            f"  Title: {title}\n"
            f"  Source: {item['platform']} ({item.get('region_name', 'unknown')})\n"
            f"  Summary: {desc}\n"
            f"  Engagement: {engagement_str}\n"
            f"  URL: {item['url']}"
        )

    prompt = template.format(
        stories_block='\n\n'.join(stories_lines),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating top5 from {len(items)} items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='top5',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )

        logger.info(f"Top 5 saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate top5: {e}")
        save_failed_story(title="Top 5 Today", format='top5', lang=lang, error=str(e))
        raise


def _build_stories_block(items: list[dict]) -> str:
    """Build the stories_block text for multi-item prompts."""
    lines = []
    for i, item in enumerate(items, 1):
        signals = item.get('engagement_signals', {})
        engagement_parts = []
        for key in ['score', 'upvotes', 'views', 'likes', 'comments', 'num_comments']:
            if key in signals and signals[key]:
                engagement_parts.append(
                    f"{key}: {signals[key]:,}" if isinstance(signals[key], (int, float))
                    else f"{key}: {signals[key]}"
                )
        engagement_str = ', '.join(engagement_parts) if engagement_parts else 'N/A'

        title = item.get('canonical_title') or item['title_original']
        desc = item.get('description_original') or 'No description available'
        region = item.get('region_name', item.get('region_key', 'unknown'))
        lines.append(
            f"Story {i}:\n"
            f"  Title: {title}\n"
            f"  Source: {item['platform']} ({region})\n"
            f"  Region: {region}\n"
            f"  Summary: {desc}\n"
            f"  Engagement: {engagement_str}\n"
            f"  URL: {item['url']}"
        )
    return '\n\n'.join(lines)


def generate_radar(items: list[dict], lang: str = 'zh', channel: int = 2) -> int:
    """
    Generate "stories US media ignores" script (Format C).
    """
    template = (PROMPTS_DIR / 'radar.txt').read_text()
    prompt = template.format(
        stories_block=_build_stories_block(items),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating radar from {len(items)} non-US items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='radar',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )
        logger.info(f"Radar saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate radar: {e}")
        save_failed_story(title="Global Radar", format='radar', lang=lang, error=str(e))
        raise


def generate_regional(items: list[dict], region_name: str, lang: str = 'zh', channel: int = 2) -> int:
    """
    Generate regional perspective script (Format D).
    """
    template = (PROMPTS_DIR / 'regional.txt').read_text()
    prompt = template.format(
        region_name=region_name,
        stories_block=_build_stories_block(items),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating regional ({region_name}) from {len(items)} items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='regional',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )
        logger.info(f"Regional saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate regional ({region_name}): {e}")
        save_failed_story(title=f"Regional: {region_name}", format='regional', lang=lang, error=str(e))
        raise


def generate_two_takes(items: list[dict], lang: str = 'zh', channel: int = 2) -> int:
    """
    Generate framing contrast script (Format E — two takes).
    """
    template = (PROMPTS_DIR / 'two_takes.txt').read_text()
    prompt = template.format(
        stories_block=_build_stories_block(items),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating two_takes from {len(items)} diverse items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='two_takes',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )
        logger.info(f"Two takes saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate two_takes: {e}")
        save_failed_story(title="Two Takes", format='two_takes', lang=lang, error=str(e))
        raise


def generate_pattern(items: list[dict], lang: str = 'zh', channel: int = 2) -> int:
    """
    Generate cross-region pattern analysis (Format F).
    """
    template = (PROMPTS_DIR / 'pattern.txt').read_text()
    prompt = template.format(
        stories_block=_build_stories_block(items),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating pattern from {len(items)} multi-region items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='pattern',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )
        logger.info(f"Pattern saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate pattern: {e}")
        save_failed_story(title="Pattern Analysis", format='pattern', lang=lang, error=str(e))
        raise


def generate_viral(items: list[dict], lang: str = 'zh', channel: int = 2) -> int:
    """
    Generate "before it goes viral" script (Format G).
    """
    template = (PROMPTS_DIR / 'viral.txt').read_text()
    prompt = template.format(
        stories_block=_build_stories_block(items),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating viral from {len(items)} niche items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='viral',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )
        logger.info(f"Viral saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate viral: {e}")
        save_failed_story(title="Before It Goes Viral", format='viral', lang=lang, error=str(e))
        raise


def generate_deep_dive(items: list[dict], topic: str, lang: str = 'zh', channel: int = 2) -> int:
    """
    Generate weekly deep dive script (Format H).
    """
    template = (PROMPTS_DIR / 'deep_dive.txt').read_text()
    prompt = template.format(
        topic=topic,
        stories_block=_build_stories_block(items),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating deep_dive ({topic}) from {len(items)} items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='deep_dive',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )
        logger.info(f"Deep dive saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate deep_dive ({topic}): {e}")
        save_failed_story(title=f"Deep Dive: {topic}", format='deep_dive', lang=lang, error=str(e))
        raise


def generate_niche(items: list[dict], niche: str, lang: str = 'zh', channel: int = 2) -> int:
    """
    Generate niche focus script (Format I).
    """
    template = (PROMPTS_DIR / 'niche.txt').read_text()
    prompt = template.format(
        niche=niche,
        stories_block=_build_stories_block(items),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating niche ({niche}) from {len(items)} items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = save_story(
            title=script['title'],
            format='niche',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
        )
        logger.info(f"Niche saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate niche ({niche}): {e}")
        save_failed_story(title=f"Niche: {niche}", format='niche', lang=lang, error=str(e))
        raise
