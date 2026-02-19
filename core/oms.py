"""Order management system (Phase2 skeleton).

Design goals (recommended baseline):
- explicit order state machine
- idempotency keys for submit/retry safety
- broker_order_id indexing for reconciliation
- state transition enforcement

Feature flag:
    KIS_INSTITUTIONAL_OMS=1  -> enable use by higher-level engine (Phase3+)

OMS-01/02 Extension:
- broker_order_id -> idempotency_key index
- State transition validation
- Cancel/Reject handling
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional


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


VALID_TRANSITIONS = {
    OrderState.NEW: {OrderState.INTENT},
    OrderState.INTENT: {OrderState.RISK_ALLOWED, OrderState.RISK_REJECTED},
    OrderState.RISK_ALLOWED: {OrderState.SUBMITTED},
    OrderState.RISK_REJECTED: set(),
    OrderState.SUBMITTED: {OrderState.ACKED, OrderState.CANCELED, OrderState.REJECTED},
    OrderState.ACKED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELED,
    },
    OrderState.FILLED: set(),
    OrderState.CANCELED: set(),
    OrderState.REJECTED: set(),
}


@dataclass
class OrderRecord:
    symbol: str
    side: str  # BUY/SELL
    qty: int
    idempotency_key: str
    state: OrderState = OrderState.NEW
    broker_order_id: Optional[str] = None
    filled_qty: int = 0
    submitted_at: Optional[str] = None
    filled_at: Optional[str] = None
    error_message: Optional[str] = None


class OMS:
    def __init__(self) -> None:
        self._orders_by_key: Dict[str, OrderRecord] = {}
        self._orders_by_broker_id: Dict[str, str] = {}

    def get(self, idempotency_key: str) -> Optional[OrderRecord]:
        return self._orders_by_key.get(idempotency_key)

    def get_by_broker_id(self, broker_order_id: str) -> Optional[OrderRecord]:
        key = self._orders_by_broker_id.get(broker_order_id)
        if key:
            return self._orders_by_key.get(key)
        return None

    def _transition(self, rec: OrderRecord, new_state: OrderState) -> bool:
        if new_state in VALID_TRANSITIONS.get(rec.state, set()):
            rec.state = new_state
            return True
        return False

    def register_intent(
        self, symbol: str, side: str, qty: int, idempotency_key: str
    ) -> OrderRecord:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is not None:
            return rec
        rec = OrderRecord(
            symbol=symbol,
            side=side,
            qty=int(qty),
            idempotency_key=idempotency_key,
            state=OrderState.INTENT,
        )
        self._orders_by_key[idempotency_key] = rec
        return rec

    def mark_risk(self, idempotency_key: str, allowed: bool) -> bool:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return False
        new_state = OrderState.RISK_ALLOWED if allowed else OrderState.RISK_REJECTED
        return self._transition(rec, new_state)

    def mark_submitted(
        self, idempotency_key: str, broker_order_id: Optional[str] = None
    ) -> bool:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return False
        rec.broker_order_id = broker_order_id or rec.broker_order_id
        if rec.broker_order_id:
            self._orders_by_broker_id[rec.broker_order_id] = idempotency_key
        rec.submitted_at = datetime.now().isoformat()
        return self._transition(rec, OrderState.SUBMITTED)

    def mark_acked(self, idempotency_key: str, broker_order_id: str) -> bool:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return False
        rec.broker_order_id = broker_order_id
        if broker_order_id:
            self._orders_by_broker_id[broker_order_id] = idempotency_key
        return self._transition(rec, OrderState.ACKED)

    def apply_fill(self, idempotency_key: str, fill_qty: int) -> bool:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return False
        rec.filled_qty = int(rec.filled_qty) + int(fill_qty)
        if rec.filled_qty >= rec.qty:
            rec.filled_at = datetime.now().isoformat()
            return self._transition(rec, OrderState.FILLED)
        return self._transition(rec, OrderState.PARTIALLY_FILLED)

    def mark_canceled(self, idempotency_key: str) -> bool:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return False
        return self._transition(rec, OrderState.CANCELED)

    def mark_rejected(self, idempotency_key: str, error_message: str = "") -> bool:
        rec = self._orders_by_key.get(idempotency_key)
        if rec is None:
            return False
        rec.error_message = error_message
        return self._transition(rec, OrderState.REJECTED)

    def get_working_orders(self) -> Dict[str, OrderRecord]:
        working = {}
        for key, rec in self._orders_by_key.items():
            if rec.state in {
                OrderState.SUBMITTED,
                OrderState.ACKED,
                OrderState.PARTIALLY_FILLED,
            }:
                working[key] = rec
        return working

    def get_all_orders(self) -> Dict[str, OrderRecord]:
        return self._orders_by_key.copy()

    def save_state(self, filepath: str) -> None:
        data = {
            "orders": {
                key: {
                    "symbol": rec.symbol,
                    "side": rec.side,
                    "qty": rec.qty,
                    "state": rec.state.value,
                    "broker_order_id": rec.broker_order_id,
                    "filled_qty": rec.filled_qty,
                    "submitted_at": rec.submitted_at,
                    "filled_at": rec.filled_at,
                }
                for key, rec in self._orders_by_key.items()
            },
            "broker_id_index": self._orders_by_broker_id,
            "ts": datetime.now().isoformat(),
        }
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def load_state(self, filepath: str) -> bool:
        if not os.path.exists(filepath):
            return False
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            for key, rec_data in data.get("orders", {}).items():
                rec = OrderRecord(
                    symbol=rec_data["symbol"],
                    side=rec_data["side"],
                    qty=rec_data["qty"],
                    idempotency_key=key,
                    state=OrderState(rec_data.get("state", "NEW")),
                    broker_order_id=rec_data.get("broker_order_id"),
                    filled_qty=rec_data.get("filled_qty", 0),
                    submitted_at=rec_data.get("submitted_at"),
                    filled_at=rec_data.get("filled_at"),
                )
                self._orders_by_key[key] = rec
                if rec.broker_order_id:
                    self._orders_by_broker_id[rec.broker_order_id] = key
            return True
        except Exception:
            return False
