#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/zenith/Desktop/kis_orb_vwap_bot"
cd "$ROOT"

# Load .env (for any toggles; secrets are expected in the file too)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PY="$ROOT/venv/bin/python"

out="$($PY scripts/monitor_schd_ord_psbl.py 2>/dev/null || true)"

# 1) Quiet exit when no output (no change)
if [[ -z "${out}" ]]; then
  exit 0
fi

# 2) Closed day/weekend: one-line SKIP (already de-spammed inside the script)
if [[ "${out}" == SKIP* ]]; then
  echo "$out"
  exit 0
fi

# 3) JSON change payload emitted by monitor_schd_ord_psbl.py
transition="$($PY - <<'PY'
import json,sys
try:
    obj=json.loads(sys.stdin.read())
    print('1' if obj.get('transition',{}).get('ord_psbl_0_to_pos') else '0')
except Exception:
    print('0')
PY
<<<"$out")"

# Only notify/recover on ord_psbl_qty: 0 -> >0 transition (and ovrs_cblc_qty>0)
if [[ "$transition" != "1" ]]; then
  exit 0
fi

# Extract summary fields
summary="$($PY - <<'PY'
import json,sys
obj=json.loads(sys.stdin.read())
c=obj['curr']
p=obj['prev']
print(f"prev(ord_psbl_qty={p.get('ord_psbl_qty')}, ovrs_cblc_qty={p.get('ovrs_cblc_qty')}, excg={p.get('ovrs_excg_cd')}, loan={p.get('loan_type_cd')})")
print(f"curr(ord_psbl_qty={c.get('ord_psbl_qty')}, ovrs_cblc_qty={c.get('ovrs_cblc_qty')}, excg={c.get('ovrs_excg_cd')}, loan={c.get('loan_type_cd')})")
PY
<<<"$out")"

# --- US modules process check / restart ---
PID_FILE="$ROOT/.kis_us_pid"
LOG_FILE="$ROOT/logs/nohup_us_modules.log"

is_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  if [[ -f "$PID_FILE" ]]; then
    tr -d ' \t\n\r' < "$PID_FILE" || true
  fi
}

start_modules() {
  "$ROOT/scripts/run_us_modules.sh" >/dev/null 2>&1 || true
  sleep 6
}

stop_modules() {
  local pid="$1"
  if is_alive "$pid"; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..20}; do
      if ! is_alive "$pid"; then
        return 0
      fi
      sleep 0.5
    done
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

pid="$(read_pid)"
modules_action="alive"
if ! is_alive "$pid"; then
  modules_action="started"
  start_modules
  pid="$(read_pid)"
fi

# Fatal log signature check (best-effort): if found, delete token cache and restart once
fatal_hit="0"
if [[ -f "$LOG_FILE" ]]; then
  if tail -n 400 "$LOG_FILE" | rg -n "EGW00123|invalid token|token expired|치명|CRITICAL" >/dev/null 2>&1; then
    fatal_hit="1"
  fi
fi

restarted="0"
if [[ "$fatal_hit" == "1" ]]; then
  # one-time restart path
  oldpid="$(read_pid)"
  stop_modules "$oldpid"
  rm -f "$ROOT/.kis_token_cache.json" || true
  modules_action="restarted(token-cache-cleared)"
  start_modules
  restarted="1"
  pid="$(read_pid)"
fi

ready="NOT READY"
if is_alive "$pid"; then
  ready="READY"
fi

# Optional healthcheck snapshot (best-effort, no spam)
hc=""
if [[ "$ready" == "READY" ]]; then
  hc_out="$($PY scripts/healthcheck_kis.py 2>/dev/null | tail -n 5 || true)"
  if [[ -n "$hc_out" ]]; then
    hc=" | health: $(echo "$hc_out" | tr '\n' ' ' | sed 's/  */ /g')"
  fi
fi

# Final message (this stdout will be delivered by cron)
echo "SCHD ord_psbl_qty 풀림 → 자동매매 가능"
echo "$summary"
echo "US modules: $ready (pid=${pid:-?}, action=$modules_action)${hc}"
