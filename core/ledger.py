"""Fill-based ledger (Phase2 skeleton).

Tracks derived cash/position state from Fill events.

Feature flag:
    KIS_INSTITUTIONAL_LEDGER=1

LED-02 Extension:
- Fill idempotency (dedup by fill_id)
- Realized PnL tracking
- Fee/tax integration
- Position snapshot persistence
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Set


def ledger_enabled() -> bool:
    return os.environ.get("KIS_INSTITUTIONAL_LEDGER", "0") == "1"


@dataclass
class Position:
    symbol: str
    qty: int = 0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    fill_ids: Set[str] = field(default_factory=set)

    def apply_fill(
        self, side: str, qty: int, price: float, fee: float = 0.0, fill_id: str = ""
    ) -> bool:
        qty = int(qty)
        price = float(price)

        if fill_id and fill_id in self.fill_ids:
            return False

        if qty <= 0:
            return False

        if fill_id:
            self.fill_ids.add(fill_id)

        if side.upper() == "BUY":
            new_qty = self.qty + qty
            if new_qty <= 0:
                self.qty = new_qty
                self.avg_price = 0.0
                return True
            self.avg_price = (
                (self.avg_price * self.qty + price * qty) / new_qty
                if self.qty > 0
                else price
            )
            self.qty = new_qty
            self.total_fees += fee
            return True

        if side.upper() == "SELL":
            if self.qty > 0 and qty <= self.qty:
                pnl = (price - self.avg_price) * qty
                self.realized_pnl += pnl
            self.qty -= qty
            self.total_fees += fee
            if self.qty <= 0:
                self.avg_price = 0.0
            return True

        return False


@dataclass
class LedgerSnapshot:
    cash: float
    positions: Dict[str, Dict]
    realized_pnl: float
    total_fees: float
    ts: str


class Ledger:
    def __init__(self, starting_cash: float = 0.0, base_dir: str = "") -> None:
        self.cash: float = float(starting_cash)
        self.positions: Dict[str, Position] = {}
        self.realized_pnl: float = 0.0
        self.total_fees: float = 0.0
        self._processed_fills: Set[str] = set()
        self.base_dir = base_dir

    def get_position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol] = pos
        return pos

    def apply_fill(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        fee: float = 0.0,
        fill_id: str = "",
        broker_order_id: str = "",
    ) -> bool:
        if fill_id and fill_id in self._processed_fills:
            return False

        pos = self.get_position(symbol)
        result = pos.apply_fill(
            side=side, qty=qty, price=price, fee=fee, fill_id=fill_id
        )

        if not result:
            return False

        if fill_id:
            self._processed_fills.add(fill_id)

        notional = float(price) * float(qty)
        if side.upper() == "BUY":
            self.cash -= notional
        elif side.upper() == "SELL":
            realized = (
                (price - pos.avg_price + (pos.avg_price - price)) * qty
                if pos.avg_price > 0
                else 0
            )
            self.cash += notional
        self.cash -= float(fee)
        self.total_fees += fee

        self.realized_pnl = sum(p.realized_pnl for p in self.positions.values())
        return True

    def get_total_equity(
        self, market_prices: Optional[Dict[str, float]] = None
    ) -> float:
        equity = self.cash
        for sym, pos in self.positions.items():
            if pos.qty > 0:
                if market_prices and sym in market_prices:
                    equity += pos.qty * market_prices[sym]
                else:
                    equity += pos.qty * pos.avg_price
        return equity

    def get_unrealized_pnl(
        self, market_prices: Optional[Dict[str, float]] = None
    ) -> float:
        if not market_prices:
            return 0.0
        unrealized = 0.0
        for sym, pos in self.positions.items():
            if pos.qty > 0 and sym in market_prices:
                unrealized += (market_prices[sym] - pos.avg_price) * pos.qty
        return unrealized

    def snapshot(self) -> LedgerSnapshot:
        positions_data = {}
        for sym, pos in self.positions.items():
            positions_data[sym] = {
                "qty": pos.qty,
                "avg_price": pos.avg_price,
                "realized_pnl": pos.realized_pnl,
                "total_fees": pos.total_fees,
            }
        return LedgerSnapshot(
            cash=self.cash,
            positions=positions_data,
            realized_pnl=self.realized_pnl,
            total_fees=self.total_fees,
            ts=datetime.now().isoformat(),
        )

    def save_snapshot(self, filepath: str = "") -> None:
        if not filepath and self.base_dir:
            import os

            os.makedirs(os.path.join(self.base_dir, "logs", "state"), exist_ok=True)
            filepath = os.path.join(
                self.base_dir,
                "logs",
                "state",
                f"ledger_{datetime.now().strftime('%Y%m%d')}.json",
            )

        if not filepath:
            return

        data = {
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "total_fees": self.total_fees,
            "processed_fills": list(self._processed_fills),
            "positions": {
                sym: {
                    "qty": pos.qty,
                    "avg_price": pos.avg_price,
                    "realized_pnl": pos.realized_pnl,
                    "total_fees": pos.total_fees,
                    "fill_ids": list(pos.fill_ids),
                }
                for sym, pos in self.positions.items()
            },
            "ts": datetime.now().isoformat(),
        }

        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def load_snapshot(self, filepath: str = "") -> bool:
        if not filepath and self.base_dir:
            today = datetime.now().strftime("%Y%m%d")
            filepath = os.path.join(
                self.base_dir, "logs", "state", f"ledger_{today}.json"
            )

        if not filepath or not os.path.exists(filepath):
            return False

        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            self.cash = float(data.get("cash", 0))
            self.realized_pnl = float(data.get("realized_pnl", 0))
            self.total_fees = float(data.get("total_fees", 0))
            self._processed_fills = set(data.get("processed_fills", []))

            for sym, pos_data in data.get("positions", {}).items():
                pos = Position(symbol=sym)
                pos.qty = int(pos_data.get("qty", 0))
                pos.avg_price = float(pos_data.get("avg_price", 0))
                pos.realized_pnl = float(pos_data.get("realized_pnl", 0))
                pos.total_fees = float(pos_data.get("total_fees", 0))
                pos.fill_ids = set(pos_data.get("fill_ids", []))
                self.positions[sym] = pos

            return True
        except Exception:
            return False
