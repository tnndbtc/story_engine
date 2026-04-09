#!/bin/bash
# story_engine daily generation script
#
# Usage:
#   ./run_generate.sh              # Default formats (1-9)
#   ./run_generate.sh 1-9          # Formats 1 to 9
#   ./run_generate.sh 10-20        # Formats 10 to 20
#   ./run_generate.sh 1,5,10,15    # Specific formats
#   ./run_generate.sh all          # All 46 formats
#   ./run_generate.sh --dry-run    # Preview only (no generation)
#
# Crontab examples (rotate formats across hours):
#   0 6 * * * /home/tnnd/data/code/story_engine/run_generate.sh 1-9
#   0 12 * * * /home/tnnd/data/code/story_engine/run_generate.sh 10-20
#   0 18 * * * /home/tnnd/data/code/story_engine/run_generate.sh 21-30
#   0 22 * * * /home/tnnd/data/code/story_engine/run_generate.sh 31-46

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtualenv
if [ -d "/home/tnnd/.virtualenvs/crawl" ]; then
    source /home/tnnd/.virtualenvs/crawl/bin/activate
fi

# Create logs directory
mkdir -p "$SCRIPT_DIR/logs"

# Map format numbers 1-9 to legacy names
declare -A LEGACY_MAP
LEGACY_MAP[1]="explainer" LEGACY_MAP[2]="top5" LEGACY_MAP[3]="radar"
LEGACY_MAP[4]="regional" LEGACY_MAP[5]="two_takes" LEGACY_MAP[6]="pattern"
LEGACY_MAP[7]="viral" LEGACY_MAP[8]="deep_dive" LEGACY_MAP[9]="niche"

# Parse arguments
FORMAT_INPUT="${1:-1-9}"
EXTRA_ARGS=""

if [ "$FORMAT_INPUT" = "--dry-run" ]; then
    FORMAT_INPUT="1-9"
    EXTRA_ARGS="--dry-run"
elif [ "$2" = "--dry-run" ]; then
    EXTRA_ARGS="--dry-run"
fi

# Expand format input to run.py format args
FORMATS=""
for part in $(echo "$FORMAT_INPUT" | tr ',' ' '); do
    if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
        for ((i=${BASH_REMATCH[1]}; i<=${BASH_REMATCH[2]}; i++)); do
            if [ "$i" -ge 1 ] && [ "$i" -le 9 ] && [ -n "${LEGACY_MAP[$i]}" ]; then
                FORMATS="$FORMATS ${LEGACY_MAP[$i]}"
            elif [ "$i" -ge 10 ] && [ "$i" -le 46 ]; then
                FORMATS="$FORMATS format_${i}"
            fi
        done
    elif [ "$part" = "all" ]; then
        FORMATS="all_extended"
        break
    elif [[ "$part" =~ ^[0-9]+$ ]]; then
        i=$part
        if [ "$i" -ge 1 ] && [ "$i" -le 9 ] && [ -n "${LEGACY_MAP[$i]}" ]; then
            FORMATS="$FORMATS ${LEGACY_MAP[$i]}"
        elif [ "$i" -ge 10 ] && [ "$i" -le 46 ]; then
            FORMATS="$FORMATS format_${i}"
        fi
    fi
done

if [ -z "$FORMATS" ]; then
    echo "Error: no valid formats from input '$FORMAT_INPUT'"
    exit 1
fi

echo "========================================="
echo "  story_engine — Generation Run"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "  Formats: $FORMAT_INPUT"
echo "========================================="

# Generate Chinese stories (Channel 2)
echo ""
echo "--- Generating Chinese stories ---"
python "$SCRIPT_DIR/src/engine/run.py" --format $FORMATS --lang zh --channel 2 $EXTRA_ARGS

# Show summary
echo ""
echo "========================================="
cd "$SCRIPT_DIR"
python -c "
import sys; sys.path.insert(0, 'src')
from db.models import get_connection, _ts_to_iso
conn = get_connection()
# Get the latest story set
ss = conn.execute('SELECT id, batch_ts FROM story_sets ORDER BY id DESC LIMIT 1').fetchone()
if ss:
    ready = conn.execute('SELECT COUNT(*) FROM stories WHERE batch_id=? AND status=\"ready\"', (ss['id'],)).fetchone()[0]
    failed = conn.execute('SELECT COUNT(*) FROM stories WHERE batch_id=? AND status=\"failed\"', (ss['id'],)).fetchone()[0]
    print(f'  Set #{ss[\"id\"]} ({_ts_to_iso(ss[\"batch_ts\"])})')
    print(f'  Ready: {ready}  Failed: {failed}')
    if failed > 0:
        print(f'  ⚠ Check logs/generate.log for failure details')
else:
    print('  No story set created')
conn.close()
"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================="
