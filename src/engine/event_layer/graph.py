"""
event_layer/graph.py — Event graph builder.

Builds a graph linking related but distinct events, enabling multi-event
narrative chains (e.g. "tariff announced" → "market reacts" → "congress responds").

Graph structure
---------------
Nodes:  event_id strings
Edges:  (event_id_a, event_id_b, weight) where weight = cosine similarity
        between embedding_centers, in the range [GRAPH_LOWER, GRAPH_UPPER).

Similarity bands
----------------
>= COSINE_CLUSTER_THRESHOLD (0.75)  → same event (handled by clustering.py)
[GRAPH_LOWER, GRAPH_UPPER)           → related but distinct events → graph edge
< GRAPH_LOWER                        → unrelated → no edge

GRAPH_UPPER = 0.75   (below clustering threshold — different events)
GRAPH_LOWER = 0.45   (above noise floor — meaningfully related)

Entity-based edges (supplementary)
-----------------------------------
When events share >= ENTITY_OVERLAP_MIN entities (by text, case-insensitive),
an entity edge is added with weight = shared_entity_count / total_unique_entities
(Jaccard on entity text set). This catches related events whose embeddings
diverge (e.g. same person, different topic).

Public API
----------
build_event_graph(clusters) -> EventGraph
    Takes {candidate_id: EventCluster}, returns EventGraph with nodes,
    edges, and per-node neighbor lookup.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from engine.event_layer.clustering import EventCluster

logger = logging.getLogger(__name__)

# Cosine similarity band for graph edges (embedding-based)
GRAPH_UPPER = 0.75   # >= this → same event (clustering.py territory)
GRAPH_LOWER = 0.45   # < this  → unrelated noise

# Entity overlap threshold for entity-based edges
ENTITY_OVERLAP_MIN = 2   # need at least 2 shared entities to add an entity edge


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class EventEdge:
    """A directed edge between two events in the graph."""
    source_event_id: str
    target_event_id: str
    weight:          float        # cosine similarity or Jaccard entity overlap
    edge_type:       str          # 'embedding' | 'entity'
    shared_entities: list[str] = field(default_factory=list)  # for entity edges


@dataclass
class EventGraph:
    """
    Graph of related events built from a single batch's cluster_map.

    nodes:     set of event_id strings
    edges:     list of EventEdge objects
    neighbors: {event_id: [(neighbor_event_id, weight, edge_type)]}
               — pre-built adjacency list for fast lookup by generators
    """
    nodes:     set[str]                              = field(default_factory=set)
    edges:     list[EventEdge]                       = field(default_factory=list)
    neighbors: dict[str, list[tuple[str, float, str]]] = field(default_factory=dict)


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


def _entity_set(cluster: EventCluster) -> set[str]:
    """
    Return a set of lowercased entity text strings for a cluster.
    Uses representative's title tokens as a fallback when no structured
    entities are available (entities are extracted post-generation and
    won't be on the current batch's clusters).
    """
    # Structured entities (available for clusters built from event_memory)
    if hasattr(cluster, 'entities') and cluster.entities:
        return {e['text'].lower() for e in cluster.entities if e.get('text')}
    # Fallback: tokenize representative title (no stopwords needed here —
    # just want named-entity-like tokens: capitalised words)
    title = cluster.representative.title_original or cluster.representative.canonical_title or ''
    return {w.lower() for w in title.split() if w and w[0].isupper() and len(w) > 2}


def _add_edge(graph: EventGraph, src: str, tgt: str, weight: float,
              edge_type: str, shared: list[str] | None = None) -> None:
    """Add a bidirectional edge to the graph."""
    edge = EventEdge(
        source_event_id = src,
        target_event_id = tgt,
        weight          = weight,
        edge_type       = edge_type,
        shared_entities = shared or [],
    )
    graph.edges.append(edge)
    graph.neighbors.setdefault(src, []).append((tgt, weight, edge_type))
    graph.neighbors.setdefault(tgt, []).append((src, weight, edge_type))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_event_graph(clusters: dict[str, EventCluster]) -> EventGraph:
    """
    Build an event relationship graph from the current batch's cluster_map.

    Connects related but distinct events via:
      1. Embedding similarity (cosine in [GRAPH_LOWER, GRAPH_UPPER))
      2. Entity overlap (>= ENTITY_OVERLAP_MIN shared named entities)

    Args:
        clusters: {candidate_id: EventCluster} — output of build_clusters().

    Returns:
        EventGraph with nodes, edges, and neighbors adjacency list.
    """
    if not clusters:
        return EventGraph()

    cluster_list = list(clusters.values())
    graph = EventGraph(nodes={c.event_id for c in cluster_list})

    # Deduplicate by event_id (multiple candidate_ids can map to same event_id
    # in edge cases — take the first seen)
    seen_event_ids: dict[str, EventCluster] = {}
    for cluster in cluster_list:
        if cluster.event_id not in seen_event_ids:
            seen_event_ids[cluster.event_id] = cluster
    unique_clusters = list(seen_event_ids.values())

    emb_edges = 0
    ent_edges = 0

    for i, ca in enumerate(unique_clusters):
        for cb in unique_clusters[i + 1:]:
            # --- Embedding-based edge ---
            if ca.embedding_center and cb.embedding_center:
                sim = _cosine(ca.embedding_center, cb.embedding_center)
                if GRAPH_LOWER <= sim < GRAPH_UPPER:
                    _add_edge(graph, ca.event_id, cb.event_id, round(sim, 4), 'embedding')
                    emb_edges += 1

            # --- Entity-based edge ---
            ents_a = _entity_set(ca)
            ents_b = _entity_set(cb)
            shared = ents_a & ents_b
            if len(shared) >= ENTITY_OVERLAP_MIN:
                union = ents_a | ents_b
                jaccard = len(shared) / len(union) if union else 0.0
                # Only add if not already connected by embedding edge
                already_connected = any(
                    nb == cb.event_id for nb, _, _ in graph.neighbors.get(ca.event_id, [])
                )
                if not already_connected:
                    _add_edge(graph, ca.event_id, cb.event_id, round(jaccard, 4),
                              'entity', sorted(shared))
                    ent_edges += 1

    logger.info(
        "Event graph: %d nodes, %d embedding edges, %d entity edges",
        len(unique_clusters), emb_edges, ent_edges,
    )
    return graph
