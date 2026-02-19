"""Audit logging helpers (append-only) for decisions and order rationale.

Goal:
- Make every trade decision explainable *after the fact*.
- Keep a stable, replayable JSONL stream under logs/events (UTC partition).

Constraints:
- Best-effort: never crash trading loops on logging failures.
- Do NOT include secrets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from core.event_store import EventStore
from core.events import Event


_BASE_DIR = Path(__file__).resolve().parents[1]


def _safe_load_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            j = json.loads(path.read_text(encoding="utf-8"))
            return j if isinstance(j, dict) else {}
    except Exception:
        return {}
    return {}


def _dynamic_thresholds_snapshot() -> dict[str, Any]:
    """Return latest dynamic thresholds snapshot (best-effort)."""
    p = _BASE_DIR / "logs" / "dynamic_thresholds.json"
    j = _safe_load_json(p)
    if not j:
        return {}

    # Keep only small/important fields to reduce log bloat.
    return {
        "dynamic_ts_kst": j.get("ts_kst"),
        "thresholds": j.get("thresholds"),
        "inputs": (j.get("inputs") or {}),
    }


def _env_snapshot(keys: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in keys:
        v = os.environ.get(k)
        if v is None:
            continue
        # Avoid logging secrets accidentally
        if "KEY" in k or "SECRET" in k or "TOKEN" in k:
            continue
        out[k] = str(v)
    return out


_STORE: Optional[EventStore] = None


def get_store() -> EventStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    events_dir = str(_BASE_DIR / "logs" / "events")
    run_id = os.environ.get("KIS_RUN_ID") or None
    _STORE = EventStore(events_dir, enabled=True, run_id=run_id)
    return _STORE


def append_event(*, type: str, payload: dict[str, Any], symbol: str | None = None, run_id: str | None = None) -> None:
    """Append a raw event."""
    try:
        store = get_store()
        ev = Event.make(type=type, payload=payload, symbol=symbol, run_id=run_id or store.run_id)
        store.append(ev)
    except Exception:
        return


def audit_decision(
    *,
    symbol: str,
    kind: str,
    correlation_id: str = "",
    market: str | None = None,
    style: str | None = None,
    score: float | None = None,
    threshold: float | None = None,
    side: str | None = None,
    reasons: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """High-level decision audit entry.

    Examples:
    - kind=signal_score
    - kind=entry_plan
    - kind=exit_plan
    - kind=order_result
    """

    payload: dict[str, Any] = {
        "kind": kind,
        "correlation_id": correlation_id,
        "market": market,
        "style": style,
        "side": side,
        "score": score,
        "threshold": threshold,
        "reasons": list(reasons or [])[:12],
        "dynamic": _dynamic_thresholds_snapshot(),
        "env": _env_snapshot(
            [
                "KIS_LIVE_ENABLED",
                "KIS_LIVE_CONFIRM",
                "KIS_KILL_SWITCH",
                "KIS_MAX_TRADES_PER_DAY_KR",
                "KIS_MAX_TRADES_PER_DAY_US",
                "FG_BLEND_WEIGHT",
                "VIX_BLEND_WEIGHT",
            ]
        ),
    }
    if extra:
        payload["extra"] = extra

    append_event(type="Decision", payload=payload, symbol=symbol)
