"""
attract_scorer.py — Score a generated story for channel audience fit + pipe gating.

One small Claude call reads the story title + body and returns a 0–100 score
plus a per-dimension breakdown. Works for both EN and ZH story text.

Optimizes for THIS channel's audience retention, not general news value.
Channel identity: "Hidden systems made visible."

Used by:
  - generate_story_batch() in generator.py (real-time, after each story save)
  - score_existing.py (retroactive, one-time calibration script)

Scoring dimensions (total 100):
  curiosity_gap             0–20
  ordinary_people_stakes    0–20
  hidden_mechanism          0–20
  consequence_clarity       0–15
  audience_fit              0–15
  title_retention_alignment 0–5
  low_context_accessibility 0–5
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
# __file__ = story_engine/src/engine/attract_scorer.py
# .parent        = story_engine/src/engine/
# .parent.parent = story_engine/src/
# → story_engine/src/prompts/  ✓


def _prompt_path(lang: str) -> Path:
    """
    Pick the attract-score prompt file for the given language.

    Routes:
        lang='en'  → attract_score_en.txt  (English-audience rubric)
        otherwise  → attract_score_zh.txt  (default / ZH rubric)

    Both files use identical dimensions and JSON schema, so downstream code
    (total computation, breakdown storage) is unchanged.
    """
    name = "attract_score_en.txt" if lang == "en" else "attract_score_zh.txt"
    return _PROMPTS_DIR / name

_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

_DIMS = [
    "curiosity_gap",
    "ordinary_people_stakes",
    "hidden_mechanism",
    "consequence_clarity",
    "audience_fit",
    "title_retention_alignment",
    "low_context_accessibility",
]


def score_story(title: str, body: str, lang: str = "zh") -> tuple[int | None, dict]:
    """
    Score a story on six attractiveness dimensions using Claude.

    Returns:
        (total_score, breakdown_dict)
        total_score: int 0–100 (deterministically summed from 6 dimension scores)
        breakdown_dict: {dimension: {"score": int, "reason": str}, ...}

    On any failure, returns (None, {}) — caller must check for None before saving.
    The gate in run_generate.sh treats NULL score as pass (fail open), so callers
    must NOT save a failure result to DB — only save when score is not None.
    """
    try:
        prompt_template = _prompt_path(lang).read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("attract_scorer: cannot read prompt — %s", exc)
        return None, {}

    prompt = prompt_template.format(
        title=title,
        body=body[:3000],   # cap body to keep call cheap (~500 tokens input)
    )

    try:
        result = subprocess.run(
            [_CLAUDE_BIN, "--output-format", "text", "--max-turns", "1",
             "--tools", "", "-p", prompt],
            capture_output=True, text=True, timeout=45,
            cwd=Path(__file__).resolve().parents[2],   # story_engine/
        )
        raw = result.stdout.strip()
    except Exception as exc:
        logger.error("attract_scorer: Claude call failed — %s", exc)
        return None, {}

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("attract_scorer: invalid JSON response — %r", raw[:200])
        return None, {}

    # Guard: if the LLM returned valid JSON but scored fewer than 4 of 7 dimensions,
    # it likely failed silently (returned {}, nulls, or garbled output that still parsed).
    # Return None so the gate treats this as "no score" rather than saving a false 0.
    scored_dims = sum(1 for d in _DIMS if int((data.get(d) or {}).get("score", 0)) > 0)
    if scored_dims < 4:
        logger.error(
            "attract_scorer: only %d/%d dimensions scored — LLM response likely malformed "
            "(returning None to prevent false score=0 blocking a valid story)",
            scored_dims, len(_DIMS),
        )
        return None, {}

    # Compute total deterministically — do NOT trust LLM's own arithmetic.
    total = sum(int((data.get(d) or {}).get("score", 0)) for d in _DIMS)
    breakdown = {
        k: v for k, v in data.items()
        if k not in ("total", "verdict")
    }
    return total, breakdown
