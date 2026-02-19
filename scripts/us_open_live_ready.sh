#!/usr/bin/env bash
set -euo pipefail

# Make the bot "live-ready" 5 minutes before US regular session open.
# - Ensures modules daemon is running
# - Does NOT place orders

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/us_open_live_ready.log"

stamp() { date '+%Y-%m-%d %H:%M:%S%z'; }
log() { echo "[$(stamp)] $*" >> "$LOG_FILE"; }

# prevent overlap
LOCK="$ROOT/.us_open_live_ready.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

# load env (non-secret toggles)
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT/.env"
  set +a
fi

# NOTE: do not modify kill switch or STOP_TRADING.flag here.
# We only ensure the intended confirm flag is present.
export KIS_US_LIVE_CONFIRM="${KIS_US_LIVE_CONFIRM:-YES}"

PID="$(pgrep -f "$ROOT/venv/bin/python $ROOT/main\.py --modules" | head -n1 || true)"
if [[ -n "$PID" ]]; then
  log "modules already running (pid=$PID)"
else
  log "modules not running; starting nohup main.py --modules"
  nohup "$ROOT/venv/bin/python" "$ROOT/main.py" --modules >> "$ROOT/logs/nohup_modules.log" 2>&1 &
  sleep 2
  PID2="$(pgrep -f "$ROOT/venv/bin/python $ROOT/main\.py --modules" | head -n1 || true)"
  if [[ -n "$PID2" ]]; then
    log "modules started (pid=$PID2)"
  else
    log "ERROR: failed to start modules"
    exit 1
  fi
fi

# quick health snapshot (best-effort)
"$ROOT/venv/bin/python" scripts/healthcheck_kis.py >> "$LOG_FILE" 2>&1 || true
log "done"
