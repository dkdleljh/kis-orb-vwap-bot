#!/usr/bin/env python3
"""Show US open (not-concluded) orders via inquire-nccs.

This helps diagnose 'NO_ROW_YET' situations by querying the official
미체결내역 endpoint.

Usage:
  ./venv/bin/python scripts/show_us_open_orders.py --exchange AMEX
  ./venv/bin/python scripts/show_us_open_orders.py --symbol SCHD

Prints a compact table-like output.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

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
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="NASD", help="NASD/NYSE/AMEX")
    ap.add_argument("--symbol", default="", help="Filter by symbol (e.g., SCHD)")
    args = ap.parse_args()

    _load_dotenv_like(ROOT / ".env")

    logger = setup_logger(str(ROOT / "logs" / "tmp"), "Asia/Seoul")
    app_key, app_secret, _ = load_auth_from_env()
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    await auth.fetch_token(force=True)

    account = AccountInfo(
        account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
        product_code=os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", ""),
    )
    rest = KISOverseasRestOrders(auth.base_url, auth, account, logger, exchange=args.exchange)
    try:
        rows = await rest.inquire_nccs(exchange=args.exchange)
    finally:
        await rest.aclose()

    symf = args.symbol.strip().upper()
    if symf:
        rows = [r for r in rows if str(r.get("pdno") or r.get("ovrs_pdno") or "").strip().upper() == symf]

    if not rows:
        print("NO_OPEN_ORDERS")
        return 0

    # Print compact
    for r in rows[:200]:
        pdno = str(r.get("pdno") or r.get("ovrs_pdno") or "").strip()
        odno = str(r.get("odno") or r.get("ODNO") or "").strip()
        side = str(r.get("sll_buy_dvsn_cd") or r.get("SLL_BUY_DVSN_CD") or "").strip()
        qty = str(r.get("ord_qty") or r.get("ORD_QTY") or "").strip()
        unpr = str(r.get("ovrs_ord_unpr") or r.get("OVRS_ORD_UNPR") or r.get("ord_unpr") or "").strip()
        st = str(r.get("ord_stat") or r.get("ORD_STAT") or r.get("ord_stat_cd") or "").strip()
        print(f"{pdno} odno={odno} side={side} qty={qty} unpr={unpr} stat={st}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
