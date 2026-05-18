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

    # Per-run channel profile id (None = base file, no overlay applied)
    profile_id: str | None = None

    # Cluster-level title keyword blocklist (raw regex strings).
    # Clusters whose representative title matches ANY pattern are excluded before
    # story selection. Applied in story_orchestrate() after the quality floor.
    # Set per-profile in config/{lang}/story_mix_{profile}.json → ranking.cluster_title_blocklist.
    cluster_title_blocklist: list[str] = field(default_factory=list)


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

    cluster_title_blocklist: list[str] = list(
        ranking.get('cluster_title_blocklist') or raw.get('cluster_title_blocklist', [])
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
        cluster_title_blocklist=cluster_title_blocklist,
        profile_id=None,
    )


# ---------------------------------------------------------------------------
# Per-run profile overlays
# ---------------------------------------------------------------------------
#
# Overlays live in the same directory as the base story_mix.json. Each
# overlay file is a JSON object that may override ONLY the soft-layer keys
# listed in _OVERLAY_ALLOWED_KEYS. Merge semantics: ATOMIC REPLACE at the
# keys listed below (if the overlay has `soft_targets.category_mix`, it
# fully replaces the base's category_mix dict; other base keys unchanged).
#
# Introduced 2026-04-14 as part of the channel specialization proposal
# (story2.txt). No hard goals change; this is a pure extension of the
# existing tuning layer.

_OVERLAY_ALLOWED_KEYS = frozenset({
    # Metadata
    'profile_id',
    'extends',
    '_notes',
    # Soft-layer keys that overlays may override.
    # These match design.md's tuning_layer_bounds.can_adjust:
    #   ranking.platform_weight_overrides
    #   ranking.topic_boosts
    #   soft_targets.category_mix
    #   soft_targets.platform_targets
    # design.md tuning_layer_bounds.cannot_adjust explicitly lists
    # 'format_eligibility' — overlays cannot override format eligibility
    # rules. Goal 5 (Format fidelity over local diversity) requires that
    # format fidelity never be downgraded by a diversity/tuning mechanism.
    'soft_targets',          # only category_mix + platform_targets inside
    'ranking',               # only topic_boosts + platform_weight_overrides
})

_OVERLAY_ALLOWED_SOFT_KEYS = frozenset({
    'category_mix',
    'platform_targets',
})

_OVERLAY_ALLOWED_RANKING_KEYS = frozenset({
    'topic_boosts',
    'platform_weight_overrides',
    'cluster_title_blocklist',   # list[str] of regex patterns; matched against cluster titles
})


