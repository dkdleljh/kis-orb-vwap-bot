#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -x "${ROOT_DIR}/venv/bin/python" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

echo "[smoke] compile check"
"$PYTHON_BIN" -m py_compile main.py scheduler.py kis_auth.py kis_ws_marketdata.py kis_rest_orders.py

echo "[smoke] runtime guardrails check"
"$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
from main import TradingEngine

root = Path.cwd()

# NOTE: this smoke test runs in the user's shell environment.
# To make results deterministic, we explicitly set env vars for each case.

# Case 1: live enabled but NOT confirmed -> must be disabled
os.environ["KIS_LIVE_ENABLED"] = "1"
os.environ["KIS_LIVE_CONFIRM"] = ""  # force empty, not inherited
os.environ["KIS_KILL_SWITCH"] = "0"
engine = TradingEngine(str(root))
assert engine.live_ordering_enabled() is False, "live trading must stay disabled without KIS_LIVE_CONFIRM=YES"
status = engine.get_health_status()
assert "state" in status and "live_ordering_active" in status and "kill_switch" in status

# Case 2: live explicitly confirmed -> active
os.environ["KIS_LIVE_ENABLED"] = "1"
os.environ["KIS_LIVE_CONFIRM"] = "YES"
os.environ["KIS_KILL_SWITCH"] = "0"
engine2 = TradingEngine(str(root))
assert engine2.live_ordering_enabled() is True, "live trading should activate only when enabled+confirmed"

# Case 3: kill switch env hard-stop
os.environ["KIS_LIVE_ENABLED"] = "1"
os.environ["KIS_LIVE_CONFIRM"] = "YES"
os.environ["KIS_KILL_SWITCH"] = "1"
engine3 = TradingEngine(str(root))
assert engine3.kill_switch_on() is True, "env kill switch must force block"
assert engine3.live_ordering_enabled() is False, "kill switch must override live flags"

print("smoke checks passed")
PY

echo "[smoke] done"
