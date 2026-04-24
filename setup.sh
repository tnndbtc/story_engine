#!/bin/bash
# story_engine service manager

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$SCRIPT_DIR/.api_server.pid"
LOG_FILE="$LOG_DIR/api_server.log"
PORT=8003
LAST_EXPORT_FILE="$SCRIPT_DIR/.last_export_txt"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Activate virtualenv
if [ -d "/home/tnnd/.virtualenvs/crawl" ]; then
    source /home/tnnd/.virtualenvs/crawl/bin/activate
fi

# Load .env if present (exports all vars so child processes inherit them)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

mkdir -p "$LOG_DIR"

get_pid() {
    # Check PID file first, then fall back to port scan
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return
        fi
        rm -f "$PID_FILE"
    fi
    # Find by port
    lsof -ti :$PORT 2>/dev/null | head -1
}

is_running() {
    local pid=$(get_pid)
    [ -n "$pid" ]
}

start_service() {
    echo ""
    if is_running; then
        local pid=$(get_pid)
        echo -e "  ${YELLOW}Service already running (PID $pid). Restarting...${NC}"
        stop_service_quiet
        sleep 1
    fi

    echo -e "  ${CYAN}Starting story_engine API on port $PORT...${NC}"
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] === Starting story_engine API (port $PORT) ===" >> "$LOG_FILE"
    cd "$SRC_DIR"
    nohup python -m uvicorn main:app --host 0.0.0.0 --port $PORT \
        >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 2

    if kill -0 "$pid" 2>/dev/null; then
        # Verify endpoint
        local status=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/api/status 2>/dev/null)
        if [ "$status" = "200" ]; then
            echo -e "  ${GREEN}Service started (PID $pid)${NC}"
            echo -e "  ${GREEN}API: http://0.0.0.0:$PORT/docs${NC}"
        else
            echo -e "  ${YELLOW}Process started (PID $pid) but API not responding yet${NC}"
        fi
    else
        echo -e "  ${RED}Failed to start. Check $LOG_FILE${NC}"
        rm -f "$PID_FILE"
    fi
    echo ""
}

stop_service_quiet() {
    local pid=$(get_pid)
    if [ -n "$pid" ]; then
        echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] === Stopping story_engine API (PID $pid) ===" >> "$LOG_FILE"
        kill "$pid" 2>/dev/null
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null
        fi
        rm -f "$PID_FILE"
    fi
}

stop_service() {
    echo ""
    local pid=$(get_pid)
    if [ -z "$pid" ]; then
        echo -e "  ${YELLOW}Service is not running${NC}"
    else
        echo -e "  ${CYAN}Stopping service (PID $pid)...${NC}"
        stop_service_quiet
        echo -e "  ${GREEN}Service stopped${NC}"
    fi
    echo ""
}

