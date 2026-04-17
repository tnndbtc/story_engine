#!/bin/bash
# story_engine daily generation script
#
# Usage:
#   ./run_generate.sh                              # Default formats (1-9), base config
#   ./run_generate.sh 1-9                          # Formats 1 to 9, base config
#   ./run_generate.sh 10-20                        # Formats 10 to 20
#   ./run_generate.sh 1,5,10,15                    # Specific formats
#   ./run_generate.sh all                          # All 46 formats
#   ./run_generate.sh --dry-run                    # Preview only (no generation)
#   ./run_generate.sh 1-9 --profile run2_ai        # Apply AI channel overlay
#   ./run_generate.sh --deep-story --profile run2_ai   # Deep story only (no flat formats)
#
# The --profile flag picks a per-run overlay from
# story_engine/config/story_mix_<profile>.json.
# The --deep-story flag runs ONLY the hierarchical deep story pipeline,
# skipping all flat format generation entirely.
#
# Crontab examples (rotate formats + profiles across the day):
#   0 6 * * * /home/tnnd/data/code/story_engine/run_generate.sh 1-9   --profile run1_legacy
#   0 12 * * * /home/tnnd/data/code/story_engine/run_generate.sh 10-20 --profile run2_ai
#   0 18 * * * /home/tnnd/data/code/story_engine/run_generate.sh 21-30 --profile run3_world
#   0 22 * * * /home/tnnd/data/code/story_engine/run_generate.sh 31-46 --profile run4_business
#   0 6 * * * /home/tnnd/data/code/story_engine/run_generate.sh --deep-story --profile run3_world

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

# Parse arguments: first positional is FORMAT_INPUT (only if it is not a
# flag), then scan remaining args for --dry-run, --profile, --deep-story.
# Robust to any order: `--profile X`, `1-9 --profile X`, `--dry-run`, etc.
FORMAT_INPUT=""
if [ $# -gt 0 ] && [[ "$1" != --* ]]; then
    FORMAT_INPUT="$1"
    shift
fi

EXTRA_ARGS=""
PROFILE_ARGS=""
DEEP_STORY_MODE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)
            EXTRA_ARGS="$EXTRA_ARGS --dry-run"
            shift
            ;;
        --deep-story)
            DEEP_STORY_MODE=1
            shift
            ;;
        --profile)
            if [ -n "$2" ]; then
                PROFILE_ARGS="--config-profile $2"
                shift 2
            else
                echo "Error: --profile requires a value"
                exit 1
            fi
            ;;
        *)
            echo "Warning: unrecognized argument '$1'" >&2
            shift
            ;;
    esac
done

# In --deep-story mode, FORMAT_INPUT is ignored — run.py handles clustering only.
# In flat-format mode, default to 1-9 if no format range was given.
if [ "$DEEP_STORY_MODE" -eq 0 ]; then
    FORMAT_INPUT="${FORMAT_INPUT:-1-9}"
fi

# Expand format input to run.py format args (flat-format mode only)
FORMATS=""
if [ "$DEEP_STORY_MODE" -eq 0 ]; then
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
fi

echo "========================================="
echo "  story_engine — Generation Run"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
if [ "$DEEP_STORY_MODE" -eq 1 ]; then
    echo "  Mode: deep-story-only"
else
    echo "  Formats: $FORMAT_INPUT"
fi
if [ -n "$PROFILE_ARGS" ]; then
    echo "  Profile: ${PROFILE_ARGS#--config-profile }"
fi
echo "========================================="

# Generate Chinese stories (Channel 2)
echo ""
echo "--- Generating Chinese stories ---"
if [ "$DEEP_STORY_MODE" -eq 1 ]; then
    python "$SCRIPT_DIR/src/engine/run.py" --deep-story-only --lang zh --channel 2 $EXTRA_ARGS $PROFILE_ARGS
else
    python "$SCRIPT_DIR/src/engine/run.py" --format $FORMATS --lang zh --channel 2 $EXTRA_ARGS $PROFILE_ARGS
fi

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
    hier = conn.execute('SELECT COUNT(*) FROM hierarchical_stories WHERE story_set_id=? AND status=\"ready\"', (ss['id'],)).fetchone()[0]
    print(f'  Set #{ss[\"id\"]} ({_ts_to_iso(ss[\"batch_ts\"])})')
    print(f'  Flat stories — Ready: {ready}  Failed: {failed}')
    if hier > 0:
        print(f'  Deep stories — Ready: {hier}')
    if failed > 0:
        print(f'  ⚠ Check logs/generate.log for failure details')
else:
    print('  No story set created')
conn.close()
"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "========================================="
