#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

export KIS_CONFIG_PATH="config.us.json"
export KIS_LOCK_FILE=".kis_bot_us.lock"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

export KIS_US_LIVE_CONFIRM="${KIS_US_LIVE_CONFIRM:-NO}"
export KIS_KILL_SWITCH="${KIS_KILL_SWITCH:-1}"

# Integrated margin mode: allow US buying power estimate from KRW cash when USD cash is 0.
export KIS_US_USE_INTEGRATED_MARGIN="1"
export KIS_US_MAX_POSITIONS="${KIS_US_MAX_POSITIONS:-5}"

mkdir -p logs
nohup ./venv/bin/python main.py --modules > logs/nohup_us_modules.log 2>&1 &
echo $! > .kis_us_pid
