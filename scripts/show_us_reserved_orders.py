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
    ap.add_argument("--dedupe", action="store_true", help="Dedupe to latest 1 per (symbol,price,qty)")
    ap.add_argument("--debug", action="store_true", help="Print keys for first row")
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

    if args.debug:
        k = sorted(list(set().union(*[set(r.keys()) for r in rows if isinstance(r, dict)])))
        print(f"DEBUG_KEYS({len(k)}): {k[:80]}")
        print(f"DEBUG_FIRST_ROW: {rows[0]}")

    def _get_sym(r: dict) -> str:
        return str(r.get("pdno") or r.get("PDNO") or "").strip().upper()

    def _get_qty(r: dict) -> str:
        return str(r.get("ft_ord_qty") or r.get("FT_ORD_QTY") or r.get("ord_qty") or r.get("ORD_QTY") or "").strip()

    def _get_px(r: dict) -> str:
        return str(r.get("ft_ord_unpr3") or r.get("FT_ORD_UNPR3") or r.get("ovrs_ord_unpr") or r.get("OVRS_ORD_UNPR") or r.get("ord_unpr") or "").strip()

    def _get_dt(r: dict) -> str:
        return str(r.get("rsvn_ord_dt") or r.get("RSVN_ORD_DT") or r.get("rsvn_ord_rcit_dt") or r.get("RSVN_ORD_RCIT_DT") or r.get("ord_dt") or "").strip()

    def _get_seq(r: dict) -> str:
        return str(r.get("rsvn_ord_seq") or r.get("RSVN_ORD_SEQ") or "").strip()

    def _get_odno(r: dict) -> str:
        return str(r.get("odno") or r.get("ODNO") or r.get("ovrs_rsvn_odno") or r.get("OVRS_RSVN_ODNO") or "").strip()

    def _get_stat(r: dict) -> str:
        return str(r.get("ord_stat") or r.get("ORD_STAT") or r.get("ord_stat_cd") or "").strip()

    if args.dedupe:
        latest = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            key = (_get_sym(r), _get_px(r), _get_qty(r))
            # prefer non-empty receipt date/seq if available
            score = (1 if _get_dt(r) else 0) + (1 if _get_seq(r) else 0) + (1 if _get_odno(r) else 0)
            if key not in latest or score > latest[key][0]:
                latest[key] = (score, r)
        rows = [v[1] for v in latest.values()]

    # Print compact view
    for r in rows[:200]:
        pdno = _get_sym(r)
        odno = _get_odno(r)
        qty = _get_qty(r)
        px = _get_px(r)
        st = _get_stat(r)
        dt = _get_dt(r)
        seq = _get_seq(r)
        print(f"{pdno} rcit_dt={dt} seq={seq} odno={odno} qty={qty} px={px} stat={st}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
