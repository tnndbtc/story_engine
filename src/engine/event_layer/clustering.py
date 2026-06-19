"""
event_layer/clustering.py — Event clustering via embedding cosine similarity.

For each selected candidate, finds cluster mates among the full candidate pool
using cosine similarity on crawler embeddings (BAAI/bge-small-en-v1.5, 384-dim).

Two-tier clustering decision (Option A upgrade):
  Layer 1 — candidate filter:  cosine >= COSINE_CLUSTER_THRESHOLD (0.75)
  Layer 2 — validation gate:   auto-merge if cosine >= COSINE_AUTO_MERGE (0.85),
                                otherwise require >= 1 shared keyword in titles.

This prevents topically-related-but-distinct events from merging incorrectly
while still catching same-event rephrases across locales and platforms.

Phase 3 (future): LLM classifier for borderline pairs where keyword check is
                  unreliable (e.g. very short titles, pure CJK vs pure EN).

Source role classification (platform-based):
  fact:     Reuters / AP / BBC / Guardian / NYT / WaPo / Al Jazeera / FT / Bloomberg
  reaction: Reddit / YouTube / Bilibili / Twitter / X / Weibo
  context:  everything else (mainstream news, regional outlets)

Key constants
-------------
COSINE_AUTO_MERGE = 0.85
    Auto-accept threshold: same-event rephrases on BGE-small-en-v1.5 score here.

COSINE_CLUSTER_THRESHOLD = 0.75
    Candidate filter: items in [0.75, 0.85) are accepted only with keyword overlap.
    Items below 0.75 fall into the event_graph band (related but distinct events).

MAX_CLUSTER_MEMBERS = 5
    Cap on corroborating sources per cluster to bound prompt size.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field

import json
import os
from pathlib import Path

from db.crawler_reader import get_embeddings
from db.models import DB_PATH, log_purity_decision
from engine.selector.schemas import NormalizedCandidate

logger = logging.getLogger(__name__)

# Two-tier clustering thresholds:
#   >= COSINE_AUTO_MERGE   → same event, merge immediately (high confidence)
#   >= COSINE_CLUSTER_THRESHOLD and keyword overlap >= 1 → merge after validation
#   <  COSINE_CLUSTER_THRESHOLD → reject (or fall into event_graph band)
COSINE_AUTO_MERGE        = 0.85   # auto-accept: no further validation needed
COSINE_CLUSTER_THRESHOLD = 0.75   # candidate filter: requires keyword validation
MAX_CLUSTER_MEMBERS = 5
MAX_CLUSTER_AGE_DIFF_HOURS = 48   # context mates older than this are excluded

FACT_PLATFORMS: frozenset[str] = frozenset({
    'reuters', 'ap', 'apnews', 'bbc', 'guardian', 'nytimes', 'wapo',
    'wsj', 'aljazeera', 'ft', 'bloomberg', 'nikkei',
})
REACTION_PLATFORMS: frozenset[str] = frozenset({
    'reddit', 'twitter', 'x', 'youtube', 'bilibili', 'weibo',
    'instagram', 'tiktok', 'mastodon',
})

# ---------------------------------------------------------------------------
# Purity gate config — loaded dynamically from clustering_config.json
# ---------------------------------------------------------------------------

_CLUSTERING_CONFIG_PATH: Path = Path(__file__).resolve().parents[3] / 'config' / 'clustering_config.json'


def _load_clustering_config() -> dict:
    """
    Load clustering_config.json for dynamic purity gate settings.
    Returns safe defaults if file is missing or unreadable.
    Defaults to gate DISABLED so missing config never blocks merges.
    """
    try:
        with open(_CLUSTERING_CONFIG_PATH) as _f:
            return json.load(_f)
    except Exception:
        return {
            'purity_gate_enabled':   False,
            'purity_gate_threshold': 0.55,
        }


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class EventCluster:
    """A group of articles covering the same real-world event."""

    event_id:         str                            # sha256[:16] of representative URL
    representative:   NormalizedCandidate            # highest-hotness article
    fact_sources:     list[NormalizedCandidate] = field(default_factory=list)
    context_sources:  list[NormalizedCandidate] = field(default_factory=list)
    reaction_sources: list[NormalizedCandidate] = field(default_factory=list)
    embedding_center: list[float] | None = None      # mean of member embeddings
    event_hotness:    float = 0.0                    # set by hotness.compute_event_hotness()
    member_count:     int = 1
    novelty_score:    float = 1.0                    # 0.0–1.0; set by selector after memory check
                                                     # 1.0 = new event (or soft-penalised duplicate
                                                     #        whose penalty is in effective_hotness)
                                                     # 0.7 = new development (follow-up)
    timeline:         list[dict] = field(default_factory=list)
                                                     # [{timestamp, title, platform, role}]
                                                     # sorted ascending by freshness
    source_diversity: float = 0.0                    # distinct domains / member_count; set by build_clusters()
    cluster_countries: set = field(default_factory=set)  # union of candidate_entities['countries']
    cluster_orgs:      set = field(default_factory=set)  # union of candidate_entities['orgs']


# Stopwords for keyword overlap (mirrors memory.py — kept local to avoid coupling)
_STOPWORDS: frozenset[str] = frozenset({
    'a', 'an', 'the', 'this', 'that', 'these', 'those',
    'from', 'to', 'in', 'on', 'at', 'by', 'for', 'with', 'as', 'of',
    'about', 'into', 'through', 'over', 'under', 'after', 'before',
    'between', 'up', 'down', 'out', 'off', 'and', 'or', 'but', 'nor',
    'so', 'yet', 'both', 'either', 'neither', 'than', 'not', 'no',
    'it', 'its', 'he', 'she', 'they', 'we', 'you', 'i',
    'his', 'her', 'their', 'our', 'your', 'my',
    'me', 'him', 'them', 'us', 'who', 'which', 'what', 'where',
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did',
    'will', 'would', 'shall', 'should', 'may', 'might', 'can', 'could',
    'also', 'just', 'still', 'now', 'here', 'there', 'when', 'how', 'why',
    'more', 'most', 'much', 'many', 'some', 'any', 'all', 'each', 'every',
    'says', 'said', 'say',
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in pure Python (no numpy dependency)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens (ASCII + CJK), stopwords removed."""
    tokens = set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text.lower()))
    return tokens - _STOPWORDS


