"""US position auto-manager (single-symbol, single-position).

Purpose:
- Provide deterministic, auditable auto-exit management for a live US position
  when the main daemon is not actively running WS monitoring.

Safety:
- Requires explicit user approval (given in chat).
- Respects STOP_TRADING.flag (global kill switch).
- Only manages ONE symbol/position and stops after exit.

Notes:
- KIS overseas order APIs are sensitive to JSON serialization; we use
  KISOverseasRestOrders which sends `data=json.dumps(payload)`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo
from kis_rest_overseas import KISOverseasRestOrders
from logger import setup_logger
from core.audit_log import audit_decision


@dataclass
class Config:
    symbol: str
    exchange: str
    qty: int
    stop_loss: float
    take_profit: float
    poll_sec: float = 20.0


def _load_dotenv_like(path: Path) -> None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        return


def _kill_switch_active() -> bool:
    # Global kill-switch convention across modules.
    return (ROOT / "STOP_TRADING.flag").exists()


async def _inquire_nccs(auth: KISAuth, account: AccountInfo, exchange: str) -> list[dict]:
    """Best-effort open orders list."""
    import aiohttp

    url = f"{auth.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
    headers = auth.auth_headers()
    headers.update({"custtype": "P", "tr_id": "TTTS3018R"})
    params = {
        "CANO": account.account_no,
        "ACNT_PRDT_CD": account.product_code,
        "OVRS_EXCG_CD": exchange,
        "SORT_SQN": "DS",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }

    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers, params=params, timeout=15) as r:
            j = await r.json()
            outs = j.get("output") or []
            if isinstance(outs, dict):
                outs = [outs]
            return outs


async def main() -> int:
    cfg = Config(
        symbol=os.environ.get("SYMBOL", "SCHD"),
        exchange=os.environ.get("EXCHANGE", "AMEX"),
        qty=int(os.environ.get("QTY", "1")),
        stop_loss=float(os.environ.get("STOP_LOSS", "30.82")),
        take_profit=float(os.environ.get("TAKE_PROFIT", "32.22")),
        poll_sec=float(os.environ.get("POLL_SEC", "20")),
    )

    _load_dotenv_like(ROOT / ".env")

    logger = setup_logger(str(ROOT / "logs"), "Asia/Seoul")
    app_key, app_secret, _ = load_auth_from_env()
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    await auth.fetch_token(force=True)

    account = AccountInfo(
        account_no=os.environ["KIS_ACCOUNT_NO"],
        product_code=os.environ["KIS_ACCOUNT_PRODUCT_CODE"],
    )

    rest = KISOverseasRestOrders(auth.base_url, auth, account, logger, exchange=cfg.exchange)

    logger.info(
        f"[auto-manage] start {cfg.symbol} qty={cfg.qty} SL={cfg.stop_loss} TP={cfg.take_profit} poll={cfg.poll_sec}s"
    )

    try:
        while True:
            if _kill_switch_active():
                logger.warning("[auto-manage] STOP_TRADING.flag detected -> stopping manager")
                return 2

            # If TP order already filled, we should observe no position later.
            # We don't have a dedicated positions endpoint wired here, so we do a simple check:
            # if there is no open sell order at TP and also no open buy (rare), we keep monitoring;
            # stop-loss trigger uses quote.

            # Quote (real-time best-effort)
            try:
                last = await rest.get_current_price(cfg.symbol)
                last = float(last or 0)
            except Exception as e:
                logger.error(f"[auto-manage] quote error: {e}")
                await asyncio.sleep(cfg.poll_sec)
                continue

            logger.info(f"[auto-manage] {cfg.symbol} last={last:.4f}")

            # Stop-loss (market)
            if last > 0 and last <= cfg.stop_loss:
                logger.warning(f"[auto-manage] STOP LOSS triggered: last={last:.4f} <= {cfg.stop_loss}")
                from core.correlation import new_corr
                corr = new_corr("us_exit")
                audit_decision(
                    symbol=cfg.symbol,
                    kind="exit_plan",
                    correlation_id=corr,
                    market="US",
                    style="SWING",
                    side="SELL",
                    extra={"reason": "stop_loss", "qty": int(cfg.qty), "trigger_last": float(last), "stop_loss": float(cfg.stop_loss)},
                )
                res = await rest.place_sell_order(cfg.symbol, cfg.qty, price=None, order_type="01")
                audit_decision(
                    symbol=cfg.symbol,
                    kind="order_result",
                    correlation_id=corr,
                    market="US",
                    style="SWING",
                    side="SELL",
                    extra={"reason": "stop_loss", "odno": getattr(res, 'order_id', None), "order": getattr(res, '__dict__', None) or str(res)},
                )
                logger.warning(f"[auto-manage] STOP SELL submitted: {res}")
                return 0

            # Take-profit fill detection (optional): if price >= TP and TP order absent -> assume filled.
            # We'll do a lightweight open-orders check to decide if we can stop the manager.
            if last > 0 and last >= cfg.take_profit:
                try:
                    open_orders = await _inquire_nccs(auth, account, cfg.exchange)
                    has_tp = any((str(o.get("pdno")) == cfg.symbol and float(o.get("ft_ord_unpr3") or 0) >= cfg.take_profit - 1e-6 and (o.get("sll_buy_dvsn_cd") == "01")) for o in open_orders)
                    if not has_tp:
                        logger.info("[auto-manage] TP likely filled (no matching open TP order). stopping manager")
                        return 0
                except Exception as e:
                    logger.error(f"[auto-manage] inquire_nccs error: {e}")

            await asyncio.sleep(cfg.poll_sec)

    finally:
        await rest.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
