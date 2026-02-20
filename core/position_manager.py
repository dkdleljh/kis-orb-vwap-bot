from __future__ import annotations

import asyncio
from typing import Protocol

from models import Position
from utils_time import now_local
from core import events as ievents


class _EngineLike(Protocol):
    tz: str
    rest: object
    state_machine: object
    peak_pnl_pct: dict[str, float]
    max_concurrent_positions: int
    logger: object
    ledger: object | None
    run_id: str
    _error_counts: dict[str, int]

    def _append_event(self, ev: ievents.Event) -> None: ...


class PositionManager:
    """Position/state orchestration extracted from TradingEngine."""

    def __init__(self, engine: _EngineLike) -> None:
        self.engine = engine

    def active_positions(self) -> dict[str, Position]:
        return dict(self.engine.state_machine.positions)

    async def restore_positions(self) -> None:
        restored: dict[str, Position] = {}
        for attempt in range(3):
            pos_map = await self.engine.rest.get_all_positions()
            if pos_map:
                for sym, info in pos_map.items():
                    qty = int(float((info or {}).get("qty", 0) or 0))
                    avg = float((info or {}).get("avg_price", 0.0) or 0.0)
                    if qty > 0 and avg > 0:
                        restored[str(sym)] = Position(
                            symbol=str(sym),
                            qty=qty,
                            avg_price=avg,
                            entry_time=now_local(self.engine.tz),
                        )
                break
            await asyncio.sleep(0.5 * (attempt + 1))

        self.engine.state_machine.positions.clear()
        if restored:
            for sym, pos in restored.items():
                self.engine.state_machine.set_position(pos)
                self.engine.peak_pnl_pct[sym] = -0.01
            if len(restored) > self.engine.max_concurrent_positions:
                self.engine.logger.warning(
                    "Restored positions exceed configured max_concurrent_positions: %s > %s",
                    len(restored),
                    self.engine.max_concurrent_positions,
                )
            try:
                for pos in restored.values():
                    cash = 0.0
                    if self.engine.ledger is not None:
                        lp = self.engine.ledger.get_position(str(pos.symbol))
                        lp.qty = int(pos.qty)
                        lp.avg_price = float(pos.avg_price)
                        cash = float(self.engine.ledger.cash)
                    self.engine._append_event(
                        ievents.PositionRestored(
                            symbol=str(pos.symbol),
                            qty=int(pos.qty),
                            avg_price=float(pos.avg_price),
                            cash=float(cash),
                            equity=None,
                            note="startup_restore",
                        ).to_event(run_id=self.engine.run_id)
                    )
                    self.record_position_snapshot(str(pos.symbol), trigger="restore")
            except Exception:
                pass
            self.engine.logger.info(
                "restored positions: count=%s symbols=%s",
                len(restored),
                list(restored.keys()),
            )
        else:
            self.engine.state_machine.positions.clear()
            self.engine.state_machine.position = None

    def record_position_snapshot(
        self, symbol: str, *, trigger: str, note: str = "", correlation_id: str = ""
    ) -> None:
        qty = 0
        avg = 0.0
        cash = 0.0

        if self.engine.ledger is not None:
            try:
                pos = self.engine.ledger.get_position(symbol)
                qty = int(pos.qty)
                avg = float(pos.avg_price)
                cash = float(self.engine.ledger.cash)
            except Exception:
                self.engine._error_counts["ledger_snapshot"] += 1
        else:
            try:
                pos2 = self.engine.state_machine.get_position(symbol)
                if pos2:
                    qty = int(pos2.qty)
                    avg = float(pos2.avg_price)
            except Exception:
                pass

        try:
            self.engine._append_event(
                ievents.PositionSnapshot(
                    symbol=symbol,
                    qty=qty,
                    avg_price=avg,
                    cash=cash,
                    equity=None,
                    trigger=trigger,
                    note=note,
                    correlation_id=correlation_id,
                ).to_event(run_id=self.engine.run_id)
            )
        except Exception:
            self.engine._error_counts["snapshot_event"] += 1
