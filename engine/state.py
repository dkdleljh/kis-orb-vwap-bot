from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, cast

from models import OrderBookTop, Position
from strategy_state_machine import Signal, State
from utils_time import is_after, is_between, now_local

from core import events as ievents
from core.session_rules import exit_phase


def _compat_now_local(tz: str):
    try:
        import main as main_mod  # local import for test monkeypatch compatibility

        fn = getattr(main_mod, "now_local", None)
        if callable(fn):
            return fn(tz)
    except Exception:
        pass
    return now_local(tz)


def _active_positions(self) -> dict[str, Position]:
    return self.position_manager.active_positions()


def _append_event(self, ev: ievents.Event) -> None:
    try:
        self.event_store.append(ev)
    except Exception:
        try:
            self._error_counts["event_append"] += 1
        except Exception:
            pass


def _record_position_snapshot(
    self, symbol: str, *, trigger: str, note: str = "", correlation_id: str = ""
) -> None:
    self.position_manager.record_position_snapshot(
        symbol, trigger=trigger, note=note, correlation_id=correlation_id
    )


def _build_risk_context(
    self,
    *,
    signal: Signal,
    book: OrderBookTop,
    cash: float | None = None,
    equity_est: float | None = None,
    exposure: float | None = None,
    remaining_cap: float | None = None,
    sym_remaining: float | None = None,
    reserve_amt: float | None = None,
    budget_pct: float | None = None,
    budget: float | None = None,
    max_total_position_pct: float | None = None,
    max_symbol_position_pct: float | None = None,
) -> dict[str, Any]:
    return self.entry_gates.build_risk_context(
        signal=signal,
        book=book,
        cash=cash,
        equity_est=equity_est,
        exposure=exposure,
        remaining_cap=remaining_cap,
        sym_remaining=sym_remaining,
        reserve_amt=reserve_amt,
        budget_pct=budget_pct,
        budget=budget,
        max_total_position_pct=max_total_position_pct,
        max_symbol_position_pct=max_symbol_position_pct,
    )


def _make_idempotency_key(
    self, symbol: str, side: str, *, bar_start: datetime | None = None
) -> str:
    from datetime import timezone

    dt = bar_start or _compat_now_local(self.tz)
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        dt_utc = dt.astimezone(timezone.utc)
    except Exception:
        dt_utc = dt

    ts = dt_utc.strftime("%Y%m%dT%H%MZ")
    return f"{self.run_id}:{symbol}:{side}:{ts}"


def _make_correlation_id(self, symbol: str, side: str) -> str:
    from core.correlation import new_corr

    s = (side or "").strip().upper() or "X"
    return new_corr(f"eng_{s.lower()}")


