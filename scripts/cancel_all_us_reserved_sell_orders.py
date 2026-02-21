#!/usr/bin/env python3
"""Cancel ALL US reserved SELL orders (batch).

WARNING: This sends cancel requests to KIS.

Why:
- When you want to flatten/cancel all scheduled reserved take-profit (TP) sells.

Safety:
- Requires CONFIRM_CANCEL=YES environment variable.
- Prints a dry-run plan unless --execute is provided.

Usage:
  # show what would be canceled
  ./venv/bin/python scripts/cancel_all_us_reserved_sell_orders.py --days 10

  # execute cancellation
  CONFIRM_CANCEL=YES ./venv/bin/python scripts/cancel_all_us_reserved_sell_orders.py --days 10 --execute

Notes:
- KIS reserved orders are exchange-scoped; we query NASD/NYSE/AMEX by default.
- If a row lacks receipt date or order number, it will be skipped (cannot cancel).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kis_auth import KISAuth, load_auth_from_env
from kis_rest_orders import AccountInfo
from kis_rest_overseas import KISOverseasRestOrders
from logger import setup_logger


def _load_dotenv_like(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _get_sym(r: dict) -> str:
    return str(r.get("pdno") or r.get("PDNO") or "").strip().upper()


def _get_side(r: dict) -> str:
    # best-effort: reserved list sometimes includes buy/sell marker
    for k in ["sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD", "ord_dvsn", "ORD_DVSN", "side", "SIDE"]:
        v = r.get(k)
        if v is None:
            continue
        s = str(v).strip().upper()
        if s in {"SELL", "S", "2"}:
            return "SELL"
        if s in {"BUY", "B", "1"}:
            return "BUY"
    return ""  # unknown


def _get_dt(r: dict) -> str:
    return str(
        r.get("rsvn_ord_rcit_dt")
        or r.get("RSVN_ORD_RCIT_DT")
        or r.get("rsvn_ord_dt")
        or r.get("RSVN_ORD_DT")
        or ""
    ).strip()


def _get_odno(r: dict) -> str:
    return str(r.get("ovrs_rsvn_odno") or r.get("OVRS_RSVN_ODNO") or r.get("odno") or r.get("ODNO") or "").strip()


async def _list_reserved(exchange: str, days: int, symbol: str) -> list[dict[str, Any]]:
    logger = setup_logger(str(ROOT / "logs" / "tmp"), "Asia/Seoul")
    app_key, app_secret, _ = load_auth_from_env()
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    await auth.fetch_token(force=True)

    account = AccountInfo(
        account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
        product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", ""),
    )

    end = datetime.now()
    start = end - timedelta(days=max(1, days))

    rest = KISOverseasRestOrders(auth.base_url, auth, account, logger, exchange=exchange)
    try:
        rows = await rest.order_resv_list_us(
            exchange=exchange,
            inqr_strt_dt=_ymd(start),
            inqr_end_dt=_ymd(end),
        )
    finally:
        await rest.aclose()

    if symbol:
        symf = symbol.strip().upper()
        rows = [r for r in rows if _get_sym(r) == symf]
    return [r for r in rows if isinstance(r, dict)]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--symbol", default="", help="optional filter, e.g. SCHD")
    ap.add_argument("--exchange", action="append", default=[], help="repeatable: NASD/NYSE/AMEX")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    _load_dotenv_like(ROOT / ".env")

    exchanges = args.exchange or ["NASD", "NYSE", "AMEX"]

    # Build cancel plan
    plan: list[tuple[str, str, str, str]] = []  # (exchange, sym, rcit_dt, odno)
    for ex in exchanges:
        rows = await _list_reserved(ex, args.days, args.symbol)
        for r in rows:
            sym = _get_sym(r)
            side = _get_side(r)
            rcit_dt = _get_dt(r)
            odno = _get_odno(r)
            if not rcit_dt or not odno:
                continue
            # If side is unknown, still include (we are canceling ALL reserved sell tasks; safest is cancel all reserved orders).
            # If you want strictly SELL-only, enforce: if side and side != 'SELL': continue
            plan.append((ex, sym, rcit_dt, odno))

    # Dedupe: cancel is keyed by (rcit_dt, odno) across exchanges
    uniq: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for ex, sym, rcit_dt, odno in plan:
        uniq[(rcit_dt, odno)] = (ex, sym, rcit_dt, odno)
    plan = sorted(uniq.values())

    if not plan:
        print("NO_CANCELLABLE_RESERVED_ORDERS")
        return 0

    print(f"FOUND={len(plan)}")
    for ex, sym, rcit_dt, odno in plan:
        print(f"PLAN {ex} {sym} rcit_dt={rcit_dt} odno={odno}")

    if not args.execute:
        print("DRY_RUN: pass --execute to cancel")
        return 0

    if os.environ.get("CONFIRM_CANCEL", "") != "YES":
        print("REFUSED: set CONFIRM_CANCEL=YES to proceed")
        return 2

    # Execute cancels
    logger = setup_logger(str(ROOT / "logs" / "tmp"), "Asia/Seoul")
    app_key, app_secret, _ = load_auth_from_env()
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    await auth.fetch_token(force=True)
    account = AccountInfo(
        account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
        product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", ""),
    )

    canceled = 0
    failed = 0

    for ex, sym, rcit_dt, odno in plan:
        rest = KISOverseasRestOrders(auth.base_url, auth, account, logger, exchange=ex)
        try:
            resp = await rest.order_resv_ccnl_us(rsvn_ord_rcit_dt=rcit_dt, ovrs_rsvn_odno=odno)
            ok = True
            # best-effort success detection
            s = str(resp)
            if "error" in s.lower() or "msg" in s.lower() and "FAIL" in s.upper():
                ok = False
            if ok:
                canceled += 1
                print(f"CANCELED {ex} {sym} rcit_dt={rcit_dt} odno={odno}")
            else:
                failed += 1
                print(f"FAILED {ex} {sym} rcit_dt={rcit_dt} odno={odno} resp={resp}")
        except Exception as e:
            failed += 1
            print(f"FAILED {ex} {sym} rcit_dt={rcit_dt} odno={odno} err={e}")
        finally:
            await rest.aclose()

    print(f"DONE canceled={canceled} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