def _keyword_overlap(a: NormalizedCandidate, b: NormalizedCandidate) -> bool:
    """
    Return True when the two candidates share at least one meaningful keyword.

    Used as the validation gate for borderline cosine pairs (0.75 <= sim < 0.85).

    Two-pass comparison to handle cross-locale pairs (e.g. ZH vs EN):
      Pass 1 — title_original: works when both posts are in the same language.
               A ZH post vs EN post will have zero token overlap here.
      Pass 2 — canonical_title: English-normalised title present for all items.
               Cross-locale pairs that share an event will overlap here even
               when their title_original tokens are disjoint.
    Returns True as soon as either pass finds overlap.
    """
    # Pass 1: title_original (same-locale pairs)
    orig_a = a.title_original or ''
    orig_b = b.title_original or ''
    if orig_a and orig_b and (_tokenize(orig_a) & _tokenize(orig_b)):
        return True

    # Pass 2: canonical_title (cross-locale pairs — both normalised to English)
    canon_a = a.canonical_title or ''
    canon_b = b.canonical_title or ''
    if canon_a and canon_b and (_tokenize(canon_a) & _tokenize(canon_b)):
        return True

    return False


def _entity_gate(
    a: 'NormalizedCandidate',
    b: 'NormalizedCandidate',
) -> tuple[bool, str]:
    """
    Validation gate for borderline cosine pairs (0.75 <= sim < 0.85).
    Returns (allow_merge, reason_string) for logging.

    Replaces the bare _keyword_overlap() check. Wraps keyword overlap
    with entity-level rules that catch cross-country and cross-event-type
    false merges that keyword overlap misses.

    Rules applied in order (first match wins):
      1. Country conflict  → BLOCK  (both sides have countries, none overlap)
      2. Event type clash  → BLOCK  (POLICY_ACTION vs INCIDENT only)
      3. Keyword overlap   → ALLOW  (existing check preserved)
      4. Country match     → ALLOW  (same canonical country on both sides)
      5. Org overlap       → ALLOW  (shared ALL-CAPS acronym)
      6. No signal         → BLOCK  (no positive evidence found)

    Entities come from candidate.candidate_entities populated at Stage 1
    by title_ner.extract_title_entities(). None → treat as neutral (empty dict).
    Countries are canonical lowercase strings (demonym-normalized by title_ner).
    """
    ents_a = a.candidate_entities or {}
    ents_b = b.candidate_entities or {}

    countries_a = set(ents_a.get('countries', []))
    countries_b = set(ents_b.get('countries', []))
    etype_a     = ents_a.get('event_type', 'UNKNOWN')
    etype_b     = ents_b.get('event_type', 'UNKNOWN')

    # Rule 1: Country conflict
    # Both sides identified countries and they don't intersect → different events.
    # Only fires when BOTH sides have country data; one empty side → skip rule.
    if countries_a and countries_b and not (countries_a & countries_b):
        return False, f"country_conflict({sorted(countries_a)} vs {sorted(countries_b)})"

    # Rule 2: Event type mismatch
    # POLICY_ACTION vs INCIDENT is a hard incompatibility.
    # UNKNOWN and ANALYSIS are not used as blockers — too imprecise.
    _HARD_TYPES: frozenset[str] = frozenset({'POLICY_ACTION', 'INCIDENT'})
    if etype_a in _HARD_TYPES and etype_b in _HARD_TYPES and etype_a != etype_b:
        return False, f"event_type_mismatch({etype_a} vs {etype_b})"

    # Rule 3: Keyword overlap (existing check — preserved as positive signal)
    if _keyword_overlap(a, b):
        return True, "keyword_overlap"

    # Rule 4: Country match (same canonical country on both sides)
    shared_countries = countries_a & countries_b
    if shared_countries:
        return True, f"country_match({sorted(shared_countries)})"

    # Rule 5: Org overlap (shared ALL-CAPS acronym e.g. NATO, IMF, WHO)
    orgs_a = set(ents_a.get('orgs', []))
    orgs_b = set(ents_b.get('orgs', []))
    shared_orgs = orgs_a & orgs_b
    if orgs_a and orgs_b and shared_orgs:
        return True, f"org_overlap({sorted(shared_orgs)})"

    # Rule 6: No positive signal found → block
    return False, "no_entity_signal"