async def handle_entry(
    self, signal: Signal, book: OrderBookTop, *, bar_start: datetime | None = None
) -> None:
    if self.state_machine.state != State.WAIT_SIGNAL:
        return

    symbol = cast(str, signal.symbol)
    side = str(signal.side or "BUY")

    if self.kill_switch_on():
        self.logger.info("[Entry] blocked by kill switch symbol=%s", symbol)
        return
    if self.state_machine.in_position(symbol):
        self.logger.info("[Entry] skipped; symbol already held symbol=%s", symbol)
        return
    if self.state_machine.active_position_count() >= self.max_concurrent_positions:
        self.logger.info(
            "[Entry] blocked by max_concurrent_positions (%s/%s)",
            self.state_machine.active_position_count(),
            self.max_concurrent_positions,
        )
        return

    idempotency_key = self._make_idempotency_key(symbol, side, bar_start=bar_start)
    correlation_id = self._make_correlation_id(symbol, side)

    async with self._trade_lock:
        if symbol in self._entry_inflight:
            self.logger.debug(f"[Entry] skipped: inflight symbol={symbol}")
            return
        self._entry_inflight.add(symbol)

    try:
        gate = await self.entry_gates.evaluate(
            signal, book, idempotency_key=idempotency_key, correlation_id=correlation_id
        )
        if not gate.allowed or gate.qty <= 0:
            return

        qty = int(gate.qty)
        is_kr = bool(str(symbol).isdigit())
        try:
            if self.symbol_cooldown_sec > 0:
                self._symbol_cooldown_until[str(symbol)] = time.time() + float(
                    self.symbol_cooldown_sec
                )

            self.event_store.append(
                ievents.OrderIntent(
                    symbol=symbol,
                    side="BUY",
                    qty=int(qty),
                    order_type="LMT",
                    limit_price=float(book.ask),
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                    module="engine_orb_vwap",
                ).to_event(run_id=self.run_id)
            )
            self.event_store.append(
                ievents.RiskDecision(
                    symbol=symbol,
                    allowed=True,
                    reason="ok",
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                    module="engine_orb_vwap",
                ).to_event(run_id=self.run_id)
            )
        except Exception:
            pass

        if self.oms is not None:
            try:
                self.oms.register_intent(
                    symbol=symbol,
                    side="BUY",
                    qty=int(qty),
                    idempotency_key=idempotency_key,
                )
                self.oms.mark_risk(idempotency_key=idempotency_key, allowed=True)
            except Exception:
                self._error_counts["oms_entry"] += 1

        self.risk.record_entry()
        try:
            self._entry_ts.append(time.time())
        except Exception:
            pass
        self.trades_today += 1
        self.trades_today_by_symbol[symbol] += 1
        if is_kr:
            self.trades_today_kr += 1
        else:
            self.trades_today_us += 1

        await self.order_executor.execute_entry(
            symbol=symbol,
            qty=qty,
            ask=float(book.ask),
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
    finally:
        self._entry_inflight.discard(symbol)


async def handle_exit(
    self, reason: str, *, symbol: str | None = None, use_market: bool = True
) -> None:
    if symbol is None:
        symbols = list(self._active_positions().keys())
        for sym in symbols:
            await self.handle_exit(reason, symbol=sym, use_market=use_market)
        return
    sym = str(symbol)
    pos = self.state_machine.get_position(sym)
    if not pos:
        return
    if sym in self._exit_inflight:
        return
    self._exit_inflight.add(sym)

    idempotency_key = self._make_idempotency_key(sym, "SELL")
    correlation_id = self._make_correlation_id(sym, "SELL")
    try:
        await self.order_executor.execute_exit(
            reason=reason,
            symbol=sym,
            pos=pos,
            use_market=use_market,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
    finally:
        if self.state_machine.state != State.DONE_TODAY:
            self.state_machine.set_state(State.WAIT_SIGNAL)
        self._exit_inflight.discard(sym)


def update_state_by_time(self) -> None:
    now_dt = _compat_now_local(self.tz)

    phase, final_kill_time = exit_phase(now_dt, self.early_exit, self.time_rules.force_exit)

    if phase == "emergency":
        if self.state_machine.active_position_count() > 0:
            self.logger.warning(
                "EMERGENCY EXIT (%s): Dumping all positions by market order!",
                final_kill_time,
            )
            asyncio.create_task(
                self.handle_exit(
                    "emergency_market_close", symbol=None, use_market=True
                )
            )

        if self.state_machine.state != State.DONE_TODAY:
            self.state_machine.set_state(State.DONE_TODAY)
        return

    if phase == "early":
        if self.state_machine.active_position_count() > 0:
            self.logger.warning(
                " EARLY EXIT (15:00): Closing all positions! count=%s",
                self.state_machine.active_position_count(),
            )
            asyncio.create_task(self.handle_exit("early_exit_15_00", symbol=None))

        if self.state_machine.state != State.DONE_TODAY:
            self.state_machine.set_state(State.DONE_TODAY)
        return

    if phase == "force":
        if self.state_machine.active_position_count() > 0:
            asyncio.create_task(self.handle_exit("force_exit", symbol=None))

        if self.state_machine.state != State.DONE_TODAY:
            self.state_machine.set_state(State.DONE_TODAY)
        return

    if self.state_machine.state == State.DONE_TODAY:
        return

    if self._fallback_or_start and self._fallback_or_end:
        if self._fallback_or_start <= now_dt < self._fallback_or_end:
            if self.state_machine.state != State.BUILD_OR:
                self.state_machine.set_state(State.BUILD_OR)
            return
        if (
            now_dt >= self._fallback_or_end
            and self.state_machine.state == State.BUILD_OR
        ):
            self.state_machine.set_state(State.WAIT_SIGNAL)
            return

    if is_between(self.time_rules.or_start, self.time_rules.or_end, now_dt):
        if self.state_machine.state != State.BUILD_OR:
            self.state_machine.set_state(State.BUILD_OR)
        return

    if is_after(self.time_rules.entry_start, now_dt):
        if self.state_machine.state in (State.WAIT_OPEN, State.BUILD_OR):
            self.state_machine.set_state(State.WAIT_SIGNAL)
        return

    if not is_after(self.time_rules.observe_start, now_dt):
        self.state_machine.set_state(State.WAIT_OPEN)


async def check_kill_switch(self) -> None:
    if self.kill_switch_on() and self.state_machine.state == State.WAIT_SIGNAL:
        self.state_machine.set_state(State.DONE_TODAY)
        self.logger.info("kill switch on; no new entries")


async def check_force_exit(self) -> None:
    now_dt = _compat_now_local(self.tz)

    if self.state_machine.active_position_count() > 0:
        phase, _ = exit_phase(now_dt, self.early_exit, self.time_rules.force_exit)
        if phase == "emergency":
            await self.handle_exit("emergency_market_close", symbol=None, use_market=True)
            return
        if phase == "early":
            self.logger.warning(
                " EARLY EXIT (15:00): Closing all positions! count=%s",
                self.state_machine.active_position_count(),
            )
            await self.handle_exit("early_exit_15_00", symbol=None)
            return

        if phase == "force":
            await self.handle_exit("force_exit", symbol=None)
