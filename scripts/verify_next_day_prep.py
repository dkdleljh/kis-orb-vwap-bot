#!/usr/bin/env python3
"""Verify next-day prep artifacts and event quality checks.

Checks:
- reports/trade_report_YYYY-MM-DD.md exists
- reports/next_day_reco_YYYY-MM-DD.json and .md exist
- any qty=0 RiskDecision has context payload
- Signal context includes min_score and reasons at least once

By default, if report/reco are missing it auto-generates them with defaults.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]


def _iter_events(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _run(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), check=False, capture_output=True, text=True)
        return proc.returncode == 0
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (KST). default=today", default=None)
    ap.add_argument("--no-autogen", action="store_true", help="do not auto-generate missing report/reco")
    args = ap.parse_args()

    kst = dt.timezone(dt.timedelta(hours=9))
    if args.date:
        day = dt.date.fromisoformat(args.date)
    else:
        day = dt.datetime.now(tz=kst).date()

    day_s = day.isoformat()
    ymd = day.strftime("%Y%m%d")

    events_path = ROOT / "logs" / "events" / ymd / "events.jsonl"
    report_path = ROOT / "reports" / f"trade_report_{day_s}.md"
    reco_json = ROOT / "reports" / f"next_day_reco_{day_s}.json"
    reco_md = ROOT / "reports" / f"next_day_reco_{day_s}.md"

    autogen = not args.no_autogen
    autogen_done: list[str] = []

    if autogen and not report_path.exists():
        if _run([sys.executable, "scripts/daily_trade_report.py", "--date", day_s]):
            autogen_done.append("trade_report")

    if autogen and (not reco_json.exists() or not reco_md.exists()):
        if _run([sys.executable, "scripts/recommend_next_day.py", "--date", day_s]):
            autogen_done.append("next_day_reco")

    checks: list[tuple[str, bool, str]] = []

    checks.append(("trade_report_exists", report_path.exists(), str(report_path)))
    checks.append(("next_day_reco_json_exists", reco_json.exists(), str(reco_json)))
    checks.append(("next_day_reco_md_exists", reco_md.exists(), str(reco_md)))

    qty0_total = 0
    qty0_with_context = 0
    signal_with_min_score = 0
    signal_with_reasons = 0

    for ev in _iter_events(events_path):
        typ = ev.get("type")
        payload = ev.get("payload") or {}

        if typ == "RiskDecision":
            if payload.get("allowed") is False and str(payload.get("reason") or "") == "qty=0":
                qty0_total += 1
                ctx = payload.get("context")
                if isinstance(ctx, dict) and len(ctx) > 0:
                    qty0_with_context += 1

        elif typ == "Signal":
            ctx = payload.get("context")
            if not isinstance(ctx, dict):
                continue
            if ctx.get("min_score") is not None:
                signal_with_min_score += 1
            reasons = ctx.get("reasons")
            if isinstance(reasons, list) and len(reasons) > 0:
                signal_with_reasons += 1

    qty0_ctx_ok = (qty0_total == 0) or (qty0_with_context == qty0_total)
    checks.append(
        (
            "qty0_has_context",
            qty0_ctx_ok,
            f"qty0_total={qty0_total}, qty0_with_context={qty0_with_context}",
        )
    )

    signal_ctx_ok = signal_with_min_score >= 1 and signal_with_reasons >= 1
    checks.append(
        (
            "signal_context_min_score_and_reasons",
            signal_ctx_ok,
            f"signal_with_min_score={signal_with_min_score}, signal_with_reasons={signal_with_reasons}",
        )
    )

    ok = all(x[1] for x in checks)

    print(f"date={day_s}")
    if autogen_done:
        print(f"autogen={','.join(autogen_done)}")
    else:
        print("autogen=none")

    for name, passed, detail in checks:
        print(f"{name}={'PASS' if passed else 'FAIL'} :: {detail}")

    print(
        "KIS Daily Trade Report Verification: "
        + ("PASS" if ok else "FAIL")
        + f" | date={day_s} | qty0_context={qty0_with_context}/{qty0_total}"
        + f" | signal_ctx=min_score:{signal_with_min_score},reasons:{signal_with_reasons}"
    )

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
