#!/bin/bash
# story_engine daily generation script
#
# Add to crontab for daily automation:
#   0 6 * * * /home/tnnd/data/code/story_engine/run_generate.sh >> /home/tnnd/data/code/story_engine/logs/generate.log 2>&1
#
# Or run manually:
#   ./run_generate.sh              # English, all formats
#   ./run_generate.sh --lang zh    # Chinese, all formats
#   ./run_generate.sh --dry-run    # Preview selections only

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtualenv
if [ -d "/home/tnnd/.virtualenvs/crawl" ]; then
    source /home/tnnd/.virtualenvs/crawl/bin/activate
fi

# Create logs directory
mkdir -p "$SCRIPT_DIR/logs"

echo "========================================="
echo "  story_engine — Generation Run"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================="

# ─── Language config ─────────────────────────────────────
# Comment/uncomment lines below to enable/disable languages.
# To bring English back, just uncomment the EN line.
# ─────────────────────────────────────────────────────────

# Generate English stories (Channel 1)
#echo ""
#echo "--- Generating English stories ---"
#python "$SCRIPT_DIR/src/engine/run.py" --lang en --channel 1 "$@"

# Generate Chinese stories (Channel 2)
echo ""
echo "--- Generating Chinese stories ---"
python "$SCRIPT_DIR/src/engine/run.py" --lang zh --channel 2 "$@"

echo ""
echo "========================================="
echo "  Generation complete"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================="
