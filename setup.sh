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

generate_stories() {
    echo ""
    echo -e "  ${BOLD}Generate Stories${NC}"
    echo ""
    echo "  All 9 formats: A-I"
    echo ""
    echo "   1) All formats"
    echo "   2) Explainer    (1 - 60秒解读)"
    echo "   3) Top 5        (2 - 今日热点5)"
    echo "   4) Radar        (3 - 全球雷达)"
    echo "   5) Regional     (4 - 区域视角)"
    echo "   6) Two Takes    (5 - 双面观点)"
    echo "   7) Pattern      (6 - 趋势分析)"
    echo "   8) Viral        (7 - 即将爆火)"
    echo "   9) Deep Dive    (8 - 深度报道)"
    echo "  10) Niche        (9 - 专题聚焦)"
    echo "  11) Dry run (preview selections only)"
    echo "   0) Cancel"
    echo ""
    read -p "  Select: " gen_choice

    local format_arg=""
    local extra_args=""

    case $gen_choice in
        1) format_arg="all" ;;
        2) format_arg="explainer" ;;
        3) format_arg="top5" ;;
        4) format_arg="radar" ;;
        5) format_arg="regional" ;;
        6) format_arg="two_takes" ;;
        7) format_arg="pattern" ;;
        8) format_arg="viral" ;;
        9) format_arg="deep_dive" ;;
        10) format_arg="niche" ;;
        11) format_arg="all"; extra_args="--dry-run" ;;
        0) echo -e "  ${YELLOW}Cancelled${NC}"; echo ""; return ;;
        *) echo -e "  ${RED}Invalid option${NC}"; echo ""; return ;;
    esac

    echo ""

    # Clear old stories before generating (unless dry-run)
    if [ -z "$extra_args" ]; then
        echo -e "  ${YELLOW}Clearing old stories...${NC}"
        cd "$SRC_DIR"
        python -c "
from db.models import get_connection
conn = get_connection()
count = conn.execute('SELECT COUNT(*) FROM stories').fetchone()[0]
conn.execute('DELETE FROM stories')
conn.commit()
conn.close()
print(f'  Deleted {count} old stories')
"
    fi

    echo -e "  ${CYAN}Generating stories (format=$format_arg, lang=zh)...${NC}"
    echo ""

    cd "$SRC_DIR"
    python engine/run.py --format "$format_arg" --lang zh --channel 2 $extra_args

    echo ""
    if [ -z "$extra_args" ]; then
        echo -e "  ${GREEN}Generation complete!${NC}"
        # Restart API if running so it picks up new stories
        if is_running; then
            echo -e "  ${CYAN}Stories are available via the API immediately.${NC}"
        else
            echo -e "  ${YELLOW}Note: Start the API service (option 1) to serve stories.${NC}"
        fi
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
    clear
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