show_status() {
    echo ""
    local pid=$(get_pid)
    if [ -z "$pid" ]; then
        echo -e "  ${RED}Service: STOPPED${NC}"
    else
        echo -e "  ${GREEN}Service: RUNNING (PID $pid)${NC}"
        echo -e "  ${CYAN}Port:    $PORT${NC}"
        echo -e "  ${CYAN}API:     http://0.0.0.0:$PORT/docs${NC}"
        echo -e "  ${CYAN}Log:     $LOG_FILE${NC}"

        # Query status endpoint
        local response=$(curl -s http://localhost:$PORT/api/status 2>/dev/null)
        if [ -n "$response" ]; then
            local stories=$(echo "$response" | python -c "import sys,json; print(json.load(sys.stdin).get('stories_today',0))" 2>/dev/null)
            local crawler=$(echo "$response" | python -c "import sys,json; print(json.load(sys.stdin).get('crawler_db_reachable','unknown'))" 2>/dev/null)
            echo ""
            echo -e "  ${BOLD}Engine Status:${NC}"
            echo -e "    Stories today:      $stories"
            echo -e "    Crawler DB:         $crawler"
        fi
    fi
    echo ""
}

reset_last_batch() {
    echo ""
    echo -e "  ${BOLD}Reset Last Batch${NC}"
    echo ""

    local db_path="${STORY_ENGINE_DB:-$SCRIPT_DIR/db.sqlite3}"

    if [ ! -f "$db_path" ]; then
        echo -e "  ${RED}ERROR: database not found at $db_path${NC}"
        echo ""
        return
    fi

    # Find the last story set
    local result
    result=$(python3 -c "
import sqlite3, sys
conn = sqlite3.connect('$db_path')
conn.row_factory = sqlite3.Row
row = conn.execute('SELECT id, status, created_at FROM story_sets ORDER BY id DESC LIMIT 1').fetchone()
if not row:
    print('NONE')
else:
    print(str(row['id']) + '|' + str(row['status']) + '|' + str(row['created_at']))
conn.close()
" 2>&1)

    if [ "$result" = "NONE" ]; then
        echo -e "  ${YELLOW}No story sets found in database.${NC}"
        echo ""
        return
    fi

    local set_id
    local set_status
    local set_created
    set_id=$(echo "$result" | cut -d'|' -f1)
    set_status=$(echo "$result" | cut -d'|' -f2)
    set_created=$(echo "$result" | cut -d'|' -f3)

    echo -e "  Last batch found:"
    echo -e "    ${CYAN}ID:       $set_id${NC}"
    echo -e "    ${CYAN}Status:   $set_status${NC}"
    echo -e "    ${CYAN}Created:  $set_created${NC}"
    echo ""
    echo -e "  ${YELLOW}WARNING: This will permanently delete all stories, used_items,${NC}"
    echo -e "  ${YELLOW}and the story_set record for batch #$set_id.${NC}"
    echo -e "  ${YELLOW}URLs will be freed so the next run can select fresh articles.${NC}"
    echo ""
    read -p "  Delete batch #$set_id and re-enable its articles? [y/N]: " confirm

    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo -e "  ${YELLOW}Cancelled.${NC}"
        echo ""
        return
    fi

    echo ""
    python3 -c "
import sqlite3, sys
db_path = '$db_path'
set_id = $set_id

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA foreign_keys=ON')

# Step 1 — delete used_items first (FK references story_sets + stories)
cur = conn.execute('DELETE FROM used_items WHERE story_set_id = ?', (set_id,))
print('  used_items deleted:  ' + str(cur.rowcount))

# Step 1.5 — delete event_memory rows for this batch BEFORE deleting stories.
# event_memory.story_id → stories(id) and event_memory.story_set_id → story_sets(id)
# are both FK constraints enforced by PRAGMA foreign_keys=ON.
# Without this step, Step 2 raises sqlite3.IntegrityError: FOREIGN KEY constraint failed.
cur = conn.execute('DELETE FROM event_memory WHERE story_set_id = ?', (set_id,))
print('  event_memory deleted: ' + str(cur.rowcount))

# Step 2 — delete stories linked to this set via batch_id
story_rows = conn.execute('SELECT id FROM stories WHERE batch_id = ?', (set_id,)).fetchall()
story_ids = [r['id'] for r in story_rows]
if story_ids:
    placeholders = ','.join('?' * len(story_ids))
    cur = conn.execute('DELETE FROM stories WHERE id IN (' + placeholders + ')', story_ids)
    print('  stories deleted:     ' + str(cur.rowcount) + '  (ids: ' + str(story_ids) + ')')
else:
    print('  stories deleted:     0  (none found for batch_id=' + str(set_id) + ')')

# Step 3 — delete the story set itself
cur = conn.execute('DELETE FROM story_sets WHERE id = ?', (set_id,))
print('  story_sets deleted:  ' + str(cur.rowcount))

conn.commit()
conn.close()

if cur.rowcount == 0:
    print('WARNING: story_sets row was already gone — nothing deleted')
    sys.exit(1)
else:
    print('')
    print('  Batch #' + str(set_id) + ' deleted. Run option 5 to generate a fresh set.')
" 2>&1

    echo ""
}

generate_stories() {
    echo ""
    echo -e "  ${BOLD}Generate Stories${NC}"
    echo ""
    echo "  46 formats (input number directly, or range, or comma-separated)"
    echo ""
    echo "   0) Cancel         all) All 46 formats     dry) Dry run"
    echo ""
    echo "   1) 60秒解读       2) 今日热点5      3) 全球雷达"
    echo "   4) 区域视角       5) 双面观点       6) 趋势分析"
    echo "   7) 即将爆火       8) 深度报道       9) 专题聚焦"
    echo "  10) 反直觉        11) 角色代入      12) 时间线复盘"
    echo "  13) 谁赢谁输      14) 关键数据      15) 谣言vs真相"
    echo "  16) 被忽视但重要  17) 背景补课      18) 二选一"
    echo "  19) 未来会怎样    20) 一句话总结    21) 最离谱新闻"
    echo "  22) 同类对比      23) 排行榜        24) 错误决策"
    echo "  25) 连锁反应      26) 情绪解读      27) 第一视角"
    echo "  28) 极端假设      29) 一分钟故事    30) 黑白对立"
    echo "  31) 评论精选      32) 误判合集      33) 关键词拆解"
    echo "  34) 24小时回顾    35) 标题对比      36) 冷知识"
    echo "  37) 幕后逻辑      38) 失败案例      39) 成功路径"
    echo "  40) 三点结论      41) 你需要知道的  42) 误区提醒"
    echo "  43) 对普通人      44) 短问短答      45) 概念解释"
    echo "  46) 历史对照"
    echo ""
    echo ""
    echo "  Examples: 10  |  10-16  |  10,13,19  |  all  |  dry  |  0"
    echo ""
    read -p "  Select: " gen_choice

    local extra_args=""

    # Map format numbers 1-9 to legacy names
    declare -A LEGACY_MAP
    LEGACY_MAP[1]="explainer" LEGACY_MAP[2]="top5" LEGACY_MAP[3]="radar"
    LEGACY_MAP[4]="regional" LEGACY_MAP[5]="two_takes" LEGACY_MAP[6]="pattern"
    LEGACY_MAP[7]="viral" LEGACY_MAP[8]="deep_dive" LEGACY_MAP[9]="niche"

    case $gen_choice in
        0) echo -e "  ${YELLOW}Cancelled${NC}"; echo ""; return ;;
        all) gen_choice="1-46" ;;
        dry) gen_choice="1-46"; extra_args="--dry-run" ;;
    esac

    # Parse input: supports single, range (10-16), or list (10,13,19)
    local formats_to_run=""
    local expanded=""
    for part in $(echo "$gen_choice" | tr ',' ' '); do
        if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            local range_start=${BASH_REMATCH[1]}
            local range_end=${BASH_REMATCH[2]}
            for ((i=range_start; i<=range_end; i++)); do
                expanded="$expanded $i"
            done
        elif [[ "$part" =~ ^[0-9]+$ ]]; then
            expanded="$expanded $part"
        else
            echo -e "  ${RED}Invalid input: $part${NC}"; echo ""; return
        fi
    done

    # Convert format numbers to run.py format args
    for num in $expanded; do
        if [ "$num" -ge 1 ] && [ "$num" -le 9 ] && [ -n "${LEGACY_MAP[$num]}" ]; then
            formats_to_run="$formats_to_run ${LEGACY_MAP[$num]}"
        elif [ "$num" -ge 10 ] && [ "$num" -le 46 ]; then
            formats_to_run="$formats_to_run format_${num}"
        else
            echo -e "  ${RED}Invalid format number: $num${NC}"; echo ""; return
        fi
    done

    if [ -z "$formats_to_run" ]; then
        echo -e "  ${RED}No valid formats selected${NC}"; echo ""; return
    fi

    # Run selected formats
    echo ""
    echo -e "  ${CYAN}Stories will be added as a new set (old stories preserved).${NC}"
    echo -e "  ${CYAN}Generating selected formats...${NC}"
    echo ""

    cd "$SRC_DIR"
    python engine/run.py --format $formats_to_run --lang zh --channel 2 $extra_args

    echo ""
    if [ -z "$extra_args" ]; then
        echo -e "  ${GREEN}Generation complete!${NC}"
        if is_running; then
            echo -e "  ${CYAN}Stories are available via the API immediately.${NC}"
        else
            echo -e "  ${YELLOW}Note: Start the API service (option 1) to serve stories.${NC}"
        fi
    fi
    echo ""
    return
}

