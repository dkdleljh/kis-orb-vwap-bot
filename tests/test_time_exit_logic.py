"""Tests for time-driven exit phase handling in TradingEngine."""

from __future__ import annotations

from datetime import datetime, time

import main
from strategy_state_machine import State
from utils_time import TimeRules


class _DummyStateMachine:
    def __init__(self, count: int) -> None:
        self._count = count
        self.state = State.WAIT_SIGNAL

    def active_position_count(self) -> int:
        return self._count

    def set_state(self, new_state: State) -> None:
        self.state = new_state


class _DummyLogger:
    def warning(self, *args, **kwargs):
        return None


def _build_engine(position_count: int = 1):
    engine = main.TradingEngine.__new__(main.TradingEngine)
    engine.tz = "Asia/Seoul"
    engine.time_rules = TimeRules(
        observe_start=time(8, 59, 0),
        or_start=time(9, 0, 0),
        or_end=time(9, 5, 0),
        entry_start=time(9, 5, 5),
        force_exit=time(15, 15, 0),
    )
    engine.early_exit = time(15, 0, 0)
    engine.state_machine = _DummyStateMachine(position_count)
    engine._fallback_or_start = None
    engine._fallback_or_end = None
    engine.logger = _DummyLogger()
    return engine


def _patch_exit_capture(monkeypatch, engine):
    calls = []

    def _handle_exit(reason, symbol=None, use_market=False):
        calls.append({"reason": reason, "symbol": symbol, "use_market": use_market})

        async def _noop():
            return None

        return _noop()

    def _create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(engine, "handle_exit", _handle_exit)
    monkeypatch.setattr(main.asyncio, "create_task", _create_task)
    return calls


def test_update_state_by_time_early_exit_1500(monkeypatch):
    engine = _build_engine(position_count=2)
    calls = _patch_exit_capture(monkeypatch, engine)
    monkeypatch.setattr(main, "now_local", lambda _tz: datetime(2026, 2, 20, 15, 0, 0))

    engine.update_state_by_time()

    assert engine.state_machine.state == State.DONE_TODAY
    assert calls == [{"reason": "early_exit_15_00", "symbol": None, "use_market": False}]


def test_update_state_by_time_force_exit_1515(monkeypatch):
    engine = _build_engine(position_count=1)
    calls = _patch_exit_capture(monkeypatch, engine)
    monkeypatch.setattr(main, "now_local", lambda _tz: datetime(2026, 2, 20, 15, 15, 0))

    engine.update_state_by_time()

    assert engine.state_machine.state == State.DONE_TODAY
    assert calls == [{"reason": "force_exit", "symbol": None, "use_market": False}]


def test_update_state_by_time_emergency_exit_1518(monkeypatch):
    engine = _build_engine(position_count=1)
    calls = _patch_exit_capture(monkeypatch, engine)
    monkeypatch.setattr(main, "now_local", lambda _tz: datetime(2026, 2, 20, 15, 18, 0))

    engine.update_state_by_time()

    assert engine.state_machine.state == State.DONE_TODAY
    assert calls == [{"reason": "emergency_market_close", "symbol": None, "use_market": True}]
