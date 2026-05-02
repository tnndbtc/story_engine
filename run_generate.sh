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

# Load .env (exports STORY_ENGINE_DB, AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, etc.)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
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
LANG_ARG="zh"
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
        --lang)
            if [ -n "$2" ]; then
                LANG_ARG="$2"
                shift 2
            else
                echo "Error: --lang requires a value (en or zh)"
                exit 1
            fi
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
                elif [ "$i" -ge 10 ] && [ "$i" -le 200 ]; then
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
            elif [ "$i" -ge 10 ] && [ "$i" -le 200 ]; then
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
echo "  Lang:    $LANG_ARG"
if [ -n "$PROFILE_ARGS" ]; then
    echo "  Profile: ${PROFILE_ARGS#--config-profile }"
fi
echo "========================================="

# Generate stories (language determined by --lang flag, default: zh / Channel 2)
echo ""
echo "--- Generating stories (lang=$LANG_ARG) ---"
if [ "$DEEP_STORY_MODE" -eq 1 ]; then
    python "$SCRIPT_DIR/src/engine/run.py" --deep-story-only --lang "$LANG_ARG" --channel 2 $EXTRA_ARGS $PROFILE_ARGS
else
    python "$SCRIPT_DIR/src/engine/run.py" --format $FORMATS --lang "$LANG_ARG" --channel 2 $EXTRA_ARGS $PROFILE_ARGS
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

# ── Post-generation: Export + Pipeline ───────────────────────────────────────
PIPE_DIR="$(dirname "$SCRIPT_DIR")/pipe"
EXPORT_SCRIPT="$SCRIPT_DIR/export_latest_story.sh"
RESOURCES_DIR="$PIPE_DIR/projects/resources/news"
BASE_CONFIG="$PIPE_DIR/code/http/simple_narration.config.json"

echo ""
echo "==========================================="
echo "  Post-generation: Export + Pipeline"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "==========================================="

# Guard: verify latest story_set is from this run (< 2h old) and has
# at least 1 ready hierarchical story. Outputs "1:<set_id>" or "0:0".
DB_PATH="${STORY_ENGINE_DB:-$SCRIPT_DIR/db.sqlite3}"
GUARD_RESULT=$(python3 - "$DB_PATH" <<'PYEOF'
import sqlite3, sys, time
db = sys.argv[1]
conn = sqlite3.connect(db)
ss = conn.execute(
    'SELECT id, batch_ts FROM story_sets ORDER BY id DESC LIMIT 1'
).fetchone()
if not ss:
    print("0:0"); sys.exit()
age_ms = time.time() * 1000 - int(ss[1])
if age_ms > 7_200_000:
    print("0:0"); sys.exit()
hier = conn.execute(
    'SELECT COUNT(*) FROM hierarchical_stories '
    'WHERE story_set_id=? AND status="ready"', (ss[0],)
).fetchone()[0]
print(f"1:{ss[0]}" if hier > 0 else "0:0")
conn.close()
PYEOF
)
GEN_OK="${GUARD_RESULT%%:*}"
STORY_SET_ID="${GUARD_RESULT##*:}"

if [ "$GEN_OK" != "1" ]; then
    echo "  WARNING: generation produced 0 ready stories — skipping pipeline"
else
    echo ""
    echo "--- Step A: Export latest story to .txt ---"
    if bash "$EXPORT_SCRIPT"; then
        STORY_TXT="$(head -1 "$SCRIPT_DIR/.last_export_txt" 2>/dev/null)"
        if [ -n "$STORY_TXT" ] && [ -f "$STORY_TXT" ]; then
            echo "  Exported: $STORY_TXT"

            # Derive category from the export path directory name
            # exports/<category>/<name>.txt → category
            CATEGORY="$(basename "$(dirname "$STORY_TXT")")"

            # Select matching background image (.png); fall back to news_general.png
            BG_CANDIDATE="$RESOURCES_DIR/news_${CATEGORY}.png"
            if [ -f "$BG_CANDIDATE" ]; then
                BG_PATH="$BG_CANDIDATE"
            else
                BG_PATH="$RESOURCES_DIR/news_general.png"
                echo "  No news_${CATEGORY}.png — using news_general.png"
            fi
            echo "  Background: $BG_PATH"

            # Patch config: copy base to /tmp, set locale + background + active voice.
            # Voice alternates by story_set_id parity:
            #   zh odd  → Xiaoxiao DragonHD (female) / zh even → Yunyang Customerservice (male)
            #   en odd  → Aria Narration (female)    / en even → Guy Newscast (male)
            # The opposite-language narrator block is removed so the validator
            # does not require an enabled voice for the unused locale.
            TMP_CONFIG="/tmp/simple_narration_${CATEGORY}.config.json"
            cp "$BASE_CONFIG" "$TMP_CONFIG"
            python3 -c "
import json
cfg = json.load(open('$TMP_CONFIG'))
cfg['background'] = '$BG_PATH'
use_female = ($STORY_SET_ID % 2 == 1)
lang = '$LANG_ARG'
if lang == 'en':
    cfg['locale'] = 'en-US'
    voices = cfg['narrator'].get('en-US', {})
    for name, v in voices.items():
        if name == 'Aria Narration':  v['enabled'] = use_female
        elif name == 'Guy Newscast':  v['enabled'] = not use_female
        else:                          v['enabled'] = False
    cfg['narrator'].pop('zh-Hans', None)
else:
    cfg['locale'] = 'zh-Hans'
    voices = cfg['narrator'].get('zh-Hans', {})
    for name, v in voices.items():
        if name == 'Xiaoxiao DragonHD':         v['enabled'] = use_female
        elif name == 'Yunyang Customerservice':  v['enabled'] = not use_female
        else:                                     v['enabled'] = False
    cfg['narrator'].pop('en-US', None)
json.dump(cfg, open('$TMP_CONFIG', 'w'), indent=2, ensure_ascii=False)
"
            echo "  Config:     $TMP_CONFIG"
            if [ "$LANG_ARG" = "en" ]; then
                if [ $(( STORY_SET_ID % 2 )) -eq 1 ]; then
                    echo "  Voice:      Aria Narration (female)"
                else
                    echo "  Voice:      Guy Newscast (male)"
                fi
            else
                if [ $(( STORY_SET_ID % 2 )) -eq 1 ]; then
                    echo "  Voice:      Xiaoxiao DragonHD (female)"
                else
                    echo "  Voice:      Yunyang Customerservice (male)"
                fi
            fi

            echo ""
            echo "--- Step B: Run narration pipeline ---"
            source /home/tnnd/.virtualenvs/pipe/bin/activate
            cd "$PIPE_DIR"
            ./simple_run.sh \
                --story  "$STORY_TXT" \
                --config "$TMP_CONFIG" \
              >> "$SCRIPT_DIR/logs/pipeline.log" 2>&1 \
              || echo "  WARNING: pipeline error — see logs/pipeline.log"
        else
            echo "  WARNING: export OK but .last_export_txt empty or missing"
        fi
    else
        echo "  WARNING: export failed — skipping pipeline"
    fi
fi
