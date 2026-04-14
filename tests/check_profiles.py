#!/usr/bin/env python3
"""
Feasibility gate for per-run config profiles.

Validates that every overlay profile's category_mix targets are within
2x the corpus share for that category — otherwise Stage 2 feasibility
will partial-output the affected formats on every run.

Reads:
  config/corpus_share.json        (Phase A bootstrap frozen constants)
  config/story_mix_*.json         (all overlay profiles)
  story_mix.json                  (base file)

Exit codes:
  0 — all profiles pass
  1 — one or more profiles rejected

Usage:
  python tests/check_profiles.py            # validate all profiles
  python tests/check_profiles.py run2_ai    # validate one profile
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FEASIBILITY_RATIO_CAP = 2.0

# Resolve project root (assumes this file lives at story_engine/tests/)
_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _ROOT / "config"
_CORPUS_SHARE_PATH = _CONFIG_DIR / "corpus_share.json"
_BASE_STORY_MIX = _ROOT / "story_mix.json"


def _load_corpus_share() -> dict[str, float]:
    """Load the frozen corpus_share constants."""
    if not _CORPUS_SHARE_PATH.exists():
        print(f"ERROR: corpus_share.json not found at {_CORPUS_SHARE_PATH}",
              file=sys.stderr)
        sys.exit(1)
    with open(_CORPUS_SHARE_PATH, encoding='utf-8') as f:
        data = json.load(f)
    shares = data.get("corpus_share", {})
    if not shares:
        print(f"ERROR: corpus_share.json missing 'corpus_share' key",
              file=sys.stderr)
        sys.exit(1)
    return {k: float(v) for k, v in shares.items()}


def _load_base_category_mix() -> dict[str, float]:
    """Load the base story_mix.json's category_mix."""
    if not _BASE_STORY_MIX.exists():
        print(f"ERROR: base story_mix.json not found at {_BASE_STORY_MIX}",
              file=sys.stderr)
        sys.exit(1)
    with open(_BASE_STORY_MIX, encoding='utf-8') as f:
        data = json.load(f)
    mix = data.get("soft_targets", {}).get("category_mix") or data.get("category_mix")
    if not mix:
        print(f"ERROR: base story_mix.json missing category_mix", file=sys.stderr)
        sys.exit(1)
    return mix


def _load_overlay_category_mix(profile_id: str) -> dict[str, float] | None:
    """
    Load the overlay profile's category_mix (or None if the overlay
    doesn't override category_mix, meaning base values apply).
    """
    overlay_path = _CONFIG_DIR / f"story_mix_{profile_id}.json"
    if not overlay_path.exists():
        print(f"ERROR: overlay not found at {overlay_path}", file=sys.stderr)
        sys.exit(1)
    with open(overlay_path, encoding='utf-8') as f:
        data = json.load(f)
    return data.get("soft_targets", {}).get("category_mix")


def _validate_profile(
    profile_id: str,
    corpus_share: dict[str, float],
    base_mix: dict[str, float],
) -> tuple[bool, list[str]]:
    """
    Returns (passed, messages). If the overlay doesn't override
    category_mix, the base values are used (which by construction
    already pass — but we check anyway).
    """
    overlay_mix = _load_overlay_category_mix(profile_id)
    effective_mix = overlay_mix if overlay_mix is not None else base_mix

    # Sum check
    s = sum(effective_mix.values())
    messages = []
    passed = True

    if abs(s - 1.0) > 0.01:
        messages.append(f"  ❌ SUM: category_mix sums to {s:.4f}, expected 1.0 ±0.01")
        passed = False

    # Ratio check
    for cat, target in sorted(effective_mix.items(), key=lambda x: -x[1]):
        if target <= 0:
            continue
        share = corpus_share.get(cat)
        if share is None:
            messages.append(
                f"  ❌ UNKNOWN: category {cat!r} has no entry in corpus_share.json"
            )
            passed = False
            continue
        if share <= 0:
            messages.append(
                f"  ⚠  ZERO SUPPLY: {cat} target={target:.2%}, corpus_share=0"
            )
            # Skip ratio check — infinite ratio
            continue
        ratio = target / share
        marker = "✓" if ratio <= FEASIBILITY_RATIO_CAP else "❌"
        messages.append(
            f"  {marker} {cat:14s} target={target:.2%}  share={share:.2%}  "
            f"ratio={ratio:.2f}"
        )
        if ratio > FEASIBILITY_RATIO_CAP:
            passed = False

    return passed, messages


def main() -> int:
    corpus_share = _load_corpus_share()
    base_mix = _load_base_category_mix()

    # Pick profiles to check
    if len(sys.argv) > 1:
        profiles = sys.argv[1:]
    else:
        profiles = sorted(
            p.stem.replace("story_mix_", "")
            for p in _CONFIG_DIR.glob("story_mix_*.json")
        )

    print("=" * 70)
    print("Feasibility gate (ratio cap = 2.0×)")
    print("=" * 70)
    print()
    print(f"Corpus share reference (from {_CORPUS_SHARE_PATH.name}):")
    for cat, share in sorted(corpus_share.items(), key=lambda x: -x[1]):
        print(f"  {cat:14s} {share:.2%}  (2x cap = {share*2:.2%})")
    print()

    all_pass = True
    for profile in profiles:
        print(f"--- {profile} ---")
        passed, messages = _validate_profile(profile, corpus_share, base_mix)
        for line in messages:
            print(line)
        if passed:
            print(f"  ✅ PASS\n")
        else:
            print(f"  ❌ FAIL\n")
            all_pass = False

    print("=" * 70)
    if all_pass:
        print("All profiles pass the feasibility gate.")
        return 0
    else:
        print("One or more profiles FAILED the feasibility gate.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
