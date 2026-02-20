#!/usr/bin/env python3
"""Apply next-day recommendations automatically (guarded).

This script reads reports/next_day_reco_YYYY-MM-DD.json and applies a *whitelisted*
subset of recommendations to config.kr.json (or config.json fallback).

Safety principles:
- Only whitelisted keys.
- Clamp to safe ranges.
- Require minimum confidence.
- Keep backups + append an audit log.
- Restart service after apply.

Usage:
  ./venv/bin/python scripts/apply_next_day_reco.py --date 2026-02-20
  ./venv/bin/python scripts/apply_next_day_reco.py --yesterday
  ./venv/bin/python scripts/apply_next_day_reco.py --yesterday --dry-run

Exit codes:
- 0: ok (applied or nothing)
- 2: missing input
- 3: refused by safety gate
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SAFE_RANGES: dict[str, tuple[float, float]] = {
    "trading.cash_reserve_pct": (0.10, 0.35),
    "trading.entry_budget_pct": (0.05, 0.35),
    "trading.scoring.kr_scalp_entry_threshold": (55.0, 85.0),
    "trading.symbol_cooldown_sec": (60.0, 3600.0),
    "trading.atr_min_percent": (0.10, 2.00),
}

# Minimum confidence score required per key (0..1). If missing, default below.
MIN_CONF: dict[str, float] = {
    "trading.cash_reserve_pct": 0.55,
    "trading.entry_budget_pct": 0.60,
    "trading.scoring.kr_scalp_entry_threshold": 0.55,
    "trading.symbol_cooldown_sec": 0.65,
    "trading.atr_min_percent": 0.65,
}

DEFAULT_MIN_CONF = 0.65

WHITELIST = set(SAFE_RANGES.keys())


def _kst_today() -> dt.date:
    kst = dt.timezone(dt.timedelta(hours=9))
    return dt.datetime.now(tz=kst).date()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _get_cfg_path() -> Path:
    # Prefer config.kr.json if present.
    p = ROOT / "config.kr.json"
    if p.exists():
        return p
    return ROOT / "config.json"


def _set_path(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = cfg
    for k in parts[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[parts[-1]] = value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (KST)")
    ap.add_argument("--yesterday", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-confidence", type=float, default=None)
    ap.add_argument("--restart-service", action="store_true", default=True)
    ap.add_argument("--service", default="kis-orb-bot.service")
    args = ap.parse_args()

    if args.yesterday:
        day = _kst_today() - dt.timedelta(days=1)
    elif args.date:
        day = dt.date.fromisoformat(args.date)
    else:
        day = _kst_today() - dt.timedelta(days=1)

    reco_path = ROOT / "reports" / f"next_day_reco_{day.isoformat()}.json"
    reco = _load_json(reco_path)
    if not reco:
        # Missing reco is not an error in unattended mode.
        print(f"OK: missing reco file: {reco_path}")
        return 0

    cfg_path = _get_cfg_path()
    cfg = _load_json(cfg_path)
    if cfg is None:
        print(f"REFUSED: cannot load config: {cfg_path}")
        return 3

    recos = list(reco.get("recommendations") or [])

    min_conf_global = args.min_confidence
    applied: list[tuple[str, Any, Any]] = []
    skipped: list[str] = []

    for r in recos:
        path = str(r.get("path") or "").strip()
        if path not in WHITELIST:
            continue

        try:
            conf = float(((r.get("confidence") or {}) or {}).get("score") or 0.0)
        except Exception:
            conf = 0.0

        need = MIN_CONF.get(path, (min_conf_global if min_conf_global is not None else DEFAULT_MIN_CONF))
        if conf < float(need):
            skipped.append(f"skip {path} conf={conf} < {need}")
            continue

        rec_val = r.get("recommended")
        try:
            v = float(rec_val)
        except Exception:
            skipped.append(f"skip {path} non-numeric")
            continue

        lo, hi = SAFE_RANGES[path]
        v2 = _clamp(v, lo, hi)

        # Read current (best-effort)
        cur_val = r.get("current")
        applied.append((path, cur_val, v2))
        _set_path(cfg, path, v2)

    if not applied:
        print("OK: nothing to apply")
        return 0

    # Backups
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = cfg_path.with_suffix(cfg_path.suffix + f".bak.{ts}")
    if not args.dry_run:
        shutil.copy2(cfg_path, backup)

        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Audit log
    audit_dir = ROOT / "reports" / "applied"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"apply_reco_{day.isoformat()}_{ts}.json"
    audit_payload = {
        "date": day.isoformat(),
        "reco_file": str(reco_path),
        "config": str(cfg_path),
        "backup": str(backup),
        "dry_run": bool(args.dry_run),
        "applied": [
            {"path": p, "from": frm, "to": to} for (p, frm, to) in applied
        ],
        "skipped": skipped,
    }
    if not args.dry_run:
        audit_path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Restart service
    if args.restart_service and not args.dry_run:
        subprocess.run(["sudo", "systemctl", "restart", args.service], check=False)

        # Smoke test + healthcheck (auto-rollback on failure)
        ok = True
        try:
            st = subprocess.run(["bash", "scripts/smoke_test.sh"], cwd=str(ROOT), capture_output=True, text=True)
            ok = ok and (st.returncode == 0)
        except Exception:
            ok = False

        try:
            hc = subprocess.run([str(ROOT / "venv" / "bin" / "python"), "scripts/healthcheck_kis.py"], cwd=str(ROOT), capture_output=True, text=True)
            ok = ok and (hc.returncode in (0, 1))
        except Exception:
            ok = False

        if not ok:
            # rollback
            try:
                shutil.copy2(backup, cfg_path)
            except Exception:
                pass
            subprocess.run(["sudo", "systemctl", "restart", args.service], check=False)
            print("OK: rolled back (smoke/healthcheck failed)")
            return 0

    # Print last line for OpenClaw cron wrapper
    print("DONE applied=" + str(len(applied)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
