#!/usr/bin/env python3
"""
Re-generate deep story body for stories 66 and 67 (which had body="").
Reconstructs minimal EventCluster objects from stored source data,
calls generate_deep_story(), and updates the DB in place.
"""
import sys, json, sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from engine.selector.schemas import NormalizedCandidate
from engine.event_layer.clustering import EventCluster
from engine.generator import generate_deep_story

DB_PATH = Path(__file__).parent / 'db.sqlite3'

STORIES_TO_REGEN = [66, 67]

def make_candidate(url: str, title: str, role: str, idx: int) -> NormalizedCandidate:
    """Build a minimal NormalizedCandidate from a stored source entry."""
    return NormalizedCandidate(
        candidate_id        = url,
        url                 = url,
        platform            = 'youtube' if 'youtube.com' in url else 'web',
        category            = 'world',
        language            = 'zh',
        hotness             = 100.0,
        effective_hotness   = 100.0,
        freshness           = datetime.now(timezone.utc),
        eligible_format_ids = frozenset([1]),
        crawler_item_id     = -(idx + 1),   # negative = synthetic, won't clash
        title_original      = title,
        canonical_title     = title,
        description_original= None,
        region_key          = None,
        region_name         = None,
        engagement_signals  = {},
        raw_payload         = None,
    )

def rebuild_cluster(ds: dict) -> EventCluster:
    """Reconstruct a minimal EventCluster from a stored deep_story dict."""
    sources = ds['sources']

    rep_src  = sources[0]  # first source is always representative
    rest     = sources[1:]

    rep = make_candidate(rep_src['url'], rep_src.get('title', ''), 'rep', 0)

    fact_srcs     = []
    context_srcs  = []
    reaction_srcs = []
    for i, s in enumerate(rest):
        cand = make_candidate(s['url'], s.get('title', ''), s.get('role', 'fact'), i + 1)
        role = s.get('role', 'fact')
        if role == 'context':
            context_srcs.append(cand)
        elif role == 'reaction':
            reaction_srcs.append(cand)
        else:
            fact_srcs.append(cand)

    return EventCluster(
        event_id         = ds['event_id'],
        representative   = rep,
        fact_sources     = fact_srcs,
        context_sources  = context_srcs,
        reaction_sources = reaction_srcs,
        member_count     = ds.get('cluster_size', len(sources)),
        source_diversity = ds.get('source_diversity', 0.5),
    )

def update_db(story_id: int, new_deep_story: dict) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "UPDATE hierarchical_stories SET deep_story=? WHERE id=?",
        (json.dumps(new_deep_story, ensure_ascii=False), story_id),
    )
    conn.commit()
    conn.close()
    print(f"  DB updated for story {story_id}")

def main():
    conn = sqlite3.connect(str(DB_PATH))

    for story_id in STORIES_TO_REGEN:
        print(f"\n{'='*60}")
        print(f"Re-generating story {story_id}...")

        row = conn.execute(
            "SELECT deep_story, lang, channel FROM hierarchical_stories WHERE id=?",
            (story_id,)
        ).fetchone()
        if not row:
            print(f"  ERROR: story {story_id} not found in DB")
            continue

        old_ds = json.loads(row[0])
        lang    = row[1]
        channel = row[2]

        print(f"  Topic: {old_ds.get('title', '?')}")
        print(f"  Old body length: {len(old_ds.get('body', ''))}")

        cluster = rebuild_cluster(old_ds)

        new_ds = generate_deep_story(cluster, lang=lang, channel=channel)

        print(f"  New body length: {len(new_ds.get('body', ''))}")
        print(f"  Body preview: {new_ds.get('body', '')[:100]}...")

        if not new_ds.get('body'):
            print(f"  WARNING: body still empty after regen — NOT updating DB")
            continue

        # Merge: keep original event_id, cluster_size, source_diversity
        new_ds['event_id']        = old_ds['event_id']
        new_ds['cluster_size']    = old_ds['cluster_size']
        new_ds['source_diversity']= old_ds['source_diversity']

        update_db(story_id, new_ds)
        print(f"  Story {story_id} re-generated successfully.")

    conn.close()
    print("\nDone.")

if __name__ == '__main__':
    main()
