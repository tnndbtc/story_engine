================================================================================
STORY ENGINE — CONSOLIDATED SYSTEM GOALS
================================================================================

PURPOSE
-------
This document is the authoritative system goals reference for the story engine
refactor. It consolidates the original goals, review comments, and the revised
goals proposed during design review into one clean specification.

All gaps and open decisions are resolved. No further product input is required
before architecture design begins. The two remaining pre-implementation actions
(config audit and stage interface contracts) are engineering tasks.

CORE SYSTEM GUARANTEES (hard requirements)
------------------------------------------

1. Global hard-constraint enforcement
   All hard constraints are enforced at the full-batch level, never at the
   individual-format level only.
   Examples:
     - platform caps
     - hard excludes
     - format eligibility / semantic requirements
     - duplicate suppression rules

2. Batch-level consistency as enforcement mechanism
   Selection is performed against one shared global candidate universe and one
   shared batch state.
   Per-format independent selection is forbidden for any constraint that must
   hold globally.
   Rationale:
     Batch consistency is not a feature; it is the mechanism required to make
     hard caps and stable diversity guarantees actually hold. This is the direct
     enforcement mechanism for Goal 1.

3. Feasibility validation before selection
   Before any assignment begins, the system must validate whether available
   supply can satisfy all hard constraints for the requested batch.
   If validation fails for some formats, the system records the reasons and
   proceeds immediately under the partial-output policy (Goal 4) — it does not
   abort the batch entirely.
   Relationship to Goal 4:
     Goal 3 is the detection mechanism; Goal 4 is the output policy.
     "Fail fast" means fail the infeasible formats immediately and report them,
     not abort the entire batch run. The system continues to fulfill all formats
     that remain feasible.
   Rationale:
     This prevents late-stage backfill or constraint-breaking recovery logic.
     Infeasibility is identified before greedy filling starts, not discovered
     after it fails.

4. Graceful degradation without constraint relaxation
   If a format cannot be fulfilled under hard constraints, the format is skipped,
   logged, and the batch is marked partial.
   Hard constraints must never be relaxed in order to avoid a partial set.
   A partial batch is a correct output; a cap-violating batch is not.

5. Format fidelity over local diversity
   Each format must receive semantically appropriate content.
   Per-format selection must never assign an item that violates the format's
   intended meaning just to satisfy category or platform balancing.
   Diversity is a batch-level objective, not a per-format override.

6. Measurable batch diversity
   Diversity in v1 is a soft balancing objective bounded by hard caps, not an
   independently enforceable lower/upper target. The system tries to spread
   selection across platforms and categories; it does not fail or skip formats
   when a platform is under-represented.
   Operationally:
     Hard caps (platform_caps in story_mix.json) are strictly enforced upper
     bounds — these are Priority 1 constraints.
     Category mix (category_mix in story_mix.json) is used as a target
     distribution for Stage 3 sorting and allocation preference — it is a
     soft objective, not a hard floor.
   Category dominance threshold:
     A category is logged as dominant when its share exceeds 1.5× its declared
     category_mix target (e.g. tech at 20% target → dominance log at ≥30%).
     Dominance logging is observability only; it does not block selection.
   Categories not in category_mix:
     Any category present in supply but absent from category_mix is treated as
     having a 0% target. Items from such categories are eligible but deprioritized
     in allocation preference. Dominance threshold for these categories is 10%
     of batch size (default guard).
   Batch size scaling:
     Percentage-based bounds apply at all batch sizes. Batches of ≤5 items
     degrade gracefully due to slot-rounding; this is documented behavior.

7. Supply-aware global allocation
   Allocation must account for real candidate availability before assignment.
   The system should not reserve quota for combinations that have insufficient
   or zero eligible supply.
   Rationale:
     Prevents empty-pool fallback distortion and fake target adherence.

8. Deterministic and reproducible selection
   Given the same:
     - candidate pool snapshot (see below)
     - config
     - historical reuse state
     - language context
   the system must produce the same selected items and format assignments.
   Determinism applies to selection and assignment only, not to downstream LLM
   generation.
   Requirements:
     - stable ordering
     - deterministic tie-breakers (see RANKING TIE-BREAK ORDER below)
     - no randomness in core selection path
   Candidate pool snapshot:
     The candidate pool is snapshotted at batch start. The snapshot is the unit
     of input for determinism guarantees. Two runs fetching live feeds at
     different times are not expected to produce identical output; two runs
     given the same snapshot must.
   Snapshot storage:
     Persisted to disk for 48 hours, named by batch_ts, stored in snapshots/
     alongside db.sqlite3. Snapshot is taken after Stage 1 normalization (post-
     dedup, post-eligibility-tagging), not from raw crawler output.
     Replay is supported as a debug-only operation in v1 (run selector against
     a snapshot file rather than live DB). No production replay API in v1.

9. Fully traceable decisioning
   Every selected, rejected, skipped, and excluded candidate must have a
   machine-readable explanation.
   Minimum trace fields:
     - candidate id / url
     - platform
     - category
     - language
     - format considered
     - selection status
     - rejection reason(s)
     - constraint hit(s)
     - rank / score inputs
     - final assignment reason
   Storage:
     Trace is emitted as a structured log record per candidate per batch run.

10. Editorial control through explicit config
    The config layer (story_mix.json) must remain human-editable, understandable,
    and structurally aligned with actual selector behavior.
    Config values must map cleanly to enforced logic.
    No "decorative config" that appears meaningful but is ignored in code.
    Pre-refactor audit required: see PRE-IMPLEMENTATION ACTIONS below.

11. Canonical story pool with localized presentation
    Selection happens from one shared global story pool.
    Cross-language variants of the same story (different URLs, same underlying
    story in different languages) are treated as independent candidates. They
    are not duplicates of each other. Goal 11 explicitly maintains one shared
    global pool with localized presentation; deduplication is URL-based within
    a language context, not cross-language.
    Localization changes presentation, wording, and possibly ranking weights,
    but does not create separate hidden story universes per language unless
    explicitly configured.

12. Novelty-first reuse policy (v1)
    In v1, previously used items are treated as binary used/not-used and
    excluded without exception. Hotness-based re-eligibility is deferred.
    Tracking key: canonical URL (no story ID clustering system exists).
    v1 novelty is URL-level only, not story-level. A Chinese-language URL and
    an English-language URL for the same underlying story are tracked as two
    independent entries in used_items. Cross-language story-level dedup is
    deferred to v2 when clustering exists. This is an explicit v1 limitation,
    not an oversight.
    Schema action: add nullable canonical_story_id column to used_items in v1
    (not populated). This makes v2 migration possible without flushing history.
    v1→v2 migration: flush used_items at v2 cutover. Backfill is not planned;
    cross-batch reuse history does not need to survive the transition.

13. Selection and ranking are separate phases
    Selection answers:
      "Which items are eligible and can be chosen under constraints?"
    Ranking answers:
      "Which eligible items are preferred?"
    Ranking logic must never override hard constraints.

14. Global allocation before format assignment
    The system must first determine the feasible batch-level distribution
    across constrained dimensions (platform, category, language, reuse),
    then assign concrete items to formats within that envelope.
    Stage 2 output is a batch-level allocation envelope — total platform
    budgets, total category budgets, total item count — not concrete per-format
    counts. Per-format item assignment happens in Stage 3 within the envelope.
    This distinction is critical: collapsing Stage 2 into per-format planning
    reintroduces the drift the refactor is designed to eliminate.
    Algorithm: greedy with rollback.
      - Greedy order: constraints applied in priority order (Priority 1 first)
      - Rollback depth: single level — if assigning a dimension slot creates
        an infeasibility for a higher-priority constraint, undo that assignment,
        emit a named conflict record, and mark the affected format(s) as partial
      - Tie-break during allocation: prefer formats with fewer eligible
        candidates (most constrained first — minimizes rollback risk)
      - Termination: allocation is complete when all feasible formats have a
        valid envelope assignment, or when remaining formats are all infeasible
    Rationale:
      LP solver is not warranted — the constraint space is small. Greedy with
      rollback finds feasibility reliably and produces a natural per-step trace
      satisfying Goal 9.

BATCH DEFINITION
----------------

The system runs in four scheduled batches per day (from run_generate.sh):

  Run   Time    Formats     Format range
  ────  ──────  ──────────  ────────────────────────────────────────
  1     06:00   9 formats   Legacy: explainer, top5, radar, regional,
                            two_takes, pattern, viral, deep_dive, niche
  2     12:00   11 formats  format_10 through format_20
  3     18:00   10 formats  format_21 through format_30
  4     22:00   16 formats  format_31 through format_46

