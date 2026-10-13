#!/usr/bin/env bash
# Resumable collect driver for account-posts.py.
#
# Douyin upstream risk control only allows partial, paginated collection per
# window. This loop re-invokes the collector against the SAME evidence_dir
# after each cooling period (provider retry_after, with jitter), so a single
# logical "fetch everything" job survives UPSTREAM_RISK_CONTROL breaks across
# hours. After every round it runs viral-pick.py and reports the current top
# picks plus convergence, so analysis of the 5-8 viral videos can start long
# before full-history collection finishes.
#
# Usage:
#   bash collect-loop.sh <evidence_dir> [--max-rounds N] [--budget-seconds S]
#        [--min-pick N] [--max-pick N] [--ratio R]
#        [--on-converged CMD] [--python BIN] [--collector PATH] [--picker PATH]
#
# Environment:
#   ACCOUNT_POSTS_ROOT   Same root account-posts.py validates against
#   LOOP_LOG             Log file (default: <evidence_dir>/collect-loop.log)
#   ON_CONVERGED         Same as --on-converged: command run once when
#                        viral-pick first reports converged=true; receives the
#                        evidence_dir as $1 and the pick JSON file as $2.

set -uo pipefail

EVIDENCE_DIR="${1:-}"; shift || true
[[ -n "$EVIDENCE_DIR" ]] || { echo "usage: collect-loop.sh <evidence_dir> [options]" >&2; exit 64; }

MAX_ROUNDS=8
BUDGET_SECONDS=7200
MIN_PICK=5
MAX_PICK=8
RATIO=3.0
PYTHON_BIN="${PYTHON:-/data/agent-venv/bin/python}"
COLLECTOR="${ACCOUNT_POSTS_COLLECTOR:-/data/tools/account-posts.py}"
PICKER="${VIRAL_PICKER:-/data/tools/viral-pick.py}"
ON_CONVERGED="${ON_CONVERGED:-}"
PAGES_PER_ROUND=5

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-rounds) MAX_ROUNDS="$2"; shift 2 ;;
    --budget-seconds) BUDGET_SECONDS="$2"; shift 2 ;;
    --min-pick) MIN_PICK="$2"; shift 2 ;;
    --max-pick) MAX_PICK="$2"; shift 2 ;;
    --ratio) RATIO="$2"; shift 2 ;;
    --pages-per-round) PAGES_PER_ROUND="$2"; shift 2 ;;
    --on-converged) ON_CONVERGED="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --collector) COLLECTOR="$2"; shift 2 ;;
    --picker) PICKER="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

LOOP_LOG="${LOOP_LOG:-$EVIDENCE_DIR/collect-loop.log}"
mkdir -p "$(dirname "$LOOP_LOG")"
exec 9>"$EVIDENCE_DIR/collect-loop.lock"
flock -n 9 || { echo "collect-loop already running for $EVIDENCE_DIR" | tee -a "$LOOP_LOG" >&2; exit 75; }

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOOP_LOG" >&2; }
jitter() { echo $(( $1 + RANDOM % $2 )); }

# Extract one field from the collector/picker JSON on stdout.
field() { python3 -c 'import json,sys
try: d=json.load(open(sys.argv[1],encoding="utf-8"))
except Exception: print(""); raise SystemExit
v=d
for k in sys.argv[2:]:
    v=v.get(k) if isinstance(v,dict) else None
print(v if v is not None else "")' "$1" ${@:2} 2>/dev/null; }

START=$(date +%s)
ROUND=0
CONVERGED_FIRED=0

log "collect-loop start evidence_dir=$EVIDENCE_DIR max_rounds=$MAX_ROUNDS budget_seconds=$BUDGET_SECONDS"

while (( ROUND < MAX_ROUNDS )); do
  NOW=$(date +%s)
  if (( NOW - START >= BUDGET_SECONDS )); then log "budget exhausted after $ROUND rounds"; break; fi

  ROUND=$((ROUND + 1))
  SUMMARY="$EVIDENCE_DIR/.collect-round-$ROUND.json"
  log "round $ROUND: collecting up to $PAGES_PER_ROUND pages"
  if ! "$PYTHON_BIN" "$COLLECTOR" collect "$EVIDENCE_DIR" --max-pages "$PAGES_PER_ROUND" > "$SUMMARY" 2>>"$LOOP_LOG"; then
    log "round $ROUND: collector exited non-zero; summary: $(head -c 300 "$SUMMARY" 2>/dev/null)"
  fi

  STOP=$(field "$SUMMARY" stop_reason)
  CAN_RESUME=$(field "$SUMMARY" can_resume)
  RETRY_AFTER=$(field "$SUMMARY" retry_after_seconds)
  EXHAUSTED=$(field "$SUMMARY" pagination_exhausted)
  log "round $ROUND: stop_reason=$STOP can_resume=$CAN_RESUME retry_after=${RETRY_AFTER}s exhausted=$EXHAUSTED"

  # Rank what we have so far; report picks even when collection is cooling.
  PICKS="$EVIDENCE_DIR/.viral-pick-$ROUND.json"
  if [[ -x "$PICKER" || -f "$PICKER" ]]; then
    if "$PYTHON_BIN" "$PICKER" "$EVIDENCE_DIR" --min-pick "$MIN_PICK" --max-pick "$MAX_PICK" --ratio "$RATIO" > "$PICKS" 2>>"$LOOP_LOG"; then
      CONV=$(field "$PICKS" converged)
      NPICKS=$(field "$PICKS" pick_count)
      COVERAGE=$(field "$PICKS" coverage coverage_ratio)
      log "round $ROUND: picks=$NPICKS converged=$CONV coverage=$COVERAGE"
      if (( CONVERGED_FIRED == 0 )) && [[ "$CONV" == "True" ]] && [[ -n "$ON_CONVERGED" ]]; then
        CONVERGED_FIRED=1
        log "round $ROUND: viral shortlist converged; running on-converged hook"
        bash -c "$ON_CONVERGED \"$EVIDENCE_DIR\" \"$PICKS\"" >> "$LOOP_LOG" 2>&1 || log "on-converged hook exited non-zero (ignored)"
      fi
    else
      log "round $ROUND: viral-pick failed: $(head -c 200 "$PICKS" 2>/dev/null)"
    fi
  fi

  if [[ "$EXHAUSTED" == "True" ]]; then log "pagination exhausted; done"; break; fi
  if [[ "$CAN_RESUME" != "True" ]]; then log "not resumable (stop_reason=$STOP); done"; break; fi

  # page_budget -> brief pause; risk-control/rate-limit -> provider cooling + jitter.
  if [[ "$STOP" == "page_budget" ]]; then
    SLEEP=$(jitter 5 10)
  else
    BASE="${RETRY_AFTER:-0}"
    [[ "$BASE" =~ ^[0-9]+$ ]] || BASE=0
    FLOOR="${RETREAT_FLOOR_SECONDS:-1800}"
    if (( BASE < FLOOR )); then BASE=$FLOOR; fi   # server retreat rule floor (default 30 min)
    SLEEP=$(( BASE + $(jitter 30 60) ))
  fi
  log "round $ROUND: cooling ${SLEEP}s before next round"
  sleep "$SLEEP"
done

log "collect-loop end rounds=$ROUND"
