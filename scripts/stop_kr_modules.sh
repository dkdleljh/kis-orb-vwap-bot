#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE"

PID_FILE=.kis_kr_pid
if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE" || true)
  if [[ -n "${PID:-}" ]]; then
    kill -TERM "$PID" 2>/dev/null || true
    sleep 3
    kill -KILL "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

rm -f .kis_bot_kr.lock || true

echo "[stop_kr_modules] done" >&2
