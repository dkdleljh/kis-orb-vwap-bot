import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.base import ModuleContext
from modules.mijang import MijangModule
from premarket_collector import PremarketCollector


class _StubRest:
    async def get_quote(self, symbol: str) -> dict:
        return {
            "output": {
                "last": "101.5",
                "bid": "101.4",
                "ask": "101.6",
                "tvol": "12345",
            }
        }

    async def get_positions(self):
        return None

    async def get_cash_available(self):
        return 0.0

    async def place_buy_order(self, symbol: str, qty: int, price: float):
        return SimpleNamespace(order_id="", filled_qty=0, status="suppressed")

    async def place_sell_order(self, symbol: str, qty: int, price: float | None = None):
        return SimpleNamespace(order_id="", filled_qty=0, status="suppressed")

    async def place_sell_market(self, symbol: str, qty: int):
        return SimpleNamespace(order_id="", filled_qty=0, status="suppressed")


class _StubKrRest:
    async def get_quote(self, symbol: str) -> dict:
        return {
            "output": {
                "stck_prpr": "70200",
                "stck_bprc": "70100",
                "stck_aprc": "70300",
                "acml_vol": "998877",
            }
        }


def test_premarket_collector_creates_symbol_jsonl(tmp_path: Path):
    collector = PremarketCollector(
        rest_client=_StubRest(),
        logger=logging.getLogger("test"),
        data_root=tmp_path,
        poll_sec=60,
    )

    asyncio.run(collector.collect_once(["AAPL"]))

    ymd = datetime.now().strftime("%Y%m%d")
    target = tmp_path / "premarket_us" / ymd / "AAPL.jsonl"
    assert target.exists()


def test_premarket_collector_jsonl_line_fields(tmp_path: Path):
    collector = PremarketCollector(
        rest_client=_StubRest(),
        logger=logging.getLogger("test"),
        data_root=tmp_path,
        poll_sec=60,
    )

    asyncio.run(collector.collect_once(["TSLA"]))

    ymd = datetime.now().strftime("%Y%m%d")
    target = tmp_path / "premarket_us" / ymd / "TSLA.jsonl"
    line = target.read_text(encoding="utf-8").strip().splitlines()[0]
    row = json.loads(line)

    assert {"timestamp", "symbol", "last", "bid", "ask", "vol"}.issubset(row.keys())
    assert row["symbol"] == "TSLA"
    assert isinstance(row["last"], float)


def test_premarket_collector_kr_parses_kis_keys_and_writes_jsonl(tmp_path: Path):
    collector = PremarketCollector(
        rest_client=_StubKrRest(),
        logger=logging.getLogger("test"),
        data_root=tmp_path,
        market="KR",
        poll_sec=60,
    )

    asyncio.run(collector.collect_once(["005930"]))

    ymd = datetime.now().strftime("%Y%m%d")
    target = tmp_path / "premarket_kr" / ymd / "005930.jsonl"
    assert target.exists()

    row = json.loads(target.read_text(encoding="utf-8").strip().splitlines()[0])
    assert row["symbol"] == "005930"
    assert row["last"] == 70200.0
    assert row["bid"] == 70100.0
    assert row["ask"] == 70300.0
    assert row["vol"] == 998877.0


def test_mijang_premarket_iteration_does_not_call_order_path():
    logger = logging.getLogger("test")
    ctx = ModuleContext(
        name="mijang",
        enabled=True,
        supports_trading=True,
        symbols=["AAPL"],
        exchange="NASD",
        config={},
    )
    mod = MijangModule(ctx, logger, _StubRest(), {})
    mod._running = True

    mod._check_positions = AsyncMock()  # type: ignore[method-assign]
    mod._check_force_exit = AsyncMock()  # type: ignore[method-assign]
    mod._check_next_candidate = AsyncMock()  # type: ignore[method-assign]
    mod._run_premarket_cycle = AsyncMock()  # type: ignore[method-assign]
    mod.place_buy_order = AsyncMock()  # type: ignore[method-assign]

    status = {"is_open": False, "is_pre_market": True}
    asyncio.run(mod._run_market_iteration(status))

    mod._check_positions.assert_not_called()
    mod._check_force_exit.assert_not_called()
    mod._check_next_candidate.assert_not_called()
    mod._run_premarket_cycle.assert_called_once()
    mod.place_buy_order.assert_not_called()
