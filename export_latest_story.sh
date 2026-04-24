#!/bin/bash
# export_latest_story.sh — Non-interactive export of the most recently generated
# hierarchical story from db.sqlite3 to a reflowed .txt file.
#
# Called automatically by run_generate.sh after each story generation run.
# Mirrors the logic of setup.sh option 8 → step a (_export_story_txt),
# with n=1 hardcoded (always the latest story, no user prompt).
#
# Outputs:
#   story_engine/exports/<category>/<name>.txt   — reflowed story text
#   story_engine/.last_export_txt               — absolute path of final .txt
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

if [ ! -f "$DB_PATH" ]; then
    echo "  ERROR: database not found at $DB_PATH" >&2
    exit 1
fi

mkdir -p "$EXPORT_DIR"

# ── Step 1: Export raw .txt from DB ──────────────────────────────────────────
PATHS_TMP=$(mktemp)

_EXPORT_PATHS_FILE="$PATHS_TMP" python3 - "$DB_PATH" "$EXPORT_DIR" <<'PYEOF'
import sys, json, os, re
from datetime import datetime, timezone

db_path    = sys.argv[1]
export_dir = sys.argv[2]
paths_file = os.environ.get('_EXPORT_PATHS_FILE', '')

try:
    import sqlite3
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
        SELECT h.id, h.story_set_id, h.batch_ts, h.lang, h.channel, h.status,
               h.deep_story, h.supporting_stories, h.generated_at,
               ss.profile_id
        FROM hierarchical_stories h
        LEFT JOIN story_sets ss ON ss.id = h.story_set_id
        ORDER BY h.generated_at DESC LIMIT 1
    """)
    rows = cur.fetchall()
    con.close()
except Exception as e:
    print(f"  ERROR reading database: {e}", file=sys.stderr)
    sys.exit(1)

if not rows:
    print("  ERROR: no stories found in database.", file=sys.stderr)
    sys.exit(1)

def strip_md(text):
    if not text:
        return ""
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    return text.strip()

for row in rows:
    sid, set_id, batch_ts, lang, channel, status, raw_ds, raw_ss, gen_at, profile_id = row
    ds      = json.loads(raw_ds) if raw_ds else {}
    ss_list = json.loads(raw_ss) if raw_ss else []

    dt_utc    = datetime.fromtimestamp(gen_at / 1000, tz=timezone.utc)
    date_slug = dt_utc.strftime("%Y-%m-%d")
    time_slug = dt_utc.strftime("%H%M")

    if profile_id:
        category = re.sub(r'^run\d+_', '', profile_id)
    else:
        category = "unknown"

    category_dir = os.path.join(export_dir, category)
    os.makedirs(category_dir, exist_ok=True)
    base = os.path.join(category_dir, f"{category}_story_{date_slug}_{time_slug}utc_{sid}")

    title   = ds.get("title", "Untitled")
    hook    = ds.get("hook", "")
    bullets = ds.get("bullets", [])
    twist   = ds.get("twist", "")

    items = [f"## {strip_md(title)}"]
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

    def outlet_from_title(src_title):
        parts = src_title.rsplit(" - ", 1)
        return parts[-1].strip() if len(parts) == 2 else ""

    def outlet_from_url(url):
        try:
            host = url.split("//", 1)[1].split("/")[0]
            parts = host.split(".")
            tlds = {"com","net","org","co","io","tv","uk","au","ca","kr","jp","cn"}
            meaningful = [p for p in parts if p.lower() not in tlds
                          and p.lower() not in ("www","m","n","news","rss")]
            return meaningful[-1] if meaningful else ""
        except Exception:
            return ""

    def to_slug(name):
        name = re.sub(r'\.(com|net|org|co|io|tv|uk|au|ca|kr|jp|cn)$', '', name, flags=re.IGNORECASE)
        name_clean = re.sub(r'[^\w\s]', '', name).strip()
        return re.sub(r'\s+', '', name_clean)

    sources_raw = ds.get("sources", [])
    seen = set()
    outlets = []
    for src in sources_raw:
        name = outlet_from_title(src.get("title", ""))
        if not name:
            name = outlet_from_url(src.get("url", ""))
        slug = to_slug(name)
        if slug and slug.lower() not in seen:
            seen.add(slug.lower())
            outlets.append(f"#{slug}")
    if outlets:
        items.append("### " + "  ".join(outlets))

    raw_path = base + "_raw.txt"
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write("\n-\n".join(items) + "\n-\n")

    if paths_file:
        with open(paths_file, "a") as pf:
            pf.write(raw_path + "\n")

    print(f"  ✓  Raw txt  : {raw_path}")

print("  Renaming to _no_norm.txt...")
PYEOF

py_exit=$?
if [ $py_exit -ne 0 ] || [ ! -s "$PATHS_TMP" ]; then
    echo "  ERROR: export step failed." >&2
    rm -f "$PATHS_TMP"
    exit 1
fi

# ── Step 2: Rename _raw.txt → _no_norm.txt (Step 1a — no clip splitting) ─────
# Complete sentences are preserved so Azure TTS produces natural pacing and
# breaks. Reflow (clip splitting) is only needed for Grok prompt generation
# (setup.sh option 8 → step 1b), which runs separately on demand.
: > "$LAST_EXPORT_FILE"
all_ok=1
while IFS= read -r raw_path; do
    [ -z "$raw_path" ] && continue
    final_path="${raw_path%_raw.txt}_no_norm.txt"
    mv "$raw_path" "$final_path"
    if [ $? -eq 0 ]; then
        echo "$final_path" >> "$LAST_EXPORT_FILE"
        echo "  ✓  Final txt: $final_path"
    else
        echo "  ERROR: rename failed for: $raw_path" >&2
        all_ok=0
    fi
done < "$PATHS_TMP"
rm -f "$PATHS_TMP"

if [ "$all_ok" -ne 1 ]; then
    exit 1
fi
