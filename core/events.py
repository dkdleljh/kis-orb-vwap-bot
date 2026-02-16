"""Event definitions for append-only trading logs.

Institutional principle: every decision and state transition should be
explainable and replayable. We use an append-only JSONL event stream.

This module defines a minimal, stable event envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Event:
    """Stable event envelope.

    We keep the outer shape stable and evolve by adding new *types* and
    *payload* keys. This makes replay/backfills simpler.
    """

    ts: str
    type: str
    payload: Dict[str, Any]
    symbol: Optional[str] = None
    run_id: Optional[str] = None

    @staticmethod
    def now_iso() -> str:
        return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

    @classmethod
    def make(
        cls,
        type: str,
        payload: Dict[str, Any],
        symbol: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> "Event":
        return cls(ts=cls.now_iso(), type=type, payload=payload, symbol=symbol, run_id=run_id)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"ts": self.ts, "type": self.type, "payload": self.payload}
        if self.symbol:
            d["symbol"] = self.symbol
        if self.run_id:
            d["run_id"] = self.run_id
        return d


# --- Phase1: typed payload helpers (recommended baseline) --------------------


@dataclass(frozen=True)
class Signal:
    """Strategy signal derived from bars/indicators.

    This is intentionally minimal; add keys without breaking old logs.
    """

    symbol: str
    side: str  # "BUY" | "SELL" | "FLAT"
    strength: float = 1.0
    reason: str = ""
    model: str = ""

    def to_event(self, run_id: Optional[str] = None) -> Event:
        return Event.make(
            type="Signal",
            payload={
                "side": self.side,
                "strength": float(self.strength),
                "reason": self.reason,
                "model": self.model,
            },
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class OrderIntent:
    """Pre-risk-check intent to place an order."""

    symbol: str
    side: str  # "BUY" | "SELL"
    qty: int
    order_type: str = "MKT"  # "MKT" | "LMT" etc.
    limit_price: Optional[float] = None
    idempotency_key: str = ""

    def to_event(self, run_id: Optional[str] = None) -> Event:
        return Event.make(
            type="OrderIntent",
            payload={
                "side": self.side,
                "qty": int(self.qty),
                "order_type": self.order_type,
                "limit_price": self.limit_price,
                "idempotency_key": self.idempotency_key,
            },
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class RiskDecision:
    """Result of risk checks for an OrderIntent."""

    symbol: str
    allowed: bool
    reason: str = ""
    idempotency_key: str = ""

    def to_event(self, run_id: Optional[str] = None) -> Event:
        return Event.make(
            type="RiskDecision",
            payload={
                "allowed": bool(self.allowed),
                "reason": self.reason,
                "idempotency_key": self.idempotency_key,
            },
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class OrderSubmitted:
    """Order was submitted to broker (request accepted client-side)."""

    symbol: str
    idempotency_key: str
    broker_order_id: Optional[str] = None

    def to_event(self, run_id: Optional[str] = None) -> Event:
        return Event.make(
            type="OrderSubmitted",
            payload={
                "idempotency_key": self.idempotency_key,
                "broker_order_id": self.broker_order_id,
            },
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class OrderAck:
    """Broker acknowledged an order."""

    symbol: str
    idempotency_key: str
    broker_order_id: str
    status: str = "ACK"

    def to_event(self, run_id: Optional[str] = None) -> Event:
        return Event.make(
            type="OrderAck",
            payload={
                "idempotency_key": self.idempotency_key,
                "broker_order_id": self.broker_order_id,
                "status": self.status,
            },
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class Fill:
    """Execution fill (partial or full)."""

    symbol: str
    side: str  # "BUY" | "SELL"
    qty: int
    price: float
    broker_order_id: Optional[str] = None
    idempotency_key: str = ""
    fee: float = 0.0

    def to_event(self, run_id: Optional[str] = None) -> Event:
        return Event.make(
            type="Fill",
            payload={
                "side": self.side,
                "qty": int(self.qty),
                "price": float(self.price),
                "broker_order_id": self.broker_order_id,
                "idempotency_key": self.idempotency_key,
                "fee": float(self.fee),
            },
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class PositionSnapshot:
    """Derived position/cash snapshot (typically after fills)."""

    symbol: str
    qty: int
    avg_price: float
    cash: float
    equity: Optional[float] = None

    def to_event(self, run_id: Optional[str] = None) -> Event:
        return Event.make(
            type="PositionSnapshot",
            payload={
                "qty": int(self.qty),
                "avg_price": float(self.avg_price),
                "cash": float(self.cash),
                "equity": self.equity,
            },
            symbol=self.symbol,
            run_id=run_id,
        )
