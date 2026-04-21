#!/bin/bash
# story_engine service manager

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$SCRIPT_DIR/.api_server.pid"
LOG_FILE="$LOG_DIR/api_server.log"
PORT=8003

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

    local db_path="${STORY_ENGINE_DB:-$SCRIPT_DIR/db.sqlite3}"
    local export_dir="$SCRIPT_DIR/exports"

    if [ ! -f "$db_path" ]; then
        echo -e "  ${RED}ERROR: database not found at $db_path${NC}"
        echo ""; return
    fi

    # Ask how many stories
    read -p "  How many recent stories to export? [1]: " n_choice
    n_choice="${n_choice:-1}"
    if ! [[ "$n_choice" =~ ^[1-9][0-9]*$ ]]; then
        echo -e "  ${RED}Invalid number: $n_choice${NC}"; echo ""; return
    fi

    # Ask format
    echo ""
    echo "  Format:"
    echo "    1) Markdown (.md)"
    echo "    2) HTML     (.html)"
    echo "    3) Both"
    echo "    4) Plain text (.txt)  — narration script only"
    echo "    0) Cancel"
    echo ""
    read -p "  Select: " fmt_choice
    case $fmt_choice in
        0) echo -e "  ${YELLOW}Cancelled${NC}"; echo ""; return ;;
        1|2|3|4) ;;
        *) echo -e "  ${RED}Invalid option${NC}"; echo ""; return ;;
    esac

    mkdir -p "$export_dir"

    echo ""
    echo -e "  ${CYAN}Fetching $n_choice story/stories...${NC}"
    echo ""

    python3 - "$db_path" "$export_dir" "$fmt_choice" "$n_choice" <<'PYEOF'
import sys, json, os, re
from datetime import datetime, timezone

db_path, export_dir, fmt, n_arg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
n_stories = max(1, int(n_arg))

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
    """Remove common markdown artifacts so output is plain prose."""
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)   # bold / italic
    text = re.sub(r'`(.+?)`', r'\1', text)                 # inline code
    text = re.sub(r'https?://\S+', '', text)               # URLs
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)  # [text](url)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE) # headings
    text = text.strip()
    return text

