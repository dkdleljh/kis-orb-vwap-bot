#!/usr/bin/env python3
"""Show US reserved orders via order-resv-list.

This is used to confirm whether a reserved TP order (order-resv) is actually
registered at KIS.

Usage:
  ./venv/bin/python scripts/show_us_reserved_orders.py --exchange AMEX --days 3
  ./venv/bin/python scripts/show_us_reserved_orders.py --symbol SCHD

Notes:
- This API may not work for demo accounts.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
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
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="AMEX", help="NASD/NYSE/AMEX")
    ap.add_argument("--symbol", default="", help="Filter symbol (e.g., SCHD)")
    ap.add_argument("--days", type=int, default=3, help="Lookback days")
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

    end = datetime.now()
    start = end - timedelta(days=max(1, args.days))

    rest = KISOverseasRestOrders(auth.base_url, auth, account, logger, exchange=args.exchange)
    try:
        rows = await rest.order_resv_list_us(
            exchange=args.exchange,
            inqr_strt_dt=_ymd(start),
            inqr_end_dt=_ymd(end),
        )
    finally:
        await rest.aclose()

    symf = args.symbol.strip().upper()
    if symf:
        rows = [r for r in rows if str(r.get("pdno") or r.get("PDNO") or "").strip().upper() == symf]

    if not rows:
        print("NO_RESERVED_ORDERS")
        return 0

    # Print compact view
    for r in rows[:200]:
        pdno = str(r.get("pdno") or r.get("PDNO") or "").strip()
        odno = str(r.get("odno") or r.get("ODNO") or "").strip()
        qty = str(r.get("ft_ord_qty") or r.get("FT_ORD_QTY") or "").strip()
        px = str(r.get("ft_ord_unpr3") or r.get("FT_ORD_UNPR3") or r.get("ord_unpr") or "").strip()
        st = str(r.get("ord_stat") or r.get("ORD_STAT") or r.get("ord_stat_cd") or "").strip()
        dt = str(r.get("rsvn_ord_dt") or r.get("RSVN_ORD_DT") or r.get("ord_dt") or "").strip()
        seq = str(r.get("rsvn_ord_seq") or r.get("RSVN_ORD_SEQ") or "").strip()
        print(f"{pdno} rsvn_dt={dt} seq={seq} odno={odno} qty={qty} px={px} stat={st}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
