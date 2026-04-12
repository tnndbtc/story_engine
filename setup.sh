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
    CRAWLER_DB=/home/tnnd/data/code/crawler/db.sqlite3 \
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
