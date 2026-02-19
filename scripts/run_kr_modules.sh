#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

export KIS_CONFIG_PATH="config.kr.json"
export KIS_LOCK_FILE=".kis_bot_kr.lock"

mkdir -p logs
nohup ./venv/bin/python main.py --modules > logs/nohup_kr_modules.log 2>&1 &
echo $! > .kis_kr_pid