exported = 0
for row in rows:
    sid, set_id, batch_ts, lang, channel, status, raw_ds, raw_ss, gen_at, profile_id = row
    ds = json.loads(raw_ds) if raw_ds else {}
    ss = json.loads(raw_ss) if raw_ss else []

    dt_utc   = datetime.fromtimestamp(gen_at / 1000, tz=timezone.utc)
    ts       = dt_utc.strftime("%Y-%m-%d %H:%M UTC")
    date_slug = dt_utc.strftime("%Y-%m-%d")
    time_slug = dt_utc.strftime("%H%M")

    # Extract category from profile_id (e.g. "run5_entertainment" → "entertainment")
    if profile_id:
        category = re.sub(r'^run\d+_', '', profile_id)
    else:
        category = "unknown"

    base = os.path.join(export_dir, f"story_{date_slug}_{time_slug}utc_{category}_{sid}")

    title   = ds.get("title", "Untitled")
    hook    = ds.get("hook", "")
    bullets = ds.get("bullets", [])
    twist   = ds.get("twist", "")
    sources = ds.get("sources", [])
    cluster = ds.get("cluster_size", len(sources))

    # ── Markdown ────────────────────────────────────────────────────────────────
    def write_md():
        lines = []
        lines.append(f"# {title}\n")
        lines.append(f"> **Generated:** {ts} | **Language:** {lang} | **Channel:** {channel} | **Category:** {category} | **Story ID:** {sid} | **Sources:** {cluster}\n")
        lines.append("---\n")
        lines.append("## Hook\n")
        lines.append(f"{hook}\n")
        lines.append("---\n")
        lines.append("## Key Points\n")
        for b in bullets:
            lines.append(f"- {b}\n")
        lines.append("\n---\n")
        lines.append("## Twist\n")
        lines.append(f"{twist}\n")
        lines.append("---\n")
        lines.append("## Sources\n")
        for i, src in enumerate(sources, 1):
            role = src.get("role", "")
            hot  = src.get("hotness", "")
            note = f"*(hotness: {hot})*" if hot and not role else f"*({role})*" if role else ""
            lines.append(f"{i}. [{src.get('title','Source')}]({src.get('url','')}) {note}\n")
        if ss:
            lines.append("\n---\n")
            lines.append("## Supporting Stories\n")
            for i, s in enumerate(ss, 1):
                lines.append(f"\n### {i}. {s.get('title','')}\n")
                lines.append(f"**Summary:** {s.get('summary','')}\n\n")
                lines.append(f"**Why It Matters:** {s.get('why_it_matters','')}\n\n")
                for src in s.get("sources", []):
                    lines.append(f"**Source:** [{src.get('title','Source')}]({src.get('url','')})\n")
                lines.append("\n---\n")
        lines.append(f"\n*Exported from Story Engine — Story Set #{set_id} | Category: {category} | Cluster size: {cluster}*\n")
        path = base + ".md"
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"  ✓  Markdown : {path}")

    # ── HTML ─────────────────────────────────────────────────────────────────────
    def write_html():
        def src_badge(src):
            role = src.get("role", "")
            hot  = src.get("hotness", "")
            if hot and not role:
                return f'<span class="hotness">🔥 {hot}</span>'
            if role:
                return f'<span class="role">{role}</span>'
            return ""

        def esc(s):
            return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

        bullets_html = "\n".join(f"<li>{esc(b)}</li>" for b in bullets)
        sources_html = ""
        for src in sources:
            sources_html += f"""
    <div class="source-item">
      {src_badge(src)}
      <a href="{esc(src.get('url',''))}" target="_blank">{esc(src.get('title','Source'))}</a>
    </div>"""
        ss_html = ""
        for s in ss:
            src_links = "".join(
                f'<a href="{esc(src.get("url",""))}" target="_blank">{esc(src.get("title","Source"))}</a><br>'
                for src in s.get("sources", [])
            )
            ss_html += f"""
    <div class="support-card">
      <h3>{esc(s.get('title',''))}</h3>
      <div class="label">Summary</div>
      <p>{esc(s.get('summary',''))}</p>
      <div class="label">Why It Matters</div>
      <p>{esc(s.get('why_it_matters',''))}</p>
      <div class="label">Source</div>
      {src_links}
    </div>"""

        html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{esc(title)}</title>
  <style>
    :root{{--bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--accent:#e84545;--accent2:#f0a500;--text:#e8eaf0;--muted:#8892a4;--border:#2e3347;}}
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{font-family:-apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;background:var(--bg);color:var(--text);line-height:1.8;padding:2rem 1rem;}}
    .wrapper{{max-width:780px;margin:0 auto;}}
    .meta{{display:flex;flex-wrap:wrap;gap:.5rem;font-size:.75rem;color:var(--muted);margin-bottom:2rem;}}
    .meta span{{background:var(--surface2);padding:.2rem .6rem;border-radius:999px;}}
    h1{{font-size:clamp(1.5rem,5vw,2.2rem);font-weight:800;border-left:4px solid var(--accent);padding-left:1rem;margin-bottom:1.5rem;}}
    .hook{{background:var(--surface);border:1px solid var(--border);border-top:3px solid var(--accent);border-radius:8px;padding:1.25rem 1.5rem;font-size:1.05rem;font-style:italic;color:#ccd0db;margin-bottom:2rem;}}
    h2{{font-size:1rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--accent2);margin:2rem 0 1rem;}}
    .bullets{{list-style:none;display:flex;flex-direction:column;gap:.9rem;}}
    .bullets li{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem 1.2rem 1rem 3rem;position:relative;}}
    .bullets li::before{{content:"▸";position:absolute;left:1rem;color:var(--accent);top:1rem;}}
    .twist{{background:linear-gradient(135deg,#1e1030 0%,#1a1d27 100%);border:1px solid #3a2a55;border-left:4px solid #9b59b6;border-radius:8px;padding:1.25rem 1.5rem;font-size:1.05rem;margin:2rem 0;}}
    .sources{{display:flex;flex-direction:column;gap:.6rem;}}
    .source-item{{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:.75rem 1rem;font-size:.875rem;}}
    .source-item a{{color:#5b9cf6;text-decoration:none;}} .source-item a:hover{{text-decoration:underline;}}
    .hotness{{float:right;background:var(--accent);color:#fff;font-size:.7rem;padding:.15rem .45rem;border-radius:999px;margin-left:.5rem;}}
    .role{{float:right;background:var(--surface2);color:var(--muted);font-size:.7rem;padding:.15rem .45rem;border-radius:999px;margin-left:.5rem;}}
    .supporting{{display:flex;flex-direction:column;gap:1.25rem;}}
    .support-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1.2rem 1.4rem;}}
    .support-card h3{{font-size:1rem;font-weight:700;margin-bottom:.5rem;}}
    .label{{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--accent2);margin:.8rem 0 .25rem;}}
    .support-card p{{font-size:.9rem;color:#b0b8c8;}}
    .support-card a{{color:#5b9cf6;font-size:.85rem;text-decoration:none;}} .support-card a:hover{{text-decoration:underline;}}
    footer{{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--border);font-size:.75rem;color:var(--muted);text-align:center;}}
  </style>
</head>
<body><div class="wrapper">
  <div class="meta">
    <span>📅 {ts}</span><span>🌐 {lang}</span><span>📡 Channel {channel}</span>
    <span>🏷 {category}</span><span>🆔 Story #{sid}</span><span>🔗 {cluster} sources</span><span>📦 Set #{set_id}</span>
  </div>
  <h1>{esc(title)}</h1>
  <div class="hook">{esc(hook)}</div>
  <h2>Key Points</h2>
  <ul class="bullets">{bullets_html}</ul>
  <h2>Twist</h2>
  <div class="twist">{esc(twist)}</div>
  <h2>Sources</h2>
  <div class="sources">{sources_html}</div>
  {'<h2>Supporting Stories</h2><div class="supporting">' + ss_html + '</div>' if ss else ''}
  <footer>Exported from Story Engine — Story Set #{set_id} · Category: {category} · Cluster size: {cluster} · Generated {ts}</footer>
</div></body></html>"""

        path = base + ".html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  ✓  HTML     : {path}")

    # ── Plain text (narration script) ────────────────────────────────────────────
    def write_txt():
        paragraphs = []

        # Deep story
        if title:
            paragraphs.append(f"## {strip_md(title)}")

        if hook:
            paragraphs.append(strip_md(hook))

        for b in bullets:
            clean = strip_md(str(b))
            if clean:
                paragraphs.append(clean)

        if twist:
            paragraphs.append(strip_md(twist))

        # Supporting stories
        for s in ss:
            s_title = strip_md(s.get("title", ""))
            summary = strip_md(s.get("summary", ""))
            why     = strip_md(s.get("why_it_matters", ""))
            if s_title:
                paragraphs.append(f"## {s_title}")
            if summary:
                paragraphs.append(summary)
            if why:
                paragraphs.append(why)

        path = base + ".txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n-\n".join(paragraphs))
            f.write("\n")
        print(f"  ✓  Plain txt : {path}")

    if fmt in ("1", "3"):
        write_md()
    if fmt in ("2", "3"):
        write_html()
    if fmt == "4":
        write_txt()
    exported += 1

print(f"\n  {exported} story/stories exported to: {export_dir}")
PYEOF

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
    echo "  8)  Export Stories    (.md / .html)"
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
