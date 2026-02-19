#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

PID_FILE=.kis_us_pid
if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE" || true)
  if [[ -n "${PID:-}" ]]; then
    echo "[stop_us_modules] stopping pid=$PID" >&2
    kill -TERM "$PID" 2>/dev/null || true
    sleep 3
    kill -KILL "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

# remove lock if present
rm -f .kis_bot_us.lock || true

echo "[stop_us_modules] done" >&2
