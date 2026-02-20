from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, time

import pytest

import main
from core.entry_gates import EntryGateEvaluator
from core.order_executor import OrderExecutor
from core.position_manager import PositionManager
from models import OrderBookTop
from risk_manager import RiskManager
from strategy_state_machine import Signal, State, StrategyStateMachine
from utils_time import TimeRules


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


class _DummyRest:
    async def get_cash_available(self, symbol: str, ask: float) -> float:
        return 10000.0


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_day_regression_entry_then_early_forced_exit(monkeypatch):
    engine = main.TradingEngine.__new__(main.TradingEngine)
    engine.tz = "Asia/Seoul"
    engine.state_machine = StrategyStateMachine(logger=None)
    engine.state_machine.set_state(State.WAIT_SIGNAL)
    engine.time_rules = TimeRules(
        observe_start=time(8, 59, 0),
        or_start=time(9, 0, 0),
        or_end=time(9, 5, 0),
        entry_start=time(9, 5, 5),
        force_exit=time(15, 15, 0),
    )
    engine.early_exit = time(15, 0, 0)
    engine._fallback_or_start = None
    engine._fallback_or_end = None

    engine.config = {"trading": {}}
    engine.entry_budget_pct = 0.2
    engine.cash_reserve_pct = 0.2
    engine.max_new_entries_per_minute = 5
    engine.max_position_qty = 200
    engine.symbol_cooldown_sec = 60
    engine.symbol_inverse = "INV"
    engine.market_regime = "NEUTRAL"
    engine.max_trades_per_symbol = 3
    engine.max_concurrent_positions = 20
    engine.max_trades_per_day_kr = 50
    engine.max_trades_per_day_us = 50
    engine.trades_today = 0
    engine.trades_today_kr = 0
    engine.trades_today_us = 0
    engine.trades_today_by_symbol = defaultdict(int)

    engine._entry_ts = deque(maxlen=5000)
    engine._symbol_cooldown_until = {}
    engine._last_signal_context_by_symbol = {}
    engine._trade_lock = main.asyncio.Lock()
    engine._entry_inflight = set()
    engine._exit_inflight = set()

    engine.last_price = {"INV": 100.0}
    engine.last_book = {
        "INV": OrderBookTop(
            symbol="INV",
            bid=99.0,
            ask=100.0,
            bid_size=10,
            ask_size=10,
            timestamp=datetime(2026, 2, 20, 9, 5, 0),
        )
    }

    engine.rest = _DummyRest()
    engine.event_store = _DummyEventStore()
    engine.logger = _DummyLogger()
    engine.risk = RiskManager(10, -0.05, 3)
    engine.fee_calculator = _DummyFeeCalculator()
    engine.stop_loss_pct = -0.015
    engine.entry_retry_limit = 1
    engine.run_id = "test"
    engine.peak_pnl_pct = {}
    engine.oms = None
    engine.ledger = None
    engine._error_counts = defaultdict(int)

    engine.position_manager = PositionManager(engine)
    engine.entry_gates = EntryGateEvaluator(engine)
    engine.order_executor = OrderExecutor(engine)

    monkeypatch.setattr(engine, "kill_switch_on", lambda: False)
    monkeypatch.setattr(engine, "live_ordering_enabled", lambda: False)

    signal = Signal(symbol="INV", side="BUY")
    book = engine.last_book["INV"]
    await engine.handle_entry(signal, book, bar_start=datetime(2026, 2, 20, 9, 6, 0))

    assert engine.state_machine.get_position("INV") is not None

    monkeypatch.setattr(main, "now_local", lambda _tz: datetime(2026, 2, 20, 15, 0, 0))
    await engine.check_force_exit()

    assert engine.state_machine.get_position("INV") is None
    event_types = [e.type for e in engine.event_store.events]
    assert event_types.count("OrderIntent") == 2
    assert event_types.count("Fill") == 2
