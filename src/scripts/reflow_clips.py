#!/usr/bin/env python3
"""
reflow_clips.py — Reformat exported story .txt clips for Grok video generation.

Strategy:
  1. Parse the file into sections (each ## heading starts a new section).
  2. Concatenate all clips within a section into one continuous text.
  3. Re-split the text at natural punctuation (，。、！？,.) to produce clips
     that are close to the video duration targets:
       10s clip → 55–65 chars
        6s clip → 33–39 chars

Usage:
  python reflow_clips.py input.txt [output.txt]

  If output.txt is omitted, writes to input_fmt.txt.
"""

import re
import sys
import os

# ── Configuration ──────────────────────────────────────────────────────────────

PUNCT       = '，。、！？,.：:'   # split AFTER any of these chars
TARGET_10S  = (55, 65)           # ideal char range for a 10s clip
TARGET_6S   = (33, 39)           # ideal char range for a 6s clip
HARD_MAX    = 75                 # never produce a clip longer than this
MIN_VIABLE  = 20                 # minimum chars to be worth its own clip


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


# ── Scoring & Splitting ────────────────────────────────────────────────────────

def score(n):
    """
    Rate a clip length.  Returns (priority, fine_distance) — lower is better.

    Priority bands:
      0 — perfect 10s  (55–65)
      1 — perfect 6s   (33–39)
      2 — acceptable   (40–54 gray zone, or 66–75 slightly over)
      3 — short        (20–32)
      9 — bad          (< 20 or > HARD_MAX)
    """
    if TARGET_10S[0] <= n <= TARGET_10S[1]:
        return (0, abs(n - 60))
    if TARGET_6S[0] <= n <= TARGET_6S[1]:
        return (1, abs(n - 36))
    if 40 <= n < TARGET_10S[0]:                      # gray zone below 10s
        return (2, TARGET_10S[0] - n)
    if TARGET_10S[1] < n <= HARD_MAX:                # slightly over 10s
        return (2, n - TARGET_10S[1])
    if MIN_VIABLE <= n < TARGET_6S[0]:               # short but viable
        return (3, TARGET_6S[0] - n)
    return (9, 999)


def reflow(text):
    """
    Re-split `text` into a list of clips targeting the video duration ranges.
    Split points are positions immediately AFTER a punctuation character.
    """
    # Build sorted list of all valid split positions (index of first char AFTER punct)
    split_pts = [i + 1 for i, ch in enumerate(text) if ch in PUNCT]

    clips = []
    start = 0

    while start < len(text):
        # Skip any leading whitespace that accumulated from join separators.
        # This ensures chunk-size scoring matches the actual stripped clip length.
        while start < len(text) and text[start] == ' ':
            start += 1
        if start >= len(text):
            break

        remaining = len(text) - start

        # If what's left fits in one clip (even if slightly over target), emit it.
        if remaining <= HARD_MAX:
            chunk = text[start:].strip()
            if chunk:
                clips.append(chunk)
            break

        # Candidate split positions relative to `start`, within a generous window.
        candidates = [
            p - start
            for p in split_pts
            if start + MIN_VIABLE <= p <= start + HARD_MAX
        ]

        if candidates:
            # Pick the split length with the best score.
            best_len = min(candidates, key=score)
            clips.append(text[start : start + best_len].strip())
            start += best_len
        else:
            # No punctuation in window — force-split at the target midpoint.
            force = TARGET_10S[0]   # 55 chars
            clips.append(text[start : start + force].strip())
            start += force

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
    """Print each line with its length tag."""
    counts = {'✓10s': 0, '✓6s': 0, 'gray': 0, 'short': 0, 'LONG': 0}
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.rstrip('\n').rstrip()
            if not line or line == '-' or line.startswith('## ') or line.startswith('### '):
                print(line)
                continue
            n = len(line)
            if TARGET_10S[0] <= n <= TARGET_10S[1]:
                tag = '✓10s'; counts['✓10s'] += 1
            elif TARGET_6S[0] <= n <= TARGET_6S[1]:
                tag = '✓6s '; counts['✓6s'] += 1
            elif n > TARGET_10S[1]:
                tag = f'LONG({n})'; counts['LONG'] += 1
            elif n < TARGET_6S[0]:
                tag = f'short({n})'; counts['short'] += 1
            else:
                tag = f'gray({n})'; counts['gray'] += 1
            print(f"  [{tag:>10}]  {line}")

    total = sum(counts.values())
    good  = counts['✓10s'] + counts['✓6s']
    print(f"\n  Clips: {total}  |  ✓10s:{counts['✓10s']}  ✓6s:{counts['✓6s']}"
          f"  gray:{counts['gray']}  short:{counts['short']}  LONG:{counts['LONG']}"
          f"  |  {good}/{total} on-target ({100*good//total if total else 0}%)")


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
