#!/bin/bash
# export_latest_story.sh — Non-interactive export of the most recently generated
# hierarchical story from db.sqlite3 to reflowed .txt files.
#
# Called automatically by run_generate.sh after each story generation run.
# Mirrors the logic of setup.sh option 8, with n=1 hardcoded (always the
# latest story, no user prompt).
#
# Outputs (per story):
#   exports/<category>/<name>_no_norm.txt   — full sentences, no clip splitting
#   exports/<category>/<name>_with_norm.txt — split at ，。, clips ≤ 60 chars
#   .last_export_txt                        — absolute path of _no_norm.txt
#                                             (used by narration pipeline)
#
# Exit codes:
#   0 — success
#   1 — failure (DB missing, no stories, reflow error)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Activate virtualenv
if [ -d "/home/tnnd/.virtualenvs/crawl" ]; then
    source /home/tnnd/.virtualenvs/crawl/bin/activate
fi

# Load .env (exports all vars including STORY_ENGINE_DB, AZURE_* etc.)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

DB_PATH="${STORY_ENGINE_DB:-$SCRIPT_DIR/db.sqlite3}"
EXPORT_DIR="$SCRIPT_DIR/exports"
LAST_EXPORT_FILE="$SCRIPT_DIR/.last_export_txt"
EXPORT_SCRIPT="$SCRIPT_DIR/src/scripts/export_story.py"
REFLOW_SCRIPT="$SCRIPT_DIR/src/scripts/reflow_clips.py"

if [ ! -f "$DB_PATH" ]; then
    echo "  ERROR: database not found at $DB_PATH" >&2
    exit 1
fi

if [ ! -f "$EXPORT_SCRIPT" ]; then
    echo "  ERROR: export_story.py not found at $EXPORT_SCRIPT" >&2
    exit 1
fi

if [ ! -f "$REFLOW_SCRIPT" ]; then
    echo "  ERROR: reflow_clips.py not found at $REFLOW_SCRIPT" >&2
    exit 1
fi

mkdir -p "$EXPORT_DIR"

# ── Step 1: Export raw .txt from DB ──────────────────────────────────────────
PATHS_TMP=$(mktemp)

python3 "$EXPORT_SCRIPT" \
    --db         "$DB_PATH" \
    --export-dir "$EXPORT_DIR" \
    --n          1 \
    --paths-file "$PATHS_TMP"

py_exit=$?
if [ $py_exit -ne 0 ] || [ ! -s "$PATHS_TMP" ]; then
    echo "  ERROR: export step failed." >&2
    rm -f "$PATHS_TMP"
    exit 1
fi

# ── Step 2: Rename _raw.txt → _no_norm.txt ───────────────────────────────────
: > "$LAST_EXPORT_FILE"
all_ok=1
while IFS= read -r raw_path; do
    [ -z "$raw_path" ] && continue
    no_norm_path="${raw_path%_raw.txt}_no_norm.txt"
    if mv "$raw_path" "$no_norm_path"; then
        echo "$no_norm_path" >> "$LAST_EXPORT_FILE"
        echo "  ✓  No norm  : $no_norm_path"
    else
        echo "  ERROR: rename failed for: $raw_path" >&2
        all_ok=0
    fi
done < "$PATHS_TMP"
rm -f "$PATHS_TMP"

if [ "$all_ok" -ne 1 ]; then
    exit 1
fi

# ── Step 3: Generate _with_norm.txt (clip-split at ，。, ≤ 60 chars) ──────────
while IFS= read -r no_norm_path; do
    [ -z "$no_norm_path" ] && continue
    with_norm_path="${no_norm_path%_no_norm.txt}_with_norm.txt"
    if python3 "$REFLOW_SCRIPT" "$no_norm_path" "$with_norm_path" > /dev/null; then
        echo "  ✓  With norm: $with_norm_path"
    else
        echo "  WARNING: reflow failed for: $no_norm_path" >&2
    fi
done < "$LAST_EXPORT_FILE"
