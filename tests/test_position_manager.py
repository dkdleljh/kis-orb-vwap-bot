from __future__ import annotations

from datetime import datetime

from core.position_manager import PositionManager
from strategy_state_machine import StrategyStateMachine


class _DummyRest:
    async def get_all_positions(self):
        return {
            "005930": {"qty": 3, "avg_price": 72000.0},
            "000660": {"qty": 2, "avg_price": 130000.0},
        }


class _DummyLogger:
    def __init__(self) -> None:
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def info(self, *args, **kwargs):
        self.infos.append(args)

    def warning(self, *args, **kwargs):
        self.warnings.append(args)


class _Engine:
    def __init__(self) -> None:
        self.tz = "Asia/Seoul"
        self.rest = _DummyRest()
        self.state_machine = StrategyStateMachine(logger=None)
        self.peak_pnl_pct: dict[str, float] = {}
        self.max_concurrent_positions = 20
        self.logger = _DummyLogger()
        self.ledger = None
        self.run_id = "test"
        self._error_counts: dict[str, int] = {}
        self.events = []

    def _append_event(self, ev):
        self.events.append(ev)


def test_restore_positions_emits_restore_and_snapshot_events():
    eng = _Engine()
    mgr = PositionManager(eng)

    import asyncio

    asyncio.run(mgr.restore_positions())

    assert set(eng.state_machine.positions.keys()) == {"005930", "000660"}
    assert eng.peak_pnl_pct["005930"] == -0.01
    event_types = [e.type for e in eng.events]
    assert event_types.count("PositionRestored") == 2
    assert event_types.count("PositionSnapshot") == 2


def test_record_position_snapshot_uses_state_machine_when_ledger_disabled():
    eng = _Engine()
    pos = eng.state_machine.position
    assert pos is None
    eng.state_machine.set_position(
        __import__("models").Position(
            symbol="005930",
            qty=1,
            avg_price=70000.0,
            entry_time=datetime(2026, 2, 20, 9, 1, 0),
        )
    )
    mgr = PositionManager(eng)

    mgr.record_position_snapshot("005930", trigger="test")

    assert eng.events[-1].type == "PositionSnapshot"
    assert eng.events[-1].payload["qty"] == 1
