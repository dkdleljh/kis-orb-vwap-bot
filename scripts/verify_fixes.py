from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core.rate_limiter import get_global_rate_limiter
from kis_rest_orders import AccountInfo, KISRestOrders
from kis_rest_overseas import KISOverseasRestOrders
from core.order_executor import OrderExecutor


class _DummyAuth:
    async def fetch_token(self, force: bool = False) -> None:
        return None

    def auth_headers(self) -> dict:
        return {"authorization": "Bearer dummy"}

    async def hashkey(self, payload: dict) -> str:
        return "dummy-hash"


class _DummyLogger:
    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None

    def error(self, *args, **kwargs) -> None:
        return None


class _DummyEngine:
    config = {"trading": {"execution": {"exit_fail_cooldown_sec": 10}}}


async def _main() -> None:
    auth = _DummyAuth()
    logger = _DummyLogger()
    account = AccountInfo(account_no="12345678", product_code="01")

    kr = KISRestOrders("https://example.com", auth, account, logger)
    us = KISOverseasRestOrders("https://example.com", auth, account, logger)

    limiter = get_global_rate_limiter()
    assert kr._rate_limiter is limiter  # noqa: SLF001
    assert us._rate_limiter is limiter  # noqa: SLF001

    ex = OrderExecutor(_DummyEngine())  # type: ignore[arg-type]
    assert ex._is_sell_open_order("005930", {"pdno": "005930", "sll_buy_dvsn_cd": "01", "rmn_qty": "3"})
    assert not ex._is_sell_open_order("005930", {"pdno": "005930", "sll_buy_dvsn_cd": "02", "rmn_qty": "3"})

    await kr.aclose()
    await us.aclose()
    print("verify_fixes: OK")


if __name__ == "__main__":
    asyncio.run(_main())
