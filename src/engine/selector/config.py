"""
Config loader for the batch selection pipeline.

Loads story_mix.json into a typed BatchConfig dataclass.
Validates all fields at load time — rejects unknown keys,
invalid ranges, and missing required format IDs.

Supports both v1 (flat top-level keys) and v2 (nested sections) JSON schemas.
v2 layout:
  platform_caps         → hard_constraints.platform_caps
  platform_groups       → hard_constraints.platform_groups
  hard_excluded_platforms → hard_constraints.hard_excluded_platforms
  default_uncapped_...  → hard_constraints.default_uncapped_platform_max_share
  category_mix          → soft_targets.category_mix
  category_dominance_multiplier → soft_targets.category_policy.dominance_multiplier
  topic_boosts          → ranking.topic_boosts
  surface_weight_overrides → ranking.platform_weight_overrides
  platform_aliases      → normalization.platform_aliases
  comment_platforms     → source_groups.comment_platforms
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Sub-config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FormatEligibilityRule:
    """Per-format eligibility constraints loaded from story_mix.json."""

    excluded_categories:  list[str] = field(default_factory=list)
    requires_news_event:  bool = False
    source_restricted_to: str | None = None   # e.g. "comment_platforms"
    selection_strategy:   str | None = None   # e.g. "mix"


@dataclass
class PlatformTargetsConfig:
    """Soft platform budget parameters from soft_targets.platform_targets."""

    target_ratio_of_cap:  float = 0.7    # soft budget = floor(hard_budget × ratio)
    apply_to_default_cap: bool  = True   # apply soft budget to default-capped platforms too


@dataclass
class NewsEventConfig:
    """Entertainment-media detection params from normalization.news_event_detection."""

    video_platforms:   list[str] = field(default_factory=list)
    title_block_regex: str = r'\b(anime|PV|MV|Trailer)\b'


@dataclass
class NormalizationConfig:
    """Normalization sub-config — news event detection."""

    news_event_detection: NewsEventConfig = field(default_factory=NewsEventConfig)


@dataclass
class BatchConfig:
    """All story_mix.json fields, fully typed and validated."""

    # Core allocation signals
    category_mix:         dict[str, float]       # category → fraction (sum ≈ 1.0)
    platform_caps:        dict[str, float]       # platform → max fraction (0, 1)
    topic_boosts:         dict[str, float]       # category → hotness multiplier

    # Source metadata
    category_sources:     dict[str, Any]

    # Platform weights (surface_weight_overrides in JSON)
    surface_weight_overrides: dict[str, float]
    platform_default_weights: dict[str, float]

    # Legacy slot minimums (superseded by format_registry.item_count)
    min_slots_per_format: dict[str, Any]

    # Format eligibility
    format_eligibility:   dict[int, FormatEligibilityRule]  # int keys after parse
    format_defaults:      FormatEligibilityRule             # fallback for unknown format_ids

    # Platform grouping / normalization
    comment_platforms:    list[str]              # convenience: source_groups["comment_platforms"]
    source_groups:        dict[str, list[str]]   # full source_groups dict from JSON

    # Platform normalization
    platform_groups:           dict[str, list[str]]  # group_name → member platforms
    platform_aliases:          dict[str, str]        # alias → canonical name
    hard_excluded_platforms:   list[str]             # always-rejected platforms

    # Soft budget targets
    platform_targets:     PlatformTargetsConfig

    # Normalization config (category derivation + entertainment detection)
    normalization:        NormalizationConfig

    # Dominance thresholds
    category_dominance_multiplier:       float = 1.5
    default_uncapped_platform_max_share: float = 0.10


# ---------------------------------------------------------------------------
# Known top-level keys in story_mix.json (v1 and v2)
# ---------------------------------------------------------------------------

_ALLOWED_KEYS = frozenset({
    # Metadata (both v1 and v2)
    'version',
    'contract_notes',
    # v1 legacy metadata
    '_version',
    '_notes',
    # v1 flat top-level keys
    'category_mix',
    'category_sources',
    'comment_platforms',
    'platform_caps',
    'platform_default_weights',
    'topic_boosts',
    'surface_weight_overrides',
    'min_slots_per_format',
    'format_eligibility',
    'format_defaults',
    'category_dominance_multiplier',
    'default_uncapped_platform_max_share',
    'platform_groups',
    'platform_aliases',
    'hard_excluded_platforms',
    'soft_targets',
    'normalization',
    # v2 new nested top-level sections
    'hard_constraints',
    'localization',
    'ranking',
    'source_groups',
    'selection_policy',
    'determinism',
    'observability',
    'tuning_layer_bounds',
})


def load_config(config_path: str) -> BatchConfig:
    """
    Load and validate story_mix.json.

    Supports both v1 (flat keys) and v2 (nested sections) schema layouts.
    For each field, tries the v2 nested location first, then falls back to
    the v1 top-level key.

    Raises:
        FileNotFoundError: config file not found
        ValueError: unknown key, invalid value range, or sum check failure
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(path, encoding='utf-8') as f:
        raw = json.load(f)

    # --- Unknown key check ---
    unknown = set(raw.keys()) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"story_mix.json contains unknown top-level key(s): {sorted(unknown)}. "
            f"Allowed keys: {sorted(_ALLOWED_KEYS)}"
        )

    # --- Extract nested sections (v2 structure) ---
    hc      = raw.get('hard_constraints', {})   # v2: platform caps, groups, exclusions
    soft    = raw.get('soft_targets', {})        # both v1/v2: platform_targets; v2 also: category_mix
    ranking = raw.get('ranking', {})             # v2: topic_boosts, platform_weight_overrides
    raw_norm = raw.get('normalization', {})      # platform_aliases + news_event_detection
    source_groups: dict[str, list[str]] = raw.get('source_groups', {})

    # --- Dual-mode field resolution (v2 first, v1 fallback) ---

    platform_caps: dict[str, float] = (
        hc.get('platform_caps') or raw.get('platform_caps', {})
    )

    platform_groups: dict[str, list[str]] = (
        hc.get('platform_groups') or raw.get('platform_groups', {})
    )

    hard_excluded_platforms: list[str] = hc.get(
        'hard_excluded_platforms',
        raw.get('hard_excluded_platforms', [])
    )

    default_uncapped_share: float = float(hc.get(
        'default_uncapped_platform_max_share',
        raw.get('default_uncapped_platform_max_share', 0.10)
    ))

    category_mix: dict[str, float] = (
        soft.get('category_mix') or raw.get('category_mix', {})
    )

    category_dominance: float = float(
        soft.get('category_policy', {}).get(
            'dominance_multiplier',
            raw.get('category_dominance_multiplier', 1.5)
        )
    )

    topic_boosts: dict[str, float] = (
        ranking.get('topic_boosts') or raw.get('topic_boosts', {})
    )

    surface_weight_overrides: dict[str, float] = (
        ranking.get('platform_weight_overrides') or raw.get('surface_weight_overrides', {})
    )

    # platform_aliases: v2 nests under normalization; v1 is top-level
    platform_aliases: dict[str, str] = (
        raw_norm.get('platform_aliases') or raw.get('platform_aliases', {})
    )

    # comment_platforms: v2 is under source_groups; v1 is top-level
    comment_platforms: list[str] = source_groups.get(
        'comment_platforms',
        raw.get('comment_platforms', [])
    )

    # --- category_mix sum check (uses resolved value) ---
    mix_sum = sum(category_mix.values())
    if category_mix and abs(mix_sum - 1.0) > 0.01:
        raise ValueError(
            f"category_mix values must sum to 1.0 (±0.01); got {mix_sum:.4f}"
        )

    # --- platform_caps range check (uses resolved value) ---
    for platform, cap in platform_caps.items():
        if not (0 < cap < 1):
            raise ValueError(
                f"platform_caps['{platform}'] = {cap} is out of range (0, 1)"
            )

    # --- format_eligibility: parse string keys → int, validate format IDs ---
    raw_eligibility: dict[str, Any] = raw.get('format_eligibility', {})
    format_eligibility: dict[int, FormatEligibilityRule] = {}

    from engine.format_registry import FORMAT_ITEM_COUNTS

    for str_key, rule_dict in raw_eligibility.items():
        try:
            fid = int(str_key)
        except ValueError:
            raise ValueError(
                f"format_eligibility key '{str_key}' is not a valid integer format ID"
            )
        if fid not in FORMAT_ITEM_COUNTS:
            raise ValueError(
                f"format_eligibility key {fid} is not a known format ID "
                f"(valid range: 1–{max(FORMAT_ITEM_COUNTS)})"
            )
        # Support both v1 'source_restricted_to' and v2 'source_restricted_to_group'
        source_restricted = (
            rule_dict.get('source_restricted_to')
            or rule_dict.get('source_restricted_to_group')
        )
        rule = FormatEligibilityRule(
            excluded_categories=rule_dict.get('excluded_categories', []),
            requires_news_event=rule_dict.get('requires_news_event', False),
            source_restricted_to=source_restricted,
            selection_strategy=rule_dict.get('selection_strategy', None),
        )
        format_eligibility[fid] = rule

    # --- format_defaults ---
    raw_defaults = raw.get('format_defaults', {})
    source_restricted_default = (
        raw_defaults.get('source_restricted_to')
        or raw_defaults.get('source_restricted_to_group')
    )
    format_defaults = FormatEligibilityRule(
        excluded_categories=raw_defaults.get('excluded_categories', []),
        requires_news_event=raw_defaults.get('requires_news_event', False),
        source_restricted_to=source_restricted_default,
        selection_strategy=raw_defaults.get('selection_strategy', None),
    )

    # --- soft_targets.platform_targets ---
    raw_pt = soft.get('platform_targets', {})
    platform_targets = PlatformTargetsConfig(
        target_ratio_of_cap=float(raw_pt.get('target_ratio_of_cap', 0.7)),
        apply_to_default_cap=bool(raw_pt.get('apply_to_default_cap', True)),
    )

    # --- normalization ---
    raw_ned = raw_norm.get('news_event_detection', {})
    title_block_regex = raw_ned.get(
        'title_block_regex',
        r'\b(anime|PV|MV|Trailer|预告|番剧|AMV|OP|ED)\b'
    )
    # Validate regex compiles
    try:
        re.compile(title_block_regex, re.IGNORECASE)
    except re.error as e:
        raise ValueError(
            f"normalization.news_event_detection.title_block_regex is invalid: {e}"
        )
    news_event_detection = NewsEventConfig(
        video_platforms=raw_ned.get('video_platforms', ['bilibili', 'youtube', 'nicovideo']),
        title_block_regex=title_block_regex,
    )

    normalization = NormalizationConfig(
        news_event_detection=news_event_detection,
    )

    # --- default_uncapped_platform_max_share validation ---
    if not (0 < default_uncapped_share <= 1.0):
        raise ValueError(
            f"default_uncapped_platform_max_share must be in (0, 1]; got {default_uncapped_share}"
        )

    return BatchConfig(
        category_mix=category_mix,
        platform_caps=platform_caps,
        topic_boosts=topic_boosts,
        category_sources=raw.get('category_sources', {}),
        surface_weight_overrides=surface_weight_overrides,
        platform_default_weights=raw.get('platform_default_weights', {}),
        min_slots_per_format=raw.get('min_slots_per_format', {}),
        format_eligibility=format_eligibility,
        format_defaults=format_defaults,
        comment_platforms=comment_platforms,
        source_groups=source_groups,
        platform_groups=platform_groups,
        platform_aliases=platform_aliases,
        hard_excluded_platforms=hard_excluded_platforms,
        platform_targets=platform_targets,
        normalization=normalization,
        category_dominance_multiplier=category_dominance,
        default_uncapped_platform_max_share=default_uncapped_share,
    )
