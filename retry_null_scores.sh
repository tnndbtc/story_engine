#!/bin/bash
# retry_null_scores.sh — Retry scoring for stories with NULL attractiveness_score.
#
# Runs up to 5 stories per invocation to stay within Claude rate limits.
# Designed for daily cron execution until all NULL scores are filled.
#
# Usage:
#   ./retry_null_scores.sh                  # published-only (default)
#   ./retry_null_scores.sh --all            # all unscored stories
#   ./retry_null_scores.sh --limit 10       # custom limit
#
# Crontab example (runs daily at 9am, fills 5 NULLs per day):
#   0 9 * * * /home/tnnd/data/code/story_engine/retry_null_scores.sh >> /home/tnnd/data/code/story_engine/logs/retry_null.log 2>&1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtualenv
if [ -d "/home/tnnd/.virtualenvs/crawl" ]; then
    source /home/tnnd/.virtualenvs/crawl/bin/activate
fi

# Load .env
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

LIMIT=5
PUBLISHED_FLAG="--published-only"

while [ $# -gt 0 ]; do
    case "$1" in
        --all)
            PUBLISHED_FLAG=""
            shift ;;
        --limit)
            LIMIT="$2"; shift 2 ;;
        *)
            shift ;;
    esac
done

echo "========================================"
echo "  retry_null_scores — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "  Limit: $LIMIT  Mode: ${PUBLISHED_FLAG:-(all unscored)}"
echo "========================================"

python3 "$SCRIPT_DIR/score_existing.py" $PUBLISHED_FLAG --limit "$LIMIT"

echo "========================================"