Total formats in system: 46 (9 legacy + 37 extended, format_10 through format_46).

Items per format by strategy (from format_registry.py):
  single       →  1 item   (formats 10, 11, 16–21, 25, 27–29, 33, 36–37, 42–46)
  mix (small)  →  3 items  (formats 14, 24, 38–41)
  mix (med)    →  5 items  (formats 12, 13, 22, 23, 30)
  mix (large)  → 10 items  (format 34)
  topic_match  →  3–5 items (formats 15, 32, 35)
  comment      →  3–5 items (formats 26, 31)
  legacy mix   →  5–15 items (top5=5, two_takes=8, pattern=12, deep_dive=15)

FORMAT_REGISTRY entries are 4-tuples: (strategy, prompt_file, item_count, context_item_count).
FORMAT_CONTEXT_COUNTS dict is derived from index [3].
context_item_count = number of background articles fetched for story enrichment
  (0 = no background fetch).

The batch size for feasibility validation is the sum of item_count values for
the formats included in the specific run, derived from format_registry.py at
runtime. There is no single global batch size constant.

FORMAT FIDELITY SCHEMA
-----------------------

The refactor codifies the restrictions that exist in code today, migrates them
from hardcoded Python into story_mix.json, and makes the schema extensible for
future per-format additions.

Current restrictions, expressed as the v1 format_eligibility schema:

  Format(s)        Restriction                        Source today
  ───────────────  ─────────────────────────────────  ──────────────────────
  11, 16, 17, 18   excluded_categories: entertainment  FORMAT_REQUIRES_NEWS
                   (blocks bilibili/youtube/nicovideo  (format_registry.py)
                   items matching anime/PV/MV/Trailer
                   title patterns)
  26, 31           source restricted to               comment_platforms
                   comment_platforms only             (story_mix.json)
                   (hackernews, youtube, reddit)
                   with mix as selection_strategy
                   when comment_pool is empty
  All others       No category or content restriction  (none)
                   beyond global platform_caps and
                   category_mix ratios

The format_eligibility block to be added to story_mix.json:

  "format_eligibility": {
    "11":  { "excluded_categories": ["entertainment"], "requires_news_event": true },
    "16":  { "excluded_categories": ["entertainment"], "requires_news_event": true },
    "17":  { "excluded_categories": ["entertainment"], "requires_news_event": true },
    "18":  { "excluded_categories": ["entertainment"], "requires_news_event": true },
    "26":  { "source_restricted_to_group": "comment_platforms", "selection_mode": { "primary": "comment_platform_only", "secondary": "general_mix_if_primary_insufficient", "secondary_is_relaxation": false } },
    "31":  { "source_restricted_to_group": "comment_platforms", "selection_mode": { "primary": "comment_platform_only", "secondary": "general_mix_if_primary_insufficient", "secondary_is_relaxation": false } }
  }

All other formats: allowed_categories: null (unrestricted beyond global caps).
Per-format category restrictions for formats 12–46 are deferred to a future
editorial pass; the schema supports them when needed.

CONSTRAINT PRIORITY ORDER (must be explicit in design)
------------------------------------------------------

Priority 1: Hard platform caps / hard excludes / legal or policy exclusions
Priority 2: Format fidelity / semantic eligibility
Priority 3: Feasibility constraints / supply reality
Priority 4: Duplicate suppression / reuse policy
Priority 5: Batch-level diversity targets
Priority 6: Ranking preferences / tuning weights / editorial soft bias

Interpretation:
- Lower-priority rules may optimize within higher-priority constraints
- Lower-priority rules may never break higher-priority guarantees

FAILURE POLICY (must be enforced)
---------------------------------

If batch construction becomes infeasible under hard constraints:
  1. Do not backfill with violating items
  2. Do not silently relax caps
  3. Skip unfillable formats
  4. Mark batch as partial
  5. Emit structured failure reasons

When multiple constraints conflict (jointly infeasible at Stage 2):
  - Run a pre-allocation feasibility scan in constraint priority order
  - At the first constraint that cannot be jointly satisfied with a higher-
    priority constraint, emit a named conflict record identifying both
    constraints and their competing demands
  - Mark the batch partial before any item selection begins
  - Do not relax either constraint

This policy must be implemented explicitly, not left to downstream fallback code.

RANKING TIE-BREAK ORDER (locked)
---------------------------------

  score desc -> hotness desc -> freshness desc -> canonical_id asc

Definitions:
  score     = effective_hotness (hotness × topic_boost × platform_weight)
  hotness   = raw crawler hotness value
  freshness = item publish/crawl timestamp

Null handling:
  hotness may be absent on items from certain crawled sources.
  Null hotness must be treated as 0 in the tie-break, not as an error.

PARTIAL BATCH OBSERVABILITY
----------------------------

Implementation: DB column + structured log. No real-time alerting in v1.
  - Emit structured JSON log per skipped format at point of skip
  - Add partial_formats JSON column to story_sets table for dashboard queries
  - Webhook/queue notification deferred until a consumer exists

Required log fields (all derivable at Stage 3):
  - skipped_format_id
  - shortage_dimension
  - blocking_constraint
  - candidate_count_before_filtering
  - candidate_count_after_filtering

TUNING / CONFIGURATION LAYER (soft behavior only)
-------------------------------------------------

T1. Audience/profile personalization
    Different user cohorts or audience profiles may change ranking weights or
    presentation style, but not hard constraints.

T2. Regional soft bias
    Region relevance may adjust ranking within the eligible set only.
    It cannot override caps, format fidelity, or hard excludes.

T3. Feedback-driven adaptation
    Engagement data may adjust configurable soft weights within bounded ranges.
    Hard constraints remain immutable.

T4. A/B testing support
    Multiple mix or ranking variants may be tested, provided all variants
    remain inside the same core constraint framework.

TUNING LAYER HARD BOUNDS
------------------------

The tuning layer may:
  - adjust soft ranking weights
  - adjust batch diversity targets within predefined ranges
  - change presentation strategy
  - change audience-specific ordering preferences

The tuning layer may NOT:
  - modify hard platform caps
  - override format eligibility rules
  - disable duplicate suppression rules
  - remove hard excludes
  - bypass feasibility checks
  - force backfill when constraints fail

PRE-IMPLEMENTATION ACTIONS (critical path, engineering tasks)
-------------------------------------------------------------

Action 1: Config alignment audit (1 day, blocks Stage 2 design)
---------------------------------------------------------------
Audit every key in story_mix.json against load_story_mix_config() and every
call site of config[...] in selector.py, format_registry.py, and run.py.

Known issues found during review:
  - comment_platforms: present in story_mix.json, NOT loaded by
    load_story_mix_config(). Currently decorative. Must be wired in as part
    of the format_eligibility migration for formats 26 and 31.
  - min_slots_per_format: loaded and enforced for top5, two_takes, pattern,
    deep_dive only. Formats 10–46 call _select_by_mix() without a format_name
    argument so min_slots are never applied. Must be fixed or documented as
    intentional.
  - FORMAT_REQUIRES_NEWS: hardcoded in format_registry.py. Must be migrated
    to the format_eligibility block in story_mix.json.
  - platform_weight_overrides, topic_boosts, platform_caps, category_mix:
    confirmed clean (using current JSON key names).

Action 2: Stage interface contracts (blocks all stage implementations)
---------------------------------------------------------------------
Define the input/output schema for each stage boundary before any stage is
built. Minimum required:
  - Stage 1 output: normalized candidate record schema (per-item)
  - Stage 2 output: batch-level allocation envelope
      { platform_budgets: {platform: max_slots},
        category_budgets: {category: target_slots},
        total_item_count: int,
        per_format_feasibility: {format_id: feasible|skipped+reason} }
      Note: Stage 2 does NOT output concrete per-format item counts.
      That collapses Stage 2 into per-format planning. Per-format assignment
      is Stage 3's responsibility, within the envelope Stage 2 defines.
  - Stage 3 output: selected item list + per-item trace record
This prevents interface collisions during integration. This is the first
deliverable of architecture design.

RECOMMENDED ARCHITECTURE (four stages)
---------------------------------------

