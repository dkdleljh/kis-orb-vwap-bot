from __future__ import annotations

import os
import json
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

import main
from core.entry_gates import EntryGateDecision
from core.order_executor import OrderExecutor
from core.position_manager import PositionManager
from models import OrderBookTop, Position
from strategy_state_machine import Signal, State, StrategyStateMachine

FIXTURES_DIR = Path(__file__).parent / "fixtures"
UPDATE_FIXTURES = os.environ.get("UPDATE_GOLDEN_FIXTURES", "0") == "1"


class _DictEventStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, ev) -> None:
        self.events.append(ev.to_dict())


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class _DummyFeeCalculator:
    def get_net_pnl_percent(self, entry: float, exit_price: float) -> float:
        return (exit_price - entry) / entry if entry else 0.0

    def calculate_entry_cost(self, price: float, qty: int):
        return type("_Cost", (), {"commission": 0.12, "tax": 0.0})()

    def calculate_exit_cost(self, price: float, qty: int):
        return type("_Cost", (), {"commission": 0.15, "tax": 0.01})()


class _DummyRisk:
    def __init__(self) -> None:
        self.entries = 0
        self.exits = 0

    def record_entry(self) -> None:
        self.entries += 1

    def record_exit(self, pnl_pct: float, is_stop: bool) -> None:
        self.exits += 1


@dataclass
class _OrderResult:
    order_id: str


class _MockRestLive:
    def __init__(self) -> None:
        self.pos_reads = 0

    async def place_buy_limit(self, symbol: str, qty: int, ask: float):
        return _OrderResult(order_id="OID-BUY-0001")

    async def place_sell_limit(self, symbol: str, qty: int, bid: float):
        return _OrderResult(order_id="OID-SELL-0001")

    async def place_sell_market(self, symbol: str, qty: int):
        return _OrderResult(order_id="OID-SELL-MKT-0001")

    async def get_all_positions(self):
        self.pos_reads += 1
        if self.pos_reads == 1:
            return {"AAPL": {"qty": 2, "avg_price": 100.5}}
        return {"AAPL": {"qty": 0, "avg_price": 0.0}}

    async def cancel_order(self, order_id: str, symbol: str, qty: int):
        return None


class _LiveEngine:
    def __init__(self) -> None:
        self.tz = "Asia/Seoul"
        self.entry_retry_limit = 1
        self.stop_loss_pct = -0.015
        self.last_price = {"AAPL": 101.0}
        self.last_book = {
            "AAPL": OrderBookTop(
                symbol="AAPL",
                bid=100.8,
                ask=101.0,
                bid_size=10,
                ask_size=10,
                timestamp=datetime(2026, 2, 20, 9, 10, 0),
            )
        }
        self.fee_calculator = _DummyFeeCalculator()
        self.state_machine = StrategyStateMachine(logger=None)
        self.event_store = _DictEventStore()
        self.logger = _DummyLogger()
        self.risk = _DummyRisk()
        self.rest = _MockRestLive()
        self.run_id = "run-golden-order-executor"
        self.peak_pnl_pct: dict[str, float] = {}
        self.snapshots: list[tuple[str, str, str, str]] = []

    def live_ordering_enabled(self) -> bool:
        return True

    def kill_switch_on(self) -> bool:
        return False

    def _record_position_snapshot(
        self, symbol: str, *, trigger: str, note: str = "", correlation_id: str = ""
    ) -> None:
        self.snapshots.append((symbol, trigger, note, correlation_id))


class _FixedEntryGate:
    async def evaluate(
        self,
        signal: Signal,
        book: OrderBookTop,
        *,
        idempotency_key: str,
        correlation_id: str,
    ) -> EntryGateDecision:
        return EntryGateDecision(allowed=True, reason="ok", qty=2, cash=50000.0, budget=10000.0)


