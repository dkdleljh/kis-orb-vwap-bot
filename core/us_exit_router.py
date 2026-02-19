"""US exit order router (best-effort).

Goal:
- Place exits reliably across time windows:
  - Regular open: use normal overseas order endpoint
  - Outside regular open: try reservation order (limit only)

Safety:
- Never bypass STOP_TRADING.flag / KIS_KILL_SWITCH.
- For STOP-LOSS outside regular hours, we cannot truly "immediate sell".
  We will fallback to reservation if possible; otherwise we return an error.

This module is intentionally conservative.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

import aiohttp

from core.us_time_rules import is_regular_open, is_resv_window
from kis_auth import KISAuth
from kis_rest_orders import AccountInfo


def _kill_switch_active(base_dir: str) -> bool:
    return os.environ.get("KIS_KILL_SWITCH", "0") == "1" or os.path.exists(
        os.path.join(base_dir, "STOP_TRADING.flag")
    )


async def place_us_exit(
    *,
    base_dir: str,
    auth: KISAuth,
    account: AccountInfo,
    symbol: str,
    exchange: str,
    qty: int,
    limit_price: Optional[float],
    reason: str,
) -> dict[str, Any]:
    """Place a US exit order.

    Returns a dict with fields:
    - ok: bool
    - route: regular|resv|blocked|error
    - resp: broker json (best-effort)
    """

    if _kill_switch_active(base_dir):
        return {"ok": False, "route": "blocked", "error": "kill_switch"}

    now_kst = datetime.now().astimezone()

    # Regular session: use normal endpoint via raw call (caller may prefer rest client)
    if is_regular_open(now_kst):
        # We rely on existing rest client for regular orders in most modules.
        return {"ok": False, "route": "regular", "error": "use_rest_client"}

    # Outside regular: attempt reservation
    if limit_price is None:
        return {
            "ok": False,
            "route": "error",
            "error": "market_exit_requires_limit_outside_regular",
        }

    if not is_resv_window(now_kst):
        return {"ok": False, "route": "error", "error": "not_in_resv_window"}

    url = f"{auth.base_url}/uapi/overseas-stock/v1/trading/order-resv"
    payload = {
        "CANO": account.account_no,
        "ACNT_PRDT_CD": account.product_code,
        "PDNO": symbol,
        "OVRS_EXCG_CD": exchange,
        "FT_ORD_QTY": str(int(qty)),
        "FT_ORD_UNPR3": f"{float(limit_price):.2f}",
    }
    headers = auth.auth_headers()
    headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "custtype": "P",
            "tr_id": "TTTT3016U",
        }
    )
    headers["hashkey"] = await auth.hashkey(payload)

    body = json.dumps(payload)
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=body, headers=headers, timeout=20) as r:
            try:
                resp = await r.json()
            except Exception:
                resp = {"_http": r.status, "_text": (await r.text())[:500]}

    return {"ok": True, "route": "resv", "reason": reason, "resp": resp}