Stage 1: Candidate normalization
  - canonical story identity (URL-based in v1)
  - platform / category / language normalization
  - reuse-state join (binary URL lookup against used_items)
  - format eligibility tagging (per format_eligibility schema above)
  - emit Stage 1 summary metrics as structured log: total candidates ingested,
    counts by platform/category/language, excluded-by-reuse count, eligible
    count per format. This makes Stage 1 transformations auditable independently
    of the selector decisions in Stages 2–4.

Stage 2: Feasibility + global allocation
  - validate supply against hard constraints (constraint priority order above)
  - compute feasible batch-level quota / allocation plan via greedy with rollback
  - emit named conflict records for jointly infeasible constraint pairs
  Note: config audit (Action 1) must be complete before this stage is designed.

Stage 3: Deterministic constrained selection
  - choose items under hard constraints
  - enforce platform caps and diversity bounds (Goal 6 numeric bounds above)
  - no per-format independent fallback

Stage 4: Format assignment + trace logging
  - assign selected items to formats
  - emit structured trace records for all decisions (Goal 9)
  - add partial_formats to story_sets; emit structured failure reasons
  - mark partial output if needed

ONE-LINE SUMMARY
----------------
The refactored system must treat caps, fidelity, feasibility, reuse, and
determinism as correctness guarantees enforced through one shared global batch
selection process; tuning may shape preferences, but can never override core
constraints.

SELECTOR CONFIG SCHEMA (story_mix.json)
----------------------------------------
The canonical config file the selector reads at runtime. All selector behavior
must be derivable from this file. No hardcoded logic may duplicate or contradict
any value defined here.

This schema is the authoritative target for config alignment (Action 1 above).
Any key present here must be loaded and enforced. Any key absent here must not
be read from config (treat as a bug if it is).

  {
    "version": "1.0",
    "contract_notes": {
      "purpose": "Authoritative selector config for story engine refactor",
      "rules": [
        "Hard constraints are enforced batch-globally",
        "Soft targets influence allocation and ranking but never override hard constraints",
        "All platforms are governed by caps (explicit or default)",
        "Unspecified categories are normalized to 'unknown' and deprioritized",
        "Partial output is valid; constraint relaxation is not allowed",
        "category_mix is a soft optimization target that actively deprioritizes over-represented categories during selection — it is not merely logged"
      ]
    },

    "hard_constraints": {
      "platform_caps": {
        "bilibili":      0.10,
        "reddit":        0.10,
        "youtube":       0.10,
        "weibo":         0.05,
        "baidu":         0.05,
        "twitter":       0.10,
        "hackernews":    0.15,
        "zhihu":         0.10,
        "nicovideo":     0.10,
        "google_trends": 0.10,
        "wikipedia":     0.05,
        "news_rss":      0.40
      },

      "default_uncapped_platform_max_share": 0.10,

      "platform_groups": {
        "news_rss": [
          "rss", "generic_rss", "google_news",
          "bbc", "guardian", "reuters", "ap", "nyt",
          "aljazeera", "chinatimes", "hk01", "mirrormedia",
          "sina", "wenxuecity", "yahoo_japan",
          "naver_news", "livedoor_news", "goo_news", "daum_news"
        ]
      },

      "hard_excluded_platforms": [],

      "platform_default_policy": "cap_with_default",

      "reuse_policy": {
        "mode": "binary_url_exclusion",
        "tracking_key": "canonical_url",
        "allow_reuse_if_hotter": false
      },

      "partial_output_policy": {
        "allow_partial_batches": true,
        "relax_constraints": false,
        "skip_unfillable_formats": true,
        "emit_conflict_records": true
      }
    },

    "soft_targets": {
      "platform_targets": {
        "derive_from_caps": true,
        "target_ratio_of_cap": 0.7,
        "tolerance": 0.03,
        "apply_to_default_cap": true
      },

      "category_mix": {
        "world":         0.20,
        "politics":      0.12,
        "business":      0.10,
        "technology":    0.15,
        "ai":            0.07,
        "science":       0.08,
        "society":       0.12,
        "sports":        0.06,
        "entertainment": 0.10
      },

      "category_policy": {
        "unknown_category_target": 0.0,
        "unknown_category_dominance_threshold": 0.10,
        "dominance_multiplier": 1.5,
        "enforcement_mode": "soft_target",
        "selection_behavior": "deprioritize_when_over_target"
      }
    },

    "normalization": {
      "unknown_category_value": "unknown",

      "platform_aliases": {
        "x":  "twitter",
        "yt": "youtube",
        "hn": "hackernews"
      },

      "news_event_detection": {
        "video_platforms": ["bilibili", "youtube", "nicovideo"],
        "title_block_regex": "\\b(anime|PV|MV|Trailer|预告|番剧|AMV|OP|ED)\\b"
      }
    },

    "localization": {
      "notes": "Selection operates on one shared global candidate pool. Deduplication is URL-based within the same language context only. Cross-language variants of the same underlying story are treated as independent candidates.",
      "pool_scope": "global",
      "dedup_key": "canonical_url",
      "dedup_scope": "within_language",
      "cross_language_dedup": false
    },

    "ranking": {
      "base_score_field": "effective_hotness",
      "tie_break_order": [
        "score_desc",
        "hotness_desc",
        "freshness_desc",
        "canonical_id_asc"
      ],
      "null_hotness_value": 0.0,

      "topic_boosts": {
        "ai":            1.20,
        "technology":    1.15,
        "science":       1.10,
        "business":      1.05,
        "world":         1.00,
        "politics":      1.00,
        "society":       1.00,
        "culture":       0.95,
        "sports":        0.95,
        "entertainment": 0.90,
        "lifestyle":     0.90,
        "opinion":       0.90
      },

      "platform_weight_overrides": {
        "hackernews":    1.05,
        "reddit":        1.00,
        "twitter":       1.00,
        "youtube":       0.95,
        "bilibili":      0.95,
        "weibo":         1.00,
        "baidu":         1.00,
        "zhihu":         1.00,
        "nicovideo":     0.90,
        "google_trends": 0.85,
        "wikipedia":     0.80,
        "news_rss":      1.10
      }
    },

    "source_groups": {
      "comment_platforms": [
        "hackernews",
        "youtube",
        "reddit"
      ],

      "news_like_platforms": [
        "twitter", "weibo", "baidu", "zhihu",
        "reddit", "hackernews",
        "rss", "generic_rss", "google_news",
        "bbc", "guardian", "reuters", "ap", "nyt", "aljazeera"
      ],

      "video_platforms": [
        "youtube", "bilibili", "nicovideo"
      ]
    },

  Note on source_groups usage (plain text — not a JSON key):
    comment_platforms   — consumed by Stage 1 (source restriction for formats 26/31),
                          Stage 2 Step 2 (primary/secondary supply split),
                          Stage 3 Pass 1 Step 4.
    news_like_platforms — informational in v1; reserved for future format eligibility
                          rules. No algorithm step reads this group in v1.
    video_platforms     — informational in v1. The same platform list is used
                          functionally via normalization.news_event_detection.video_platforms
                          for _is_entertainment_media(). Do not read source_groups.video_platforms
                          in code — read news_event_detection.video_platforms instead.

    "format_eligibility": {
      "11": { "excluded_categories": ["entertainment"], "requires_news_event": true },
      "16": { "excluded_categories": ["entertainment"], "requires_news_event": true },
      "17": { "excluded_categories": ["entertainment"], "requires_news_event": true },
      "18": { "excluded_categories": ["entertainment"], "requires_news_event": true },
      "26": {
        "source_restricted_to_group": "comment_platforms",
        "selection_mode": {
          "primary": "comment_platform_only",
          "secondary": "general_mix_if_primary_insufficient",
          "secondary_is_relaxation": false
        }
      },
      "31": {
        "source_restricted_to_group": "comment_platforms",
        "selection_mode": {
          "primary": "comment_platform_only",
          "secondary": "general_mix_if_primary_insufficient",
          "secondary_is_relaxation": false
        }
      }
    },

    "format_defaults": {
      "allowed_categories": null,
      "excluded_categories": [],
      "requires_news_event": false,
      "source_restricted_to_group": null,
      "selection_mode": "normal"
    },

    "selection_policy": {
      "global_allocation_before_assignment": true,
      "selection_and_ranking_are_separate": true,
      "constraint_priority_order": [
        "platform_caps_and_hard_excludes",
        "format_fidelity",
        "feasibility_and_supply",
        "duplicate_suppression_and_reuse",
        "batch_diversity_targets",
        "ranking_preferences"
      ],

      "allocation_algorithm": {
        "type": "greedy_with_single_level_rollback",
        "format_processing_order": "most_constrained_first",
        "rollback_scope": "current_format_only",
        "on_joint_infeasibility": "emit_conflict_and_mark_partial"
      }
    },

    "determinism": {
      "snapshot_enabled": true,
      "snapshot_stage": "post_stage1_normalization",
      "snapshot_retention_hours": 48,
      "replay_supported": true,
      "randomness_allowed_in_selector": false
    },

    "observability": {
      "trace_all_candidates": true,
      "trace_storage": "jsonl",
      "stage1_metrics_log": true,
      "partial_batch_db_column": "partial_formats",
      "required_partial_fields": [
        "skipped_format_id",
        "shortage_dimension",
        "blocking_constraint",
        "candidate_count_before_filtering",
        "candidate_count_after_filtering"
      ]
    },

    "tuning_layer_bounds": {
      "can_adjust": [
        "ranking.platform_weight_overrides",
        "ranking.topic_boosts",
        "soft_targets.category_mix",
        "soft_targets.platform_targets"
      ],
      "cannot_adjust": [
        "hard_constraints.platform_caps",
        "hard_constraints.hard_excluded_platforms",
        "hard_constraints.reuse_policy",
        "hard_constraints.partial_output_policy",
        "format_eligibility",
        "selection_policy.constraint_priority_order"
      ]
    }
  }