export_last_story() {
    echo ""
    echo -e "  ${BOLD}Export Stories${NC}"
    echo ""
    echo "  a)  Step 1a — Export story to .txt  (no length normalization)"
    echo "  b)  Step 1b — Export story to .txt  (with length normalization)"
    echo "  c)  Step 2  — Generate Grok prompts from reviewed .txt"
    echo "  0)  Cancel"
    echo ""

    if [ -f "$LAST_EXPORT_FILE" ] && [ -s "$LAST_EXPORT_FILE" ]; then
        echo -e "  ${CYAN}Last export (Step 1b):${NC}"
        while IFS= read -r fp; do
            [ -n "$fp" ] && echo -e "    $fp"
        done < "$LAST_EXPORT_FILE"
        echo ""
    fi

    read -p "  Select step [a/b/c/0]: " export_choice

    case $export_choice in
        a) _export_story_txt no_reflow ;;
        b) _export_story_txt ;;
        c) _generate_grok_prompts ;;
        0) echo -e "  ${YELLOW}Cancelled${NC}"; echo ""; return ;;
        *) echo -e "  ${RED}Invalid option: $export_choice${NC}"; echo ""; return ;;
    esac
}

_export_story_txt() {
    local no_reflow=0
    [ "${1:-}" = "no_reflow" ] && no_reflow=1

    local db_path="${STORY_ENGINE_DB:-$SCRIPT_DIR/db.sqlite3}"
    local export_dir="$SCRIPT_DIR/exports"

    if [ ! -f "$db_path" ]; then
        echo -e "  ${RED}ERROR: database not found at $db_path${NC}"
        echo ""; return
    fi

    read -p "  How many recent stories to export? [1]: " n_choice
    n_choice="${n_choice:-1}"
    if ! [[ "$n_choice" =~ ^[1-9][0-9]*$ ]]; then
        echo -e "  ${RED}Invalid number: $n_choice${NC}"; echo ""; return
    fi

    mkdir -p "$export_dir"

    echo ""
    echo -e "  ${CYAN}Fetching $n_choice story/stories from database...${NC}"
    echo ""

    # Write raw .txt files — one paragraph per item, no clip splitting yet.
    # reflow_clips.py (step 2 below) joins + re-splits to target clip lengths.
    # Raw paths are recorded in a temp file so bash can drive the reflow loop.
    local paths_tmp
    paths_tmp=$(mktemp)

    _EXPORT_PATHS_FILE="$paths_tmp" python3 - "$db_path" "$export_dir" "$n_choice" <<'PYEOF'
import sys, json, os, re
from datetime import datetime, timezone

db_path, export_dir, n_arg = sys.argv[1], sys.argv[2], sys.argv[3]
n_stories  = max(1, int(n_arg))
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
        ORDER BY h.generated_at DESC LIMIT ?
    """, (n_stories,))
    rows = cur.fetchall()
    con.close()
except Exception as e:
    print(f"  ERROR reading database: {e}", file=sys.stderr)
    sys.exit(1)

if not rows:
    print("  No stories found in database.")
    sys.exit(0)

def strip_md(text):
    if not text:
        return ""
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    return text.strip()

exported = 0
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

    # One raw paragraph per item — reflow_clips.py handles clip-length splitting.
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

    # Extract outlet names from sources and append as hashtags.
    # Strategy: prefer "Headline - OutletName" suffix in title; fall back to URL domain.
    def outlet_from_title(src_title):
        parts = src_title.rsplit(" - ", 1)
        return parts[-1].strip() if len(parts) == 2 else ""

    def outlet_from_url(url):
        # e.g. "https://n.news.naver.com/..." → "naver"
        try:
            host = url.split("//", 1)[1].split("/")[0]   # strip scheme + path
            # drop www/m/n/news subdomains, keep meaningful part
            parts = host.split(".")
            # find the second-level domain (before TLD)
            tlds = {"com","net","org","co","io","tv","uk","au","ca","kr","jp","cn"}
            # walk right-to-left: skip TLD tokens, take first non-TLD as the name
            meaningful = [p for p in parts if p.lower() not in tlds
                          and p.lower() not in ("www","m","n","news","rss")]
            return meaningful[-1] if meaningful else ""
        except Exception:
            return ""

    def to_slug(name):
        name = re.sub(r'\.(com|net|org|co|io|tv|uk|au|ca|kr|jp|cn)$', '', name, flags=re.IGNORECASE)
        name_clean = re.sub(r'[^\w\s]', '', name).strip()
        return re.sub(r'\s+', '', name_clean)   # "BBC News" → "BBCNews"

    sources_raw = ds.get("sources", [])
    seen = set()
    outlets = []
    for src in sources_raw:
        name = outlet_from_title(src.get("title", ""))
        if not name:                              # Korean/non-standard titles: use domain
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
    exported += 1

print(f"\n  {exported} story/stories written.")
PYEOF

    local py_exit=$?
    if [ $py_exit -ne 0 ] || [ ! -s "$paths_tmp" ]; then
        echo -e "  ${RED}Export failed — no stories written.${NC}"
        rm -f "$paths_tmp"
        echo ""; return
    fi

    local all_ok=1

    if [ "$no_reflow" -eq 1 ]; then
        # Step 1a — no normalization: rename _raw.txt → _no_norm.txt directly
        echo ""
        while IFS= read -r raw_path; do
            [ -z "$raw_path" ] && continue
            local final_path="${raw_path%_raw.txt}_no_norm.txt"
            mv "$raw_path" "$final_path"
            if [ $? -eq 0 ]; then
                echo -e "  ${GREEN}✓${NC}  $final_path"
            else
                echo -e "  ${RED}Rename failed for: $raw_path${NC}"
                all_ok=0
            fi
        done < "$paths_tmp"
        rm -f "$paths_tmp"

        echo ""
        if [ "$all_ok" -eq 1 ]; then
            echo -e "  ${GREEN}Export complete (no normalization).${NC}"
        fi
    else
        # Step 1b — with normalization: reflow clips to target lengths
        local reflow_script="$SCRIPT_DIR/src/scripts/reflow_clips.py"
        if [ ! -f "$reflow_script" ]; then
            echo -e "  ${RED}ERROR: reflow_clips.py not found at $reflow_script${NC}"
            rm -f "$paths_tmp"
            echo ""; return
        fi

        : > "$LAST_EXPORT_FILE"
        while IFS= read -r raw_path; do
            [ -z "$raw_path" ] && continue
            local final_path="${raw_path%_raw.txt}_with_norm.txt"
            echo ""
            python3 "$reflow_script" "$raw_path" "$final_path"
            if [ $? -eq 0 ]; then
                echo "$final_path" >> "$LAST_EXPORT_FILE"
                rm -f "$raw_path"
            else
                echo -e "  ${RED}Reflow failed for: $raw_path${NC}"
                all_ok=0
            fi
        done < "$paths_tmp"
        rm -f "$paths_tmp"

        echo ""
        if [ "$all_ok" -eq 1 ]; then
            echo -e "  ${GREEN}Export complete.${NC}"
            echo -e "  Review the .txt files below, then run option 8 → c to generate Grok prompts."
            echo ""
            while IFS= read -r fp; do
                [ -n "$fp" ] && echo -e "    ${CYAN}→  $fp${NC}"
            done < "$LAST_EXPORT_FILE"
        fi
    fi
    echo ""
}

_generate_grok_prompts() {
    echo ""
    echo -e "  ${BOLD}Generating Grok prompts${NC}"
    echo ""
    read -p "  Filename to search in exports/ (or Enter to use last export): " input_path

    local list_file
    local tmp_list=""

    if [ -z "$input_path" ]; then
        # ── use last export ────────────────────────────────────
        if [ ! -f "$LAST_EXPORT_FILE" ] || [ ! -s "$LAST_EXPORT_FILE" ]; then
            echo -e "  ${RED}No last export found. Run option 8 → b first, or enter a filename.${NC}"
            echo ""; return
        fi
        list_file="$LAST_EXPORT_FILE"
        echo -e "  ${CYAN}Using last export list.${NC}"
    else
        # ── resolve from input ─────────────────────────────────
        local txt_path
        if [[ "$input_path" = /* ]]; then
            txt_path="$input_path"
        elif [[ "$input_path" = */* ]]; then
            txt_path="$SCRIPT_DIR/$input_path"
        else
            local matches
            matches=$(find "$SCRIPT_DIR/exports" -type f -name "$input_path" 2>/dev/null)
            local count
            count=$(echo "$matches" | grep -c . 2>/dev/null || echo 0)
            if [ "$count" -eq 0 ]; then
                echo -e "  ${RED}File not found in exports/: $input_path${NC}"
                echo ""; return
            elif [ "$count" -gt 1 ]; then
                echo -e "  ${YELLOW}Multiple matches — please be more specific:${NC}"
                while IFS= read -r m; do
                    echo "    ${m#$SCRIPT_DIR/}"
                done <<< "$matches"
                echo ""; return
            else
                txt_path="$matches"
                echo -e "  ${CYAN}Found: ${txt_path#$SCRIPT_DIR/}${NC}"
            fi
        fi

        if [ ! -f "$txt_path" ]; then
            echo -e "  ${RED}File not found: $txt_path${NC}"
            echo ""; return
        fi

        tmp_list=$(mktemp)
        echo "$txt_path" > "$tmp_list"
        list_file="$tmp_list"
    fi

    echo ""

    python3 - "$SCRIPT_DIR" "$list_file" <<'PYEOF'
import sys, os, re

script_dir       = sys.argv[1]
last_export_file = sys.argv[2]

with open(last_export_file, encoding="utf-8") as f:
    txt_paths = [line.strip() for line in f if line.strip()]

if not txt_paths:
    print("  No paths found in last export file.")
    sys.exit(1)

grok_template_path = os.path.join(script_dir, "src", "prompts", "grok_template.txt")
if not os.path.exists(grok_template_path):
    print(f"  ⚠  Grok template not found: {grok_template_path}")
    sys.exit(1)

with open(grok_template_path, encoding="utf-8") as f:
    grok_template = f.read()

total_prompts = 0
for txt_path in txt_paths:
    if not os.path.exists(txt_path):
        print(f"  ⚠  File not found: {txt_path}")
        continue

    # Parse clips: every non-header, non-separator, non-empty line is one clip.
    clips = []
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip('\n').rstrip()
            if line and line != '-' and not line.startswith('## ') and not line.startswith('###'):
                clips.append(line)

    if not clips:
        print(f"  ⚠  No clips found in: {txt_path}")
        continue

    story_name   = os.path.splitext(os.path.basename(txt_path))[0]
    category_dir = os.path.dirname(txt_path)

    for i, clip in enumerate(clips, 1):
        duration   = "6s" if len(clip) <= 35 else "10s"
        prompt_txt = grok_template.replace("{place_holder}", clip)
        grok_path  = os.path.join(category_dir, f"{story_name}_grok_{i}_{duration}.txt")
        with open(grok_path, "w", encoding="utf-8") as f:
            f.write(prompt_txt)

    n = len(clips)
    total_prompts += n
    print(f"  ✓  {n} Grok prompts → {category_dir}/")
    print(f"     {story_name}_grok_1_*.txt … _grok_{n}_*.txt")

print(f"\n  {total_prompts} total Grok prompt files generated.")
PYEOF

    [ -n "$tmp_list" ] && rm -f "$tmp_list"
    echo ""
}

configure_env() {
    echo ""
    echo -e "  ${BOLD}Configure Database (.env)${NC}"
    echo ""

    local env_file="$SCRIPT_DIR/.env"

    # Defaults — pre-populate from existing .env if present
    local default_host="localhost"
    local default_port="5432"
    local default_user="dbuser"
    local default_name="crawler_db"

    if [ -f "$env_file" ]; then
        local existing_url
        existing_url=$(grep "^CRAWLER_DB_URL=" "$env_file" 2>/dev/null | cut -d'=' -f2-)
        if [ -n "$existing_url" ]; then
            echo -e "  Current URL: ${CYAN}$(echo "$existing_url" | sed 's|:[^:@]*@|:***@|')${NC}"
            echo ""
            # Parse user, host, port, name from existing URL
            # Format: postgres://user:pass@host:port/name
            local after_scheme="${existing_url#postgres://}"
            local userinfo="${after_scheme%%@*}"
            local hostpart="${after_scheme#*@}"
            local parsed_user="${userinfo%%:*}"
            local parsed_host="${hostpart%%:*}"
            local portname="${hostpart#*:}"
            local parsed_port="${portname%%/*}"
            local parsed_name="${portname#*/}"
            [ -n "$parsed_user" ] && default_user="$parsed_user"
            [ -n "$parsed_host" ] && default_host="$parsed_host"
            [ -n "$parsed_port" ] && default_port="$parsed_port"
            [ -n "$parsed_name" ] && default_name="$parsed_name"
        fi
    fi

    echo -e "  Press ${CYAN}Enter${NC} to accept the default shown in brackets."
    echo ""

    read -p "  DB Host     [$default_host]: " db_host
    db_host="${db_host:-$default_host}"

    read -p "  DB Port     [$default_port]: " db_port
    db_port="${db_port:-$default_port}"

    read -p "  DB User     [$default_user]: " db_user
    db_user="${db_user:-$default_user}"

    read -p "  DB Name     [$default_name]: " db_name
    db_name="${db_name:-$default_name}"

    read -s -p "  DB Password: " db_pass
    echo ""

    local db_url="postgres://${db_user}:${db_pass}@${db_host}:${db_port}/${db_name}"
    # Crawler root: parent dir of story_engine, then /crawler
    local crawler_root
    crawler_root="$(dirname "$SCRIPT_DIR")/crawler"

    echo ""
    echo -e "  ${CYAN}Writing $env_file ...${NC}"

    cat > "$env_file" <<EOF
# story_engine environment configuration
# Generated by setup.sh — edit manually or re-run option 7 to update.

# Crawler PostgreSQL database (read-only)
CRAWLER_DB_URL=${db_url}

# Crawler root directory (for config files like auto_keywords.json)
CRAWLER_ROOT=${crawler_root}
EOF

    # Reload exported env vars in the current session
    set -a
    source "$env_file"
    set +a

    echo -e "  ${GREEN}Configuration saved.${NC}"
    echo ""

    # Quick connection test
    echo -e "  ${CYAN}Testing connection...${NC}"
    python3 - <<PYEOF
import sys
try:
    import psycopg2
    conn = psycopg2.connect("${db_url}")
    conn.close()
    print("  Connection: OK ✓")
except Exception as e:
    print(f"  Connection FAILED: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
    local rc=$?
    if [ $rc -ne 0 ]; then
        echo -e "  ${YELLOW}Tip: check host, port, user, password and that PostgreSQL is running.${NC}"
    fi
    echo ""
}

show_urls() {
    local host_ip=$(hostname -I | awk '{print $1}')
    echo ""
    echo -e "  ${BOLD}Story Engine API:${NC}"
    echo -e "    Local:   ${CYAN}http://localhost:$PORT${NC}"
    echo -e "    LAN:     ${CYAN}http://$host_ip:$PORT${NC}"
    echo ""
    echo -e "  ${BOLD}Endpoints:${NC}"
    echo -e "    Docs:           ${CYAN}http://$host_ip:$PORT/docs${NC}"
    echo -e "    Stories today:  ${CYAN}http://$host_ip:$PORT/api/stories/today${NC}"
    echo -e "    Story detail:   ${CYAN}http://$host_ip:$PORT/api/stories/{id}${NC}"
    echo -e "    Stories list:   ${CYAN}http://$host_ip:$PORT/api/stories${NC}"
    echo -e "    Engine status:  ${CYAN}http://$host_ip:$PORT/api/status${NC}"
    echo ""
    echo -e "  ${BOLD}trend_ui Stories tab:${NC}"
    echo -e "    ${CYAN}http://$host_ip:3000/app/stories${NC}"
    echo ""
}

analyze_story_clips() {
    echo ""
    echo -e "  ${BOLD}Analyze Story Clips — Speech Rate Test${NC}"
    echo ""
    read -p "  Story file path or filename: " input_path

    local txt_path

    if [[ "$input_path" = /* ]]; then
        # Absolute path — use as-is
        txt_path="$input_path"
    elif [[ "$input_path" = */* ]]; then
        # Relative path with directory component — resolve against SCRIPT_DIR
        txt_path="$SCRIPT_DIR/$input_path"
    else
        # Filename only — search under exports/
        local matches
        matches=$(find "$SCRIPT_DIR/exports" -type f -name "$input_path" 2>/dev/null)
        local count
        count=$(echo "$matches" | grep -c . 2>/dev/null || echo 0)

        if [ "$count" -eq 0 ]; then
            echo -e "  ${RED}File not found in exports/: $input_path${NC}"
            echo ""; return
        elif [ "$count" -gt 1 ]; then
            echo -e "  ${YELLOW}Multiple matches found — please use a more specific path:${NC}"
            while IFS= read -r m; do
                echo "    ${m#$SCRIPT_DIR/}"
            done <<< "$matches"
            echo ""; return
        else
            txt_path="$matches"
            local rel="${txt_path#$SCRIPT_DIR/}"
            echo -e "  ${CYAN}Found: $rel${NC}"
        fi
    fi

    if [ ! -f "$txt_path" ]; then
        echo -e "  ${RED}File not found: $txt_path${NC}"
        echo ""; return
    fi

    python3 - "$txt_path" <<'PYEOF'
import sys

txt_path = sys.argv[1]

clips = []
with open(txt_path, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip('\n').rstrip()
        # Skip separators, headers, hashtag source lines, and blanks
        if not line:
            continue
        if line == '-':
            continue
        if line.startswith('## ') or line.startswith('### '):
            continue
        clips.append(line)

if not clips:
    print("  ⚠  No clips found in file.")
    sys.exit(1)

clips_6s  = [(i+1, c) for i, c in enumerate(clips) if len(c) <= 40]
clips_10s = [(i+1, c) for i, c in enumerate(clips) if len(c) >  40]

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[0;36m"
YELLOW = "\033[1;33m"
GREEN  = "\033[0;32m"
DIM    = "\033[2m"

print(f"\n  {BOLD}File:{RESET} {txt_path}")
print(f"  Total clips parsed: {len(clips)}  "
      f"({len(clips_10s)} × 10s,  {len(clips_6s)} × 6s)\n")

def show_group(label, group, color):
    print(f"  {color}{BOLD}{label}{RESET}")
    for idx, text in group:
        print(f"  {DIM}Clip #{idx}  |  {len(text)} chars{RESET}")
        print(f"  {text}")
        print()

sep = f"  {CYAN}{'─'*57}{RESET}"

def show_section(title, clips):
    print(sep)
    print(f"  {BOLD}{title}{RESET}")
    print(sep)
    if not clips:
        print(f"  {DIM}(none){RESET}\n")
        return
    ranked = sorted(clips, key=lambda x: len(x[1]), reverse=True)
    top    = ranked[:3]
    bottom = ranked[-3:][::-1]  # ascending (fewest chars first)
    # avoid duplicates when fewer than 6 clips total
    bottom = [c for c in bottom if c not in top]
    show_group("Top 3  (most chars → longest speech)", top, YELLOW)
    if bottom:
        show_group("Bottom 3  (fewest chars → shortest speech)", bottom, GREEN)

# ── 10s clips ──────────────────────────────────────────────
show_section("10s clips  (> 40 chars)", clips_10s)

# ── 6s clips ───────────────────────────────────────────────
show_section("6s clips   (≤ 40 chars)", clips_6s)

print(sep)
PYEOF

    echo ""
}

show_menu() {
    echo -e "${BOLD}${CYAN}"
    cat << "EOF"
╔═══════════════════════════════════════════════════════════╗
║                                                           ║
║        Global Signal Radar — Story Engine                ║
║                                                           ║
╚═══════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"

    echo -e "${BOLD}Services:${NC}"
    echo "  1)  Start / Restart Service"
    echo "  2)  Stop Service"
    echo "  3)  Show Service Status"
    echo "  4)  Show API URLs"
    echo ""
    echo -e "${BOLD}Generation:${NC}"
    echo "  5)  Generate Stories (zh-Hans)"
    echo "  6)  Reset Last Batch  (delete & free articles for re-run)"
    echo "  8)  Export Stories    (a: no norm  |  b: normalized  |  c: Grok prompts)"
    echo "  9)  Analyze Story Clips  (min/max chars per 6s/10s — speech rate test)"
    echo ""
    echo -e "${BOLD}Configuration:${NC}"
    echo "  7)  Configure .env   (DB host / user / password)"
    echo ""
    echo "  0)  Exit"
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
}

# Main loop
while true; do
    show_menu
    read -p "Select option: " choice

    case $choice in
        1) start_service ;;
        2) stop_service ;;
        3) show_status ;;
        4) show_urls ;;
        5) generate_stories ;;
        6) reset_last_batch ;;
        7) configure_env ;;
        8) export_last_story ;;
        9) analyze_story_clips ;;
        0)
            echo ""
            echo -e "  ${CYAN}Exiting...${NC}"
            echo ""
            exit 0
            ;;
        *)
            echo -e "  ${RED}Invalid option: $choice${NC}"
            sleep 1
            ;;
    esac

    read -p "Press Enter to continue..."
done
