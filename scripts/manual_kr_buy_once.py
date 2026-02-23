"""Manual one-shot KR buy (aggressive) to force at least one fill today.

- Uses KIS REST quote to get ask price.
- Submits a BUY LIMIT at a slightly higher price to improve fill probability.

Usage:
  ./venv/bin/python scripts/manual_kr_buy_once.py --symbol 069500 --qty 1

WARNING: Real trading. Use with caution.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logger import setup_logger  # noqa: E402
from kis_auth import KISAuth, load_auth_from_env  # noqa: E402
from kis_rest_orders import AccountInfo, KISRestOrders  # noqa: E402


def _load_dotenv(path: Path) -> None:
    try:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        return


def _as_int(x, default=0) -> int:
    try:
        if x is None:
            return default
        s = str(x).strip()
        if not s:
            return default
        return int(float(s))
    except Exception:
        return default


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--price-buffer", type=int, default=10, help="extra KRW added on top of ask")
    args = ap.parse_args()

    _load_dotenv(ROOT / ".env")

    logger = setup_logger(str(ROOT / "logs" / "manual_orders"), "Asia/Seoul")
    base_url = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

    app_key, app_secret, _ = load_auth_from_env()
    acct_no = (os.environ.get("KIS_ACCOUNT_NO") or "").strip()
    acct_prdt = (os.environ.get("KIS_ACCOUNT_PRODUCT_CODE") or "").strip()

    if not acct_no or not acct_prdt:
        print("ERROR: account not configured")
        return 2

    auth = KISAuth(base_url, app_key, app_secret, logger)
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)
    rest = KISRestOrders(base_url, auth, account, logger)

    q = await rest.get_quote(args.symbol)
    out = (q or {}).get("output", {}) or {}
    ask = _as_int(out.get("askp1"))
    last = _as_int(out.get("stck_prpr"))
    bid = _as_int(out.get("bidp1"))

    ref = ask or last
    if ref <= 0:
        print("ERROR: quote missing ask/last")
        await rest.aclose()
        return 3

    price = int(ref + int(args.price_buffer))

    # Place BUY LIMIT
    res = await rest.place_buy_limit(args.symbol, int(args.qty), float(price))

    print(
        {
            "symbol": args.symbol,
            "qty": int(args.qty),
            "bid": bid,
            "ask": ask,
            "last": last,
            "limit_price": price,
            "order_id": getattr(res, "order_id", ""),
            "status": getattr(res, "status", ""),
        }
    )

    await rest.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