================================================================================
# END OF GOALS
================================================================================




================================================================================
STORY ENGINE — ARCHITECTURE DESIGN
================================================================================

PURPOSE
-------
This section is the implementation design for the story engine refactor.
It is derived directly from the goals above and provides concrete module
structure, data schemas, per-stage algorithms, DB schema changes, config
changes, and the orchestration contract.

An engineer should be able to implement each stage from this document without
referring back to the goals section for clarification.

--------------------------------------------------------------------------------
MODULE STRUCTURE
--------------------------------------------------------------------------------

src/engine/
  selector/
    __init__.py           # public API: run_batch() → BatchResult
    config.py             # load + validate story_mix.json → BatchConfig
    schemas.py            # all dataclasses (see DATA SCHEMAS below)
    stage1_normalize.py   # candidate normalization
    stage2_allocate.py    # feasibility + global allocation envelope
    stage3_select.py      # deterministic constrained selection
    stage4_assign.py      # format assignment + trace logging
    snapshot.py           # snapshot write/read (disk persistence)
    trace.py              # structured log emission helpers
  format_registry.py      # refactored: reads format_eligibility from config
  run.py                  # CLI entry point (interface unchanged)

Dependency flow (strictly one-way, no circular imports):
  run.py
    → selector/__init__.py
      → config.py
      → stage1_normalize.py  → schemas.py, snapshot.py, trace.py
      → stage2_allocate.py   → schemas.py, trace.py
      → stage3_select.py     → schemas.py, trace.py
      → stage4_assign.py     → schemas.py, trace.py

--------------------------------------------------------------------------------
DATA SCHEMAS  (schemas.py)
--------------------------------------------------------------------------------

All inter-stage contracts are typed dataclasses. No dicts passed between stages.

── Stage 1 output ──────────────────────────────────────────────────────────────

  @dataclass
  class NormalizedCandidate:
      candidate_id:        str            # canonical URL (v1 identity key)
      url:                 str
      platform:            str            # normalized lowercase
      category:            str            # normalized; "unknown" if not in config
      language:            str
      hotness:             float          # raw crawler value; 0.0 if absent
      effective_hotness:   float          # hotness × topic_boost multiplier
      freshness:           datetime       # publish or crawl timestamp
      title:               str
      is_used:             bool           # True if URL found in used_items
      eligible_format_ids: frozenset[int] # format ids this item qualifies for
      canonical_story_id:  str | None     # always None in v1

── Stage 2 output ──────────────────────────────────────────────────────────────

  @dataclass
  class FormatFeasibility:
      format_id:           int
      feasible:            bool
      item_count:          int            # target items (0 if not feasible)
      eligible_count:      int            # candidates passing all hard constraints
      skip_reason:         str | None
      blocking_constraint: str | None     # e.g. "platform_cap:bilibili"
      needs_secondary:     bool = False   # True for formats 26/31 when
                                          # primary (comment_platforms) supply
                                          # is insufficient; filled from general pool

  @dataclass
  class ConflictRecord:
      constraint_a:        str            # e.g. "platform_cap:bilibili"
      constraint_b:        str            # e.g. "format_fidelity:11"
      description:         str
      affected_format_ids: list[int]

  @dataclass
  class AllocationEnvelope:
      batch_ts:                int   # UNIX ms from create_story_set()
      platform_budgets:        dict[str, int]        # platform → hard max slots
      platform_soft_budgets:   dict[str, int]        # platform → soft target slots
                                                      # = floor(hard_budget × target_ratio_of_cap)
      category_budgets:        dict[str, int]        # category → target slots in batch
      total_item_count:        int                   # sum of item_counts for feasible formats
      per_format_feasibility:  dict[int, FormatFeasibility]
      partial:                 bool
      conflict_records:        list[ConflictRecord]

  NOTE: AllocationEnvelope does NOT contain per-format item assignments.
  It defines the batch-level budget envelope. Stage 3 assigns items to formats
  within this envelope. Putting per-format counts in Stage 2 output collapses
  it into per-format planning — this is explicitly prohibited.

── Stage 3 output ──────────────────────────────────────────────────────────────

  @dataclass
  class TraceRecord:
      candidate_id:          str
      url:                   str
      platform:              str
      category:              str
      language:              str
      format_considered:     int | None
      selection_status:      str          # "selected" | "rejected" | "excluded"
      rejection_reasons:     list[str]    # empty if selected
      constraint_hits:       list[str]    # which constraints triggered
      score:                 float        # effective_hotness used in sort
      hotness:               float        # raw value
      rank_inputs:           dict         # {score, hotness, freshness, candidate_id}
      final_assignment:      int | None   # format_id if selected, else None

  @dataclass
  class SelectedItem:
      candidate_id:        str
      url:                 str
      platform:            str
      category:            str
      language:            str
      title:               str
      eligible_format_ids: frozenset[int]  # carried from NormalizedCandidate
      score:               float
      hotness:             float
      freshness:           datetime
      reserved_for_format: int | None      # set by Stage 3 Pass 1; None = global fill
      trace:               TraceRecord

── Stage 4 output ──────────────────────────────────────────────────────────────

  @dataclass
  class PartialFormat:
      skipped_format_id:                  int
      shortage_dimension:                 str   # "platform" | "category" | "supply" | "fidelity"
      blocking_constraint:                str
      candidate_count_before_filtering:   int
      candidate_count_after_filtering:    int

  @dataclass
  class BatchResult:
      batch_ts:            int   # UNIX ms from create_story_set()
      story_set_id:        int
      format_assignments:  dict[int, list[NormalizedCandidate]]
                           # full objects — generators receive these directly, no re-query.
                           # For formats with context_item_count > 0, generators also
                           # fetch background context items via get_background_items()
                           # (crawler_reader.py). _build_stories_block() accepts optional
                           # context_items and renders a two-section prompt:
                           #   "## Current Development" (main sources)
                           #   "## Background Context" (reference only)
                           # Applies to generate_by_format() and 5 legacy generators
                           # (explainer, regional, viral, deep_dive, niche).
      partial:             bool
      partial_formats:     list[PartialFormat]
      trace_log_path:      str                    # path to JSONL trace file
      snapshot_path:       str                    # path to Stage 1 snapshot

--------------------------------------------------------------------------------
STAGE 1 — CANDIDATE NORMALIZATION  (stage1_normalize.py)
--------------------------------------------------------------------------------

Responsibility:
  Transform raw crawler DB rows into a clean, typed, enriched candidate list
  ready for allocation and selection. Emit a snapshot and a summary metrics log.

Input:
  - db_path: str
  - config: BatchConfig
  - format_ids: list[int]  (the formats included in this batch run)
  - hours: int             (lookback window, default 48)

Classification state filter:
  Crawler sets classification_state and story_category on all TrendItems.
  get_top_items() filters classification_state NOT IN ('pending', 'failed'),
  ensuring only fully-classified items enter the candidate pool.
  stage1_normalize uses story_category directly from the crawler field —
  no selection-time derivation in normal operation.

Output:
  - list[NormalizedCandidate]  (used items excluded)
  - snapshot written to snapshots/{batch_ts}_stage1.json

