"""
Stage 1b — Pre-selection attract pre-screener.

Runs a lightweight LLM batch-scoring pass on the top-N candidates
(by effective_hotness) BEFORE Stage 3 selection.  Each candidate
receives a pre_attract_score in [0, 1] derived from three title-level
dimensions (curiosity_gap, mechanism_hint, audience_fit).

Stage 3 uses this score as a multiplicative adjustment to
effective_hotness, penalising statement-war / price-ticker content
and boosting mechanism-reveal / curiosity-gap content.

Design goals:
  - Fail-open: LLM failures leave pre_attract_score=None (neutral in Stage 3)
  - Cheap: only title + description (~100 chars each), batched 20 per call
  - Fast: typically 8-10 calls × 30 s timeout = ~5 min added latency
  - Language-aware: uses prompts/prescreen_en.txt (ZH prompt TBD)
  - Non-blocking: any single batch failure is logged and skipped; the rest proceed
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.selector.schemas import NormalizedCandidate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Only score the top N candidates (by current effective_hotness).
# Candidates beyond this rank are very unlikely to be selected; scoring them
# wastes tokens without affecting selection outcomes.
PRESCREEN_TOP_N = 200

# Titles + descriptions sent per LLM call.  20 items × ~120 chars ≈ 400 tokens
# input per batch — cheap and within a single LLM context window.
PRESCREEN_BATCH_SIZE = 20

# Maximum score per dimension × 3 dimensions.  Used to normalise to [0, 1].
_MAX_RAW_SCORE = 30.0

_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

_SUPPORTED_LANGS = {"en"}   # ZH prompt not yet authored; extend when ready.

# ---------------------------------------------------------------------------
# Prompt loader
# ---------------------------------------------------------------------------

def _prompt_template(lang: str) -> str | None:
    """Return the prescreen prompt template for the given language, or None if unsupported."""
    prompts_dir = Path(__file__).resolve().parents[3] / "prompts"
    path = prompts_dir / f"prescreen_{lang}.txt"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Single-batch scorer
# ---------------------------------------------------------------------------

def _score_batch(
    batch: list[dict],       # [{"id": int, "title": str, "description": str}, ...]
    prompt_template: str,
) -> dict[int, float] | None:
    """
    Score one batch of up to PRESCREEN_BATCH_SIZE candidates.

    Returns:
        dict mapping item["id"] → normalised score [0, 1], or None on failure.
        A None return means the caller should leave those candidates unscored
        (pre_attract_score = None → neutral multiplier in Stage 3).
    """
    stories_json = json.dumps(
        [{"id": item["id"], "title": item["title"], "description": item["description"]} for item in batch],
        ensure_ascii=False,
    )
    prompt = prompt_template.format(stories_json=stories_json)

    try:
        result = subprocess.run(
            [_CLAUDE_BIN, "--output-format", "text", "--max-turns", "1",
             "--tools", "", "-p", prompt],
            capture_output=True, text=True, timeout=60,
            cwd=Path(__file__).resolve().parents[3],   # story_engine/
        )
        raw = result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("stage1b_prescreen: LLM call timed out for batch of %d items", len(batch))
        return None
    except Exception as exc:
        logger.warning("stage1b_prescreen: LLM call failed — %s", exc)
        return None

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "stage1b_prescreen: invalid JSON from LLM — %r (batch size=%d)",
            raw[:300], len(batch),
        )
        return None

    if not isinstance(data, list):
        logger.warning("stage1b_prescreen: expected JSON array, got %s", type(data).__name__)
        return None

    scores: dict[int, float] = {}
    for row in data:
        try:
            item_id = int(row["id"])
            raw_score = (
                int(row.get("curiosity_gap", 0))
                + int(row.get("mechanism_hint", 0))
                + int(row.get("audience_fit", 0))
            )
            # Clamp raw_score to [0, _MAX_RAW_SCORE] before normalising.
            clamped = max(0.0, min(float(raw_score), _MAX_RAW_SCORE))
            scores[item_id] = clamped / _MAX_RAW_SCORE
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("stage1b_prescreen: bad row %r — %s", row, exc)
            continue

    return scores if scores else None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def prescreen_candidates(
    candidates: list[NormalizedCandidate],
    lang: str | None,
) -> None:
    """
    Score the top PRESCREEN_TOP_N candidates and mutate their pre_attract_score
    in-place.  Candidates not reached (beyond top-N or in a failed batch)
    retain pre_attract_score=None, which Stage 3 treats as a neutral multiplier.

    Args:
        candidates: Stage 1 output list (all candidates, used items excluded).
        lang:       Output language ("en" or "zh").  If no prescreen prompt
                    exists for this language, the function returns immediately
                    (all candidates stay at None → neutral).
    """
    if not lang or lang not in _SUPPORTED_LANGS:
        logger.info(
            "stage1b_prescreen: no prescreen prompt for lang=%r — skipping pre-scoring "
            "(all candidates neutral)", lang,
        )
        return

    prompt_template = _prompt_template(lang)
    if prompt_template is None:
        logger.warning(
            "stage1b_prescreen: prompt file prompts/prescreen_%s.txt not found — "
            "skipping pre-scoring", lang,
        )
        return

    # Sort by effective_hotness descending; score only top-N.
    ranked = sorted(candidates, key=lambda c: -c.effective_hotness)
    to_score = ranked[:PRESCREEN_TOP_N]

    if not to_score:
        return

    logger.info(
        "stage1b_prescreen: pre-scoring %d/%d candidates (top-%d by effective_hotness, "
        "lang=%s, batch_size=%d)",
        len(to_score), len(candidates), PRESCREEN_TOP_N, lang, PRESCREEN_BATCH_SIZE,
    )

    # Build a stable integer id → candidate mapping for this scoring run.
    # Using a local integer index avoids exposing crawler_item_id to the prompt.
    id_to_candidate: dict[int, NormalizedCandidate] = {
        idx: cand for idx, cand in enumerate(to_score)
    }

    # Slice into batches and score.
    scored = 0
    failed_batches = 0
    for batch_start in range(0, len(to_score), PRESCREEN_BATCH_SIZE):
        batch_slice = list(id_to_candidate.items())[batch_start:batch_start + PRESCREEN_BATCH_SIZE]
        batch_payload = [
            {
                "id": idx,
                "title": cand.title_original or "",
                "description": (cand.description_original or "")[:200],
            }
            for idx, cand in batch_slice
        ]

        batch_scores = _score_batch(batch_payload, prompt_template)

        if batch_scores is None:
            failed_batches += 1
            # Leave pre_attract_score=None for this batch (neutral in Stage 3).
            continue

        for idx, score in batch_scores.items():
            if idx in id_to_candidate:
                id_to_candidate[idx].pre_attract_score = score
                scored += 1

    logger.info(
        "stage1b_prescreen: done — %d/%d candidates scored, %d batch(es) failed (fail-open)",
        scored, len(to_score), failed_batches,
    )

    if scored > 0:
        # Log score distribution for observability.
        scores_list = [
            c.pre_attract_score for c in to_score if c.pre_attract_score is not None
        ]
        low  = sum(1 for s in scores_list if s < 0.33)
        mid  = sum(1 for s in scores_list if 0.33 <= s < 0.67)
        high = sum(1 for s in scores_list if s >= 0.67)
        logger.info(
            "stage1b_prescreen: score distribution — low(<0.33): %d, mid(0.33-0.67): %d, "
            "high(>=0.67): %d",
            low, mid, high,
        )
