from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

from core.entry_gates import EntryGateEvaluator
from models import OrderBookTop
from strategy_state_machine import Signal, State, StrategyStateMachine


class _DummyEventStore:
    def __init__(self) -> None:
        self.events = []

    def append(self, ev):
        self.events.append(ev)


class _DummyLogger:
    def warning(self, *args, **kwargs):
        return None

    def info(self, *args, **kwargs):
        return None


class _DummyRest:
    async def get_cash_available(self, symbol: str, ask: float) -> float:
        return 10000.0


class _Engine:
    def __init__(self) -> None:
        self.config = {"trading": {}}
        self.entry_budget_pct = 0.2
        self.cash_reserve_pct = 0.2
        self.max_new_entries_per_minute = 2
        self.max_position_qty = 200
        self.symbol_cooldown_sec = 900
        self.symbol_inverse = "INV"
        self.market_regime = "NEUTRAL"
        self.max_trades_per_symbol = 3
        self.max_concurrent_positions = 20
        self.max_trades_per_day_kr = 50
        self.max_trades_per_day_us = 50
        self.trades_today_kr = 0
        self.trades_today_us = 0
        self.trades_today_by_symbol = {}
        self._entry_ts = []
        self._symbol_cooldown_until = {}
        self._last_signal_context_by_symbol = {}
        self.last_price = {}
        self.state_machine = StrategyStateMachine(logger=None)
        self.state_machine.set_state(State.WAIT_SIGNAL)
        self.state_machine.news_analyzer = SimpleNamespace(
            get_sentiment_score=lambda _sym: {"score": 0, "summary": "ok"}
        )
        self.event_store = _DummyEventStore()
        self.logger = _DummyLogger()
        self.rest = _DummyRest()
        self.run_id = "test"

    def _active_positions(self):
        return dict(self.state_machine.positions)


def _book(symbol: str = "INV") -> OrderBookTop:
    return OrderBookTop(
        symbol=symbol,
        bid=99.0,
        ask=100.0,
        bid_size=10,
        ask_size=10,
        timestamp=datetime(2026, 2, 20, 9, 5, 0),
    )


def test_entry_gates_blocks_by_rate_limit_with_risk_event():
    eng = _Engine()
    eng._entry_ts = [time.time(), time.time()]
    evaluator = EntryGateEvaluator(eng)

    out = asyncio.run(
        evaluator.evaluate(
            Signal(symbol="INV", side="BUY"),
            _book("INV"),
            idempotency_key="k1",
            correlation_id="c1",
        )
    )

    assert out.allowed is False
    assert out.reason == "entry_rate_limit"
    assert eng.event_store.events[-1].type == "RiskDecision"
    assert eng.event_store.events[-1].payload["reason"] == "entry_rate_limit"


def test_entry_gates_allows_and_computes_qty_from_budget_rules():
    eng = _Engine()
    evaluator = EntryGateEvaluator(eng)

    out = asyncio.run(
        evaluator.evaluate(
            Signal(symbol="INV", side="BUY"),
            _book("INV"),
            idempotency_key="k2",
            correlation_id="c2",
        )
    )

    assert out.allowed is True
    # Default max_symbol_position_pct(0.08) caps 10,000 equity at 800 => qty=8 at ask=100.
    assert out.qty == 8
