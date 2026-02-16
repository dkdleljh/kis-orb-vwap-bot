"""Replay helper (Phase1 skeleton).

Goal: provide a minimal offline replay loop over the append-only JSONL event
store.

Recommended usage (read-only):
    python -m tools.replay --events-dir logs/events --symbol 005930

This skeleton currently supports:
- scanning events.jsonl partitions
- filtering Bar/Signal events

Integration points (Phase3+):
- drive Strategy/StateMachine deterministically
- compare expected OrderIntent/RiskDecision/Fill to broker exports
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Optional


@dataclass(frozen=True)
class ReplayEvent:
    ts: str
    type: str
    payload: Dict[str, Any]
    symbol: Optional[str] = None
    run_id: Optional[str] = None


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
    parts = []
    for name in os.listdir(base_dir):
        if len(name) != 8 or not name.isdigit():
            continue
        path = os.path.join(base_dir, name, "events.jsonl")
        if os.path.exists(path):
            parts.append(path)
    return sorted(parts)


def replay(
    events_dir: str,
    symbol: Optional[str] = None,
    types: tuple[str, ...] = ("Bar", "Signal"),
    limit: int = 0,
) -> list[ReplayEvent]:
    out: list[ReplayEvent] = []
    for path in iter_partitions(events_dir):
        for ev in iter_jsonl(path):
            if symbol and ev.symbol != symbol:
                continue
            if types and ev.type not in types:
                continue
            out.append(ev)
            if limit and len(out) >= limit:
                return out
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Replay JSONL event partitions")
    p.add_argument("--events-dir", default=os.path.join("logs", "events"))
    p.add_argument("--symbol", default=None)
    p.add_argument("--type", action="append", dest="types", default=None)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args(argv)

    types: tuple[str, ...]
    if args.types:
        types = tuple(args.types)
    else:
        types = ("Bar", "Signal")

    events = replay(args.events_dir, symbol=args.symbol, types=types, limit=args.limit)
    for ev in events:
        print(json.dumps({"ts": ev.ts, "type": ev.type, "symbol": ev.symbol, "payload": ev.payload}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
