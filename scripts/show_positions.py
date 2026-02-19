"""Show KR/US positions (read-only) in a human-friendly format."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logger import setup_logger  # noqa: E402
from kis_auth import KISAuth, load_auth_from_env  # noqa: E402
from kis_rest_orders import AccountInfo, KISRestOrders  # noqa: E402
from kis_rest_overseas import KISOverseasRestOrders  # noqa: E402


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


def _fmt_money(v) -> str:
    try:
        if v is None:
            return "-"
        if isinstance(v, str) and not v.strip():
            return "-"
        return f"{float(v):.4f}" if abs(float(v)) < 100 else f"{float(v):.2f}"
    except Exception:
        return str(v)


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    _load_dotenv(root / ".env")

    logger = setup_logger(str(root / "logs" / "positions"), "Asia/Seoul")
    base_url = os.environ.get("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

    app_key, app_secret, _ = load_auth_from_env()
    acct_no = (os.environ.get("KIS_ACCOUNT_NO") or "").strip()
    acct_prdt = (os.environ.get("KIS_ACCOUNT_PRODUCT_CODE") or "").strip()

    auth = KISAuth(base_url, app_key, app_secret, logger)
    account = AccountInfo(account_no=acct_no, product_code=acct_prdt)

    # KR
    kr = KISRestOrders(base_url, auth, account, logger)
    kr_map = await kr.get_all_positions()

    # US (try NASD + AMEX buckets; holdings may be tagged AMEX)
    us_nasd = KISOverseasRestOrders(base_url, auth, account, logger, exchange="NASD")
    us_amex = KISOverseasRestOrders(base_url, auth, account, logger, exchange="AMEX")

    us_positions = []
    us_positions.extend(await us_nasd.get_positions_list())
    us_positions.extend(await us_amex.get_positions_list())

    # de-dupe by symbol
    by_sym = {}
    for p in us_positions:
        by_sym[p.symbol] = p

    print("== KR POSITIONS ==")
    if not kr_map:
        print("(none)")
    else:
        for sym in sorted(kr_map.keys()):
            info = kr_map[sym] or {}
            print(
                f"- {sym} qty={int(info.get('qty', 0) or 0)} avg={_fmt_money(info.get('avg_price'))}"
            )

    print("\n== US POSITIONS ==")
    if not by_sym:
        print("(none)")
    else:
        for sym in sorted(by_sym.keys()):
            p = by_sym[sym]
            last = getattr(p, "last_price", None)
            print(
                f"- {sym} qty={p.qty} avg={_fmt_money(p.avg_price)} last={_fmt_money(last)} pnl={p.unrealized_pnl_pct:.2%}"
            )

    await kr.aclose()
    await us_nasd.aclose()
    await us_amex.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
