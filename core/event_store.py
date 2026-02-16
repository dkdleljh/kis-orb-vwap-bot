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
            return
