"""KIS bot operational healthcheck.

Goals:
- Return machine-friendly JSON for external monitors (OpenClaw/Jarvis).
- Provide exit code:
  - 0: OK
  - 1: DEGRADED (running but needs attention)
  - 2: CRITICAL (not running / health file missing / invalid)

Heuristics (recommended defaults):
- Process must be running.
- health_status.json must exist and be recent.
- ws_connected is *not* required outside KR regular session.

Usage:
  cd ~/Desktop/kis_orb_vwap_bot && venv/bin/python scripts/healthcheck_kis.py

Env:
- KIS_HEALTH_MAX_AGE_SEC (default 300)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure project root is importable when running from scripts/
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from healthcheck import read_health_status


def _now_ts() -> float:
    return time.time()


def _pgrep_lines(pattern: str) -> list[str]:
    try:
        out = subprocess.check_output(["pgrep", "-a", "-f", pattern], text=True).strip()
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except subprocess.CalledProcessError:
        return []
    except Exception:
        return []


def _parse_iso(ts: str) -> float | None:
    try:
        # e.g. 2026-02-17T12:57:07.663365+09:00
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def main() -> int:
    base_dir = os.getcwd()

    pids = _pgrep_lines(r"kis_orb_vwap_bot/venv/bin/python.*main\.py")
    running = bool(pids)

    max_age = int(os.environ.get("KIS_HEALTH_MAX_AGE_SEC", "300"))

    hs = read_health_status(base_dir)

    # base payload
    payload: dict[str, Any] = {
        "ok": False,
        "verdict": "CRITICAL",
        "running": running,
        "process": pids[:3],
        "health": hs,
        "checked_at_epoch": _now_ts(),
    }

    if not running:
        payload["reason"] = "process_not_running"
        print(json.dumps(payload, ensure_ascii=True))
        return 2

    if not isinstance(hs, dict) or not hs.get("ok"):
        payload["reason"] = "health_status_bad"
        print(json.dumps(payload, ensure_ascii=True))
        return 2

    ts_raw = str(hs.get("timestamp") or "")
    ts_epoch = _parse_iso(ts_raw) if ts_raw else None
    if ts_epoch is None:
        payload["reason"] = "health_timestamp_missing_or_invalid"
        payload["verdict"] = "DEGRADED"
        print(json.dumps(payload, ensure_ascii=True))
        return 1

    age = _now_ts() - ts_epoch
    payload["health_age_sec"] = age

    # live flags check
    live_enabled = bool(hs.get("live_enabled"))
    live_confirmed = bool(hs.get("live_confirmed"))
    live_ordering = bool(hs.get("live_ordering_active"))

    if age > max_age:
        payload["reason"] = f"health_stale(age_sec={int(age)})"
        payload["verdict"] = "DEGRADED"
        print(json.dumps(payload, ensure_ascii=True))
        return 1

    if not (live_enabled and live_confirmed):
        payload["reason"] = "live_flags_not_confirmed"
        payload["verdict"] = "DEGRADED"
        print(json.dumps(payload, ensure_ascii=True))
        return 1

    # OK
    payload["ok"] = True
    payload["verdict"] = "OK"
    payload["summary"] = {
        "state": hs.get("state"),
        "ws_connected": bool(hs.get("ws_connected")),
        "kill_switch": bool(hs.get("kill_switch")),
        "trades_today": hs.get("trades_today"),
        "max_trades_per_day": hs.get("max_trades_per_day"),
        "live_ordering_active": live_ordering,
    }

    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
