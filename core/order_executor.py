from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from models import OrderBookTop, Position
from utils_time import now_local

from core import events as ievents


class _EngineLike(Protocol):
    tz: str
    entry_retry_limit: int
    stop_loss_pct: float
    last_price: dict[str, float]
    last_book: dict[str, OrderBookTop]
    fee_calculator: object
    state_machine: object
    event_store: object
    logger: object
    risk: object
    rest: object
    run_id: str
    peak_pnl_pct: dict[str, float]

    def live_ordering_enabled(self) -> bool: ...
    def kill_switch_on(self) -> bool: ...
    def _record_position_snapshot(
        self, symbol: str, *, trigger: str, note: str = "", correlation_id: str = ""
    ) -> None: ...


@dataclass
class EntryExecutionResult:
    filled: bool


class OrderExecutor:
    """Thin order execution wrapper around KISRestOrders + event logging."""

    def __init__(self, engine: _EngineLike) -> None:
        self.engine = engine

    def _execution_cfg(self) -> dict:
        return (
            (getattr(self.engine, "config", {}) or {})
            .get("trading", {})
            .get("execution", {})
            or {}
        )

    def _slippage_cfg(self) -> dict:
        return (
            (getattr(self.engine, "config", {}) or {})
            .get("trading", {})
            .get("slippage_guard", {})
            or {}
        )

    @staticmethod
    def _slippage_bps(*, side: str, expected: float, filled: float) -> float | None:
        if expected <= 0 or filled <= 0:
            return None
        if str(side).upper() == "BUY":
            return ((filled - expected) / expected) * 10000.0
        return ((expected - filled) / expected) * 10000.0

    def _track_slippage(
        self,
        *,
        symbol: str,
        side: str,
        expected_price: float,
        fill_price: float,
        correlation_id: str,
    ) -> tuple[float | None, float | None, float | None]:
        cfg = self._slippage_cfg()
        if not bool(cfg.get("enabled", False)):
            return None, None, None

        slippage_bps = self._slippage_bps(
            side=side, expected=expected_price, filled=fill_price
        )
        if slippage_bps is None:
            return None, None, None

        window = max(3, int(cfg.get("rolling_window", 20) or 20))
        stats_map = getattr(self.engine, "slippage_stats_by_symbol", None)
        if not isinstance(stats_map, dict):
            stats_map = {}
            setattr(self.engine, "slippage_stats_by_symbol", stats_map)

        buf = stats_map.get(symbol)
        if not isinstance(buf, deque):
            buf = deque(maxlen=window)
            stats_map[symbol] = buf
        elif int(getattr(buf, "maxlen", 0) or 0) != window:
            buf = deque(list(buf), maxlen=window)
            stats_map[symbol] = buf

        buf.append(float(slippage_bps))
        values = list(buf)
        avg_bps = sum(values) / max(1, len(values))
        worst_abs_bps = max(abs(v) for v in values) if values else 0.0

        try:
            self.engine.event_store.append(
                ievents.Event.make(
                    type="SlippageObservation",
                    symbol=symbol,
                    run_id=getattr(self.engine, "run_id", None),
                    payload={
                        "side": str(side).upper(),
                        "expected_price": float(expected_price),
                        "fill_price": float(fill_price),
                        "slippage_bps": float(slippage_bps),
                        "rolling_avg_bps": float(avg_bps),
                        "rolling_worst_abs_bps": float(worst_abs_bps),
                        "window": int(window),
                        "correlation_id": str(correlation_id or ""),
                    },
                )
            )
        except Exception:
            pass

        max_avg = float(cfg.get("max_avg_slippage_bps", 35.0) or 35.0)
        max_worst = float(cfg.get("max_worst_slippage_bps", 80.0) or 80.0)
        min_samples = max(1, int(cfg.get("min_samples_before_block", 5) or 5))
        if len(values) >= min_samples and (
            abs(avg_bps) >= max_avg or worst_abs_bps >= max_worst
        ):
            block_sec = max(60, int(cfg.get("block_seconds", 1800) or 1800))
            block_until_map = getattr(self.engine, "slippage_block_until", None)
            if not isinstance(block_until_map, dict):
                block_until_map = {}
                setattr(self.engine, "slippage_block_until", block_until_map)
            block_until = time.time() + float(block_sec)
            block_until_map[symbol] = block_until
            try:
                self.engine.event_store.append(
                    ievents.Event.make(
                        type="SlippageGuardBlock",
                        symbol=symbol,
                        run_id=getattr(self.engine, "run_id", None),
                        payload={
                            "rolling_avg_bps": float(avg_bps),
                            "rolling_worst_abs_bps": float(worst_abs_bps),
                            "threshold_avg_bps": float(max_avg),
                            "threshold_worst_bps": float(max_worst),
                            "sample_count": len(values),
                            "block_seconds": int(block_sec),
                            "block_until_epoch": float(block_until),
                        },
                    )
                )
            except Exception:
                pass

        return float(slippage_bps), float(avg_bps), float(worst_abs_bps)

    async def execute_entry(
        self,
        *,
        symbol: str,
        qty: int,
        ask: float,
        idempotency_key: str,
        correlation_id: str,
    ) -> EntryExecutionResult:
        if not self.engine.live_ordering_enabled():
            pos = Position(
                symbol=symbol,
                qty=qty,
                avg_price=float(ask),
                entry_time=now_local(self.engine.tz),
            )
            self.engine.state_machine.set_position(pos)
            self.engine.logger.info(
                "[PAPER] entry simulated %s qty=%s price=%s", symbol, qty, ask
            )
            try:
                self.engine.event_store.append(
                    ievents.OrderSubmitted(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id="PAPER",
                        correlation_id=correlation_id,
                    ).to_event(run_id=self.engine.run_id)
                )
                self.engine.event_store.append(
                    ievents.OrderAck(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id="PAPER",
                        status="ACK",
                        correlation_id=correlation_id,
                    ).to_event(run_id=self.engine.run_id)
                )
                self.engine.event_store.append(
                    ievents.Fill(
                        symbol=symbol,
                        side="BUY",
                        qty=int(qty),
                        price=float(ask),
                        broker_order_id="PAPER",
                        idempotency_key=idempotency_key,
                        fee=0.0,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.engine.run_id)
                )
                self.engine._record_position_snapshot(
                    symbol,
                    trigger="fill",
                    note="paper_entry",
                    correlation_id=correlation_id,
                )
            except Exception:
                pass
            self.engine.peak_pnl_pct[symbol] = -0.01
            return EntryExecutionResult(filled=True)

        ecfg = self._execution_cfg()
        poll_sec = max(0.2, float(ecfg.get("entry_poll_sec", 2.0) or 2.0))
        cancel_enabled = bool(ecfg.get("cancel_unfilled_entry", True))
        max_cancel_attempts = max(0, int(ecfg.get("entry_max_cancel_attempts", 1) or 1))
        cancel_attempts = 0

        for _ in range(max(1, int(self.engine.entry_retry_limit))):
            if self.engine.kill_switch_on():
                break
            order = await self.engine.rest.place_buy_limit(symbol, qty, ask)
            self.engine.logger.info(
                "entry order submitted %s qty=%s id=%s", symbol, qty, order.order_id
            )
            try:
                broker_order_id = str(order.order_id or "")
                self.engine.event_store.append(
                    ievents.OrderSubmitted(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id=broker_order_id,
                        correlation_id=correlation_id,
                    ).to_event(run_id=self.engine.run_id)
                )
                if broker_order_id:
                    self.engine.event_store.append(
                        ievents.OrderAck(
                            symbol=symbol,
                            idempotency_key=idempotency_key,
                            broker_order_id=broker_order_id,
                            status="ACK",
                            correlation_id=correlation_id,
                        ).to_event(run_id=self.engine.run_id)
                    )
            except Exception:
                pass

            await asyncio.sleep(poll_sec)
            pos_map = await self.engine.rest.get_all_positions()
            info = (pos_map or {}).get(symbol)
            qty_after = int(float((info or {}).get("qty", 0) or 0))
            avg_after = float((info or {}).get("avg_price", 0.0) or 0.0)
            if qty_after >= qty and avg_after > 0:
                pos = Position(
                    symbol=symbol,
                    qty=qty_after,
                    avg_price=avg_after,
                    entry_time=now_local(self.engine.tz),
                )
                self.engine.state_machine.set_position(pos)
                self.engine.logger.info(
                    "entry filled %s qty=%s avg=%s", symbol, pos.qty, pos.avg_price
                )
                try:
                    broker_order_id = str(order.order_id or "")
                    costs = self.engine.fee_calculator.calculate_entry_cost(
                        float(pos.avg_price), int(pos.qty)
                    )
                    slippage_bps, _, _ = self._track_slippage(
                        symbol=symbol,
                        side="BUY",
                        expected_price=float(ask),
                        fill_price=float(pos.avg_price),
                        correlation_id=correlation_id,
                    )
                    self.engine.event_store.append(
                        ievents.Fill(
                            symbol=symbol,
                            side="BUY",
                            qty=int(pos.qty),
                            price=float(pos.avg_price),
                            broker_order_id=broker_order_id,
                            idempotency_key=idempotency_key,
                            fee=costs.commission + costs.tax,
                            correlation_id=correlation_id,
                            module="engine_orb_vwap",
                            expected_price=(float(ask) if slippage_bps is not None else None),
                            slippage_bps=slippage_bps,
                        ).to_event(run_id=self.engine.run_id)
                    )
                    self.engine._record_position_snapshot(
                        symbol,
                        trigger="fill",
                        note="live_entry",
                        correlation_id=correlation_id,
                    )
                except Exception:
                    pass
                self.engine.peak_pnl_pct[symbol] = -0.01
                return EntryExecutionResult(filled=True)
            if (
                cancel_enabled
                and order.order_id
                and cancel_attempts < max_cancel_attempts
            ):
                await self.engine.rest.cancel_order(order.order_id, symbol, qty)
                cancel_attempts += 1

        self.engine.logger.info("entry failed after retries")
        await asyncio.sleep(30)
        return EntryExecutionResult(filled=False)

    async def execute_exit(
        self,
        *,
        reason: str,
        symbol: str,
        pos: Position,
        use_market: bool,
        idempotency_key: str,
        correlation_id: str,
    ) -> None:
        self.engine.event_store.append(
            ievents.OrderIntent(
                symbol=symbol,
                side="SELL",
                qty=int(pos.qty),
                order_type="MKT" if use_market else "LMT",
                limit_price=None,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                module="engine_orb_vwap",
            ).to_event(run_id=self.engine.run_id)
        )

        if not self.engine.live_ordering_enabled():
            exit_price = self.engine.last_price.get(symbol, pos.avg_price)
            gross_pnl_pct = (
                (exit_price - pos.avg_price) / pos.avg_price if pos.avg_price else 0.0
            )
            net_pnl_pct = self.engine.fee_calculator.get_net_pnl_percent(
                pos.avg_price, exit_price
            )
            pnl_pct = net_pnl_pct
            self.engine.risk.record_exit(pnl_pct, pnl_pct <= self.engine.stop_loss_pct)
            self.engine.state_machine.remove_position(symbol)
            self.engine.peak_pnl_pct.pop(symbol, None)
            try:
                self.engine.event_store.append(
                    ievents.OrderSubmitted(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id="PAPER",
                        correlation_id=correlation_id,
                    ).to_event(run_id=self.engine.run_id)
                )
                self.engine.event_store.append(
                    ievents.OrderAck(
                        symbol=symbol,
                        idempotency_key=idempotency_key,
                        broker_order_id="PAPER",
                        status="ACK",
                        correlation_id=correlation_id,
                    ).to_event(run_id=self.engine.run_id)
                )
                self.engine.event_store.append(
                    ievents.Fill(
                        symbol=symbol,
                        side="SELL",
                        qty=int(pos.qty),
                        price=float(exit_price),
                        broker_order_id="PAPER",
                        idempotency_key=idempotency_key,
                        fee=0.0,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.engine.run_id)
                )
                self.engine._record_position_snapshot(
                    symbol,
                    trigger="fill",
                    note=f"paper_exit:{reason}",
                    correlation_id=correlation_id,
                )
            except Exception:
                pass
            self.engine.logger.info(
                "[PAPER] exit simulated symbol=%s reason=%s pnl=%.4f(gross=%.4f, net=%.4f)",
                symbol,
                reason,
                pnl_pct,
                gross_pnl_pct,
                net_pnl_pct,
            )
            return

        ecfg = self._execution_cfg()
        smart_limit_exit = bool(ecfg.get("enable_smart_limit_exit", False))
        exit_retry_limit = max(1, int(ecfg.get("exit_retry_limit", 2) or 2))
        exit_poll_sec = max(0.2, float(ecfg.get("exit_poll_sec", 2.0) or 2.0))
        exit_max_cancel_attempts = max(0, int(ecfg.get("exit_max_cancel_attempts", 1) or 1))
        cancel_attempts = 0
        expected_exit_price = float(self.engine.last_price.get(symbol, pos.avg_price) or pos.avg_price)
        order = None
        order_submitted = False

        for attempt in range(exit_retry_limit):
            current_book = self.engine.last_book.get(symbol)
            should_limit = (
                current_book is not None
                and float(current_book.bid) > 0
                and ((not use_market) or smart_limit_exit)
            )
            if should_limit:
                expected_exit_price = float(current_book.bid)
                order = await self.engine.rest.place_sell_limit(
                    symbol, pos.qty, float(current_book.bid)
                )
                self.engine.logger.info(
                    "exit order(SmartLimit) symbol=%s reason=%s price=%s id=%s try=%s/%s",
                    symbol,
                    reason,
                    current_book.bid,
                    order.order_id,
                    attempt + 1,
                    exit_retry_limit,
                )
            else:
                if use_market:
                    expected_exit_price = float(
                        self.engine.last_price.get(symbol, pos.avg_price) or pos.avg_price
                    )
                order = await self.engine.rest.place_sell_market(symbol, pos.qty)
                self.engine.logger.info(
                    "exit order(Market) symbol=%s reason=%s id=%s try=%s/%s",
                    symbol,
                    reason,
                    order.order_id,
                    attempt + 1,
                    exit_retry_limit,
                )

            if not order_submitted:
                try:
                    broker_order_id = str(order.order_id or "")
                    self.engine.event_store.append(
                        ievents.OrderSubmitted(
                            symbol=symbol,
                            idempotency_key=idempotency_key,
                            broker_order_id=broker_order_id,
                            correlation_id=correlation_id,
                        ).to_event(run_id=self.engine.run_id)
                    )
                    if broker_order_id:
                        self.engine.event_store.append(
                            ievents.OrderAck(
                                symbol=symbol,
                                idempotency_key=idempotency_key,
                                broker_order_id=broker_order_id,
                                status="ACK",
                                correlation_id=correlation_id,
                            ).to_event(run_id=self.engine.run_id)
                        )
                    order_submitted = True
                except Exception:
                    pass

            await asyncio.sleep(exit_poll_sec)
            pos_map = await self.engine.rest.get_all_positions()
            info = (pos_map or {}).get(symbol)
            qty_after = int(float((info or {}).get("qty", 0) or 0))
            if qty_after <= 0:
                break

            if (
                order is not None
                and getattr(order, "order_id", None)
                and cancel_attempts < exit_max_cancel_attempts
                and should_limit
            ):
                await self.engine.rest.cancel_order(order.order_id, symbol, pos.qty)
                cancel_attempts += 1

        pos_map = await self.engine.rest.get_all_positions()
        info = (pos_map or {}).get(symbol)
        qty_after = int(float((info or {}).get("qty", 0) or 0))
        if qty_after <= 0 and order is not None:
            exit_price = self.engine.last_price.get(symbol, pos.avg_price)
            pnl_pct = self.engine.fee_calculator.get_net_pnl_percent(pos.avg_price, exit_price)
            self.engine.risk.record_exit(pnl_pct, pnl_pct <= self.engine.stop_loss_pct)
            self.engine.state_machine.remove_position(symbol)
            self.engine.peak_pnl_pct.pop(symbol, None)
            try:
                broker_order_id = str(order.order_id or "")
                costs = self.engine.fee_calculator.calculate_exit_cost(
                    float(exit_price), int(pos.qty)
                )
                slippage_bps, _, _ = self._track_slippage(
                    symbol=symbol,
                    side="SELL",
                    expected_price=float(expected_exit_price),
                    fill_price=float(exit_price),
                    correlation_id=correlation_id,
                )
                self.engine.event_store.append(
                    ievents.Fill(
                        symbol=symbol,
                        side="SELL",
                        qty=int(pos.qty),
                        price=float(exit_price),
                        broker_order_id=broker_order_id,
                        idempotency_key=idempotency_key,
                        fee=costs.commission + costs.tax,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                        expected_price=(
                            float(expected_exit_price) if slippage_bps is not None else None
                        ),
                        slippage_bps=slippage_bps,
                    ).to_event(run_id=self.engine.run_id)
                )
                self.engine._record_position_snapshot(
                    symbol,
                    trigger="fill",
                    note=f"live_exit:{reason}",
                    correlation_id=correlation_id,
                )
            except Exception:
                pass
            self.engine.logger.info(
                "exit done symbol=%s reason=%s pnl=%.4f", symbol, reason, pnl_pct
            )
