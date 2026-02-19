import logging

from modules.base import ModuleContext
from modules.mijang import MijangModule
from modules.us_swing import USSwingModule
from models import Position


class _StubRest:
    async def get_positions(self):
        return None

    async def get_positions_list(self):
        return []


def _ctx(name: str) -> ModuleContext:
    return ModuleContext(
        name=name,
        enabled=True,
        supports_trading=True,
        symbols=["AAPL", "MSFT", "TSLA"],
        exchange="NASD",
        config={},
    )


def test_us_swing_blocks_entry_when_max_positions_reached():
    mod = USSwingModule(_ctx("us_swing"), logging.getLogger("test"), _StubRest(), {})
    mod.max_positions = 2
    positions = {
        "AAPL": Position(symbol="AAPL", qty=1, avg_price=100.0),
        "MSFT": Position(symbol="MSFT", qty=1, avg_price=200.0),
    }
    assert mod._entry_blocked_by_positions("TSLA", positions) is True


def test_us_swing_blocks_entry_for_existing_symbol():
    mod = USSwingModule(_ctx("us_swing"), logging.getLogger("test"), _StubRest(), {})
    positions = {"AAPL": Position(symbol="AAPL", qty=1, avg_price=100.0)}
    assert mod._entry_blocked_by_positions("AAPL", positions) is True


def test_mijang_blocks_entry_when_max_positions_reached():
    mod = MijangModule(_ctx("mijang"), logging.getLogger("test"), _StubRest(), {})
    mod.max_positions = 2
    positions = {
        "AAPL": Position(symbol="AAPL", qty=1, avg_price=100.0),
        "MSFT": Position(symbol="MSFT", qty=1, avg_price=200.0),
    }
    assert mod._entry_blocked_by_positions("TSLA", positions) is True


def test_mijang_blocks_entry_for_existing_symbol():
    mod = MijangModule(_ctx("mijang"), logging.getLogger("test"), _StubRest(), {})
    positions = {"AAPL": Position(symbol="AAPL", qty=1, avg_price=100.0)}
    assert mod._entry_blocked_by_positions("AAPL", positions) is True
