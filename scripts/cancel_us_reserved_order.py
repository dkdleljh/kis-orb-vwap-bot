#!/usr/bin/env python3
"""Cancel a US reserved order.

WARNING: This will send a cancel request to KIS.

Usage:
  ./venv/bin/python scripts/cancel_us_reserved_order.py --rcit-date 20260219 --odno 0030517739

Notes:
- Requires RSVN_ORD_RCIT_DT (receipt date) and OVRS_RSVN_ODNO (reserved ODNO).
- If you don't know rcit-date, run scripts/show_us_reserved_orders.py --debug.
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
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rcit-date", required=True, help="RSVN_ORD_RCIT_DT (YYYYMMDD)")
    ap.add_argument("--odno", required=True, help="OVRS_RSVN_ODNO")
    ap.add_argument("--exchange", default="AMEX", help="for auth context only")
    args = ap.parse_args()

    _load_dotenv_like(ROOT / ".env")

    # Safety: require explicit confirm
    if os.environ.get("CONFIRM_CANCEL", "") != "YES":
        print("REFUSED: set CONFIRM_CANCEL=YES to proceed")
        return 2

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
        resp = await rest.order_resv_ccnl_us(rsvn_ord_rcit_dt=args.rcit_date, ovrs_rsvn_odno=args.odno)
        import json
        print(json.dumps(resp, ensure_ascii=False))
    finally:
        await rest.aclose()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