def _entity_overlap_score(ents_a: dict, ents_b: dict) -> float:
    """
    Jaccard overlap score between the combined entity sets of two candidates.
    Used by _purity_score() for diagnostic logging only (not a hard gate).

    Returns 0.5 when either side has no entities — neutral, not penalising.
    Returns Jaccard(all_a, all_b) otherwise (countries ∪ orgs on each side).
    """
    all_a = set(ents_a.get('countries', [])) | set(ents_a.get('orgs', []))
    all_b = set(ents_b.get('countries', [])) | set(ents_b.get('orgs', []))
    if not all_a or not all_b:
        return 0.5  # neutral: NER found nothing on one or both sides
    union = all_a | all_b
    return len(all_a & all_b) / len(union) if union else 0.5


def _purity_score(cosine: float, ents_a: dict, ents_b: dict) -> float:
    """
    Event purity score for a candidate merge pair. DIAGNOSTIC ONLY — not a gate.

    Formula:
        0.5 × cosine + 0.3 × entity_overlap + 0.2 × event_type_match

    Weights are provisional — do not use as a hard gate until 2+ weeks of
    logged data justifies a threshold. Logged at DEBUG level for every merge
    in the 0.75-0.85 tier so data can be collected.

    Typical ranges on this embedding model:
        same-event rephrases:     0.80+
        related-but-distinct:     0.55–0.79
        unrelated:                below 0.55
    """
    entity_overlap = _entity_overlap_score(ents_a, ents_b)
    etype_match    = 1.0 if ents_a.get('event_type') == ents_b.get('event_type') else 0.0
    return round(0.5 * cosine + 0.3 * entity_overlap + 0.2 * etype_match, 4)


