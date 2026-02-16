"""Broker reconciliation (Phase2 skeleton).

Purpose:
- compare internal derived ledger/oms state with broker-reported positions/orders
- surface discrepancies early (before they become risk events)

Feature flag:
    KIS_INSTITUTIONAL_RECONCILE=1

Until Phase3 integration, this module is intentionally unused.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class ReconcileSeverity(str, Enum):
    CRITICAL = "critical"  # Immediately block new entries
    WARNING = "warning"  # Log and report, but allow trading
    INFO = "info"  # Just for awareness


def reconcile_enabled() -> bool:
    return os.environ.get("KIS_INSTITUTIONAL_RECONCILE", "0") == "1"


@dataclass(frozen=True)
class ReconcileIssue:
    kind: str
    symbol: Optional[str]
    message: str
    details: Dict[str, Any]
    severity: ReconcileSeverity = ReconcileSeverity.INFO


def reconcile_positions(
    *,
    internal: Dict[str, Dict[str, Any]],
    broker: Dict[str, Dict[str, Any]],
) -> List[ReconcileIssue]:
    """Compare internal vs broker positions.

    Args:
        internal: mapping symbol -> {qty, avg_price, ...}
        broker: mapping symbol -> {qty, avg_price, ...}

    Returns:
        List of human-readable issues.
    """

    issues: List[ReconcileIssue] = []
    syms = set(internal) | set(broker)
    for sym in sorted(syms):
        a = internal.get(sym) or {}
        b = broker.get(sym) or {}
        internal_qty = int(a.get("qty", 0))
        broker_qty = int(b.get("qty", 0))
        if internal_qty != broker_qty:
            severity = ReconcileSeverity.CRITICAL
            issues.append(
                ReconcileIssue(
                    kind="position_qty_mismatch",
                    symbol=sym,
                    message=f"qty mismatch internal={internal_qty} broker={broker_qty}",
                    details={"internal": a, "broker": b},
                    severity=severity,
                )
            )
    return issues


def reconcile_open_orders(
    *, internal_orders: Dict[str, Dict[str, Any]], broker_orders: List[Dict[str, Any]]
) -> List[ReconcileIssue]:
    """Compare internal OMS view vs broker open orders (Phase5 skeleton).

    Args:
        internal_orders: mapping idempotency_key -> {symbol, side, qty, state, broker_order_id, filled_qty, ...}
        broker_orders: list of broker open order dicts (best-effort)

    Returns:
        Reconcile issues. This is intentionally conservative and best-effort.
    """
    issues: List[ReconcileIssue] = []

    # Normalize broker orders by broker_order_id when available.
    broker_by_id: Dict[str, Dict[str, Any]] = {}
    for o in broker_orders or []:
        try:
            oid = str(o.get("order_id") or o.get("ODNO") or "")
        except Exception:
            oid = ""
        if oid:
            broker_by_id[oid] = dict(o)

    # Compare: internal submitted/acked orders should exist broker-side (when broker query is available).
    for key, rec in (internal_orders or {}).items():
        broker_id = str(rec.get("broker_order_id") or "")
        state = str(rec.get("state") or "")
        symbol = rec.get("symbol")

        if (
            state in {"SUBMITTED", "ACKED", "PARTIALLY_FILLED"}
            and broker_orders is not None
        ):
            if broker_id and broker_id not in broker_by_id:
                issues.append(
                    ReconcileIssue(
                        kind="open_order_missing_on_broker",
                        symbol=str(symbol) if symbol else None,
                        message=f"internal order present but missing on broker open-orders: broker_order_id={broker_id} state={state}",
                        details={"idempotency_key": key, "internal": rec},
                        severity=ReconcileSeverity.WARNING,
                    )
                )

    return issues


def classify_issues(
    issues: List[ReconcileIssue],
) -> Dict[ReconcileSeverity, List[ReconcileIssue]]:
    """이슈 리스트를 심각도별로 분류합니다.

    Returns:
        Dict mapping severity -> list of issues
    """
    result: Dict[ReconcileSeverity, List[ReconcileIssue]] = {
        ReconcileSeverity.CRITICAL: [],
        ReconcileSeverity.WARNING: [],
        ReconcileSeverity.INFO: [],
    }
    for issue in issues:
        result[issue.severity].append(issue)
    return result


def should_block_new_entries(issues: List[ReconcileIssue]) -> bool:
    """CRITICAL severity 이슈가 있는지 확인합니다.

    Returns:
        True if any critical issue exists (should block new entries)
    """
    return any(i.severity == ReconcileSeverity.CRITICAL for i in issues)
