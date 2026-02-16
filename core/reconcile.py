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
from typing import Any, Dict, List, Optional


def reconcile_enabled() -> bool:
    return os.environ.get("KIS_INSTITUTIONAL_RECONCILE", "0") == "1"


@dataclass(frozen=True)
class ReconcileIssue:
    kind: str
    symbol: Optional[str]
    message: str
    details: Dict[str, Any]


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
        if int(a.get("qty", 0)) != int(b.get("qty", 0)):
            issues.append(
                ReconcileIssue(
                    kind="position_qty_mismatch",
                    symbol=sym,
                    message=f"qty mismatch internal={a.get('qty')} broker={b.get('qty')}",
                    details={"internal": a, "broker": b},
                )
            )
    return issues
