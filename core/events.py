"""Event definitions for append-only trading logs.

Institutional principle: every decision and state transition should be
explainable and replayable. We use an append-only JSONL event stream.

This module defines a minimal, stable event envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

EVENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Event:
    """Stable event envelope.

    We keep the outer shape stable and evolve by adding new *types* and
    *payload* keys. This makes replay/backfills simpler.

    Added (backward-compatible):
    - schema_version: envelope schema version integer.
    """

    ts: str
    type: str
    payload: Dict[str, Any]
    symbol: Optional[str] = None
    run_id: Optional[str] = None
    schema_version: int = EVENT_SCHEMA_VERSION

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @classmethod
    def make(
        cls,
        type: str,
        payload: Dict[str, Any],
        symbol: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> "Event":
        return cls(
            ts=cls.now_iso(), type=type, payload=payload, symbol=symbol, run_id=run_id
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ts": self.ts,
            "type": self.type,
            "payload": self.payload,
            "schema_version": int(self.schema_version),
        }
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

    Added (backward-compatible):
    - context: optional dict for *explainability* (e.g., close/vwap/rsi/spread/atr).
      This should never include secrets/PII.
    """

    symbol: str
    side: str  # "BUY" | "SELL" | "FLAT"
    strength: float = 1.0
    reason: str = ""
    model: str = ""
    correlation_id: str = ""
    context: Optional[Dict[str, Any]] = None

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload: Dict[str, Any] = {
            "side": self.side,
            "strength": float(self.strength),
            "reason": self.reason,
            "model": self.model,
        }
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        if isinstance(self.context, dict) and self.context:
            payload["context"] = self.context
        else:
            payload["context"] = {
                "context_missing": True,
                "autofilled_at_emit": True,
            }
        return Event.make(
            type="Signal",
            payload=payload,
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
    correlation_id: str = ""
    module: str = ""  # human-friendly strategy/module tag

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload: Dict[str, Any] = {
            "side": self.side,
            "qty": int(self.qty),
            "order_type": self.order_type,
            "idempotency_key": self.idempotency_key,
        }
        if self.limit_price is not None:
            payload["limit_price"] = self.limit_price
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        if self.module:
            payload["module"] = self.module
        return Event.make(
            type="OrderIntent",
            payload=payload,
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
    correlation_id: str = ""
    module: str = ""  # human-friendly strategy/module tag
    context: Optional[Dict[str, Any]] = None

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload: Dict[str, Any] = {
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
        }
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        if self.module:
            payload["module"] = self.module
        if isinstance(self.context, dict) and self.context:
            payload["context"] = self.context
        else:
            payload["context"] = {
                "context_missing": True,
                "autofilled_at_emit": True,
            }
        return Event.make(
            type="RiskDecision",
            payload=payload,
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class OrderSubmitted:
    """Order was submitted to broker (request accepted client-side)."""

    symbol: str
    idempotency_key: str
    broker_order_id: Optional[str] = None
    correlation_id: str = ""

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload = {
            "idempotency_key": self.idempotency_key,
            "broker_order_id": self.broker_order_id,
        }
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        return Event.make(
            type="OrderSubmitted",
            payload=payload,
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
    correlation_id: str = ""

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload = {
            "idempotency_key": self.idempotency_key,
            "broker_order_id": self.broker_order_id,
            "status": self.status,
        }
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        return Event.make(
            type="OrderAck",
            payload=payload,
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
    correlation_id: str = ""
    module: str = ""  # human-friendly strategy/module tag
    expected_price: Optional[float] = None
    slippage_bps: Optional[float] = None

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload: Dict[str, Any] = {
            "side": self.side,
            "qty": int(self.qty),
            "price": float(self.price),
            "broker_order_id": self.broker_order_id,
            "idempotency_key": self.idempotency_key,
            "fee": float(self.fee),
        }
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        if self.module:
            payload["module"] = self.module
        if self.expected_price is not None:
            payload["expected_price"] = float(self.expected_price)
        if self.slippage_bps is not None:
            payload["slippage_bps"] = float(self.slippage_bps)
        return Event.make(
            type="Fill",
            payload=payload,
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class PositionSnapshot:
    """Derived position/cash snapshot.

    Phase4 adds lightweight metadata about *when/why* the snapshot was taken.
    Older logs remain compatible because new fields are optional.
    """

    symbol: str
    qty: int
    avg_price: float
    cash: float
    equity: Optional[float] = None
    trigger: str = ""  # e.g. fill|restore|periodic|manual
    note: str = ""
    correlation_id: str = ""

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload: Dict[str, Any] = {
            "qty": int(self.qty),
            "avg_price": float(self.avg_price),
            "cash": float(self.cash),
            "equity": self.equity,
        }
        if self.trigger:
            payload["trigger"] = self.trigger
        if self.note:
            payload["note"] = self.note
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id

        return Event.make(
            type="PositionSnapshot",
            payload=payload,
            symbol=self.symbol,
            run_id=run_id,
        )


@dataclass(frozen=True)
class PositionRestored:
    """Broker position restored at startup without synthetic fills."""

    symbol: str
    qty: int
    avg_price: float
    cash: float = 0.0
    equity: Optional[float] = None
    note: str = ""
    correlation_id: str = ""

    def to_event(self, run_id: Optional[str] = None) -> Event:
        payload: Dict[str, Any] = {
            "qty": int(self.qty),
            "avg_price": float(self.avg_price),
            "cash": float(self.cash),
            "equity": self.equity,
        }
        if self.note:
            payload["note"] = self.note
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        return Event.make(
            type="PositionRestored",
            payload=payload,
            symbol=self.symbol,
            run_id=run_id,
        )
