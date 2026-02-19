"""Bar aggregation utilities.

추천 스윙 기본 주기: 15분봉(1분봉 15개 집계)
- 국장/미장 모두 동일한 방식으로 집계 가능
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from models import Bar1m


@dataclass
class AggBar:
    start: object
    open: float
    high: float
    low: float
    close: float
    volume: float


class BarAggregator:
    """Aggregate 1m bars into N-minute bars."""

    def __init__(self, n: int, on_close: Callable[[AggBar], None]):
        if n <= 1:
            raise ValueError("n must be >= 2")
        self.n = int(n)
        self.on_close = on_close
        self._count = 0
        self._cur: Optional[AggBar] = None

    def update(self, bar: Bar1m) -> None:
        if self._cur is None:
            self._cur = AggBar(
                start=bar.start,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume),
            )
            self._count = 1
            return

        c = self._cur
        c.high = max(c.high, float(bar.high))
        c.low = min(c.low, float(bar.low))
        c.close = float(bar.close)
        c.volume += float(bar.volume)
        self._count += 1

        if self._count >= self.n:
            finished = c
            self._cur = None
            self._count = 0
            self.on_close(finished)
