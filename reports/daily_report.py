"""Daily event-based report generator (Phase5).

This reads the append-only event store and produces a lightweight summary.

Usage:
    python3 -m reports.daily_report --events-dir logs/events --day 20260216

Notes:
- This is best-effort and does not require live credentials.
- Intended for institutional-style operational visibility.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, Optional


@dataclass(frozen=True)
class Ev:
    ts: str
    type: str
    symbol: Optional[str]
    payload: Dict[str, Any]


def iter_jsonl(path: str) -> Iterator[Ev]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    continue
                yield Ev(
                    ts=str(d.get("ts", "")),
                    type=str(d.get("type", "")),
                    symbol=d.get("symbol"),
                    payload=dict(d.get("payload") or {}),
                )
            except Exception:
                continue


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def partition_path(events_dir: str, day: str) -> str:
    return os.path.join(events_dir, day, "events.jsonl")


def build_report(events: Iterable[Ev]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    fills: list[dict[str, Any]] = []
    symbols: set[str] = set()
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None

    for ev in events:
        by_type[ev.type] = int(by_type.get(ev.type, 0)) + 1
        if ev.symbol:
            symbols.add(str(ev.symbol))

        dt = _parse_iso(ev.ts)
        if dt is not None:
            first_ts = dt if first_ts is None else min(first_ts, dt)
            last_ts = dt if last_ts is None else max(last_ts, dt)

        if ev.type == "Fill":
            p = ev.payload or {}
            fills.append(
                {
                    "ts": ev.ts,
                    "symbol": ev.symbol,
                    "side": p.get("side"),
                    "qty": p.get("qty"),
                    "price": p.get("price"),
                    "broker_order_id": p.get("broker_order_id"),
                    "idempotency_key": p.get("idempotency_key"),
                    "fee": p.get("fee"),
                }
            )

    return {
        "event_count": int(sum(by_type.values())),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
        "symbols": sorted(symbols),
        "fills": fills,
        "first_ts": first_ts.isoformat() if first_ts else None,
        "last_ts": last_ts.isoformat() if last_ts else None,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate daily report from event store")
    p.add_argument("--events-dir", default=os.path.join("logs", "events"))
    p.add_argument("--day", required=True, help="UTC partition day YYYYMMDD")
    p.add_argument("--out", default="", help="output path (json). default: stdout")
    args = p.parse_args(argv)

    path = partition_path(args.events_dir, args.day)
    if not os.path.exists(path):
        raise SystemExit(f"events partition not found: {path}")

    report = build_report(iter_jsonl(path))

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return 0

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
