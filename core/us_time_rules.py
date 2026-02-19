"""US trading/reservation time rules in KST.

KIS overseas order constraints (observed + official sample notes):
- Regular US trading (KST): 23:30~06:00 (DST: 22:30~05:00)
- US reservation window (KST): 10:00~23:20 (DST may end 22:20)

We keep this module conservative and best-effort. The broker is the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None
NY = ZoneInfo("America/New_York") if ZoneInfo else None


@dataclass(frozen=True)
class USSessionKST:
    regular_open: time
    regular_close: time
    resv_start: time
    resv_end: time


def _is_dst_ny(now_kst: datetime) -> bool:
    if not (NY and KST) or now_kst.tzinfo is None:
        return False
    try:
        now_ny = now_kst.astimezone(NY)
        # heuristic: dst() non-zero
        return bool(now_ny.dst() and now_ny.dst().total_seconds() != 0)
    except Exception:
        return False


def sessions(now_kst: datetime) -> USSessionKST:
    """Return KST sessions for the given datetime (DST-aware)."""
    dst = _is_dst_ny(now_kst)
    if dst:
        return USSessionKST(
            regular_open=time(22, 30),
            regular_close=time(5, 0),
            resv_start=time(10, 0),
            resv_end=time(22, 20),
        )
    return USSessionKST(
        regular_open=time(23, 30),
        regular_close=time(6, 0),
        resv_start=time(10, 0),
        resv_end=time(23, 20),
    )


def is_regular_open(now_kst: datetime) -> bool:
    s = sessions(now_kst)
    t = now_kst.time()
    # regular spans midnight
    return (t >= s.regular_open) or (t < s.regular_close)


def is_resv_window(now_kst: datetime) -> bool:
    s = sessions(now_kst)
    t = now_kst.time()
    return s.resv_start <= t <= s.resv_end
