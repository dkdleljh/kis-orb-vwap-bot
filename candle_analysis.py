"""Daily/session candle aggregation and simple analysis.

Goal: Analyze previous-day and current-day candles, and keep per-session
(Pre/Regular/After/Night) candles for KR/US markets.

This is intended for *personal* use and lightweight decision support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

from models import Bar1m
from market_sessions import Market, Session, market_for_symbol, session_for_dt


@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_bar(cls, bar: Bar1m) -> "Candle":
        return cls(
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
        )

    def update(self, bar: Bar1m) -> None:
        self.high = max(self.high, float(bar.high))
        self.low = min(self.low, float(bar.low))
        self.close = float(bar.close)
        self.volume += float(bar.volume)


class CandleAnalyzer:
    """Maintain per-symbol candles for today and per-session buckets."""

    def __init__(self) -> None:
        self._ymd_by_symbol: Dict[str, date] = {}
        self.today_by_symbol: Dict[str, Candle] = {}
        self.session_by_symbol: Dict[str, Dict[Session, Candle]] = {}
        self.last_session_by_symbol: Dict[str, Session] = {}

        # Optional previous day candle (can be populated by fetcher).
        self.prev_day_by_symbol: Dict[str, Candle] = {}

    def set_prev_day_candle(self, symbol: str, candle: Candle) -> None:
        self.prev_day_by_symbol[symbol] = candle

    def update(self, symbol: str, bar: Bar1m) -> Optional[Session]:
        market: Market = market_for_symbol(symbol)
        ses = session_for_dt(bar.start, market)

        ymd = bar.start.date()
        if self._ymd_by_symbol.get(symbol) != ymd:
            # New day reset
            self._ymd_by_symbol[symbol] = ymd
            self.today_by_symbol[symbol] = Candle.from_bar(bar)
            self.session_by_symbol[symbol] = {ses: Candle.from_bar(bar)}
            self.last_session_by_symbol[symbol] = ses
            return ses

        # Today candle
        if symbol not in self.today_by_symbol:
            self.today_by_symbol[symbol] = Candle.from_bar(bar)
        else:
            self.today_by_symbol[symbol].update(bar)

        # Session candle
        smap = self.session_by_symbol.setdefault(symbol, {})
        if ses not in smap:
            smap[ses] = Candle.from_bar(bar)
        else:
            smap[ses].update(bar)

        prev_ses = self.last_session_by_symbol.get(symbol)
        self.last_session_by_symbol[symbol] = ses
        if prev_ses != ses:
            return ses
        return None

    def snapshot(self, symbol: str) -> dict:
        out: dict = {"symbol": symbol}
        prev = self.prev_day_by_symbol.get(symbol)
        if prev:
            out["prev_day"] = prev.__dict__
        today = self.today_by_symbol.get(symbol)
        if today:
            out["today"] = today.__dict__
        ses_map = self.session_by_symbol.get(symbol, {})
        out["sessions"] = {k.value: v.__dict__ for k, v in ses_map.items()}

        # Basic derived metrics
        if prev and today and prev.close:
            out["gap_pct"] = (today.open - prev.close) / prev.close
        if today and today.open:
            out["today_range_pct"] = (today.high - today.low) / today.open
        return out