def load_with_profile(
    base_path:  str,
    profile_id: str | None,
    lang:       str | None = None,
) -> BatchConfig:
    """
    Load base config and optionally apply a per-run overlay profile.

    Args:
        base_path:  Path to config/story_mix.json (the base file).
        profile_id: Profile identifier (e.g. "run2_ai", "run_en") or None.
        lang:       Output language ('en' or 'zh'). Used to select the
                    locale-specific overlay subfolder (config/zh/ or config/en/).

    Returns:
        A merged BatchConfig. If profile_id is None, equivalent to
        load_config(base_path).

    Raises:
        FileNotFoundError: base or overlay file missing.
        ValueError: overlay contains forbidden keys, category_mix does not
                    sum to 1.0, or any validation error from load_config.

    Merge semantics:
        ATOMIC REPLACE at the keys listed in _OVERLAY_ALLOWED_KEYS.
        If the overlay has `soft_targets.category_mix`, it fully replaces
        the base's category_mix dict — authors must list every category
        they want non-zero.

    Overlay search order (first match wins):
        1. config/{lang}/story_mix_{profile_id}.json  (locale subfolder)
        2. config/story_mix_{profile_id}.json          (flat, backward compat)
    """
    if profile_id is None:
        return load_config(base_path)

    base_dir = Path(base_path).parent   # = config/
    filename  = f'story_mix_{profile_id}.json'
    searched  = []

    overlay_path = None

    # Priority 1: locale-specific subfolder (config/{lang}/story_mix_{profile_id}.json)
    if lang:
        lang_path = base_dir / lang / filename
        searched.append(str(lang_path))
        if lang_path.exists():
            overlay_path = lang_path

    # Priority 2: flat config/ directory (backward compat)
    if overlay_path is None:
        flat_path = base_dir / filename
        searched.append(str(flat_path))
        if flat_path.exists():
            overlay_path = flat_path

    if overlay_path is None:
        raise FileNotFoundError(
            f"Profile overlay not found for profile={profile_id!r}, lang={lang!r}. "
            f"Searched: {', '.join(searched)}"
        )

    with open(overlay_path, encoding='utf-8') as f:
        overlay = json.load(f)

    # Validate overlay top-level keys
    unknown_top = set(overlay.keys()) - _OVERLAY_ALLOWED_KEYS
    if unknown_top:
        raise ValueError(
            f"Overlay {overlay_path.name} contains forbidden top-level "
            f"key(s): {sorted(unknown_top)}. Overlays may only override: "
            f"{sorted(_OVERLAY_ALLOWED_KEYS - {'profile_id', 'extends', '_notes'})}"
        )

    # Validate nested soft_targets
    if 'soft_targets' in overlay:
        unknown_soft = set(overlay['soft_targets'].keys()) - _OVERLAY_ALLOWED_SOFT_KEYS
        if unknown_soft:
            raise ValueError(
                f"Overlay {overlay_path.name} soft_targets contains forbidden "
                f"key(s): {sorted(unknown_soft)}. Allowed: "
                f"{sorted(_OVERLAY_ALLOWED_SOFT_KEYS)}"
            )

    # Validate nested ranking
    if 'ranking' in overlay:
        unknown_rank = set(overlay['ranking'].keys()) - _OVERLAY_ALLOWED_RANKING_KEYS
        if unknown_rank:
            raise ValueError(
                f"Overlay {overlay_path.name} ranking contains forbidden "
                f"key(s): {sorted(unknown_rank)}. Allowed: "
                f"{sorted(_OVERLAY_ALLOWED_RANKING_KEYS)}"
            )

    # Validate category_mix sum (if overlay provides one)
    cat_mix_overlay = overlay.get('soft_targets', {}).get('category_mix')
    if cat_mix_overlay is not None:
        s = sum(cat_mix_overlay.values())
        if abs(s - 1.0) > 0.01:
            raise ValueError(
                f"Overlay {overlay_path.name} category_mix must sum to 1.0 "
                f"(±0.01); got {s:.4f}"
            )

    # Merge: read base raw, overwrite allowed paths atomically, hand off to
    # load_config() via a temporary merged-dict approach. To avoid
    # re-implementing load_config's parsing logic, we write the merged raw
    # to an in-memory dict and re-parse.
    with open(base_path, encoding='utf-8') as f:
        base_raw = json.load(f)

    merged = dict(base_raw)  # shallow copy

    # Apply soft_targets overrides at depth-2 atomic replace
    if 'soft_targets' in overlay:
        merged_soft = dict(base_raw.get('soft_targets', {}))
        for k, v in overlay['soft_targets'].items():
            merged_soft[k] = v  # full replace at key level
        merged['soft_targets'] = merged_soft

    # Apply ranking overrides at depth-2
    if 'ranking' in overlay:
        merged_ranking = dict(base_raw.get('ranking', {}))
        for k, v in overlay['ranking'].items():
            merged_ranking[k] = v
        merged['ranking'] = merged_ranking

    # NOTE: format_eligibility is intentionally NOT merged here.
    # design.md lists it under tuning_layer_bounds.cannot_adjust; Goal 5
    # (Format fidelity over local diversity) forbids the tuning layer
    # from overriding it. The _OVERLAY_ALLOWED_KEYS validation above
    # rejects any overlay that tries.

    # Write merged to temp file and parse via load_config
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', suffix='.json', delete=False
    ) as tf:
        json.dump(merged, tf)
        tmp_path = tf.name

    try:
        cfg = load_config(tmp_path)
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    cfg.profile_id = profile_id
    return cfg
