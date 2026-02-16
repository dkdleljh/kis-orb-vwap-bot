"""Replay helper for the append-only JSONL event store.

Recommended usage:
    python -m tools.replay --events-dir logs/events --symbol 005930

Phase3 enhancements:
- filter by event type(s), symbol, run_id, and time window
- print per-type summaries for quick debugging

This tool is intentionally read-only.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, Optional


@dataclass(frozen=True)
class ReplayEvent:
    ts: str
    type: str
    payload: Dict[str, Any]
    symbol: Optional[str] = None
    run_id: Optional[str] = None


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        # Event.ts is like: 2026-02-16T01:02:03.456Z
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def iter_jsonl(path: str) -> Iterator[ReplayEvent]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    continue
                yield ReplayEvent(
                    ts=str(d.get("ts", "")),
                    type=str(d.get("type", "")),
                    payload=dict(d.get("payload") or {}),
                    symbol=d.get("symbol"),
                    run_id=d.get("run_id"),
                )
            except Exception:
                continue


def iter_partitions(base_dir: str) -> Iterable[str]:
    """Yield events.jsonl paths from YYYYMMDD partitions."""
    if not os.path.isdir(base_dir):
        return []
    parts: list[str] = []
    for name in os.listdir(base_dir):
        if len(name) != 8 or not name.isdigit():
            continue
        path = os.path.join(base_dir, name, "events.jsonl")
        if os.path.exists(path):
            parts.append(path)
    return sorted(parts)


def replay(
    events_dir: str,
    *,
    symbol: Optional[str] = None,
    run_id: Optional[str] = None,
    types: tuple[str, ...] = (),
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 0,
) -> list[ReplayEvent]:
    since_dt = _parse_iso(since) if since else None
    until_dt = _parse_iso(until) if until else None

    out: list[ReplayEvent] = []
    for path in iter_partitions(events_dir):
        for ev in iter_jsonl(path):
            if symbol and ev.symbol != symbol:
                continue
            if run_id and ev.run_id != run_id:
                continue
            if types and ev.type not in types:
                continue

            if since_dt or until_dt:
                ev_dt = _parse_iso(ev.ts)
                if ev_dt is None:
                    continue
                if since_dt and ev_dt < since_dt:
                    continue
                if until_dt and ev_dt > until_dt:
                    continue

            out.append(ev)
            if limit and len(out) >= limit:
                return out
    return out


def summarize(events: list[ReplayEvent]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    by_type_symbol: dict[str, dict[str, int]] = {}

    for ev in events:
        by_type[ev.type] = int(by_type.get(ev.type, 0)) + 1
        if ev.symbol:
            by_symbol[ev.symbol] = int(by_symbol.get(ev.symbol, 0)) + 1
            by_type_symbol.setdefault(ev.type, {})
            by_type_symbol[ev.type][ev.symbol] = int(by_type_symbol[ev.type].get(ev.symbol, 0)) + 1

    return {
        "count": int(len(events)),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_type_symbol": by_type_symbol,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Replay JSONL event partitions")
    p.add_argument("--events-dir", default=os.path.join("logs", "events"))
    p.add_argument("--symbol", default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--type", action="append", dest="types", default=None)
    p.add_argument("--since", default=None, help="ISO timestamp, e.g. 2026-02-16T00:00:00Z")
    p.add_argument("--until", default=None, help="ISO timestamp, e.g. 2026-02-16T03:00:00Z")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--summary", action="store_true", help="print counts instead of events")
    args = p.parse_args(argv)

    types: tuple[str, ...] = tuple(args.types) if args.types else ()

    events = replay(
        args.events_dir,
        symbol=args.symbol,
        run_id=args.run_id,
        types=types,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )

    if args.summary:
        print(json.dumps(summarize(events), ensure_ascii=False, indent=2))
        return 0

    for ev in events:
        print(
            json.dumps(
                {"ts": ev.ts, "type": ev.type, "symbol": ev.symbol, "run_id": ev.run_id, "payload": ev.payload},
                ensure_ascii=False,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
