from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class OrderBookTop:
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    timestamp: datetime


@dataclass
class TradeTick:
    symbol: str
    price: float
    volume: float
    timestamp: datetime


@dataclass
class Bar1m:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float
    unrealized_pnl_pct: float = 0.0
    unrealized_gross_pnl_pct: float = 0.0
    tp1_done: bool = False
    adds: int = 0
    entry_time: Optional[datetime] = None
    profit_locked: bool = False

    def __getitem__(self, key: str):
        return getattr(self, key)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "unrealized_gross_pnl_pct": self.unrealized_gross_pnl_pct,
            "tp1_done": self.tp1_done,
            "adds": self.adds,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "profit_locked": self.profit_locked,
        }


@dataclass
class OrderResult:
    order_id: str
    filled_qty: int
    status: str
    avg_price: Optional[float] = None
