"""Append-only JSONL event store.

Recommended baseline for personal/institutional progression:
- local file-based JSONL (fast, debuggable, replayable)
- strict append-only writes
- never crash the trading engine on logging failures
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from core.events import Event


@dataclass
class EventStore:
    base_dir: str
    enabled: bool = True
    run_id: Optional[str] = None
    error_count: int = 0

    def _path_for_day(self, ymd: str) -> str:
        d = os.path.join(self.base_dir, ymd)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "events.jsonl")

    def _path_for_today(self) -> str:
        # UTC day partition keeps it stable for replay.
        # (You can also partition by local day; choose one and keep it consistent.)
        from datetime import datetime

        ymd = datetime.utcnow().strftime("%Y%m%d")
        return self._path_for_day(ymd)

    def recent_event_counts(self, days: int = 2, max_lines: int = 200_000) -> dict[str, int]:
        """Best-effort recent event line counts per UTC day partition.

        This is intended for health/status summaries, not for correctness-critical logic.
        Counts are line-count based (each JSONL line == one event).
        """
        from datetime import datetime, timedelta

        out: dict[str, int] = {}
        for i in range(days):
            ymd = (datetime.utcnow() - timedelta(days=i)).strftime("%Y%m%d")
            path = self._path_for_day(ymd)
            if not os.path.exists(path):
                out[ymd] = 0
                continue
            try:
                # Read line-by-line; cap to avoid pathological cases.
                n = 0
                with open(path, "r", encoding="utf-8") as f:
                    for _ in f:
                        n += 1
                        if n >= max_lines:
                            break
                out[ymd] = n
            except Exception:
                out[ymd] = -1
        return out

    def recent_type_distribution(self, *, max_lines: int = 5000) -> dict[str, int]:
        """Best-effort distribution of recent event *types* (today's partition).

        Reads at most ``max_lines`` from the tail of today's events.jsonl.
        Intended for health/status UX only.
        """
        path = self._path_for_today()
        if not os.path.exists(path):
            return {}

        # naive tail: read all if small; otherwise read last N lines.
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return {}

        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]

        out: dict[str, int] = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    continue
                t = str(d.get("type", ""))
                if not t:
                    continue
                out[t] = int(out.get(t, 0)) + 1
            except Exception:
                continue
        return out

    def append(self, ev: Event) -> None:
        if not self.enabled:
            return

        path = self._path_for_today()
        try:
            line = json.dumps(ev.to_dict(), ensure_ascii=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception:
            # Best effort: never take down the trading process.
            try:
                self.error_count += 1
            except Exception:
                pass
            return
