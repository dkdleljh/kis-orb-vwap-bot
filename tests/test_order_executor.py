from __future__ import annotations

import asyncio
from datetime import datetime

from core.order_executor import OrderExecutor
from models import Position
from strategy_state_machine import StrategyStateMachine


class _DummyEventStore:
    def __init__(self) -> None:
        self.events = []

    def append(self, ev):
        self.events.append(ev)


class _DummyFeeCalculator:
    def get_net_pnl_percent(self, entry: float, exit_price: float) -> float:
        return (exit_price - entry) / entry if entry else 0.0

    def calculate_entry_cost(self, price: float, qty: int):
        return type("_Cost", (), {"commission": 0.0, "tax": 0.0})()

    def calculate_exit_cost(self, price: float, qty: int):
        return type("_Cost", (), {"commission": 0.0, "tax": 0.0})()


class _DummyRisk:
    def __init__(self) -> None:
        self.entries = 0
        self.exits = 0

    def record_entry(self):
        self.entries += 1

    def record_exit(self, pnl_pct: float, is_stop: bool):
        self.exits += 1


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None


class _Engine:
    def __init__(self) -> None:
        self.tz = "Asia/Seoul"
        self.entry_retry_limit = 1
        self.stop_loss_pct = -0.015
        self.last_price = {"005930": 71000.0}
        self.last_book = {}
        self.fee_calculator = _DummyFeeCalculator()
        self.state_machine = StrategyStateMachine(logger=None)
        self.event_store = _DummyEventStore()
        self.logger = _DummyLogger()
        self.risk = _DummyRisk()
        self.rest = None
        self.run_id = "test"
        self.peak_pnl_pct = {}
        self.snapshots = []

    def live_ordering_enabled(self) -> bool:
        return False

    def kill_switch_on(self) -> bool:
        return False

    def _record_position_snapshot(self, symbol: str, *, trigger: str, note: str = "", correlation_id: str = ""):
        self.snapshots.append((symbol, trigger, note, correlation_id))


def test_order_executor_paper_entry_then_exit_emits_expected_events():
    eng = _Engine()
    ex = OrderExecutor(eng)

    entry = asyncio.run(
        ex.execute_entry(
            symbol="005930",
            qty=2,
            ask=70000.0,
            idempotency_key="k1",
            correlation_id="c1",
        )
    )
    assert entry.filled is True
    pos = eng.state_machine.get_position("005930")
    assert pos is not None

    asyncio.run(
        ex.execute_exit(
            reason="force_exit",
            symbol="005930",
            pos=pos,
            use_market=True,
            idempotency_key="k2",
            correlation_id="c2",
        )
    )

    assert eng.state_machine.get_position("005930") is None
    event_types = [e.type for e in eng.event_store.events]
    assert event_types == [
        "OrderSubmitted",
        "OrderAck",
        "Fill",
        "OrderIntent",
        "OrderSubmitted",
        "OrderAck",
        "Fill",
    ]
    assert len(eng.snapshots) == 2
