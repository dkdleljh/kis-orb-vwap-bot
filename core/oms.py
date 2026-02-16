"""Order management system (Phase2 skeleton).

Design goals (recommended baseline):
- explicit order state machine
- idempotency keys for submit/retry safety
- keep this module import-safe; existing runtime paths must not depend on it

Feature flag:
    KIS_INSTITUTIONAL_OMS=1  -> enable use by higher-level engine (Phase3+)

Until Phase3 integration, this module is intentionally unused.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


def oms_enabled() -> bool:
    return os.environ.get("KIS_INSTITUTIONAL_OMS", "0") == "1"


class OrderState(str, Enum):
    NEW = "NEW"
    INTENT = "INTENT"
    RISK_ALLOWED = "RISK_ALLOWED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass
class OrderRecord:
    symbol: str
    side: str  # BUY/SELL
    qty: int
    idempotency_key: str
    state: OrderState = OrderState.NEW
    broker_order_id: Optional[str] = None
    filled_qty: int = 0


class OMS:
    """Minimal in-memory OMS.

    Phase3+ will:
    - persist to event_store
    - implement state transitions on OrderIntent/RiskDecision/OrderAck/Fill
    """

    def __init__(self) -> None:
        self._orders_by_key: dict[str, OrderRecord] = {}

    def get(self, idempotency_key: str) -> Optional[OrderRecord]:
        return self._orders_by_key.get(idempotency_key)

    def register_intent(self, symbol: str, side: str, qty: int, idempotency_key: str) -> OrderRecord:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is not None:
            # idempotent: return existing record.
            return rec
        rec = OrderRecord(symbol=symbol, side=side, qty=int(qty), idempotency_key=idempotency_key, state=OrderState.INTENT)
        self._orders_by_key[idempotency_key] = rec
        return rec

    def mark_risk(self, idempotency_key: str, allowed: bool) -> None:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return
        rec.state = OrderState.RISK_ALLOWED if allowed else OrderState.RISK_REJECTED

    def mark_submitted(self, idempotency_key: str, broker_order_id: Optional[str] = None) -> None:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return
        rec.broker_order_id = broker_order_id or rec.broker_order_id
        rec.state = OrderState.SUBMITTED

    def mark_acked(self, idempotency_key: str, broker_order_id: str) -> None:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return
        rec.broker_order_id = broker_order_id
        rec.state = OrderState.ACKED

    def apply_fill(self, idempotency_key: str, fill_qty: int) -> None:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return
        rec.filled_qty = int(rec.filled_qty) + int(fill_qty)
        if rec.filled_qty >= rec.qty:
            rec.state = OrderState.FILLED
        else:
            rec.state = OrderState.PARTIALLY_FILLED
