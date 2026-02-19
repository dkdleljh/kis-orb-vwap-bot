#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

export KIS_CONFIG_PATH="config.us.json"
export KIS_LOCK_FILE=".kis_bot_us.lock"

mkdir -p logs
nohup ./venv/bin/python main.py --modules > logs/nohup_us_modules.log 2>&1 &
echo $! > .kis_us_pid