Algorithm:

  Step 1 — Raw query (category-aware fetch)

    The fetch query has two modes depending on whether the active profile
    declares a category allowlist (see Step 4b below and the category_mix
    allowlist contract):

    Mode A — Unfocused profile (no zero-target categories in category_mix,
             or category_mix absent entirely — base / run1_legacy):
      SELECT url, platform, category, language, hotness, published_at,
             title, crawler_item_id
      FROM crawler_items
      WHERE published_at >= NOW() - INTERVAL hours HOURS
      ORDER BY hotness DESC
      LIMIT N                          # one global top-N fetch

      This is the original Phase A behavior — a single global hotness-ranked
      pool with platform-level cap applied via per_platform_k.

    Mode B — Focused profile (category_mix has at least one target == 0):
      For each cat in allowed_categories:
        SELECT ... FROM crawler_items
        WHERE story_category = cat
          AND published_at >= NOW() - INTERVAL hours HOURS
        ORDER BY hotness DESC
        LIMIT N_per_cat                # top-N per allowed category

      Then merge all per-category slices into one deduplicated global list.
      Downstream stages still see one shared global candidate pool — the
      "global pool" invariant (localization section, pool_scope=global)
      describes how selection operates, not how fetch shapes the pool.

      Rationale: without this split, high-hotness categories (entertainment,
      politics) saturate the top-N and drown out scarce but on-topic
      categories (business, ai, science, world). The Stage 1 hard allowlist
      would then filter a nearly-empty pool, producing artificially small
      focused batches even when the crawler has hundreds of matching items.

      Concretely: with N=500 global and business corpus share ≈ 6.6%, only
      ~20-30 business items reach Stage 1 out of 1,400+ business items the
      crawler collected over a 48h window. Mode B surfaces the full on-topic
      pool before the Step 4b hard filter and Step 5 used-item filter are
      applied.

  Step 2 — Per-item normalization
    For each raw row:
      a. platform  = row.platform.lower().strip()
                     apply alias map if configured (e.g. "yt" → "youtube")
                     if platform in config.hard_excluded_platforms: skip row
                       (emit TraceRecord status="excluded",
                        reasons=["hard_excluded_platform"],
                        constraint_hits=["hard_excluded_platforms"])
                     resolve through platform_groups: if platform is a member
                     of any group in config.platform_groups, replace platform
                     with the group name (e.g. "bbc" → "news_rss").
                     This ensures all downstream cap checks in Stages 2 and 3
                     use the group key that exists in platform_budgets.
                     Original source platform is preserved in raw_payload only.
      b. category  = derive_category(row, config)
                     Emergency fallback only. The crawler now sets story_category
                     on all items before they reach the story engine.
                     stage1_normalize uses story_category directly;
                     _derive_category() is called only when story_category is
                     absent (an edge case that should not occur in normal
                     operation). The function returns 'unknown' unconditionally —
                     the config-driven 4-rule pipeline
                     (bucket_direct, topic_tag, bucket_hot_now_platform) has
                     been removed. Invocations are counted and logged as
                     emergency_derivation_rate per batch. In production this
                     rate is 0%.
      c. hotness   = float(row.hotness) if row.hotness is not None else 0.0
      d. boost            = config.topic_boosts.get(category, 1.0)
         platform_weight  = config.platform_weight_overrides.get(platform, 1.0)
         effective_hotness = hotness × boost × platform_weight
      e. is_used   = url in used_urls_set  (pre-fetched SET from used_items)
      f. freshness = row.published_at (parsed to datetime, UTC)

  Step 3 — Format eligibility tagging
    For each candidate, compute eligible_format_ids:
      eligible = set()
      for format_id in format_ids:
        rule = config.format_eligibility.get(format_id)
        if rule is None:
          eligible.add(format_id)   # no restriction → eligible
          continue
        if rule.excluded_categories and candidate.category in rule.excluded_categories:
          continue                  # blocked by category exclusion
        if rule.requires_news_event and _is_entertainment_media(candidate):
          continue                  # blocked by news-event requirement
        if rule.source_restricted_to_group == "comment_platforms":
          if candidate.platform not in config.source_groups["comment_platforms"]:
            continue                # blocked by source restriction
        eligible.add(format_id)
      candidate.eligible_format_ids = frozenset(eligible)

  Step 4 — Filter used items + emit reuse exclusion traces
    For every candidate where is_used == True:
      Emit TraceRecord(
          candidate_id      = candidate.candidate_id,
          url               = candidate.url,
          platform          = candidate.platform,
          category          = candidate.category,
          language          = candidate.language,
          format_considered = None,
          selection_status  = "excluded",
          rejection_reasons = ["used_item"],
          constraint_hits   = ["reuse_policy:binary_url"],
          score             = candidate.effective_hotness,
          hotness           = candidate.hotness,
          rank_inputs       = {},
          final_assignment  = None
      )
      Write trace to logs/trace_{batch_ts}.jsonl immediately (same stream as
      Stage 3/4 traces — all candidate decisions land in one file).
    Return only candidates where is_used == False for downstream stages.

  Step 5 — Emit Stage 1 summary metrics (structured log)
    {
      "stage": 1,
      "batch_ts": ...,
      "total_ingested": N,
      "excluded_by_reuse": N,
      "available": N,
      "by_platform": {platform: count, ...},
      "by_category": {category: count, ...},
      "by_language": {language: count, ...},
      "eligible_per_format": {format_id: count, ...}
    }

  Step 6 — Write snapshot
    Serialize list[NormalizedCandidate] to snapshots/{batch_ts}_stage1.json.
    Snapshots older than 48 hours are deleted at the start of each run.

_is_entertainment_media(candidate) definition:
  Returns True if platform in {"bilibili", "youtube", "nicovideo"}
  AND title matches regex: \b(anime|PV|MV|Trailer|预告|番剧|AMV|OP|ED)\b (case-insensitive)
  This mirrors the existing FORMAT_REQUIRES_NEWS filter logic exactly.

--------------------------------------------------------------------------------
STAGE 2 — FEASIBILITY + GLOBAL ALLOCATION  (stage2_allocate.py)
--------------------------------------------------------------------------------

Responsibility:
  Validate that the available candidate pool can satisfy the batch's hard
  constraints. Compute a batch-level allocation envelope. Identify and record
  infeasible formats before any selection begins.

Input:
  - candidates: list[NormalizedCandidate]  (Stage 1 output, used items excluded)
  - config: BatchConfig
  - format_ids: list[int]

Output:
  - AllocationEnvelope

