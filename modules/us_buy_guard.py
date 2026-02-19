from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class USBuyGuardDecision:
    buy_attempt_allowed: bool
    reason: str
    used_integrated_margin_fallback: bool


def evaluate_us_buy_guard(
    *,
    integrated_margin_mode: bool,
    ord_psbl_qty: Optional[int],
    integrated_margin_estimate_usd: float,
    min_usd: float,
) -> USBuyGuardDecision:
    """Decide whether one buy attempt is allowed in US integrated-margin mode."""
    if ord_psbl_qty is not None and ord_psbl_qty > 0:
        return USBuyGuardDecision(
            buy_attempt_allowed=True,
            reason=f"ord_psbl_qty={ord_psbl_qty}",
            used_integrated_margin_fallback=False,
        )

    if not integrated_margin_mode:
        return USBuyGuardDecision(
            buy_attempt_allowed=False,
            reason=f"ord_psbl_qty={ord_psbl_qty} and integrated margin disabled",
            used_integrated_margin_fallback=False,
        )

    if float(integrated_margin_estimate_usd) >= float(min_usd):
        return USBuyGuardDecision(
            buy_attempt_allowed=True,
            reason=f"ord_psbl_qty={ord_psbl_qty} fallback_estimate={integrated_margin_estimate_usd:.2f}",
            used_integrated_margin_fallback=True,
        )

    return USBuyGuardDecision(
        buy_attempt_allowed=False,
        reason=f"ord_psbl_qty={ord_psbl_qty} fallback_estimate={integrated_margin_estimate_usd:.2f} < min_usd={min_usd:.2f}",
        used_integrated_margin_fallback=False,
    )


def is_buy_order_failed(status: str, order_id: str) -> bool:
    if not order_id:
        return True
    s = (status or "").strip().lower()
    return ("reject" in s) or ("리젝트" in s) or ("error" in s) or ("exception" in s) or ("fail" in s)


def set_symbol_cooldown(
    *,
    cooldown_map: dict[str, float],
    symbol: str,
    now_ts: float,
    cooldown_sec: float = 120.0,
) -> float:
    until = float(now_ts) + float(cooldown_sec)
    cooldown_map[symbol] = until
    return until