def _extract_domain(url: str) -> str:
    """Normalise a URL to its bare hostname, stripping 'www.' prefix.

    Used by source_diversity to count distinct publishing outlets rather than
    distinct platforms.  Five Times-of-India articles are one domain; CNBC +
    Reuters + TOI are three.
    """
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return url


def _source_role(platform: str) -> str:
    p = platform.lower()
    if p in FACT_PLATFORMS:
        return 'fact'
    if p in REACTION_PLATFORMS:
        return 'reaction'
    return 'context'


def _embedding_center(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    dim = len(vectors[0])
    center = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            center[i] += x
    n = len(vectors)
    return [x / n for x in center]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_clusters(
    selected: list[NormalizedCandidate],
    pool: list[NormalizedCandidate],
) -> dict[str, EventCluster]:
    """
    Build EventCluster objects for each selected candidate.

    For each selected candidate that has an embedding, scans the full pool
    for cluster mates using the two-tier decision:
      sim >= COSINE_AUTO_MERGE (0.85)        → auto-merge
      sim >= COSINE_CLUSTER_THRESHOLD (0.75) → merge only if keyword overlap >= 1
    Mates are classified by source role (fact / context / reaction).

    Candidates without embeddings become singleton clusters with the
    representative classified into the appropriate role bucket.

    Args:
        selected: Candidates chosen by Stage 3/4 (one per format slot).
        pool:     Full Stage 1 candidate list including non-selected items.

    Returns:
        {candidate_id: EventCluster} — one entry per selected candidate.
    """
    if not selected:
        return {}

    # Collect all item IDs needing embeddings in one DB round-trip
    selected_item_ids = [c.crawler_item_id for c in selected]
    pool_item_ids     = [c.crawler_item_id for c in pool]
    all_item_ids      = list(set(selected_item_ids + pool_item_ids))

    embeddings = get_embeddings(all_item_ids)
    logger.info(
        "Clustering: %d embeddings fetched for %d unique items (%d selected, %d pool)",
        len(embeddings), len(all_item_ids), len(selected), len(pool),
    )

    # Build pool index: (candidate, embedding) pairs — only embedded pool items
    pool_index: list[tuple[NormalizedCandidate, list[float]]] = [
        (c, embeddings[c.crawler_item_id])
        for c in pool
        if c.crawler_item_id in embeddings
    ]

    clusters: dict[str, EventCluster] = {}

    # Load purity gate config dynamically — picks up changes without restart
    _cfg             = _load_clustering_config()
    _gate_enabled    = _cfg.get('purity_gate_enabled', False)
    _gate_threshold  = float(_cfg.get('purity_gate_threshold', 0.55))

    for sel_cand in selected:
        event_id = hashlib.sha256(sel_cand.url.encode()).hexdigest()[:16]
        sel_emb  = embeddings.get(sel_cand.crawler_item_id)

        if sel_emb is None:
            # No embedding yet — singleton cluster
            cluster = _singleton_cluster(event_id, sel_cand)
            clusters[sel_cand.candidate_id] = cluster
            continue

        # Score every pool candidate against the selected one.
        # Two-tier decision:
        #   sim >= COSINE_AUTO_MERGE (0.85)        → same event, auto-accept
        #   sim >= COSINE_CLUSTER_THRESHOLD (0.75) → candidate; accept only if
        #                                            titles share >= 1 keyword
        mates: list[tuple[float, NormalizedCandidate]] = []
        for pool_cand, pool_emb in pool_index:
            if pool_cand.candidate_id == sel_cand.candidate_id:
                continue
            sim = _cosine(sel_emb, pool_emb)
            if sim >= COSINE_AUTO_MERGE:
                # GAP-PURITY-2: run entity gate on auto-merge tier too.
                # Skip gate only when BOTH sides have no entity data (NER skipped).
                ents_a = sel_cand.candidate_entities or {}
                ents_b = pool_cand.candidate_entities or {}
                if ents_a or ents_b:
                    auto_allow, auto_reason = _entity_gate(sel_cand, pool_cand)
                    if not auto_allow:
                        logger.debug(
                            "Auto-merge blocked by entity gate [%s] sim=%.3f: %r vs %r",
                            auto_reason, sim,
                            (sel_cand.canonical_title or sel_cand.title_original or '')[:40],
                            (pool_cand.canonical_title or pool_cand.title_original or '')[:40],
                        )
                        continue
                # GAP-PURITY-1: time proximity check for context-role mates.
                mate_role = _source_role(pool_cand.platform)
                if mate_role == 'context':
                    age_diff = abs(
                        (sel_cand.freshness - pool_cand.freshness).total_seconds()
                    ) / 3600
                    if age_diff > MAX_CLUSTER_AGE_DIFF_HOURS:
                        logger.debug(
                            "Auto-merge context mate skipped — age_diff=%.1fh > %dh: %r",
                            age_diff, MAX_CLUSTER_AGE_DIFF_HOURS,
                            (pool_cand.canonical_title or pool_cand.title_original or '')[:40],
                        )
                        continue
                mates.append((sim, pool_cand))
            elif sim >= COSINE_CLUSTER_THRESHOLD:
                ents_a = sel_cand.candidate_entities or {}
                ents_b = pool_cand.candidate_entities or {}
                purity = _purity_score(sim, ents_a, ents_b)
                allow, reason = _entity_gate(sel_cand, pool_cand)

                # Purity gate (Sprint 3): additional hard filter when enabled.
                # Auto-calibrated by purity_calibrator.py — disabled until
                # min_samples_to_enable threshold is reached.
                if allow and _gate_enabled and purity < _gate_threshold:
                    allow  = False
                    reason = f"purity_gate({purity:.3f}<{_gate_threshold:.3f})"

                # Log every borderline decision for calibration data collection
                log_purity_decision(sim, purity, allow, reason)

                if allow:
                    # GAP-PURITY-1: time proximity check for context-role mates.
                    if _source_role(pool_cand.platform) == 'context':
                        age_diff = abs(
                            (sel_cand.freshness - pool_cand.freshness).total_seconds()
                        ) / 3600
                        if age_diff > MAX_CLUSTER_AGE_DIFF_HOURS:
                            logger.debug(
                                "Borderline context mate skipped — age_diff=%.1fh > %dh: %r",
                                age_diff, MAX_CLUSTER_AGE_DIFF_HOURS,
                                (pool_cand.canonical_title or pool_cand.title_original or '')[:40],
                            )
                            continue
                    mates.append((sim, pool_cand))
                    logger.debug(
                        "Merge allowed [%s] sim=%.3f purity=%.3f: %r + %r",
                        reason, sim, purity,
                        (sel_cand.canonical_title or sel_cand.title_original or '')[:40],
                        (pool_cand.canonical_title or pool_cand.title_original or '')[:40],
                    )
                else:
                    logger.debug(
                        "Merge blocked [%s] sim=%.3f purity=%.3f: %r vs %r",
                        reason, sim, purity,
                        (sel_cand.canonical_title or sel_cand.title_original or '')[:40],
                        (pool_cand.canonical_title or pool_cand.title_original or '')[:40],
                    )

        # Keep top MAX_CLUSTER_MEMBERS by similarity
        mates.sort(key=lambda x: -x[0])
        mates = mates[:MAX_CLUSTER_MEMBERS]

        # Classify mates by source role
        fact_sources:     list[NormalizedCandidate] = []
        context_sources:  list[NormalizedCandidate] = []
        reaction_sources: list[NormalizedCandidate] = []
        member_embeddings = [sel_emb]

        for _, mate in mates:
            role = _source_role(mate.platform)
            if role == 'fact':
                fact_sources.append(mate)
            elif role == 'reaction':
                reaction_sources.append(mate)
            else:
                context_sources.append(mate)
            mate_emb = embeddings.get(mate.crawler_item_id)
            if mate_emb:
                member_embeddings.append(mate_emb)

        # Classify the representative itself
        sel_role = _source_role(sel_cand.platform)
        if sel_role == 'fact':
            fact_sources.insert(0, sel_cand)
        elif sel_role == 'reaction':
            reaction_sources.insert(0, sel_cand)
        else:
            context_sources.insert(0, sel_cand)

        all_members_sorted = sorted(
            fact_sources + context_sources + reaction_sources,
            key=lambda c: c.freshness,
        )
        timeline = [
            {
                'timestamp': c.freshness.isoformat(),
                'title':     c.title_original or c.canonical_title or '',
                'platform':  c.platform,
                'role':      _source_role(c.platform),
            }
            for c in all_members_sorted
        ]

        all_members = [sel_cand] + [mate for _, mate in mates]

        # Compute source_diversity: distinct publishing domains / member_count.
        # Domain-based diversity is stricter than platform-based diversity:
        # five articles from timesofindia.com all share one domain and score
        # the same as a single-source cluster, forcing low-diversity India-only
        # clusters below the MIN_SOURCE_DIVERSITY floor in story_orchestrate.
        distinct_domains = {_extract_domain(m.url) for m in all_members}
        source_diversity = len(distinct_domains) / max(len(all_members), 1)

        # Aggregate entity sets across all cluster members
        cluster_countries: set[str] = set()
        cluster_orgs:      set[str] = set()
        for member in all_members:
            ents = member.candidate_entities or {}
            cluster_countries.update(ents.get('countries', []))
            cluster_orgs.update(ents.get('orgs', []))

        cluster = EventCluster(
            event_id         = event_id,
            representative   = sel_cand,
            fact_sources     = fact_sources,
            context_sources  = context_sources,
            reaction_sources = reaction_sources,
            embedding_center = _embedding_center(member_embeddings),
            member_count     = 1 + len(mates),
            timeline         = timeline,
            source_diversity = source_diversity,
            cluster_countries = cluster_countries,
            cluster_orgs      = cluster_orgs,
        )
        clusters[sel_cand.candidate_id] = cluster

        if mates:
            auto_count      = sum(1 for sim, _ in mates if sim >= COSINE_AUTO_MERGE)
            validated_count = len(mates) - auto_count
            logger.debug(
                "Cluster %r: %d mates (auto=%d entity_gated=%d | fact=%d ctx=%d react=%d)",
                (sel_cand.title_original or sel_cand.canonical_title or '')[:50],
                len(mates), auto_count, validated_count,
                len(fact_sources), len(context_sources), len(reaction_sources),
            )

    embedded_count = sum(1 for c in selected if c.crawler_item_id in embeddings)
    multi_count    = sum(1 for cl in clusters.values() if cl.member_count > 1)
    logger.info(
        "Clustering complete: %d/%d selected had embeddings, %d multi-source clusters formed",
        embedded_count, len(selected), multi_count,
    )
    return clusters


def _singleton_cluster(event_id: str, candidate: NormalizedCandidate) -> EventCluster:
    """Wrap a single candidate as a lone-member cluster."""
    role = _source_role(candidate.platform)
    timeline = [
        {
            'timestamp': candidate.freshness.isoformat(),
            'title':     candidate.title_original or candidate.canonical_title or '',
            'platform':  candidate.platform,
            'role':      role,
        }
    ]
    ents = candidate.candidate_entities or {}
    return EventCluster(
        event_id          = event_id,
        representative    = candidate,
        fact_sources      = [candidate] if role == 'fact'     else [],
        context_sources   = [candidate] if role == 'context'  else [],
        reaction_sources  = [candidate] if role == 'reaction' else [],
        member_count      = 1,
        timeline          = timeline,
        source_diversity  = 1.0,
        cluster_countries = set(ents.get('countries', [])),
        cluster_orgs      = set(ents.get('orgs', [])),
    )