def _assert_or_write_fixture(name: str, events: list[dict]) -> None:
    path = FIXTURES_DIR / name
    payload = json.dumps(events, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if UPDATE_FIXTURES or (not path.exists()):
        path.write_text(payload, encoding="utf-8")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert events == expected


def _install_deterministic_event_time(monkeypatch: pytest.MonkeyPatch) -> None:
    seq = {"n": 0}

    def _now_iso() -> str:
        n = seq["n"]
        seq["n"] += 1
        return f"2026-02-20T00:00:{n:02d}.000Z"

    import core.events as ievents

    monkeypatch.setattr(ievents.Event, "now_iso", staticmethod(_now_iso))


@pytest.mark.asyncio
async def test_order_executor_entry_exit_events_match_golden(monkeypatch: pytest.MonkeyPatch):
    _install_deterministic_event_time(monkeypatch)

    async def _sleep_noop(_: float) -> None:
        return None

    monkeypatch.setattr("core.order_executor.asyncio.sleep", _sleep_noop)

    eng = _LiveEngine()
    ex = OrderExecutor(eng)

    entry = await ex.execute_entry(
        symbol="AAPL",
        qty=2,
        ask=100.5,
        idempotency_key="idem:AAPL:BUY:20260220T0010",
        correlation_id="corr:AAPL:BUY",
    )
    assert entry.filled is True

    pos = eng.state_machine.get_position("AAPL")
    assert pos is not None

    await ex.execute_exit(
        reason="force_exit",
        symbol="AAPL",
        pos=pos,
        use_market=False,
        idempotency_key="idem:AAPL:SELL:20260220T0011",
        correlation_id="corr:AAPL:SELL",
    )

    _assert_or_write_fixture("order_executor_entry_exit_events.json", eng.event_store.events)


@pytest.mark.asyncio
async def test_trading_engine_entry_exit_events_match_golden(monkeypatch: pytest.MonkeyPatch):
    _install_deterministic_event_time(monkeypatch)

    engine = main.TradingEngine.__new__(main.TradingEngine)
    engine.tz = "Asia/Seoul"
    engine.config = {"trading": {}}
    engine.state_machine = StrategyStateMachine(logger=None)
    engine.state_machine.set_state(State.WAIT_SIGNAL)
    engine.entry_budget_pct = 0.2
    engine.cash_reserve_pct = 0.2
    engine.max_new_entries_per_minute = 10
    engine.max_position_qty = 200
    engine.symbol_cooldown_sec = 60
    engine.symbol_inverse = "INV"
    engine.market_regime = "NEUTRAL"
    engine.max_trades_per_symbol = 10
    engine.max_concurrent_positions = 20
    engine.max_trades_per_day_kr = 50
    engine.max_trades_per_day_us = 50
    engine.trades_today = 0
    engine.trades_today_kr = 0
    engine.trades_today_us = 0
    engine.trades_today_by_symbol = defaultdict(int)
    engine._entry_ts = []
    engine._symbol_cooldown_until = {}
    engine._last_signal_context_by_symbol = {}
    engine._trade_lock = main.asyncio.Lock()
    engine._entry_inflight = set()
    engine._exit_inflight = set()
    engine.event_store = _DictEventStore()
    engine.logger = _DummyLogger()
    engine.risk = _DummyRisk()
    engine.fee_calculator = _DummyFeeCalculator()
    engine.rest = _MockRestLive()
    engine.run_id = "run-golden-trading-engine"
    engine.stop_loss_pct = -0.015
    engine.entry_retry_limit = 1
    engine.last_price = {"INV": 100.7}
    engine.last_book = {}
    engine.peak_pnl_pct = {}
    engine.ws_connected = True
    engine.ws = object()
    engine.oms = None
    engine.ledger = None
    engine._error_counts = {}
    engine.position_manager = PositionManager(engine)
    engine.entry_gates = _FixedEntryGate()
    engine.order_executor = OrderExecutor(engine)

    monkeypatch.setattr(engine, "live_ordering_enabled", lambda: False)
    monkeypatch.setattr(engine, "kill_switch_on", lambda: False)
    monkeypatch.setattr(
        engine,
        "_make_correlation_id",
        lambda symbol, side: f"corr:{symbol}:{side}",
    )

    def _deterministic_idem(symbol: str, side: str, *, bar_start: datetime | None = None) -> str:
        dt = bar_start or datetime(2026, 2, 20, 9, 10, 0)
        return f"idem:{symbol}:{side}:{dt.strftime('%Y%m%dT%H%M')}"

    monkeypatch.setattr(engine, "_make_idempotency_key", _deterministic_idem)

    book = OrderBookTop(
        symbol="INV",
        bid=100.5,
        ask=100.6,
        bid_size=10,
        ask_size=10,
        timestamp=datetime(2026, 2, 20, 9, 10, 0),
    )
    await engine.handle_entry(
        Signal(symbol="INV", side="BUY"),
        book,
        bar_start=datetime(2026, 2, 20, 9, 10, 0),
    )

    pos = engine.state_machine.get_position("INV")
    assert isinstance(pos, Position)

    await engine.handle_exit("force_exit", symbol="INV", use_market=True)

    _assert_or_write_fixture("trading_engine_entry_exit_events.json", engine.event_store.events)
