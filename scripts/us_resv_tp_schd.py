"""Place/refresh a US reserved take-profit sell order for SCHD.

Why:
- KIS overseas regular order endpoint rejects outside market hours.
- Reserved order endpoint (/order-resv, tr_id TTTT3016U) accepts during
  the reservation window (KST 10:00~23:20; DST may shorten end).

This script:
1) Reads current SCHD position (avg, qty)
2) Computes TP price (default: avg * 1.015, rounded to cents)
3) If now is before reservation window, waits until 10:00 KST
4) Submits reserved sell order (limit)

Env overrides:
- TP_PCT (default 0.015)
- SYMBOL (default SCHD)
- EXCHANGE (default AMEX)

Safety:
- Respects STOP_TRADING.flag (killswitch)
- Avoids repeated submissions by keeping state in data/us_resv_tp_state.json
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATE_PATH = ROOT / "data" / "us_resv_tp_state.json"

from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo
from kis_rest_overseas import KISOverseasRestOrders
from logger import setup_logger
from core.audit_log import audit_decision


def _read_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _to_int(v) -> int:
    try:
        if v is None or v == "":
            return 0
        return int(float(v))
    except Exception:
        return 0


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
    return (ROOT / "STOP_TRADING.flag").exists() or os.environ.get("KIS_KILL_SWITCH", "0") == "1"


def _now_kst() -> datetime:
    if ZoneInfo is None:
        return datetime.now()
    return datetime.now(tz=ZoneInfo("Asia/Seoul"))


async def _sleep_until_kst(target_t: time) -> None:
    while True:
        if _kill_switch_active():
            raise SystemExit(2)
        now = _now_kst()
        today = now.date()
        target = datetime.combine(today, target_t, tzinfo=now.tzinfo)
        if now >= target:
            return
        await asyncio.sleep(min(60.0, (target - now).total_seconds()))


async def _get_schd_position(rest: KISOverseasRestOrders) -> tuple[int, float, str, int] | None:
    """Return (qty, avg, exchange, ord_psbl_qty) for SCHD best-effort."""
    raw = await rest.get_balance_raw(exchange=rest.exchange)
    if not isinstance(raw, dict):
        return None
    out1 = raw.get("output1")
    if not isinstance(out1, list):
        return None
    for r in out1:
        if str(r.get("ovrs_pdno") or "").upper() != "SCHD":
            continue
        qty = _to_int(r.get("ovrs_cblc_qty"))
        avg = float(r.get("pchs_avg_pric") or 0)
        ex = str(r.get("ovrs_excg_cd") or rest.exchange or "").strip().upper()
        ord_psbl = _to_int(r.get("ord_psbl_qty"))
        if qty > 0 and avg > 0:
            return qty, avg, ex, ord_psbl
    return None


async def _place_resv_sell(auth: KISAuth, account: AccountInfo, symbol: str, exchange: str, qty: int, price: float) -> dict:
    import aiohttp

    url = f"{auth.base_url}/uapi/overseas-stock/v1/trading/order-resv"
    payload = {
        "CANO": account.account_no,
        "ACNT_PRDT_CD": account.product_code,
        "PDNO": symbol,
        "OVRS_EXCG_CD": exchange,
        "FT_ORD_QTY": str(int(qty)),
        "FT_ORD_UNPR3": f"{float(price):.2f}",
    }

    headers = auth.auth_headers()
    headers.update({
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "custtype": "P",
        "tr_id": "TTTT3016U",  # US reserved SELL
    })

    # hashkey
    headers["hashkey"] = await auth.hashkey(payload)

    body = json.dumps(payload)
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=body, headers=headers, timeout=20) as r:
            try:
                return await r.json()
            except Exception:
                return {"_http": r.status, "_text": (await r.text())[:500]}


async def main() -> int:
    _load_dotenv_like(ROOT / ".env")

    symbol = os.environ.get("SYMBOL", "SCHD").strip().upper()
    # Default exchange is best-effort; we will override with the exchange from balance row.
    exchange = os.environ.get("EXCHANGE", "AMEX").strip().upper()
    tp_pct = float(os.environ.get("TP_PCT", "0.015") or 0.015)

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
        pos = await _get_schd_position(rest)
        if not pos:
            print("NO_POSITION")
            return 0
        qty, avg, ex_from_row, ord_psbl_qty = pos
        if ex_from_row:
            exchange = ex_from_row

        # If ord_psbl_qty is 0, reserved order is likely to fail or be meaningless.
        if int(ord_psbl_qty) <= 0:
            print(f"SKIP_NOT_ORDERABLE ord_psbl_qty={ord_psbl_qty} exchange={exchange}")
            return 0

        tp = round(avg * (1.0 + tp_pct) + 1e-9, 2)

        # De-dup submissions: if we've already submitted the same TP today, skip.
        st = _read_state()
        today = _now_kst().date().isoformat()
        last = (st.get("last") or {}) if isinstance(st, dict) else {}
        if (
            last.get("date") == today
            and str(last.get("symbol")) == symbol
            and str(last.get("exchange")) == exchange
            and int(last.get("qty") or 0) == int(qty)
            and float(last.get("tp") or 0.0) == float(tp)
            and str(last.get("result")) == "submitted"
        ):
            print("SKIP_ALREADY_SUBMITTED")
            return 0

        from core.correlation import new_corr
        corr = new_corr("us_exit")
        audit_decision(
            symbol=symbol,
            kind="exit_plan",
            correlation_id=corr,
            market="US",
            style="SWING",
            side="SELL",
            extra={"reason": "take_profit_reserved", "qty": int(qty), "avg": float(avg), "tp": float(tp), "tp_pct": float(tp_pct), "exchange": exchange, "ord_psbl_qty": int(ord_psbl_qty)},
        )
        print(f"POSITION {symbol} qty={qty} avg={avg} -> TP={tp} (tp_pct={tp_pct}) exch={exchange} ord_psbl={ord_psbl_qty}")

        # Reservation window starts 10:00 KST
        now = _now_kst()
        if now.time() < time(10, 0):
            print("WAIT_UNTIL_10:00_KST")
            await _sleep_until_kst(time(10, 0))

        if _kill_switch_active():
            print("KILL_SWITCH_ON")
            return 2

        resp = await _place_resv_sell(auth, account, symbol, exchange, qty, tp)
        odno = None
        rt_cd = None
        try:
            rt_cd = str((resp or {}).get("rt_cd"))
            odno = (resp or {}).get("output", {}).get("ODNO")
        except Exception:
            odno = None

        result = "submitted" if rt_cd == "0" else "error"
        _write_state(
            {
                "last": {
                    "date": _now_kst().date().isoformat(),
                    "ts": _now_kst().isoformat(),
                    "symbol": symbol,
                    "exchange": exchange,
                    "qty": int(qty),
                    "avg": float(avg),
                    "tp": float(tp),
                    "tp_pct": float(tp_pct),
                    "ord_psbl_qty": int(ord_psbl_qty),
                    "result": result,
                    "odno": odno,
                }
            }
        )

        audit_decision(
            symbol=symbol,
            kind="order_result",
            correlation_id=corr,
            market="US",
            style="SWING",
            side="SELL",
            extra={"reason": "take_profit_reserved", "odno": odno, "resp": resp},
        )
        print(json.dumps(resp, ensure_ascii=False))
        return 0
    finally:
        await rest.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
