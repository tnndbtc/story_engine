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

from db.crawler_reader import get_background_items
from db.models import save_story, save_failed_story, store_event
from engine.format_registry import FORMAT_REGISTRY, FORMAT_CONTEXT_COUNTS

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'prompts'
CLAUDE_TIMEOUT = 120  # seconds
CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', 'sonnet')  # opus / sonnet / haiku


def _extract_entities(title: str, sources: list[dict]) -> list[dict] | None:
    """
    Extract named entities from a story title + source titles using Claude Haiku.

    Returns a list of {text, type} dicts, e.g.:
        [{"text": "Eric Swalwell", "type": "PERSON"},
         {"text": "Congress",      "type": "ORG"},
         {"text": "Washington",    "type": "GPE"}]

    Entity types: PERSON, ORG, GPE (place), EVENT, PRODUCT, DATE
    Returns None on any failure — callers must treat None gracefully.
    Failure is logged and swallowed; entity extraction must never block saving.
    """
    source_titles = [s.get('title') or s.get('title_original') or '' for s in sources if s]
    source_titles = [t for t in source_titles if t][:3]  # cap at 3 source titles

    text_block = f"Story title: {title}"
    if source_titles:
        text_block += "\nSource titles:\n" + "\n".join(f"- {t}" for t in source_titles)

    prompt = (
        f"{text_block}\n\n"
        "Extract all named entities from the text above.\n"
        "Return a JSON array of objects with 'text' and 'type' fields.\n"
        "Types: PERSON, ORG, GPE, EVENT, PRODUCT, DATE\n"
        "Return ONLY the JSON array. No explanation. Example:\n"
        '[{"text": "Eric Swalwell", "type": "PERSON"}, {"text": "Congress", "type": "ORG"}]'
    )

    try:
        result = subprocess.run(
            [
                'claude', '-p',
                '--model', 'haiku',
                '--system-prompt',
                'You are a named entity extractor. Output ONLY a valid JSON array. '
                'No markdown, no explanation. Start with [ and end with ].',
                '--disable-slash-commands',
                '--tools', '',
                '--setting-sources', 'user',
                '--no-session-persistence',
                '--output-format', 'text',
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("_extract_entities: claude -p failed (exit %d)", result.returncode)
            return None

        raw = result.stdout.strip()
        # Strip markdown fences if present
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
            raw = raw.strip()

        entities = json.loads(raw)
        if not isinstance(entities, list):
            logger.warning("_extract_entities: unexpected response type %s", type(entities))
            return None

        # Validate and clean: keep only dicts with text + type
        valid = [
            {'text': e['text'], 'type': e['type']}
            for e in entities
            if isinstance(e, dict) and e.get('text') and e.get('type')
        ]
        logger.debug("_extract_entities: extracted %d entities from %r", len(valid), title[:50])
        return valid or None

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as _e:
        logger.warning("_extract_entities failed: %s", _e)
        return None
    except Exception as _e:
        logger.warning("_extract_entities unexpected error: %s", _e)
        return None


def _save_story_and_remember(
    *,
    title: str,
    format: str,
    channel: int,
    lang: str,
    hook: str,
    bullets: list,
    twist: str,
    sources: list[dict],
    comments_used: list | None = None,
    batch_id: int | None = None,
    batch_ts: int | None = None,
    embedding_center: list[float] | None = None,
    topic_clusters: list[dict] | None = None,
) -> int:
    """
    Save a story and record its event fingerprint in event_memory.

    Wraps save_story() + store_event() so future batches can avoid
    re-telling the same event within the 7-day dedup window.
    store_event() failure is logged and swallowed — it must never break generation.

    Args:
        embedding_center: Mean embedding vector of the event's source articles.
                          Pass item.get('embedding_center') for single-event formats.
                          Leave None for multi-item format stories.
                          Enables Phase 2 cosine dedup in memory.py.
        topic_clusters:   Structured cluster data for multi-item format stories.
                          None for single-item formats (cluster members are in sources).
    """
    story_id = save_story(
        title=title,
        format=format,
        channel=channel,
        lang=lang,
        hook=hook,
        bullets=bullets,
        twist=twist,
        sources=sources,
        comments_used=comments_used,
        batch_id=batch_id,
        batch_ts=batch_ts,
        topic_clusters=topic_clusters,
    )
    try:
        entities = _extract_entities(title, sources)
        store_event(
            story_id=story_id,
            story_set_id=batch_id,
            story_title=title,
            sources=sources,
            embedding_center=embedding_center,
            entities=entities,
        )
    except Exception as _e:
        logger.warning("store_event failed (story #%d): %s", story_id, _e)
    return story_id


def _call_claude(prompt: str) -> str:
    """
    Call Claude CLI and return the raw response text.

    Uses `claude -p` (pipe mode) which reads from stdin.
    No API key needed — uses the local Claude Code installation.
    """
    # Append conciseness, formatting, and JSON enforcement reminders
    prompt = prompt.rstrip() + (
        "\n\nIMPORTANT: Keep your TOTAL JSON response under 500 words. Be concise. Every bullet should be 1-2 sentences max."
        "\n\nCRITICAL JSON RULE: Inside JSON string values, NEVER use ASCII double quotes (\"). "
        "Use Chinese quotation marks \u300c\u300d or \u201c\u201d instead. "
        "ASCII double quotes inside strings will break the JSON parser."
        "\n\nYOU MUST RETURN VALID JSON ONLY. Do not ask questions. Do not explain. "
        "Do not add any text outside the JSON object. Your response must start with { "
        "and end with }. If you cannot generate the story, return: "
        "{\"hook\": \"\", \"bullets\": [], \"twist\": \"\"}"
    )

    # 2026-04-14: strip Claude Code harness overhead on every subprocess.
    # See crawler topic_llm_classifier.py (commit 7163c925) for full
    # rationale — each plain `claude -p` call was burning ~15-25 KB of
    # input tokens on the default system prompt, skill manifests, tool
    # catalogue, and CLAUDE.md that story generation never uses. Flags
    # strip the harness down to just the model + our prompt.
    try:
        result = subprocess.run(
            [
                'claude', '-p',
                '--model', CLAUDE_MODEL,
                '--system-prompt',
                'You are a short-form story script generator. Output ONLY '
                'the JSON object the user prompt requests. No prose, no '
                'markdown fences, no questions. Start with { and end with }.',
                '--disable-slash-commands',
                '--tools', '',
                '--setting-sources', 'user',
                '--no-session-persistence',
                '--output-format', 'text',
            ],
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

    # Use raw_decode to find ALL complete JSON objects in the response.
    # The agent sometimes outputs prose + draft JSON + explanation + final JSON;
    # raw_decode correctly handles each object's boundaries, and we prefer the
    # LAST object (the agent's final/corrected version).
    _decoder = json.JSONDecoder()
    _found_objects: list[dict] = []
    _i = 0
    while _i < len(cleaned):
        if cleaned[_i] == '{':
            try:
                _obj, _end = _decoder.raw_decode(cleaned, _i)
                if isinstance(_obj, dict):
                    _found_objects.append(_obj)
                _i = _end
            except json.JSONDecodeError:
                _i += 1
        else:
            _i += 1
    if _found_objects:
        # Return the last object — that is the agent's final output
        return _found_objects[-1]

    # Fallback: find JSON object in the response (between first { and last })
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end != -1 and end > start:
        extracted = cleaned[start:end + 1]
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

        # Fix unescaped ASCII quotes inside JSON string values.
        # Claude sometimes writes "word" with raw " inside Chinese text.
        # Strategy: iteratively try replacing problematic quotes until JSON parses.
        import re
        fixed = extracted
        # Replace " surrounded by non-JSON-structural characters (CJK, punctuation)
        # Pattern: non-whitespace/bracket/colon + " + non-whitespace/bracket/colon
        fixed = re.sub(
            r'(?<=[^\s\[\]{},:])"(?=[^\s\[\]{},:"])',
            '\u201c', fixed
        )
        fixed = re.sub(
            r'(?<=[^\s\[\]{},:"])"(?=[,\]\}\s\n])',
            '\u201d', fixed
        )
        try:
            result = json.loads(fixed)
            logger.warning("Fixed unescaped quotes in JSON response")
            return result
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

    # All parsing failed — log FULL raw response for debugging
    logger.error(
        f"Failed to parse JSON from Claude response (length={len(text)}).\n"
        f"=== FULL RAW RESPONSE START ===\n{text}\n=== FULL RAW RESPONSE END ==="
    )
    raise ValueError(f"Could not parse JSON from Claude response (length={len(text)})")


def _generate_script(prompt: str) -> dict:
    """
    Call Claude and parse the JSON response, with one retry on parse failure.

    On first failure, appends a stricter JSON instruction and retries once.
    This handles cases where Claude returns clarification text instead of JSON.
    """
    try:
        raw = _call_claude(prompt)
        return _parse_json_response(raw)
    except ValueError:
        logger.warning("JSON parse failed on first attempt — retrying with stricter instruction")
        retry_prompt = prompt + (
            "\n\nYour previous response could not be parsed as JSON. "
            "Return ONLY a valid JSON object. Start your response with { and end with }. "
            "No explanations, no questions, no text outside the JSON object."
        )
        raw = _call_claude(retry_prompt)
        return _parse_json_response(raw)


def _lang_instruction(lang: str) -> str:
    """Get language instruction for prompts."""
    if lang == 'zh':
        return "Write the ENTIRE script in Chinese (Simplified, zh-Hans). Read sources in any language, output in Chinese only."
    return "Write the script in English."


def _build_research_block(kp: dict) -> str:
    """
    Format a knowledge_pack from research_engine into a prompt context block.

    Injected BEFORE cluster sources — research_engine provides externally
    verified facts and entity background, making it the highest-authority layer.
    Claude should treat VERIFIED claims as ground truth, PARTIALLY_VERIFIED as
    probable, and uncertainty_notes as constraints on what NOT to assert.
    """
    lines = ["## Verified Research Context"]

    # Verified and partially verified facts only — skip UNVERIFIED / CONTRADICTED
    fact_blocks = kp.get("fact_blocks", [])
    verified = [
        f for f in fact_blocks
        if f.get("status") in ("VERIFIED", "PARTIALLY_VERIFIED")
    ]
    if verified:
        lines.append("\nVerified Facts:")
        for fb in verified:
            marker = "✓" if fb["status"] == "VERIFIED" else "~"
            sources = ", ".join(
                s.get("title", "") for s in fb.get("sources", [])[:2]
            )
            lines.append(f"  {marker} {fb['claim']}")
            if sources:
                lines.append(f"    Source: {sources}")

    # Entity background (from Wikipedia + Planner B context expansion)
    context_blocks = kp.get("context_blocks", [])
    if context_blocks:
        lines.append("\nBackground Context:")
        for cb in context_blocks:
            lines.append(f"  [{cb['entity']}] {cb['background']}")

    # Uncertainty notes — hard constraints on what Claude must NOT assert as fact
    notes = kp.get("uncertainty_notes", [])
    if notes:
        lines.append("\nUncertainty Notes — do NOT assert these as verified fact:")
        for note in notes:
            lines.append(f"  ⚠ {note}")

    confidence = kp.get("confidence_overall")
    if confidence is not None:
        lines.append(f"\nResearch confidence: {confidence:.0%}")

    return "\n".join(lines)


def _build_cluster_context_block(item: dict) -> str:
    """
    Build a structured corroborating sources prompt block from cluster mates.

    GAP-GEN-1: Sources are presented in three LABELLED sections so Claude
    knows the hierarchy: fact_sources = primary evidence; reaction_sources =
    public response; context_sources = background depth only.

    Returns an empty string when no cluster data is present (singleton cluster
    or embeddings not yet available), so callers can append unconditionally.
    """
    fact_sources     = item.get('fact_sources', [])
    context_sources  = item.get('context_sources', [])
    reaction_sources = item.get('reaction_sources', [])

    if not fact_sources and not context_sources and not reaction_sources:
        return ''

    def _fmt_source(src: dict, idx: int) -> str:
        title = src.get('canonical_title') or src.get('title_original') or src.get('title', '')
        desc  = src.get('description_original', '') or ''
        return (
            f"  [{idx}] {src.get('platform', 'unknown')}: {title}\n"
            f"       URL: {src.get('url', '')}"
            + (f"\n       Summary: {desc[:200]}" if desc else "")
        )

    lines = ["## Corroborating Sources"]
    idx = 1

    if fact_sources:
        lines.append(
            "\n### CORE EVENT SOURCES (fact sources — Reuters, AP, BBC, Bloomberg etc.)\n"
            "Use these as primary evidence. Each bullet's lead claim must trace back here."
        )
        for src in fact_sources[:3]:
            lines.append(_fmt_source(src, idx))
            idx += 1

    if reaction_sources:
        lines.append(
            "\n### REACTIONS (public and market responses — Reddit, YouTube, Twitter)\n"
            "Use these to show how people / markets responded. Do not use as factual claims."
        )
        for src in reaction_sources[:2]:
            lines.append(_fmt_source(src, idx))
            idx += 1

    if context_sources:
        lines.append(
            "\n### CONTEXT & BACKGROUND (regional outlets, analysis pieces)\n"
            "Use these for depth and supporting detail ONLY.\n"
            "Do NOT use a context source as the lead claim of any bullet point."
        )
        for src in context_sources[:2]:
            lines.append(_fmt_source(src, idx))
            idx += 1

    cluster_size = item.get('cluster_size', 1)
    if cluster_size > 1:
        lines.append(f"\n(Event confirmed across {cluster_size} sources in total)")

    return "\n".join(lines)


def _format_source(item: dict) -> dict:
    """Format a crawler item as a source reference for the story."""
    return {
        'url': item['url'],
        'platform': item['platform'],
        'hotness': round(item['hotness'], 1),
        'title': item.get('canonical_title') or item['title_original'],
    }


def _build_source_list(item: dict) -> list[dict]:
    """
    Build full source list for single-item format stories.
    Includes the representative article plus all cluster members
    (fact_sources, context_sources, reaction_sources) that were
    gathered by build_clusters() and injected by _candidates_to_dicts().
    The 'role' field on members enables downstream provenance tracing.
    """
    sources = [_format_source(item)]
    for bucket_key in ('fact_sources', 'context_sources', 'reaction_sources'):
        for member in item.get(bucket_key, []):
            sources.append({
                'url':      member.get('url', ''),
                'platform': member.get('platform', ''),
                'hotness':  round(float(member.get('hotness', 0.0)), 1),
                'title':    member.get('canonical_title') or member.get('title_original', ''),
                'role':     bucket_key.replace('_sources', ''),
            })
    return sources


def _build_topic_clusters(items: list[dict]) -> list[dict] | None:
    """
    Build structured topic cluster data for multi-item format stories.

    Each item dict is already enriched by _candidates_to_dicts() in run.py,
    so fact_sources / context_sources / reaction_sources are embedded as
    lists of source dicts (from _candidate_to_source_dict()).

    Returns None when no item has any cluster members at all (all singletons)
    — no point writing an empty structure to the DB.

    topic_clusters schema per element:
      {
        event_id:         str,          # sha256[:16] of representative URL
        representative:   source_dict,  # {url, platform, hotness, title}
        fact_sources:     [source_dict, ...],
        context_sources:  [source_dict, ...],
        reaction_sources: [source_dict, ...],
      }
    """
    import hashlib as _hashlib
    result = []
    has_any_cluster = False
    for item in items:
        fact      = item.get('fact_sources', [])
        context   = item.get('context_sources', [])
        reaction  = item.get('reaction_sources', [])
        if fact or context or reaction:
            has_any_cluster = True
        event_id = _hashlib.sha256(item.get('url', '').encode()).hexdigest()[:16]
        result.append({
            'event_id':         event_id,
            'representative':   _format_source(item),
            'fact_sources':     [_format_source(m) for m in fact],
            'context_sources':  [_format_source(m) for m in context],
            'reaction_sources': [_format_source(m) for m in reaction],
        })
    return result if has_any_cluster else None


def _extract_comments(item: dict, max_comments: int = 5) -> list[str]:
    """
    Extract top_comments from item's raw_payload.

    Comments may be stored as plain strings or dicts with a 'text'/'body' key
    depending on the collector. Returns a list of plain text strings, truncated
    to 300 chars each.
    """
    payload = item.get('raw_payload') or {}
    raw = payload.get('top_comments', [])
    if not raw:
        return []

    texts = []
    for c in raw[:max_comments]:
        if isinstance(c, str):
            texts.append(c.strip())
        elif isinstance(c, dict):
            text = c.get('text') or c.get('body') or c.get('content') or ''
            if text:
                texts.append(str(text).strip())
    return [t[:300] for t in texts if t]


def _format_comments_block(item: dict, max_comments: int = 5) -> str:
    """
    Format top_comments for inclusion in a prompt stories_block.

    Returns an empty string when no comments are available so callers
    can append it unconditionally without adding blank lines.
    """
    texts = _extract_comments(item, max_comments)
    if not texts:
        return ''
    lines = '\n'.join(f"    {i + 1}. {t}" for i, t in enumerate(texts))
    return f"  Top Comments ({len(texts)}):\n{lines}"


def _collect_comments_used(items: list[dict], max_per_item: int = 5) -> list[dict]:
    """
    Collect all comments included in the prompt, for storage in comments_used.
    """
    used = []
    for item in items:
        for text in _extract_comments(item, max_per_item):
            used.append({'text': text, 'platform': item['platform']})
    return used


def generate_explainer(item: dict, lang: str = 'en', channel: int = 1, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate a 60-second explainer script (Format 1).

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

    # New-development annotation: prepend update notice when item is a follow-up
    update_notice = ""
    if item.get('is_new_development') and item.get('prior_story_title'):
        update_notice = (
            f"\n\n[UPDATE STORY] This article is a NEW DEVELOPMENT following a story "
            f"already told: \"{item['prior_story_title']}\". "
            f"Frame your script as an update — acknowledge the prior context briefly, "
            f"then focus on what is new."
        )

    # Cluster block — same-event corroboration (fact + context sources)
    cluster_block = _build_cluster_context_block(item)

    # Background context — historical/category context (different from cluster)
    context_items: list[dict] = []
    category = item.get('story_category') if item else None
    exclude_urls = [item.get('url', '')] if item else []
    context_items = get_background_items(
        category=category,
        exclude_urls=exclude_urls,
        limit=2,
        hours=168,
    )
    background_block = ""
    if context_items:
        background_block = (
            "\n\n## Background Context\n"
            "NOTE: Do not build the story around these articles. "
            "Use them only for facts, history, statistics, and supporting detail.\n\n"
            + _build_stories_block(context_items)
        )

    # Combine: update notice → cluster sources → historical background
    context_block = update_notice
    if cluster_block:
        context_block += "\n\n" + cluster_block
    if background_block:
        context_block += background_block

    prompt = template.format(
        title=item.get('canonical_title') or item['title_original'],
        platform=item['platform'],
        region=item.get('region_name', item.get('region_key', 'unknown')),
        description=item.get('description_original') or 'No description available',
        engagement=engagement_str,
        url=item['url'],
        lang_instruction=_lang_instruction(lang),
        context_block=context_block,
    )

    logger.info(f"Generating explainer: {item['title_original'][:60]}...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = _save_story_and_remember(
            title=script['title'],
            format='explainer',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=_build_source_list(item),
            batch_id=batch_id,
            batch_ts=batch_ts,
            embedding_center=item.get('embedding_center'),
        )

        logger.info(f"Explainer saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate explainer: {e}")
        title = item.get('canonical_title') or item['title_original']
        save_failed_story(title=title[:200], format='explainer', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_top5(items: list[dict], lang: str = 'en', channel: int = 1, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate a Top 5 Today script (Format 2).

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

        story_id = _save_story_and_remember(
            title=script['title'],
            format='top5',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )

        logger.info(f"Top 5 saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate top5: {e}")
        save_failed_story(title="Top 5 Today", format='top5', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def _build_stories_block(
    items: list[dict],
    context_items: list[dict] | None = None,
) -> str:
    """Build the stories_block text for multi-item prompts.

    Includes top_comments from raw_payload when available. Format 26 (情绪解读)
    and format 31 (热门评论精选) explicitly instruct the LLM to prioritize
    comment content when present; other formats benefit from the added context.

    When context_items is None or empty, behavior is identical to the original
    (no section headers). When context_items is non-empty, main items are
    wrapped under "## Current Development" and context items are appended
    under "## Background Context".
    """
    def _render_items(item_list: list[dict], start_index: int = 1) -> str:
        lines = []
        for i, item in enumerate(item_list, start_index):
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

            block = (
                f"Story {i}:\n"
                f"  Title: {title}\n"
                f"  Source: {item['platform']} ({region})\n"
                f"  Region: {region}\n"
                f"  Summary: {desc}\n"
                f"  Engagement: {engagement_str}\n"
                f"  URL: {item['url']}"
            )

            # Cluster metadata: show coverage breadth when multi-source data exists
            cluster_size = item.get('cluster_size', 0)
            fact_count   = len(item.get('fact_sources', []))
            if cluster_size > 1:
                coverage = f"{cluster_size} sources"
                if fact_count:
                    coverage += f" (incl. {fact_count} authoritative)"
                block += f"\n  Coverage: {coverage}"

            # New-development flag: tell the LLM this is an update, not a retelling
            if item.get('is_new_development') and item.get('prior_story_title'):
                block += f"\n  [UPDATE] This is a new development following: \"{item['prior_story_title']}\""

            comments_block = _format_comments_block(item)
            if comments_block:
                block += f"\n{comments_block}"

            lines.append(block)
        return '\n\n'.join(lines)

    if not context_items:
        return _render_items(items)

    main_block = "## Current Development\n\n" + _render_items(items, start_index=1)
    ctx_block = (
        "## Background Context\n"
        "NOTE: Do not build the story around these articles. "
        "Use them only for facts, history, statistics, and supporting detail.\n\n"
        + _render_items(context_items, start_index=len(items) + 1)
    )
    return main_block + "\n\n" + ctx_block


def generate_radar(items: list[dict], lang: str = 'zh', channel: int = 2, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate "stories US media ignores" script (Format 3).
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

        story_id = _save_story_and_remember(
            title=script['title'],
            format='radar',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"Radar saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate radar: {e}")
        save_failed_story(title="Global Radar", format='radar', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_regional(items: list[dict], region_name: str, lang: str = 'zh', channel: int = 2, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate regional perspective script (Format 4).
    """
    template = (PROMPTS_DIR / 'regional.txt').read_text()

    context_items: list[dict] = []
    if items:
        category = items[0].get('story_category') if items else None
        exclude_urls = [item.get('url', '') for item in items]
        context_items = get_background_items(
            category=category,
            exclude_urls=exclude_urls,
            limit=2,
            hours=168,
        )

    prompt = template.format(
        region_name=region_name,
        stories_block=_build_stories_block(items, context_items or None),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating regional ({region_name}) from {len(items)} items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = _save_story_and_remember(
            title=script['title'],
            format='regional',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"Regional saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate regional ({region_name}): {e}")
        save_failed_story(title=f"Regional: {region_name}", format='regional', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_two_takes(items: list[dict], lang: str = 'zh', channel: int = 2, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate framing contrast script (Format 5 — two takes).
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

        story_id = _save_story_and_remember(
            title=script['title'],
            format='two_takes',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"Two takes saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate two_takes: {e}")
        save_failed_story(title="Two Takes", format='two_takes', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_pattern(items: list[dict], lang: str = 'zh', channel: int = 2, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate cross-region pattern analysis (Format 6).
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

        story_id = _save_story_and_remember(
            title=script['title'],
            format='pattern',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"Pattern saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate pattern: {e}")
        save_failed_story(title="Pattern Analysis", format='pattern', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_viral(items: list[dict], lang: str = 'zh', channel: int = 2, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate "before it goes viral" script (Format 7).
    """
    template = (PROMPTS_DIR / 'viral.txt').read_text()

    context_items: list[dict] = []
    if items:
        category = items[0].get('story_category') if items else None
        exclude_urls = [item.get('url', '') for item in items]
        context_items = get_background_items(
            category=category,
            exclude_urls=exclude_urls,
            limit=2,
            hours=168,
        )

    prompt = template.format(
        stories_block=_build_stories_block(items, context_items or None),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating viral from {len(items)} niche items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = _save_story_and_remember(
            title=script['title'],
            format='viral',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"Viral saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate viral: {e}")
        save_failed_story(title="Before It Goes Viral", format='viral', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_deep_dive(items: list[dict], topic: str, lang: str = 'zh', channel: int = 2, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate weekly deep dive script (Format 8).
    """
    template = (PROMPTS_DIR / 'deep_dive.txt').read_text()

    context_items: list[dict] = []
    if items:
        category = items[0].get('story_category') if items else None
        exclude_urls = [item.get('url', '') for item in items]
        context_items = get_background_items(
            category=category,
            exclude_urls=exclude_urls,
            limit=3,
            hours=168,
        )

    prompt = template.format(
        topic=topic,
        stories_block=_build_stories_block(items, context_items or None),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating deep_dive ({topic}) from {len(items)} items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = _save_story_and_remember(
            title=script['title'],
            format='deep_dive',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"Deep dive saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate deep_dive ({topic}): {e}")
        save_failed_story(title=f"Deep Dive: {topic}", format='deep_dive', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_niche(items: list[dict], niche: str, lang: str = 'zh', channel: int = 2, batch_id: int | None = None, batch_ts: int | None = None) -> int:
    """
    Generate niche focus script (Format 9).
    """
    template = (PROMPTS_DIR / 'niche.txt').read_text()

    context_items: list[dict] = []
    if items:
        category = items[0].get('story_category') if items else None
        exclude_urls = [item.get('url', '') for item in items]
        context_items = get_background_items(
            category=category,
            exclude_urls=exclude_urls,
            limit=2,
            hours=168,
        )

    prompt = template.format(
        niche=niche,
        stories_block=_build_stories_block(items, context_items or None),
        lang_instruction=_lang_instruction(lang),
    )

    logger.info(f"Generating niche ({niche}) from {len(items)} items...")

    try:
        raw = _call_claude(prompt)
        script = _parse_json_response(raw)

        story_id = _save_story_and_remember(
            title=script['title'],
            format='niche',
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            batch_id=batch_id,
            batch_ts=batch_ts,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"Niche saved: story #{story_id} — {script['title'][:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate niche ({niche}): {e}")
        save_failed_story(title=f"Niche: {niche}", format='niche', lang=lang, error=str(e), batch_id=batch_id, batch_ts=batch_ts)
        raise


def generate_by_format(
    format_id: int,
    items: list[dict],
    lang: str = 'zh',
    channel: int = 2,
    batch_id: int | None = None,
    batch_ts: int | None = None,
) -> int:
    """
    Generic generator for formats 10-46.
    Reads the corresponding prompt template and calls Claude.

    Comments from raw_payload are included in stories_block automatically via
    _build_stories_block(). Format 26 (情绪解读) and format 31 (热门评论精选)
    instruct the LLM to prioritize comment content when present.
    """
    from engine.format_registry import FORMAT_NAMES

    if format_id not in FORMAT_REGISTRY:
        raise ValueError(f"Unknown format_id: {format_id}")

    _, prompt_file, _, ctx_count = FORMAT_REGISTRY[format_id]
    format_name = FORMAT_NAMES.get(format_id, f'format_{format_id}')
    format_key = f'format_{format_id}'

    template_path = PROMPTS_DIR / prompt_file
    if not template_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {template_path}")

    context_items: list[dict] = []
    if ctx_count > 0 and items:
        category = items[0].get('story_category') if items else None
        # NOTE: category derived from first item only. Acceptable for current
        # formats — all multi-item formats with ctx>0 are expected same-category.
        exclude_urls = [item.get('url', '') for item in items]
        context_items = get_background_items(
            category=category,
            exclude_urls=exclude_urls,
            limit=ctx_count,
            hours=168,
        )
        if context_items:
            logger.debug(
                f"{format_name}: fetched {len(context_items)} background items "
                f"(category={category!r})"
            )

    template = template_path.read_text()
    stories_block = _build_stories_block(items, context_items or None)  # includes top_comments when available

    # For single-item formats, inject cluster corroboration block when available.
    # Multi-item formats already surface cluster_size/fact_count via _render_items.
    cluster_inject = ""
    if len(items) == 1:
        cluster_inject = _build_cluster_context_block(items[0])

    prompt = template.format(
        stories_block=stories_block + ("\n\n" + cluster_inject if cluster_inject else ""),
        lang_instruction=_lang_instruction(lang),
    )

    # Collect all comments that appear in the prompt for auditing
    comments_used = _collect_comments_used(items)
    if comments_used:
        logger.debug(f"{format_name}: {len(comments_used)} comments included in prompt")

    logger.info(f"Generating {format_name} (format_{format_id}) from {len(items)} items...")

    try:
        script = _generate_script(prompt)

        story_id = _save_story_and_remember(
            title=script.get('title', format_name),
            format=format_key,
            channel=channel,
            lang=lang,
            hook=script.get('hook', ''),
            bullets=script.get('bullets', []),
            twist=script.get('twist', ''),
            sources=[_format_source(item) for item in items],
            comments_used=comments_used or None,
            batch_id=batch_id,
            batch_ts=batch_ts,
            # Single-item formats: pass embedding_center for Phase 2 cosine dedup.
            # Multi-item formats: None (Jaccard fallback is fine for aggregated stories).
            embedding_center=items[0].get('embedding_center') if len(items) == 1 else None,
            topic_clusters=_build_topic_clusters(items),
        )
        logger.info(f"{format_name} saved: story #{story_id} — {script.get('title', '')[:50]}")
        return story_id

    except Exception as e:
        logger.error(f"Failed to generate {format_name}: {e}")
        save_failed_story(
            title=format_name, format=format_key, lang=lang,
            error=str(e), batch_id=batch_id, batch_ts=batch_ts,
        )
        raise


# ── Deep story: agent constants ───────────────────────────────────────────────

_DEEP_SYSTEM_PROMPT = (
    "You are an investigative journalist and YouTube storyteller. "
    "FIRST research the story using iterative Bash searches, THEN write the final narrative. "
    "Output ONLY a valid JSON object — no prose, no markdown before or after. "
    "Your entire response must start with { and end with }."
)

_DEEP_TIMEOUT = 600  # 10 minutes — iterative Bash tool calls take time


def _call_claude_agent_deep(prompt: str) -> tuple[str, int]:
    """
    Call claude -p with Bash tool enabled for deep story research + writing.

    Claude will:
      1. Reason about the story mechanism
      2. Use Bash to curl Serper repeatedly with targeted queries
      3. Write a 900–1100 Chinese character spoken narrative
      4. Output JSON: {title, body, sources}

    Returns:
        (raw_output: str, token_estimate: int)
        token_estimate = (len(prompt) + len(output)) // 4 — a char/4 proxy
        since claude -p subprocess does not expose token counts in stdout.

    Raises RuntimeError on timeout, missing CLI, or non-zero exit.
    """
    try:
        result = subprocess.run(
            [
                'claude', '-p',
                '--model',               'claude-sonnet-4-5',
                '--tools',               'Bash',
                '--output-format',       'text',
                '--no-session-persistence',
                '--system-prompt',       _DEEP_SYSTEM_PROMPT,
                '--disable-slash-commands',
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_DEEP_TIMEOUT,
            env={**os.environ},          # passes SERPER_API_KEY + all env vars
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"deep story agent timed out after {_DEEP_TIMEOUT}s")
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found in PATH")

    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (exit {result.returncode}): {result.stderr[:400]}"
        )

    raw = result.stdout.strip()
    token_estimate = (len(prompt) + len(raw)) // 4
    logger.info(
        "_call_claude_agent_deep: output=%d chars, token_estimate=%d",
        len(raw), token_estimate,
    )
    return raw, token_estimate


# ── Deep story generation ──────────────────────────────────────────────────────

def _cluster_to_item_dict(cluster) -> dict:
    """
    Convert an EventCluster to the item dict format generators expect.
    Mirrors _candidates_to_dicts() / _candidate_to_source_dict() in run.py.
    """
    rep = cluster.representative
    return {
        'url':                  rep.url,
        'platform':             rep.platform,
        'hotness':              rep.hotness,
        'category':             rep.category,
        'story_category':       rep.category,
        'canonical_title':      rep.canonical_title,
        'title_original':       rep.title_original,
        'description_original': rep.description_original,
        'region_key':           rep.region_key,
        'region_name':          rep.region_name,
        'engagement_signals':   rep.engagement_signals,
        'raw_payload':          rep.raw_payload,
        'title':                rep.canonical_title or rep.title_original,
        'id':                   rep.crawler_item_id,
        'is_new_development':   rep.is_new_development,
        'prior_story_title':    rep.prior_story_title,
        # Cluster-level fields
        'fact_sources':     [
            {
                'url':                  m.url,
                'platform':             m.platform,
                'hotness':              m.hotness,
                'title_original':       m.title_original,
                'canonical_title':      m.canonical_title,
                'description_original': m.description_original,
                'title':                m.canonical_title or m.title_original,
                'id':                   m.crawler_item_id,
            }
            for m in cluster.fact_sources
            if m.candidate_id != rep.candidate_id
        ],
        'context_sources':  [
            {
                'url':                  m.url,
                'platform':             m.platform,
                'hotness':              m.hotness,
                'title_original':       m.title_original,
                'canonical_title':      m.canonical_title,
                'description_original': m.description_original,
                'title':                m.canonical_title or m.title_original,
                'id':                   m.crawler_item_id,
            }
            for m in cluster.context_sources
            if m.candidate_id != rep.candidate_id
        ],
        'reaction_sources': [
            {
                'url':                  m.url,
                'platform':             m.platform,
                'hotness':              m.hotness,
                'title_original':       m.title_original,
                'canonical_title':      m.canonical_title,
                'description_original': m.description_original,
                'title':                m.canonical_title or m.title_original,
                'id':                   m.crawler_item_id,
            }
            for m in cluster.reaction_sources
            if m.candidate_id != rep.candidate_id
        ],
        'event_hotness':    cluster.event_hotness,
        'cluster_size':     cluster.member_count,
        'embedding_center': cluster.embedding_center,
        'novelty_score':    cluster.novelty_score,
        'timeline':         cluster.timeline,
    }


def generate_deep_story(
    cluster,
    lang:           str = 'en',
    channel:        int = 1,
    batch_id:       int | None = None,
    batch_ts:       int | None = None,
    knowledge_pack: dict | None = None,  # kept for signature compat — ignored; agent self-searches
) -> dict:
    """
    Generate a deep story from an EventCluster using a single claude -p --tools Bash call.

    The agent:
      1. Receives the topic + crawler seed URLs
      2. Iteratively searches Serper via Bash tool (6+ queries)
      3. Writes a 900–1100 Chinese character spoken narrative
      4. Returns JSON: {title, body, sources}

    sources in the returned dict merges:
      - all crawler cluster members (representative + fact/context/reaction)
      - all URLs Claude found during research (deduplicated)

    Returns a script dict compatible with generate_story_batch() / save_hierarchical_story().
    Does NOT save to DB — caller handles persistence.

    token_estimate is included in the returned dict for logging + UI display.
    """
    item = _cluster_to_item_dict(cluster)

    # Build seed URLs: representative + all cluster members (deduped, capped at 10)
    rep = cluster.representative
    all_members = (
        [rep]
        + cluster.fact_sources
        + cluster.context_sources
        + cluster.reaction_sources
    )
    seen_urls: set[str] = set()
    seed_urls: list[str] = []
    for m in all_members:
        url = getattr(m, 'url', '') or ''
        if url and url not in seen_urls:
            seen_urls.add(url)
            seed_urls.append(url)
        if len(seed_urls) >= 10:
            break

    seed_block = '\n'.join(f'  - {u}' for u in seed_urls) if seed_urls else '  (none provided)'
    serper_key = os.environ.get('SERPER_API_KEY', '')

    topic_title = (
        rep.canonical_title or rep.title_original
        or item.get('canonical_title') or item.get('title_original')
        or 'unknown topic'
    )

    template = (PROMPTS_DIR / 'deep_dive.txt').read_text()
    prompt = template.format(
        topic=topic_title,
        seed_urls=seed_block,
        serper_key=serper_key,
    )

    logger.info(
        "generate_deep_story: launching claude -p --tools Bash for cluster %s "
        "(%d members, %d seed URLs)",
        cluster.event_id, cluster.member_count, len(seed_urls),
    )

    raw, token_estimate = _call_claude_agent_deep(prompt)

    # Parse the JSON output from the agent
    try:
        parsed = _parse_json_response(raw)
    except ValueError:
        logger.warning(
            "generate_deep_story: JSON parse failed on first attempt — retrying"
        )
        # Append stricter instruction and run a plain (no-Bash) retry
        retry_prompt = (
            "The following text must be parsed as JSON but failed. "
            "Extract the title, body, and sources and return ONLY valid JSON "
            "starting with { and ending with }.\n\n" + raw
        )
        retry_raw = _call_claude(retry_prompt)
        parsed = _parse_json_response(retry_raw)

    # Build combined source list: crawler sources + newly searched sources
    crawler_sources = _build_source_list(item)
    existing_urls: set[str] = {s['url'] for s in crawler_sources}
    searched_sources: list[dict] = []
    for src in parsed.get('sources', []):
        url = (src.get('url') or '').strip()
        if url and url not in existing_urls:
            existing_urls.add(url)
            searched_sources.append({
                'url':      url,
                'title':    src.get('title', ''),
                'platform': 'web',
                'hotness':  0.0,
                'role':     'searched',
            })

    all_sources = crawler_sources + searched_sources

    body = parsed.get('body', '')
    char_count = len(body)
    logger.info(
        "generate_deep_story: done — cluster=%s body=%d chars "
        "token_estimate=%d sources=%d (crawler=%d searched=%d)",
        cluster.event_id, char_count, token_estimate,
        len(all_sources), len(crawler_sources), len(searched_sources),
    )

    return {
        'title':            parsed.get('title', topic_title),
        'body':             body,
        # Legacy fields — empty; get_stories_by_set() reads 'body' first
        'hook':             '',
        'bullets':          [],
        'twist':            '',
        'event_id':         cluster.event_id,
        'cluster_size':     cluster.member_count,
        'source_diversity': getattr(cluster, 'source_diversity', 0.0),
        'sources':          all_sources,
        'token_estimate':   token_estimate,
    }


def generate_supporting_stories(
    clusters: list,
    lang:     str = 'en',
) -> list[dict]:
    """
    Generate short supporting story summaries for a list of EventClusters.

    Sends all clusters in a single Claude call using support_story.txt.
    Returns a list of validated dicts: {event_id, title, summary, why_it_matters, sources}.

    On total JSON parse failure → falls back to individual per-cluster calls.
    Per-element validation failures → element is skipped + WARNING logged.
    """
    if not clusters:
        return []

    prompt_template = (PROMPTS_DIR / 'support_story.txt').read_text()

    # Build the events block
    events_lines = []
    for i, cluster in enumerate(clusters, 1):
        item = _cluster_to_item_dict(cluster)
        rep  = cluster.representative
        title = rep.canonical_title or rep.title_original or ''
        desc  = rep.description_original or ''

        # Include top fact source title for grounding
        top_fact = next(
            (m for m in cluster.fact_sources if m.candidate_id != rep.candidate_id),
            None,
        )
        fact_line = ''
        if top_fact:
            fact_line = f"\n  Fact source ({top_fact.platform}): {top_fact.canonical_title or top_fact.title_original or ''}"

        events_lines.append(
            f"Event {i}:\n"
            f"  Title: {title}\n"
            f"  Platform: {rep.platform}\n"
            f"  URL: {rep.url}"
            + (f"\n  Description: {desc[:200]}" if desc else "")
            + fact_line
        )

    events_block = "\n\n".join(events_lines)
    prompt = prompt_template.format(
        n=len(clusters),
        events_block=events_block,
        lang_instruction=_lang_instruction(lang),
    )

    def _validate_element(el: dict, cluster) -> dict | None:
        """Validate one supporting story element. Returns cleaned dict or None."""
        if not isinstance(el, dict):
            return None
        if not el.get('title') or not isinstance(el['title'], str):
            logger.warning("generate_supporting_stories: missing/invalid title — skipping element")
            return None
        if not el.get('summary') or not isinstance(el['summary'], str):
            logger.warning("generate_supporting_stories: missing/invalid summary — skipping element")
            return None
        if not el.get('why_it_matters') or not isinstance(el['why_it_matters'], str):
            logger.warning("generate_supporting_stories: missing/invalid why_it_matters — skipping")
            return None
        # sources must be a non-empty list of dicts with title + url
        sources = el.get('sources')
        if not sources or not isinstance(sources, list):
            logger.warning("generate_supporting_stories: missing/invalid sources — skipping element")
            return None
        valid_sources = [
            s for s in sources
            if isinstance(s, dict) and s.get('title') and s.get('url')
        ]
        if not valid_sources:
            logger.warning("generate_supporting_stories: no valid source objects — skipping element")
            return None
        return {
            'event_id':       cluster.event_id,
            'title':          el['title'],
            'summary':        el['summary'],
            'why_it_matters': el['why_it_matters'],
            'sources':        valid_sources,
        }

    def _parse_and_validate(raw: str) -> list[dict] | None:
        """Parse JSON array from Claude response. Returns None on total failure."""
        cleaned = raw.strip()
        if cleaned.startswith('```'):
            lines = cleaned.split('\n')
            lines = [l for l in lines if not l.strip().startswith('```')]
            cleaned = '\n'.join(lines).strip()
        # Find array bounds
        start = cleaned.find('[')
        end   = cleaned.rfind(']')
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            import json as _json
            return _json.loads(cleaned[start:end + 1])
        except Exception:
            return None

    # ── Batch call ─────────────────────────────────────────────────────────────
    try:
        raw   = _call_claude(prompt)
        array = _parse_and_validate(raw)

        if array is None:
            raise ValueError("Total JSON array parse failure")

        results = []
        for i, (el, cluster) in enumerate(zip(array, clusters)):
            validated = _validate_element(el, cluster)
            if validated:
                results.append(validated)
            else:
                logger.warning(
                    "generate_supporting_stories: element %d failed validation — skipped", i + 1
                )
        return results

    except Exception as _batch_err:
        logger.warning(
            "generate_supporting_stories: batch call failed (%s) — "
            "falling back to individual per-cluster calls",
            _batch_err,
        )

    # ── Fallback: individual per-cluster calls ─────────────────────────────────
    results = []
    for cluster in clusters:
        try:
            item  = _cluster_to_item_dict(cluster)
            rep   = cluster.representative
            title = rep.canonical_title or rep.title_original or ''
            desc  = rep.description_original or ''

            single_prompt = prompt_template.format(
                n=1,
                events_block=(
                    f"Event 1:\n"
                    f"  Title: {title}\n"
                    f"  Platform: {rep.platform}\n"
                    f"  URL: {rep.url}"
                    + (f"\n  Description: {desc[:200]}" if desc else "")
                ),
                lang_instruction=_lang_instruction(lang),
            )
            raw   = _call_claude(single_prompt)
            array = _parse_and_validate(raw)

            if array and len(array) > 0:
                validated = _validate_element(array[0], cluster)
                if validated:
                    results.append(validated)
        except Exception as _e:
            logger.warning(
                "generate_supporting_stories: fallback failed for cluster %s: %s",
                cluster.event_id, _e,
            )

    return results


def generate_story_batch(
    orchestration_result: dict,
    lang:           str = 'en',
    channel:        int = 1,
    batch_id:       int | None = None,
    batch_ts:       int | None = None,
    knowledge_pack: dict | None = None,
) -> dict:
    """
    Generate a full hierarchical story batch from story_orchestrate() output.

    Args:
        orchestration_result: {"deep_story": EventCluster, "supporting_stories": [...]}
        lang:     Output language ('en' or 'zh').
        channel:  Output channel (1, 2, or 3).
        batch_id: story_set_id for DB linkage.
        batch_ts: Batch timestamp (UNIX ms).

    Returns:
        {
          "deep_story":        {event_id, title, hook, bullets, twist, sources, ...},
          "supporting_stories": [{event_id, title, summary, why_it_matters, sources}, ...]
        }

    Saves result to hierarchical_stories table via save_hierarchical_story().
    On any failure, saves a failed row via save_failed_hierarchical_story().
    """
    from db.models import save_hierarchical_story, save_failed_hierarchical_story

    deep_cluster       = orchestration_result['deep_story']
    support_clusters   = orchestration_result.get('supporting_stories', [])

    try:
        logger.info(
            "generate_story_batch: generating deep story (event_id=%s) + "
            "%d supporting stories",
            deep_cluster.event_id, len(support_clusters),
        )

        deep_story = generate_deep_story(
            cluster=deep_cluster,
            lang=lang,
            channel=channel,
            batch_id=batch_id,
            batch_ts=batch_ts,
        )

        token_estimate = deep_story.get('token_estimate')
        if token_estimate:
            logger.info(
                "generate_story_batch: deep story token_estimate=%d "
                "(body=%d chars, sources=%d)",
                token_estimate,
                len(deep_story.get('body', '')),
                len(deep_story.get('sources', [])),
            )

        supporting_stories = generate_supporting_stories(
            clusters=support_clusters,
            lang=lang,
        )

        result = {
            'deep_story':        deep_story,
            'supporting_stories': supporting_stories,
        }

        story_id = save_hierarchical_story(
            story_set_id       = batch_id or 0,
            batch_ts           = batch_ts or 0,
            lang               = lang,
            channel            = channel,
            deep_story         = deep_story,
            supporting_stories = supporting_stories,
            status             = 'ready',
        )
        logger.info(
            "generate_story_batch: saved as hierarchical_story #%d", story_id
        )
        return result

    except Exception as e:
        logger.error("generate_story_batch: failed — %s", e, exc_info=True)
        save_failed_hierarchical_story(
            story_set_id = batch_id or 0,
            batch_ts     = batch_ts or 0,
            lang         = lang,
            channel      = channel,
            error        = str(e),
        )
        raise
