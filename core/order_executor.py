from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
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

        for _ in range(self.engine.entry_retry_limit):
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

            await asyncio.sleep(2)
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
            if order.order_id:
                await self.engine.rest.cancel_order(order.order_id, symbol, qty)

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

        current_book = self.engine.last_book.get(symbol)
        if current_book and current_book.bid > 0:
            order = await self.engine.rest.place_sell_limit(symbol, pos.qty, current_book.bid)
            self.engine.logger.info(
                "exit order(SmartLimit) symbol=%s reason=%s price=%s id=%s",
                symbol,
                reason,
                current_book.bid,
                order.order_id,
            )
        else:
            order = await self.engine.rest.place_sell_market(symbol, pos.qty)
            self.engine.logger.info(
                "exit order(Market) symbol=%s reason=%s id=%s",
                symbol,
                reason,
                order.order_id,
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

        await asyncio.sleep(2)
        pos_map = await self.engine.rest.get_all_positions()
        info = (pos_map or {}).get(symbol)
        qty_after = int(float((info or {}).get("qty", 0) or 0))
        if qty_after <= 0:
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
