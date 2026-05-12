"""
lm_cluster_scorer.py — Stage 4b: LLM semantic scoring of top candidate clusters.

Operates on raw cluster titles BEFORE any story is written.
Scores 7 dimensions per cluster; final_score and recommendation are computed
deterministically in code (never from LLM output — same rule as attract_scorer.py:101).

Failure is always fail-open: returns None on any error.
Callers must treat None as "use deterministic rank 1."

Pipeline position:
    build_clusters() → Stage 4 → lm_cluster_scorer() → story_orchestrate() → generator

Used by: story_orchestrate.py (between cluster ranking and deep-story selection)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "cluster_score.txt"
# __file__ = story_engine/src/engine/selector/lm_cluster_scorer.py
# .parents[2] = story_engine/src/
# → story_engine/src/prompts/cluster_score.txt  ✓

_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# 7 scoring dimensions
_DIMS = [
    "curiosity_gap",        # weight ×3 → 0–30
    "hidden_mechanism",     # weight ×3 → 0–30
    "human_relevance",      # weight ×2 → 0–20
    "audience_fit",         # weight ×2 → 0–20
    "retention_likelihood", # weight ×2 → 0–20
    "explanatory_payoff",   # weight ×1 → 0–10
    "statement_war",        # weight ×1 → 0–10  (inverted: high = no penalty)
]
_WEIGHTS: dict[str, int] = {
    "curiosity_gap":        3,
    "hidden_mechanism":     3,
    "human_relevance":      2,
    "audience_fit":         2,
    "retention_likelihood": 2,
    "explanatory_payoff":   1,
    "statement_war":        1,
}
# raw max = 10 × (3+3+2+2+2+1+1) = 130  →  divide by 1.3 to map 0–100
_RAW_MAX = 130

# Recommendation thresholds (applied after final_score is computed)
_RECOMMENDATION_THRESHOLDS = [
    (75, "STRONG_PICK"),
    (55, "PICK"),
    (35, "WEAK"),
    (0,  "AVOID"),
]


def _recommendation(final_score: int) -> str:
    """Derive recommendation label from final_score deterministically."""
    for threshold, label in _RECOMMENDATION_THRESHOLDS:
        if final_score >= threshold:
            return label
    return "AVOID"


def _extract_titles(cluster: object) -> list[str]:
    """
    Extract up to 8 article titles from an EventCluster for the LLM prompt.

    Priority order:
      1. timeline entries (have pre-sorted title_original fields)
      2. representative.canonical_title / title_original as fallback
    """
    titles: list[str] = []

    # timeline is list[dict] with 'title' key
    timeline = getattr(cluster, 'timeline', None) or []
    for entry in timeline:
        t = entry.get('title', '').strip()
        if t:
            titles.append(t)
        if len(titles) >= 8:
            break

    # fallback: representative title if timeline was empty
    if not titles:
        rep = getattr(cluster, 'representative', None)
        if rep is not None:
            t = (getattr(rep, 'canonical_title', None) or
                 getattr(rep, 'title_original', None) or '')
            if t.strip():
                titles.append(t.strip())

    return titles


def _build_clusters_block(clusters: list[object]) -> str:
    """Format up to 5 clusters as the prompt's CLUSTERS TO EVALUATE section."""
    lines: list[str] = []
    for i, cluster in enumerate(clusters[:5], 1):
        event_id = getattr(cluster, 'event_id', f'cluster_{i}')
        hotness  = getattr(cluster, 'event_hotness', 0.0)
        count    = getattr(cluster, 'member_count', 0)
        titles   = _extract_titles(cluster)

        lines.append(
            f"Cluster {i} (event_id={event_id}, member_count={count}, "
            f"hotness={hotness:.1f}):"
        )
        for title in titles:
            lines.append(f"  - {title}")
        if not titles:
            lines.append("  (no titles available)")
        lines.append("")

    return "\n".join(lines)


def score_clusters(clusters: list[object]) -> list[dict] | None:
    """
    Score up to 5 candidate EventCluster objects on 7 semantic dimensions.

    Args:
        clusters: list of EventCluster objects (from build_clusters()).
                  Only the first 5 are evaluated; extras are ignored.

    Returns:
        Ranked list (final_score DESC) of dicts, each containing:
          {
            "event_id":       str,   # matches cluster.event_id
            "final_score":    int,   # 0–100, computed deterministically
            "recommendation": str,   # "STRONG_PICK" | "PICK" | "WEAK" | "AVOID"
            "scores":         dict,  # {dim: int} for all 7 dimensions
            "reason":         str,   # one-sentence LLM explanation
          }
        Returns None on any failure — caller must fall back to deterministic pick.
    """
    if not clusters:
        logger.warning("lm_cluster_scorer: called with empty cluster list")
        return None

    try:
        prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("lm_cluster_scorer: cannot read prompt — %s", exc)
        return None

    clusters_block = _build_clusters_block(clusters)
    prompt = prompt_template.format(clusters_block=clusters_block)

    try:
        result = subprocess.run(
            [_CLAUDE_BIN, "--output-format", "text", "--max-turns", "1",
             "--tools", "", "-p", prompt],
            capture_output=True, text=True, timeout=45,
            cwd=Path(__file__).resolve().parents[3],  # story_engine/
        )
        raw = result.stdout.strip()
    except Exception as exc:
        logger.error("lm_cluster_scorer: Claude call failed — %s", exc)
        return None

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("lm_cluster_scorer: invalid JSON — %r", raw[:300])
        return None

    if not isinstance(data, list):
        logger.error(
            "lm_cluster_scorer: expected JSON array, got %s", type(data).__name__
        )
        return None

    scored: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        cluster_id = item.get("cluster_id")
        if not cluster_id:
            logger.warning("lm_cluster_scorer: item missing cluster_id — skipping")
            continue

        scores_raw = item.get("scores") or {}
        reason     = str(item.get("reason") or "")

        # Compute final_score deterministically — never trust LLM arithmetic.
        # Same rule as attract_scorer.py line 101.
        weighted_sum = sum(
            int(scores_raw.get(d) or 0) * _WEIGHTS[d]
            for d in _DIMS
        )
        final_score = round(weighted_sum / _RAW_MAX * 100)
        final_score = max(0, min(100, final_score))

        scored.append({
            "event_id":       cluster_id,
            "final_score":    final_score,
            "recommendation": _recommendation(final_score),
            "scores":         {d: int(scores_raw.get(d) or 0) for d in _DIMS},
            "reason":         reason,
        })

    if not scored:
        logger.error("lm_cluster_scorer: no valid cluster entries in LLM response")
        return None

    # Sort by final_score DESC — highest score is the LLM's preferred pick
    scored.sort(key=lambda x: -x["final_score"])

    logger.info(
        "lm_cluster_scorer: scored %d cluster(s) — top pick: event_id=%s "
        "final_score=%d (%s) | reason: %s",
        len(scored),
        scored[0]["event_id"],
        scored[0]["final_score"],
        scored[0]["recommendation"],
        scored[0]["reason"],
    )
    return scored
