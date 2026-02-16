from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from models import Bar1m, TradeTick


@dataclass
class VwapState:
    cum_pv: float = 0.0
    cum_vol: float = 0.0

    @property
    def vwap(self) -> Optional[float]:
        if self.cum_vol <= 0:
            return None
        return self.cum_pv / self.cum_vol


class BarBuilder1m:
    def __init__(self, on_bar_close: Callable[[Bar1m], None]):
        self.on_bar_close = on_bar_close
        self.current_bar: Optional[Bar1m] = None

    def _bar_start(self, ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0)

    def update(self, tick: TradeTick) -> None:
        bar_start = self._bar_start(tick.timestamp)
        if self.current_bar is None:
            self.current_bar = Bar1m(
                start=bar_start,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.volume,
            )
            return

        if bar_start > self.current_bar.start:
            finished = self.current_bar
            self.on_bar_close(finished)
            self.current_bar = Bar1m(
                start=bar_start,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.volume,
            )
            return

        b = self.current_bar
        b.high = max(b.high, tick.price)
        b.low = min(b.low, tick.price)
        b.close = tick.price
        b.volume += tick.volume


class VwapCalculator:
    def __init__(self):
        self.state = VwapState()

    def update(self, tick: TradeTick) -> None:
        self.state.cum_pv += tick.price * tick.volume
        self.state.cum_vol += tick.volume

    def vwap(self) -> Optional[float]:
        return self.state.vwap
