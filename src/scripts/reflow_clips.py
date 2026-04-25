#!/usr/bin/env python3
"""
reflow_clips.py — Reformat exported story .txt clips for Grok video generation.

Strategy:
  1. Parse the file into sections (each ## heading starts a new section).
  2. Concatenate all clips within a section into one continuous text.
  3. Split at period or comma only (，。,.), then greedily merge fragments
     into clips up to MAX_CHARS (60).  A fragment that would push the current
     clip over 60 starts a new clip instead.  Any clip already over 60 chars
     is left as-is for manual revision.

Usage:
  python reflow_clips.py input.txt [output.txt]

  If output.txt is omitted, writes to input_fmt.txt.
"""

import sys
import os

# ── Configuration ──────────────────────────────────────────────────────────────

PUNCT     = '，。'    # split AFTER Chinese period/comma only; ASCII . and , are not sentence breaks
MAX_CHARS = 60        # target max chars per clip; leave longer ones for manual revision


# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_sections(path):
    """
    Returns (sections, sources_line) where:
      sections    — list of (title_line | None, [clip_text, ...])
      sources_line — the '### #Tag1  #Tag2' line if present, else None

    '### ' lines are preserved verbatim and not reflowed.
    Multi-line clips (no '-' between them in the original) are joined.
    """
    sections = []
    sources_line = None
    cur_title = None
    cur_clips = []
    cur_lines = []

    def flush_clip():
        text = ''.join(cur_lines).strip()
        if text:
            cur_clips.append(text)
        cur_lines.clear()

    def flush_section():
        flush_clip()
        if cur_title is not None or cur_clips:
            sections.append((cur_title, list(cur_clips)))
        cur_clips.clear()

    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.rstrip('\n').rstrip()
            if line.startswith('## '):
                flush_section()
                cur_title = line
                cur_clips.clear()
            elif line.startswith('### '):
                flush_clip()          # close any open clip
                sources_line = line   # preserve verbatim, do not reflow
            elif line == '-':
                flush_clip()
            elif line:
                cur_lines.append(line)

    flush_section()
    return sections, sources_line


# ── Splitting ─────────────────────────────────────────────────────────────────

def reflow(text):
    """
    Split `text` into clips at every period or comma (，。,.), then greedily
    merge consecutive fragments into one clip as long as the combined length
    stays ≤ MAX_CHARS.  When adding the next fragment would exceed MAX_CHARS,
    flush the current clip and start a new one.  A single fragment that is
    already > MAX_CHARS is emitted as-is for manual revision.
    """
    # Build split positions: index of first char AFTER each punct
    split_pts = [i + 1 for i, ch in enumerate(text) if ch in PUNCT]

    # Slice text at each split point
    raw_pieces = []
    prev = 0
    for p in split_pts:
        piece = text[prev:p].strip()
        if piece:
            raw_pieces.append(piece)
        prev = p
    tail = text[prev:].strip()
    if tail:
        raw_pieces.append(tail)

    if not raw_pieces:
        return [text.strip()] if text.strip() else []

    # Greedy merge: keep adding fragments while combined length ≤ MAX_CHARS.
    # No space after Chinese punctuation (，。); keep a space otherwise.
    clips = []
    buf = ''
    for piece in raw_pieces:
        if not buf:
            buf = piece
        else:
            sep = '' if buf[-1] in PUNCT else ' '
            if len(buf) + len(sep) + len(piece) <= MAX_CHARS:
                buf = buf + sep + piece
            else:
                clips.append(buf)
                buf = piece
    if buf:
        clips.append(buf)

    return [c for c in clips if c.strip()]


# ── Output ─────────────────────────────────────────────────────────────────────

def reformat(input_path, output_path):
    sections, sources_line = parse_sections(input_path)

    out_items = []   # alternating: title strings and clip strings
    for title, clips in sections:
        if title:
            out_items.append(title)
        # Join all clips in this section with a single space so punctuation flows.
        full_text = ' '.join(c.strip() for c in clips if c.strip())
        if full_text:
            out_items.extend(reflow(full_text))

    # Append source tags at the end, after the last '-', verbatim.
    if sources_line:
        out_items.append(sources_line)

    # Format: items separated by '\n-\n', trailing '\n-\n'
    body = '\n-\n'.join(out_items)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(body + '\n-\n')

    return output_path


# ── CLI / report ───────────────────────────────────────────────────────────────

def report(path):
    """Print each clip with its character count; flag anything over MAX_CHARS."""
    total = 0
    long_count = 0
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.rstrip('\n').rstrip()
            if not line or line == '-' or line.startswith('## ') or line.startswith('### '):
                print(line)
                continue
            n = len(line)
            total += 1
            flag = '  ← LONG' if n > MAX_CHARS else ''
            if flag:
                long_count += 1
            print(f"  [{n:>3}]  {line}{flag}")

    note = f"  ({long_count} over {MAX_CHARS} chars — revise manually)" if long_count else "  (all within limit)"
    print(f"\n  Clips: {total}{note}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {os.path.basename(sys.argv[0])} input.txt [output.txt]")
        sys.exit(1)

    inp = sys.argv[1]
    if not os.path.isfile(inp):
        print(f"Error: file not found: {inp}")
        sys.exit(1)

    base, ext = os.path.splitext(inp)
    out = sys.argv[2] if len(sys.argv) >= 3 else f"{base}_fmt{ext}"

    reformat(inp, out)
    print(f"\nOutput → {out}\n")
    report(out)


if __name__ == '__main__':
    main()
