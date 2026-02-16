"""Fill-based ledger (Phase2 skeleton).

Tracks derived cash/position state from Fill events.

Feature flag:
    KIS_INSTITUTIONAL_LEDGER=1

Until Phase3 integration, this module is intentionally unused.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict


def ledger_enabled() -> bool:
    return os.environ.get("KIS_INSTITUTIONAL_LEDGER", "0") == "1"


@dataclass
class Position:
    symbol: str
    qty: int = 0
    avg_price: float = 0.0

    def apply_fill(self, side: str, qty: int, price: float) -> None:
        qty = int(qty)
        price = float(price)
        if qty <= 0:
            return

        if side.upper() == "BUY":
            new_qty = self.qty + qty
            if new_qty <= 0:
                self.qty = new_qty
                self.avg_price = 0.0
                return
            # weighted average
            self.avg_price = (self.avg_price * self.qty + price * qty) / new_qty if self.qty > 0 else price
            self.qty = new_qty
            return

        if side.upper() == "SELL":
            self.qty -= qty
            if self.qty <= 0:
                # flat (or short not supported in baseline)
                self.avg_price = 0.0
            return


class Ledger:
    def __init__(self, starting_cash: float = 0.0) -> None:
        self.cash: float = float(starting_cash)
        self.positions: Dict[str, Position] = {}

    def get_position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol] = pos
        return pos

    def apply_fill(self, symbol: str, side: str, qty: int, price: float, fee: float = 0.0) -> None:
        pos = self.get_position(symbol)
        pos.apply_fill(side=side, qty=qty, price=price)

        notional = float(price) * float(qty)
        if side.upper() == "BUY":
            self.cash -= notional
        elif side.upper() == "SELL":
            self.cash += notional
        self.cash -= float(fee)
