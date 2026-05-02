#!/usr/bin/env python3
"""
export_story.py — Export hierarchical stories from db.sqlite3 to .txt files.

Called by both export_latest_story.sh (cron, n=1) and setup.sh option 8
(interactive, n=user-supplied).  Keeps DB query, strip_md, and outlet-slug
logic in one place so both callers stay in sync.

Usage:
    python3 export_story.py \
        --db      /path/to/db.sqlite3 \
        --export-dir /path/to/exports \
        [--n 1] \
        [--paths-file /tmp/exported_paths.txt]

Output:
    exports/<category>/<category>_story_<date>_<time>utc_<id>_raw.txt
      — one paragraph per item, sections prefixed with "## ", source tags
        prefixed with "### ".  Callers rename/reflow as needed.

    If --paths-file is given, appends each raw_path (one per line) to that
    file so the calling shell script can drive the rename/reflow loop.

Exit codes:
    0 — at least one story exported
    1 — error (DB missing, no stories, write failure)
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone


# ── Text helpers ──────────────────────────────────────────────────────────────

def strip_md(text: str) -> str:
    """Strip markdown formatting from generated story text."""
    if not text:
        return ""
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)       # bold / italic
    text = re.sub(r'`(.+?)`',              r'\1', text)        # inline code
    text = re.sub(r'https?://\S+',          '',   text)        # bare URLs
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)      # [text](url)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)     # headings
    # Replace ASCII comma with Chinese full-width comma when adjacent to CJK text
    text = re.sub(r'(?<=[一-鿿　-〿＀-￯]),|,(?=[一-鿿　-〿＀-￯])', '，', text)
    return text.strip()


_ZH_SPLIT_PUNCT = '，。？！；：'
_ZH_TARGET_CHARS = 50     # target chars per Chinese TTS clip
_ZH_MIN_FLUSH    = 20     # don't flush a buffer shorter than this (avoids tiny leading fragments)
_EN_TARGET_WORDS = 30     # target words per English TTS clip


def _split_zh(text: str, target: int = _ZH_TARGET_CHARS) -> list[str]:
    """
    Split Chinese text at punctuation boundaries into clips of ~target chars.
    Greedy merge: keep accumulating fragments until the next would push the
    buffer past target AND the buffer is already long enough to stand alone
    (>= _ZH_MIN_FLUSH chars).  This avoids emitting tiny date/time fragments
    like '4月29日，' as standalone clips.  Single fragments already longer
    than target are emitted as-is rather than breaking mid-phrase.
    """
    split_pts = [i + 1 for i, ch in enumerate(text) if ch in _ZH_SPLIT_PUNCT]
    prev, pieces = 0, []
    for p in split_pts:
        piece = text[prev:p].strip()
        if piece:
            pieces.append(piece)
        prev = p
    tail = text[prev:].strip()
    if tail:
        pieces.append(tail)
    if not pieces:
        return [text.strip()] if text.strip() else []

    clips, buf = [], ''
    for piece in pieces:
        if not buf:
            buf = piece
        elif len(buf) + len(piece) <= target:
            buf += piece          # Chinese: no space between pieces
        elif len(buf) < _ZH_MIN_FLUSH:
            buf += piece          # buffer too short to stand alone — absorb anyway
        else:
            clips.append(buf)
            buf = piece
    if buf:
        clips.append(buf)
    return [c for c in clips if c.strip()]


def _split_en(text: str, target_words: int = _EN_TARGET_WORDS) -> list[str]:
    """
    Split English text at sentence boundaries into clips of ~target_words words.
    Greedily merges sentences; flushes when the next sentence would push the
    clip over the target.  A single sentence longer than target is emitted
    as-is to preserve natural speech rhythm for TTS.
    """
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return [text.strip()] if text.strip() else []

    clips, buf_words = [], []
    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue
        if not buf_words:
            buf_words = words
        elif len(buf_words) + len(words) <= target_words:
            buf_words.extend(words)
        else:
            clips.append(' '.join(buf_words))
            buf_words = words
    if buf_words:
        clips.append(' '.join(buf_words))
    return [c for c in clips if c.strip()]


def _split_clips(text: str, lang: str) -> list[str]:
    """
    Split a paragraph into short TTS clips.
    Chinese (lang='zh'): ~50 chars per clip, split at Chinese punctuation.
    English (any other lang): ~30 words per clip, split at sentence boundaries.
    """
    if not text or not text.strip():
        return []
    return _split_zh(text) if lang == 'zh' else _split_en(text)


def outlet_from_title(src_title: str) -> str:
    """Extract outlet name from 'Headline - OutletName' pattern."""
    parts = src_title.rsplit(" - ", 1)
    return parts[-1].strip() if len(parts) == 2 else ""


def outlet_from_url(url: str) -> str:
    """Extract meaningful domain slug from a URL (e.g. naver.com → naver)."""
    try:
        host  = url.split("//", 1)[1].split("/")[0]
        parts = host.split(".")
        tlds  = {"com", "net", "org", "co", "io", "tv",
                 "uk",  "au",  "ca",  "kr", "jp", "cn"}
        skip  = tlds | {"www", "m", "n", "news", "rss"}
        meaningful = [p for p in parts if p.lower() not in skip]
        return meaningful[-1] if meaningful else ""
    except Exception:
        return ""


def to_slug(name: str) -> str:
    """Convert outlet name to a compact hashtag slug (e.g. 'BBC News' → 'BBCNews')."""
    name = re.sub(r'\.(com|net|org|co|io|tv|uk|au|ca|kr|jp|cn)$',
                  '', name, flags=re.IGNORECASE)
    name = re.sub(r'[^\w\s]', '', name).strip()
    return re.sub(r'\s+', '', name)


# ── Export ────────────────────────────────────────────────────────────────────

def export_stories(db_path: str, export_dir: str,
                   n: int, paths_file: str) -> int:
    """
    Fetch the n most recent hierarchical stories and write _raw.txt files.

    Returns the number of stories successfully exported.
    Raises SystemExit(1) on fatal errors.
    """
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("""
            SELECT h.id, h.story_set_id, h.batch_ts, h.lang, h.channel,
                   h.status, h.deep_story, h.supporting_stories,
                   h.generated_at, ss.profile_id
            FROM   hierarchical_stories h
            LEFT JOIN story_sets ss ON ss.id = h.story_set_id
            ORDER  BY h.generated_at DESC
            LIMIT  ?
        """, (n,))
        rows = cur.fetchall()
        con.close()
    except Exception as exc:
        print(f"  ERROR reading database: {exc}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("  ERROR: no stories found in database.", file=sys.stderr)
        sys.exit(1)

    exported = 0

    for row in rows:
        (sid, set_id, batch_ts, lang, channel, status,
         raw_ds, raw_ss, gen_at, profile_id) = row

        ds      = json.loads(raw_ds)  if raw_ds else {}
        ss_list = json.loads(raw_ss)  if raw_ss else []

        dt_utc    = datetime.fromtimestamp(gen_at / 1000, tz=timezone.utc)
        date_slug = dt_utc.strftime("%Y-%m-%d")
        time_slug = dt_utc.strftime("%H%M")

        category = re.sub(r'^run\d+_', '', profile_id) if profile_id else "unknown"

        category_dir = os.path.join(export_dir, category)
        os.makedirs(category_dir, exist_ok=True)

        base = os.path.join(
            category_dir,
            f"{category}_story_{date_slug}_{time_slug}utc_{sid}",
        )

        # ── Build item list ───────────────────────────────────────────────────
        title   = ds.get("title",   "Untitled")
        body    = ds.get("body",    "")   # new single-narrative format
        hook    = ds.get("hook",    "")   # legacy format
        bullets = ds.get("bullets", [])   # legacy format
        twist   = ds.get("twist",   "")   # legacy format

        items = [f"## {strip_md(title)}"]
        if body:
            # Split body into paragraphs first (\n\n-separated), then each paragraph
            # into short TTS clips (~50 chars for Chinese, ~30 words for English).
            # This lets one bad clip be re-generated without re-doing the whole story.
            paragraphs = [p.strip() for p in body.split('\n\n') if p.strip()]
            if not paragraphs:
                paragraphs = [p.strip() for p in body.split('\n') if p.strip()]
            if not paragraphs:
                paragraphs = [body.strip()] if body.strip() else []
            for para in paragraphs:
                clean = strip_md(para)
                for clip in _split_clips(clean, lang):
                    items.append(clip)
        else:
            # Legacy format: hook + bullets + twist
            for raw in [hook] + [str(b) for b in bullets] + [twist]:
                clean = strip_md(raw)
                if clean:
                    items.append(clean)

        for s in ss_list:
            s_title = strip_md(s.get("title", ""))
            summary = strip_md(s.get("summary", ""))
            why     = strip_md(s.get("why_it_matters", ""))
            if s_title:
                items.append(f"## {s_title}")
            for raw_item in [summary, why]:
                clean = strip_md(raw_item)
                if clean:
                    items.append(clean)

        # ── Source outlet hashtags ────────────────────────────────────────────
        seen    = set()
        outlets = []
        for src in ds.get("sources", []):
            name = outlet_from_title(src.get("title", ""))
            if not name:
                name = outlet_from_url(src.get("url", ""))
            slug = to_slug(name)
            if slug and slug.lower() not in seen:
                seen.add(slug.lower())
                outlets.append(f"#{slug}")
        if outlets:
            items.append("### " + "  ".join(outlets))

        # ── Write raw .txt ────────────────────────────────────────────────────
        raw_path = base + "_raw.txt"
        try:
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write("\n-\n".join(items) + "\n-\n")
        except Exception as exc:
            print(f"  ERROR writing {raw_path}: {exc}", file=sys.stderr)
            continue

        if paths_file:
            with open(paths_file, "a", encoding="utf-8") as pf:
                pf.write(raw_path + "\n")

        print(f"  ✓  Raw txt  : {raw_path}")
        exported += 1

    print(f"\n  {exported} story/stories written.")
    return exported


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export hierarchical stories from db.sqlite3 to _raw.txt files."
    )
    parser.add_argument("--db",          required=True,
                        help="Path to db.sqlite3")
    parser.add_argument("--export-dir",  required=True,
                        help="Root export directory (exports/)")
    parser.add_argument("--n",           type=int, default=1,
                        help="Number of most-recent stories to export (default: 1)")
    parser.add_argument("--paths-file",  default="",
                        help="File to append exported raw paths to (one per line)")
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"  ERROR: database not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.export_dir, exist_ok=True)

    exported = export_stories(
        db_path    = args.db,
        export_dir = args.export_dir,
        n          = args.n,
        paths_file = args.paths_file,
    )

    if exported == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
