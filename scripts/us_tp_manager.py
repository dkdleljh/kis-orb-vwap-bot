"""US take-profit manager (best-effort) for a single symbol.

Purpose:
- Ensure a take-profit exit is always staged:
  - Regular hours: no-op here (strategy/engine may place normal orders)
  - Outside regular hours: place reservation sell order during reservation window
- Recompute TP from avg + tp_pct each run.

This script is idempotent-ish:
- It does not attempt to cancel existing orders (broker APIs vary).
- It relies on reservation order screen semantics (DAY validity).

Recommended cron:
- every 10 minutes during KST 10:00~23:20

Env:
- SYMBOL (default SCHD)
- EXCHANGE (default AMEX)
- TP_PCT (default 0.015)

Safety:
- Respects STOP_TRADING.flag / KIS_KILL_SWITCH
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.audit_log import audit_decision
from core.correlation import new_corr
from core.us_time_rules import is_regular_open, is_resv_window
from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo
from kis_rest_overseas import KISOverseasRestOrders
from logger import setup_logger


def _load_dotenv_like(path: Path) -> None:
    try:
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        return


def _kill_switch() -> bool:
    return os.environ.get("KIS_KILL_SWITCH", "0") == "1" or (ROOT / "STOP_TRADING.flag").exists()


def _now_kst() -> datetime:
    return datetime.now().astimezone()


async def main() -> int:
    _load_dotenv_like(ROOT / ".env")

    if _kill_switch():
        return 2

    symbol = os.environ.get("SYMBOL", "SCHD").strip().upper()
    exchange = os.environ.get("EXCHANGE", "AMEX").strip().upper()
    tp_pct = float(os.environ.get("TP_PCT", "0.015") or 0.015)

    now = _now_kst()
    if is_regular_open(now):
        # Regular session: let engine/strategy place normal TP/SL.
        return 0

    if not is_resv_window(now):
        return 0

    logger = setup_logger(str(ROOT / "logs" / "tmp"), "Asia/Seoul")
    app_key, app_secret, _ = load_auth_from_env()
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    await auth.fetch_token(force=True)

    account = AccountInfo(
        account_no=os.environ["KIS_ACCOUNT_NO"],
        product_code=os.environ["KIS_ACCOUNT_PRODUCT_CODE"],
    )

    rest = KISOverseasRestOrders(auth.base_url, auth, account, logger, exchange=exchange)
    try:
        pos = await rest.get_balance()
        if not isinstance(pos, list):
            return 0
        row = None
        for r in pos:
            if str(r.get("ovrs_pdno") or "").upper() == symbol:
                row = r
                break
        if not row:
            return 0

        qty = int(float(row.get("ovrs_cblc_qty") or 0))
        avg = float(row.get("pchs_avg_pric") or 0)
        if qty <= 0 or avg <= 0:
            return 0

        tp = round(avg * (1.0 + tp_pct) + 1e-9, 2)
        corr = new_corr("us_exit")

        audit_decision(
            symbol=symbol,
            kind="exit_plan",
            correlation_id=corr,
            market="US",
            style="SWING",
            side="SELL",
            extra={"reason": "take_profit_reserved_manager", "qty": qty, "avg": avg, "tp": tp, "tp_pct": tp_pct, "exchange": exchange},
        )

        # Place reservation sell (limit)
        # Reuse the same endpoint behavior as us_resv_tp_schd.py
        import json, aiohttp

        url = f"{auth.base_url}/uapi/overseas-stock/v1/trading/order-resv"
        payload = {
            "CANO": account.account_no,
            "ACNT_PRDT_CD": account.product_code,
            "PDNO": symbol,
            "OVRS_EXCG_CD": exchange,
            "FT_ORD_QTY": str(int(qty)),
            "FT_ORD_UNPR3": f"{float(tp):.2f}",
        }
        headers = auth.auth_headers()
        headers.update({
            "Content-Type": "application/json",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "custtype": "P",
            "tr_id": "TTTT3016U",
        })
        headers["hashkey"] = await auth.hashkey(payload)

        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=json.dumps(payload), headers=headers, timeout=20) as resp:
                try:
                    j = await resp.json()
                except Exception:
                    j = {"_http": resp.status, "_text": (await resp.text())[:500]}

        odno = None
        try:
            odno = (j or {}).get("output", {}).get("ODNO")
        except Exception:
            pass

        audit_decision(
            symbol=symbol,
            kind="order_result",
            correlation_id=corr,
            market="US",
            style="SWING",
            side="SELL",
            extra={"reason": "take_profit_reserved_manager", "odno": odno, "resp": j},
        )
        return 0
    finally:
        await rest.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