Algorithm:

  Step 1 — Build supply index
    supply: dict[format_id, list[NormalizedCandidate]]
    For each format_id, collect candidates where format_id in eligible_format_ids.

  Step 2 — Per-format feasibility scan (most-constrained first)
    Sort format_ids by len(supply[format_id]) ascending (fewest candidates first).
    For each format_id:
      required = format_registry.item_count(format_id)
      rule = config.format_eligibility.get(format_id, config.format_defaults)

      If rule.selection_mode is a structured object (primary/secondary):
        # Formats 26, 31: comment_platform_only with general_mix fallback
        supply_primary   = [c for c in supply[format_id]
                            if c.platform in config.source_groups["comment_platforms"]]
        supply_secondary = [c for c in supply[format_id]
                            if c.platform not in config.source_groups["comment_platforms"]]
        eligible_primary   = len(supply_primary)
        eligible_secondary = len(supply_secondary)
        eligible_total     = eligible_primary + eligible_secondary

        if eligible_total < required:
          mark format infeasible
          record FormatFeasibility(feasible=False, skip_reason="insufficient_supply",
                                   blocking_constraint="supply:{format_id}",
                                   eligible_count=eligible_total, item_count=required)
        else:
          mark format feasible
          record FormatFeasibility(feasible=True, eligible_count=eligible_total,
                                   item_count=required,
                                   needs_secondary=(eligible_primary < required))
          # needs_secondary=True is NOT a partial flag — secondary_is_relaxation=false
          # means falling back to general pool is expected behavior, not degradation.
      Else (normal format):
        eligible = len(supply[format_id])
        if eligible < required:
          mark format infeasible
          record FormatFeasibility(feasible=False, skip_reason="insufficient_supply",
                                   blocking_constraint="supply:{format_id}",
                                   eligible_count=eligible, item_count=required)
        else:
          mark format feasible
          record FormatFeasibility(feasible=True, eligible_count=eligible,
                                   item_count=required)

  Step 3 — Compute total_item_count
    total_item_count = sum(ff.item_count for ff in feasibility.values() if ff.feasible)

  Step 4 — Compute platform_budgets
    First, resolve each candidate's platform through platform_groups:
      For each candidate with platform p:
        resolved_platform = p
        For each group_name, members in config.platform_groups:
          if p in members:
            resolved_platform = group_name
            break
      Use resolved_platform for all cap lookups from this point forward.
      Example: platform="bbc" → resolved_platform="news_rss"
               platform="reddit" → resolved_platform="reddit" (no group match)

    Then compute budgets:
      For each resolved platform p in config.platform_caps:
        platform_budgets[p] = floor(total_item_count × platform_caps[p])
        Floor ensures we never exceed the cap even with rounding.
      For any resolved platform NOT in config.platform_caps:
        platform_budgets[p] = floor(total_item_count ×
                                    config.default_uncapped_platform_max_share)
        Default cap applied — no platform is fully uncapped.
    This ensures group caps (e.g. news_rss: 0.40) are enforced across all
    member platforms collectively, not per individual source.

    Then compute soft platform budgets (used for Stage 3 deprioritization only,
    never as hard caps):
      ratio = config.platform_targets.target_ratio_of_cap   # e.g. 0.7
      For each platform p in platform_budgets:
        platform_soft_budgets[p] = floor(platform_budgets[p] × ratio)
      If config.platform_targets.apply_to_default_cap is True:
        also compute soft budgets for default-capped platforms using the same ratio.
      platform_soft_budgets is stored in AllocationEnvelope and read by Stage 3
      Pass 2 to deprioritize candidates from over-soft-target platforms.

  Step 5 — Compute category_budgets
    For each category c in config.category_mix:
      category_budgets[c] = floor(total_item_count × category_mix[c])
    Categories not in config.category_mix →
      category_budgets["unknown"] = floor(total_item_count ×
                                    config.unknown_category_dominance_threshold)
      (eligible but deprioritized; dominance guard fires at this threshold)

  Step 6 — Joint feasibility check (platform × feasible-format supply)
    For each feasible format:
      Check whether supply[format_id] contains at least item_count candidates
      that would fit within remaining platform_budgets after allocating other
      higher-priority feasible formats.
      If not: emit ConflictRecord naming the competing constraints.
      Mark format as partial in the envelope.

  Step 7 — Return AllocationEnvelope
    partial = any(not ff.feasible for ff in per_format_feasibility.values())

  Greedy rollback rule (Step 6 detail):
    Process formats in most-constrained-first order.
    For each format F, count how many candidates in supply[F] fit within
    remaining platform_budgets — iterate supply[F] and count candidates where
    platform_used[c.platform] < platform_budgets[c.platform].
    This is a platform-slot-aware count, not a raw supply count.
    If that count < required:
      - Do not reserve for F
      - Emit ConflictRecord(constraint_a="platform_cap:{p}",
                            constraint_b="format_supply:{F}",
                            description="...",
                            affected_format_ids=[F])
      - Mark F as infeasible in per_format_feasibility
    If feasible: increment platform_used for each platform consumed by F's
    required item_count (distributed proportionally across F's supply).
    Single-level rollback only: undo the current format's reservation,
    do not re-evaluate previously confirmed formats.
    v1 tradeoff: this greedy approach is order-sensitive. Processing formats
    in most-constrained-first order minimizes avoidable partials but does not
    guarantee the globally optimal feasible assignment. The allocator may
    produce conservative partial output in edge cases where a different
    processing order would have found a valid assignment. This is an acceptable
    v1 simplification. Document in observability output when partial output
    occurs, so the pattern can be detected and a smarter allocator added in v2
    if needed.

--------------------------------------------------------------------------------
STAGE 3 — DETERMINISTIC CONSTRAINED SELECTION  (stage3_select.py)
--------------------------------------------------------------------------------

Responsibility:
  Select the exact items to include in the batch, respecting all hard
  constraints from the AllocationEnvelope, in a fully deterministic order.
  Emit a TraceRecord for every candidate.

Input:
  - candidates: list[NormalizedCandidate]  (Stage 1 output)
  - envelope:   AllocationEnvelope         (Stage 2 output)

Output:
  - list[SelectedItem]
  - list[TraceRecord]  (one per candidate, selected + rejected)

Algorithm (two-pass — format reservation then global fill):

  The single-pass design had a format starvation bug: Stage 3 selected globally
  by score and Stage 4 assigned with lowest-format-id-first, which could starve
  later formats even though Stage 2 declared them feasible. The fix is to
  reserve items per format in Stage 3 before filling the remaining global budget.

  Step 1 — Initialize shared state
    platform_used:    dict[str, int] = {p: 0 for p in envelope.platform_budgets}
    category_used:    dict[str, int] = {c: 0 for c in envelope.category_budgets}
    reserved:         dict[str, list[str]] = {}  # format_id → [candidate_ids]
    reserved_ids:     set[str] = set()
    feasible_formats: set[str] = {fid for fid, ff in
                          envelope.per_format_feasibility.items() if ff.feasible}

  Step 2 — Sort candidates (deterministic, applied once, used in both passes)
    sorted_candidates = sorted(candidates, key=lambda c: (
        -c.effective_hotness,      # score desc
        -c.hotness,                # hotness desc
        -c.freshness.timestamp(),  # freshness desc
        c.candidate_id             # stable tiebreak asc
    ))

  PASS 1 — Per-format quota reservation (most-constrained first)

  Step 3 — Sort feasible formats by eligible supply ascending
    feasible_sorted = sorted(
        feasible_formats,
        key=lambda fid: envelope.per_format_feasibility[fid].eligible_count
    )

  Step 4 — Reserve items per format
    For each format_id in feasible_sorted:
      ff = envelope.per_format_feasibility[format_id]
      rule = config.format_eligibility.get(format_id, config.format_defaults)

      If rule.selection_mode is a structured object (formats 26, 31):
        # Primary pass: comment_platforms only — iterate with inline platform_used update
        picked = []
        for c in sorted_candidates:
            if len(picked) >= ff.item_count:
                break
            if format_id not in c.eligible_format_ids:
                continue
            if c.candidate_id in reserved_ids:
                continue
            if c.platform not in config.source_groups["comment_platforms"]:
                continue
            if platform_used[c.platform] >= envelope.platform_budgets[c.platform]:
                continue
            picked.append(c)
            platform_used[c.platform] += 1  # update inline — enforces cap within this format
        # Secondary pass: fill remainder from general pool if primary insufficient
        if len(picked) < ff.item_count:
            picked_ids = {p.candidate_id for p in picked}
            for c in sorted_candidates:
                if len(picked) >= ff.item_count:
                    break
                if format_id not in c.eligible_format_ids:
                    continue
                if c.candidate_id in reserved_ids or c.candidate_id in picked_ids:
                    continue
                if c.platform in config.source_groups["comment_platforms"]:
                    continue
                if platform_used[c.platform] >= envelope.platform_budgets[c.platform]:
                    continue
                picked.append(c)
                platform_used[c.platform] += 1  # update inline
                picked_ids.add(c.candidate_id)
        # secondary_is_relaxation=false: using secondary does NOT trigger partial flag
      Else (normal format):
        picked = []
        for c in sorted_candidates:
            if len(picked) >= ff.item_count:
                break
            if format_id not in c.eligible_format_ids:
                continue
            if c.candidate_id in reserved_ids:
                continue
            if platform_used[c.platform] >= envelope.platform_budgets[c.platform]:
                continue
            picked.append(c)
            platform_used[c.platform] += 1  # update inline — enforces cap within this format

      reserved[format_id] = [c.candidate_id for c in picked]
      reserved_ids.update(reserved[format_id])
      for c in picked:
          category_used[c.category]  = category_used.get(c.category, 0) + 1

      if len(picked) < ff.item_count:
          # Format partially filled despite Stage 2 feasibility — caused by
          # platform cap competition with higher-priority formats processed
          # earlier in this pass. Emit a conflict trace; format will be
          # marked partial in Stage 4.
          emit_pass1_partial_warning(format_id, len(picked), ff.item_count)

  PASS 2 — Global fill (remaining budget slots)

  Step 5 — Fill remaining slots from unreserved candidates
    remaining = envelope.total_item_count - len(reserved_ids)
    global_fill_ids: list[str] = []

  # Deprioritize candidates whose category OR platform is over its soft target.
  # Uses post-Pass-1 category_used and platform_used state.
  # Group A: neither category nor platform over soft target — processed first
  # Group B: category or platform over soft target          — processed second
  # Hard platform cap is enforced for all candidates in both groups.
  # Items are never blocked — deprioritization only affects ordering.

  def _over_target(c, category_used, platform_used, envelope):
      cat_target  = envelope.category_budgets.get(c.category, 0)
      cat_used_n  = category_used.get(c.category, 0)
      cat_over    = cat_target > 0 and cat_used_n >= cat_target
      plat_soft   = envelope.platform_soft_budgets.get(c.platform)
      plat_used_n = platform_used.get(c.platform, 0)
      plat_over   = plat_soft is not None and plat_used_n >= plat_soft
      return cat_over or plat_over

  unreserved = [c for c in sorted_candidates if c.candidate_id not in reserved_ids]
  group_a = [c for c in unreserved if not _over_target(c, category_used, platform_used, envelope)]
  group_b = [c for c in unreserved if     _over_target(c, category_used, platform_used, envelope)]

  for candidate in group_a + group_b:
      if remaining <= 0:
          break
      # Hard reject: platform cap (hard budget — never bypassed)
      if platform_used.get(candidate.platform, 0) >= envelope.platform_budgets.get(candidate.platform, envelope.total_item_count):
          emit TraceRecord(status="rejected", reasons=["platform_cap:..."], ...)
          continue
      # Log dominance if threshold exceeded (never blocks)
      cat_target = envelope.category_budgets.get(candidate.category, 0)
      cat_used_n = category_used.get(candidate.category, 0)
      if cat_target > 0 and cat_used_n >= cat_target * config.category_dominance_multiplier:
          log_dominance_warning(candidate.category, cat_used_n, cat_target)
      platform_used[candidate.platform] = platform_used.get(candidate.platform, 0) + 1
      category_used[candidate.category] = category_used.get(candidate.category, 0) + 1
      global_fill_ids.append(candidate.candidate_id)
      remaining -= 1
      emit TraceRecord(status="selected", ...)

  Step 6 — Build SelectedItem list
    all_selected_ids = reserved_ids ∪ set(global_fill_ids)
    selected = []
    for candidate in candidates:
        if candidate.candidate_id not in all_selected_ids: continue
        reserved_fmt = next(
            (fid for fid, ids in reserved.items()
             if candidate.candidate_id in ids), None)
        selected.append(SelectedItem(
            ...,
            reserved_for_format = reserved_fmt,   # None for global fill items
            eligible_format_ids = candidate.eligible_format_ids,
        ))

  Step 7 — Emit trace for all candidates not yet traced
    For any candidate in sorted_candidates whose candidate_id is NOT in traced_ids:
      - If candidate_id is in reserved_ids → status="selected" (already counted in Pass 1)
      - Otherwise → status="rejected", reasons=["not_reached_or_over_budget"]
    Pass 1 reserved items must be traced here as "selected", not "rejected".

  Step 8 — Return
    return selected, traces

--------------------------------------------------------------------------------
STAGE 4 — FORMAT ASSIGNMENT + TRACE LOGGING  (stage4_assign.py)
--------------------------------------------------------------------------------

Note: original Stage 4 was a single function; split into compute_assignments /
persist_batch to enable batch validation before any DB write (see ENFORCEMENT
MECHANISMS section below).

Responsibility:
  Assign selected items to formats. Validate the result. Write to DB only if
  valid. Emit the full trace log. Mark partial output where needed.

Input:
  - selected:       list[SelectedItem]      (Stage 3 output)
  - candidates:     list[NormalizedCandidate] (Stage 1 output — for format_assignments lookup)
  - traces:         list[TraceRecord]       (Stage 3 output)
  - envelope:       AllocationEnvelope      (Stage 2 output)
  - config:         BatchConfig
  - db_path:        str
  - story_set_id:   int

Output:
  - BatchResult
  - DB writes (story_sets UPDATE, used_items INSERT) — only if validation passes
  - JSONL trace log at logs/trace_{batch_ts}.jsonl

Algorithm:

  Step 1 — Format assignment (uses Stage 3 reservations)
  [compute_assignments — no DB writes]

    assignments: dict[int, list[str]] = {}  # format_id → [candidate_ids]
    assigned_ids: set[str] = set()

    Process formats in deterministic order (format_id ascending).
    For each feasible format_id:
      ff = envelope.per_format_feasibility[format_id]

      # Tier 1: items Stage 3 reserved specifically for this format
      reserved = [s for s in selected if s.reserved_for_format == format_id]

      # Tier 2: global fill items eligible for this format (not yet assigned)
      fill_eligible = [
          s for s in selected
          if s.reserved_for_format is None
          and format_id in s.eligible_format_ids
          and s.candidate_id not in assigned_ids
      ]
      fill_eligible.sort(key=lambda s: (-s.score, -s.hotness,
                                         -s.freshness.timestamp(),
                                         s.candidate_id))

      # Combine: reserved first, then fill up to item_count from global pool
      picked = reserved + fill_eligible[:max(0, ff.item_count - len(reserved))]
      assignments[format_id] = [s.candidate_id for s in picked]
      assigned_ids.update(assignments[format_id])

  # Convert candidate IDs to NormalizedCandidate objects for BatchResult
  # Use the full NormalizedCandidate objects (from Stage 1) — generators need
  # fields like crawler_item_id, raw_payload, region_key that SelectedItem lacks.
  # Generators with context_item_count > 0 additionally call get_background_items()
  # (crawler_reader.py) to fetch background context items at generation time.
  candidate_by_id = {c.candidate_id: c for c in candidates}
  format_assignments: dict[int, list[NormalizedCandidate]] = {
      fid: [candidate_by_id[cid] for cid in cids]
      for fid, cids in assignments.items()
  }
      # Update TraceRecord.final_assignment for picked items

    Each item is assigned to at most one format. Reserved items go to their
    reserved format. Global fill items are assigned in format_id ascending
    order. No format can be starved by assignment order because its required
    items were already reserved in Stage 3 Pass 1.

  Step 2 — Identify partial formats
  [compute_assignments — no DB writes]

    partial_formats = []
    For each feasible format where len(assignments[format_id]) < ff.item_count:
      Record PartialFormat with shortage details.
    For each infeasible format from envelope:
      Record PartialFormat with blocking_constraint from FormatFeasibility.

  Step 3 — Validate batch result
  [validate_batch_result — no DB writes]

    Run validate_batch_result(batch_result, config).
    If is_valid == False:
      - log all errors
      - emit trace log (Step 4) for observability
      - return FAILURE — persist_batch (Step 5) is never called

  Step 4 — Emit trace log
    Write logs/trace_{batch_ts}.jsonl (one JSON object per line).
    Each line = one TraceRecord (all candidates, not just selected).
    Flush and close before returning BatchResult.

  Step 5 — DB writes (single transaction)
  [persist_batch — only reached if Step 3 passes]

    a. UPDATE story_sets SET
         status = "partial" if batch_result.partial else "complete",
         partial_formats = json.dumps([pf.__dict__ for pf in partial_formats])
       WHERE id = story_set_id

    b. For each candidate_id in assigned_ids:
       INSERT INTO used_items (crawler_item_id, crawler_url, hotness_at_use,
                               story_set_id, story_id, format,
                               used_at, platform, role)
       -- role = 'main' for primary sources; 'context' for background-only items

    If any write fails: rollback entire transaction, do not write partial DB state.

  Step 6 — Return BatchResult

--------------------------------------------------------------------------------
ORCHESTRATION  (selector/__init__.py)
--------------------------------------------------------------------------------

Public API:

  def run_batch(
      format_ids: list[int],
      db_path: str,
      config_path: str,
      hours: int = 48,
      snapshot_path: str | None = None   # if set, replay from snapshot (debug only)
  ) -> BatchResult:
      # format_ids must be int by the time run_batch() is called.
      # run.py (CLI entry point) is responsible for converting CLI string args
      # (e.g. ["11", "26"]) to int (e.g. [11, 26]) before calling run_batch().
      # All internal stage functions operate on int format IDs throughout.

      config     = load_config(config_path)
      story_set_id, batch_ts = create_story_set(lang, channel)
      # batch_ts is int (UNIX ms) returned by create_story_set()

      if snapshot_path:
          candidates = load_snapshot(snapshot_path)   # debug replay
      else:
          candidates = stage1_normalize(db_path, config, format_ids,
                                        hours, batch_ts)

      envelope   = stage2_allocate(candidates, config, format_ids, batch_ts)
      selected, traces = stage3_select(candidates, envelope, config, batch_ts)
      result     = stage4_assign(selected, candidates, traces, envelope, config,
                                 db_path, batch_ts, story_set_id)
      return result

run.py CLI integration:
  The existing run.py CLI interface is unchanged. run_batch() replaces the
  current call chain to selector.py functions. The --format argument maps
  to format_ids. The --hours argument maps to hours.

--------------------------------------------------------------------------------
CONFIG CHANGES  (story_mix.json)
--------------------------------------------------------------------------------

Add the following keys. Existing keys are unchanged.

  1. format_eligibility  (new top-level key)
     As defined in FORMAT FIDELITY SCHEMA in the goals section above.

  2. source_groups  (already present but not loaded — wire in)
     load_story_mix_config() must read this key and expose it as
     config.source_groups: dict[str, list[str]]
     All stage algorithms access comment_platforms via
     config.source_groups["comment_platforms"].

  3. category_dominance_multiplier  (new, default 1.5)
     Controls the dominance logging threshold in Stage 3.
     "category_dominance_multiplier": 1.5

  4. platform_caps remains as-is  (hard_max values, no schema change)
     Target and tolerance are derived in Stage 2 (target = 0.7 × hard_max),
     not stored in config.

  5. load_story_mix_config() must be updated to:
     - Load and validate format_eligibility
     - Load source_groups (exposes config.source_groups["comment_platforms"])
     - Load category_dominance_multiplier (with default 1.5)
     - Allowlist top-level keys: "version" and "contract_notes" are metadata-only
       keys — load but do not use in selection logic. All other unrecognized
       top-level keys are rejected (prevents decorative config).

--------------------------------------------------------------------------------
DB SCHEMA CHANGES
--------------------------------------------------------------------------------

  1. story_sets table — add columns:
       batch_ts        TEXT NOT NULL DEFAULT ''
       partial         INTEGER NOT NULL DEFAULT 0   -- boolean
       partial_formats TEXT NOT NULL DEFAULT '[]'   -- JSON array of PartialFormat

  2. used_items table — add columns:
       canonical_story_id TEXT DEFAULT NULL
       -- Not populated in v1. Reserved for v2 story-level dedup.
       role  TEXT NOT NULL DEFAULT 'main'
       -- 'main' = primary source (excluded from reuse);
       -- 'context' = background only (never excluded)
       -- get_used_urls_with_hotness() filters WHERE role = 'main' OR role IS NULL

  3. snapshots/ directory (new)
       Location: same directory as db.sqlite3
       File format: snapshots/{batch_ts}_stage1.json
       Retention: deleted if older than 48 hours at batch start

  4. logs/ directory (existing or new)
       Trace log: logs/trace_{batch_ts}.jsonl
       Retention: no automatic cleanup in v1 (operator-managed)

  Migration script required for story_sets and used_items column additions.
  Migration must be idempotent (ALTER TABLE IF NOT EXISTS column pattern).

--------------------------------------------------------------------------------
FORMAT_REGISTRY REFACTOR
--------------------------------------------------------------------------------

format_registry.py currently hardcodes FORMAT_REQUIRES_NEWS and strategies.
After refactor:

  - FORMAT_REQUIRES_NEWS set is removed from format_registry.py
  - Format eligibility rules are read from config.format_eligibility
  - format_registry.py retains only:
      - FORMAT_REGISTRY: dict[int, tuple]  (format_id → 4-tuple:
          (strategy, prompt_file, item_count, context_item_count))
      - FORMAT_STRATEGIES: dict[int, str]  (format_id → strategy name)
      - FORMAT_ITEM_COUNTS: dict[int, int] (format_id → item_count)
      - FORMAT_CONTEXT_COUNTS: dict[int, int] (format_id → context_item_count,
          derived from index [3]; 0 = no background fetch)
      - item_count(format_id) helper
      - strategy(format_id) helper
  - All eligibility logic moves to stage1_normalize.py (eligibility tagging)
    and config.py (loading format_eligibility rules)

--------------------------------------------------------------------------------
IMPLEMENTATION ORDER
--------------------------------------------------------------------------------

  Phase 0 — Pre-work (blocks everything)
    1. Config audit (Action 1 from goals): wire comment_platforms,
       migrate FORMAT_REQUIRES_NEWS to format_eligibility in story_mix.json
    2. DB migration: add columns to story_sets and used_items
    3. Write schemas.py (all dataclasses)
    4. Write config.py (updated load_story_mix_config with new keys + validation)
    5. Refactor format_registry.py (remove FORMAT_REQUIRES_NEWS, keep strategies)

  Phase 1 — Stage 1
    Implement stage1_normalize.py and snapshot.py.
    Verify with: run stage1 against live DB, inspect snapshot JSON and metrics log.

  Phase 2 — Stage 2
    Implement stage2_allocate.py.
    Verify with: inject candidate list with known supply gaps, confirm
    FormatFeasibility records and ConflictRecords are correct.

  Phase 3 — Stage 3
    Implement stage3_select.py and trace.py.
    Verify with: same input snapshot twice → identical output (determinism test).
    Verify: platform cap is never exceeded in output.

  Phase 4 — Stage 4
    Implement stage4_assign.py.
    Verify with: confirm DB writes are atomic (inject failure mid-write, confirm
    rollback). Confirm trace JSONL covers all candidates.

  Phase 5 — Integration
    Wire selector/__init__.py run_batch().
    Update run.py to call run_batch().
    End-to-end test: run full batch, inspect story_sets, stories, used_items,
    trace log, and snapshot.

  Phase 6 — Regression
    Run regression tests against existing behavior.
    Confirm platform cap compliance rate = 100% (was drifting before refactor).

================================================================================
ENFORCEMENT MECHANISMS
================================================================================

PURPOSE
-------
Minimal mechanisms required to ensure hard constraints, determinism, and format
fidelity are actually enforced at runtime. Three mechanisms only.

--------------------------------------------------------------------

1. BATCH VALIDATOR (blocking — runs before persist)
----------------------------------------------------

Checks (minimum required):
  - Platform caps not exceeded
  - No item assigned to more than one format
  - All assigned items satisfy format_eligibility
  - No reused items (based on reuse_policy)
  - Total assigned items ≤ allocation envelope total
  - Partial flag correctness (if any format underfilled → partial=true)

Interface:

  def validate_batch_result(batch: BatchResult, config: BatchConfig) -> ValidationResult:
      return {"is_valid": bool, "errors": list[str]}

Integration:
  Called in Stage 4 Step 3 — after compute_assignments, before persist_batch.
  If is_valid == False: log errors, do not call persist_batch, return failure.

--------------------------------------------------------------------

2. DETERMINISM TEST
--------------------

Run Stages 2–4 twice against the same Stage 1 snapshot.
Compare format_assignments for exact equality.
(selected_candidate_ids is derivable from format_assignments — no separate field needed.)

  def test_determinism(snapshot_path: str, config: BatchConfig) -> bool:
      db = open_isolated_test_db()   # in-memory or temp file; empty used_items
      run1 = run_from_snapshot(snapshot_path, config, db=db, reset_db=True)
      run2 = run_from_snapshot(snapshot_path, config, db=db, reset_db=True)
      return run1.format_assignments == run2.format_assignments

Notes:
  - story_set_id and batch_ts are excluded from comparison (differ by design).
  - reset_db=True clears used_items before each run so both runs see identical input.
  - Run manually before release and after any change to selector logic.

--------------------------------------------------------------------

3. GOLDEN SNAPSHOT TESTS
--------------------------

3–5 fixed input snapshots covering known edge cases. Tests assert properties,
not exact outputs.

Required scenarios:

  A. Skewed platform supply (e.g. 70% bilibili)
     → platform cap enforced, overflow rejected

  B. Missing required format supply (e.g. no comment_platform items)
     → format marked partial, no fallback violation

  C. Category imbalance (e.g. all tech)
     → selection valid, dominance logged, no hard failure

  D. Duplicate reuse (items already in used_items)
     → excluded

  E. Small batch / low supply
     → partial batch, no constraint relaxation

Test structure:
  {
    "snapshot": "path/to/snapshot.json",
    "expected_properties": [
      "platform_caps_respected",
      "no_duplicates",
      "partial_if_needed",
      "deterministic"
    ]
  }

Run manually after any change to selector logic.

--------------------------------------------------------------------

================================================================================
# END OF DESIGN
================================================================================
