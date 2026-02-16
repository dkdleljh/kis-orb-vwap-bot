"""Market session classification (KR/US) for candle aggregation.

Personal-use defaults:
- KR (KST): pre 08:00-09:00, regular 09:00-15:30, after 15:30-18:00, night 18:00-08:00
- US (ET): pre 04:00-09:30, regular 09:30-16:00, after 16:00-20:00, night 20:00-04:00

These are heuristic windows intended for chart/candle analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


class Market(str, Enum):
    KR = "KR"
    US = "US"


class Session(str, Enum):
    PRE = "PRE"
    REGULAR = "REGULAR"
    AFTER = "AFTER"
    NIGHT = "NIGHT"


@dataclass(frozen=True)
class SessionWindows:
    tz: str
    pre_start: time
    reg_start: time
    reg_end: time
    after_end: time


KR_WINDOWS = SessionWindows(
    tz="Asia/Seoul",
    pre_start=time(8, 0),
    reg_start=time(9, 0),
    reg_end=time(15, 30),
    after_end=time(18, 0),
)

US_WINDOWS = SessionWindows(
    tz="America/New_York",
    pre_start=time(4, 0),
    reg_start=time(9, 30),
    reg_end=time(16, 0),
    after_end=time(20, 0),
)


def market_for_symbol(symbol: str) -> Market:
    # Simple heuristic: KRX/KIS domestic symbols are digits; US are tickers.
    return Market.KR if symbol.isdigit() else Market.US


def _localize(dt: datetime, tz: str) -> datetime:
    if dt.tzinfo is None:
        # Treat naive datetimes as already-local for safety.
        return dt
    if ZoneInfo is None:
        return dt
    try:
        return dt.astimezone(ZoneInfo(tz))
    except Exception:
        return dt


def session_for_dt(dt: datetime, market: Market) -> Session:
    w = KR_WINDOWS if market == Market.KR else US_WINDOWS
    d = _localize(dt, w.tz)
    t = d.time()

    if w.pre_start <= t < w.reg_start:
        return Session.PRE
    if w.reg_start <= t < w.reg_end:
        return Session.REGULAR
    if w.reg_end <= t < w.after_end:
        return Session.AFTER
    return Session.NIGHT
