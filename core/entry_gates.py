from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Dict, Protocol

from models import OrderBookTop
from strategy_state_machine import Signal

from core import events as ievents
from core.session_rules import resolve_entry_risk_bounds


class _EngineLike(Protocol):
    config: dict[str, Any]
    entry_budget_pct: float
    cash_reserve_pct: float
    max_new_entries_per_minute: int
    max_position_qty: int
    symbol_cooldown_sec: int
    symbol_inverse: str
    market_regime: str
    max_trades_per_symbol: int
    max_concurrent_positions: int
    max_trades_per_day_kr: int
    max_trades_per_day_us: int
    trades_today_kr: int
    trades_today_us: int
    trades_today_by_symbol: dict[str, int]
    _entry_ts: Any
    _symbol_cooldown_until: dict[str, float]
    _last_signal_context_by_symbol: dict[str, dict[str, Any]]
    last_price: dict[str, float]
    state_machine: object
    event_store: object
    logger: object
    rest: object
    run_id: str

    def _active_positions(self) -> dict[str, Any]: ...


@dataclass
class EntryGateDecision:
    allowed: bool
    reason: str
    qty: int
    cash: float
    budget: float


class EntryGateEvaluator:
    """Entry risk gating and exposure computation."""

    def __init__(self, engine: _EngineLike) -> None:
        self.engine = engine

    @staticmethod
    def _age_seconds(ts: datetime | None) -> float | None:
        if ts is None:
            return None
        try:
            if ts.tzinfo is None:
                ts_utc = ts.replace(tzinfo=timezone.utc)
            else:
                ts_utc = ts.astimezone(timezone.utc)
            now_utc = datetime.now(timezone.utc)
            return float((now_utc - ts_utc).total_seconds())
        except Exception:
            return None

    def build_risk_context(
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
        ctx: Dict[str, Any] = {}
        sig_ctx = self.engine._last_signal_context_by_symbol.get(str(signal.symbol), {})
        numeric_fields = {
            "cash": cash,
            "equity_est": equity_est,
            "exposure": exposure,
            "remaining_cap": remaining_cap,
            "sym_remaining": sym_remaining,
            "reserve_amt": reserve_amt,
            "budget_pct": budget_pct,
            "budget": budget,
            "ask": float(getattr(book, "ask", 0.0) or 0.0),
            "max_total_position_pct": max_total_position_pct,
            "max_symbol_position_pct": max_symbol_position_pct,
            "cash_reserve_pct": float(self.engine.cash_reserve_pct),
            "max_new_entries_per_minute": int(self.engine.max_new_entries_per_minute),
            "min_score": sig_ctx.get("min_score"),
            "market_regime": str(self.engine.market_regime or ""),
        }
        for k, v in numeric_fields.items():
            if isinstance(v, (int, float)):
                ctx[k] = float(v) if isinstance(v, float) else int(v)
            else:
                ctx[k] = v
        return ctx

    async def evaluate(self, signal: Signal, book: OrderBookTop, *, idempotency_key: str, correlation_id: str) -> EntryGateDecision:
        symbol = str(signal.symbol)
        tcfg = self.engine.config.get("trading", {}) or {}
        micro_cfg = (tcfg.get("microstructure_filter", {}) or {})
        sl_cfg = (tcfg.get("slippage_guard", {}) or {})
        news_safety_mode = bool(tcfg.get("news_safety_mode", False))
        bounds = resolve_entry_risk_bounds(tcfg)
        cash = 0.0
        exposure = 0.0
        equity_est = 0.0
        remaining_cap = 0.0
        sym_remaining = 0.0
        reserve_amt = 0.0
        budget_pct = float(self.engine.entry_budget_pct)
        budget = 0.0
        max_total_position_pct = float(bounds.max_total_position_pct)
        max_symbol_position_pct = float(bounds.max_symbol_position_pct)

        try:
            now_s = time.time()
            while self.engine._entry_ts and (now_s - float(self.engine._entry_ts[0])) > 60.0:
                self.engine._entry_ts.popleft()
            if len(self.engine._entry_ts) >= int(self.engine.max_new_entries_per_minute):
                try:
                    self.engine.event_store.append(
                        ievents.RiskDecision(
                            symbol=symbol,
                            allowed=False,
                            reason="entry_rate_limit",
                            idempotency_key=idempotency_key,
                            correlation_id=correlation_id,
                            module="engine_orb_vwap",
                            context=self.build_risk_context(
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
                            ),
                        ).to_event(run_id=self.engine.run_id)
                    )
                except Exception:
                    pass
                self.engine.logger.warning(
                    "entry blocked by rate limit: %s entries/60s (limit=%s)",
                    len(self.engine._entry_ts),
                    self.engine.max_new_entries_per_minute,
                )
                return EntryGateDecision(False, "entry_rate_limit", 0, cash, budget)
        except Exception:
            pass

        # Allow a temporary override (e.g. "today only") to force-enable entries.
        # NOTE: This increases slippage/false entries risk.
        if str(os.environ.get("KIS_DISABLE_MICROSTRUCTURE_FILTER", "0")).strip() in {"1", "true", "TRUE", "yes", "YES"}:
            micro_cfg = dict(micro_cfg)
            micro_cfg["enabled"] = False

        if bool(micro_cfg.get("enabled", False)):
            max_spread_pct = float(micro_cfg.get("max_spread_pct", 0.003) or 0.003)
            max_book_age_sec = float(micro_cfg.get("max_book_age_sec", 3.0) or 3.0)
            min_depth_ratio = float(micro_cfg.get("min_quote_depth_ratio", 0.60) or 0.60)
            spread_pct = 0.0
            if float(book.ask) > 0 and float(book.bid) > 0:
                spread_pct = (float(book.ask) - float(book.bid)) / float(book.ask)
            quote_depth_ratio = 0.0
            max_depth = max(float(book.bid_size or 0.0), float(book.ask_size or 0.0))
            if max_depth > 0:
                quote_depth_ratio = min(float(book.bid_size or 0.0), float(book.ask_size or 0.0)) / max_depth
            book_age_sec = self._age_seconds(getattr(book, "timestamp", None))

            micro_failed = (
                spread_pct > max_spread_pct
                or quote_depth_ratio < min_depth_ratio
                or (book_age_sec is None)
                or (book_age_sec > max_book_age_sec)
            )
            if micro_failed:
                try:
                    self.engine.event_store.append(
                        ievents.RiskDecision(
                            symbol=symbol,
                            allowed=False,
                            reason="microstructure_filter",
                            idempotency_key=idempotency_key,
                            correlation_id=correlation_id,
                            module="engine_orb_vwap",
                            context={
                                "spread_pct": float(spread_pct),
                                "max_spread_pct": float(max_spread_pct),
                                "quote_depth_ratio": float(quote_depth_ratio),
                                "min_quote_depth_ratio": float(min_depth_ratio),
                                "book_age_sec": book_age_sec,
                                "max_book_age_sec": float(max_book_age_sec),
                            },
                        ).to_event(run_id=self.engine.run_id)
                    )
                except Exception:
                    pass
                return EntryGateDecision(False, "microstructure_filter", 0, cash, budget)

        if bool(sl_cfg.get("enabled", False)):
            block_map = getattr(self.engine, "slippage_block_until", None)
            block_until = 0.0
            if isinstance(block_map, dict):
                block_until = float(block_map.get(symbol, 0.0) or 0.0)
            if block_until and time.time() < block_until:
                try:
                    self.engine.event_store.append(
                        ievents.RiskDecision(
                            symbol=symbol,
                            allowed=False,
                            reason="slippage_blocklist",
                            idempotency_key=idempotency_key,
                            correlation_id=correlation_id,
                            module="engine_orb_vwap",
                            context={"block_until_epoch": float(block_until)},
                        ).to_event(run_id=self.engine.run_id)
                    )
                except Exception:
                    pass
                return EntryGateDecision(False, "slippage_blocklist", 0, cash, budget)

        if symbol != self.engine.symbol_inverse:
            try:
                news_result = await asyncio.wait_for(
                    self.engine.state_machine.news_analyzer.get_sentiment_score(symbol),
                    timeout=3.0,
                )
                score = news_result.get("score", 0)
                summary = str(news_result.get("summary") or "")
                news_unavailable = (
                    summary in {"HTTP_ERROR", "ERROR", "NO_NEWS"}
                    or "429" in summary
                )
                self.engine.logger.info(
                    f"[News Filter] {symbol} Score: {score} ({summary})"
                )
                if news_safety_mode and news_unavailable:
                    score = 0
                if score < -20:
                    self.engine.logger.warning(
                        f"[News Filter] BLOCKED: Sentiment is too negative ({score})"
                    )
                    return EntryGateDecision(False, "news_sentiment", 0, cash, budget)
            except Exception as e:
                self.engine.logger.warning(
                    f"[News Filter] Check failed, proceeding with technical only: {e}"
                )

        try:
            until = float(self.engine._symbol_cooldown_until.get(symbol, 0.0) or 0.0)
            if until and time.time() < until:
                try:
                    self.engine.event_store.append(
                        ievents.RiskDecision(
                            symbol=symbol,
                            allowed=False,
                            reason="cooldown",
                            idempotency_key=idempotency_key,
                            correlation_id=correlation_id,
                            module="engine_orb_vwap",
                            context={"cooldown_until_epoch": until},
                        ).to_event(run_id=self.engine.run_id)
                    )
                except Exception:
                    pass
                return EntryGateDecision(False, "cooldown", 0, cash, budget)
        except Exception:
            pass

        if book.ask <= 0:
            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="ask=0",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            self.engine.logger.info("entry skipped: ask=0")
            return EntryGateDecision(False, "ask=0", 0, cash, budget)

        cash = await self.engine.rest.get_cash_available(symbol, book.ask)
        if cash <= 0:
            self.engine.logger.warning("cash query failed (0), using default budget")
            cash = 1000

        exposure = 0.0
        try:
            for p in self.engine._active_positions().values():
                px = self.engine.last_price.get(p.symbol)
                if not px:
                    px = float(p.avg_price or 0.0)
                exposure += float(px) * int(p.qty)
        except Exception:
            exposure = 0.0

        equity_est = float(cash) + float(exposure)
        cap_amt = float(equity_est) * float(max_total_position_pct)
        remaining_cap = float(cap_amt) - float(exposure)

        budget_pct = self.engine.entry_budget_pct * (0.5 if self.engine.market_regime == "BEAR" else 1.0)
        budget = cash * budget_pct

        reserve_amt = float(equity_est) * float(self.engine.cash_reserve_pct)
        cash_budget_cap = float(cash) - float(reserve_amt)
        if cash_budget_cap <= 0:
            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="cash_reserve",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                        context=self.build_risk_context(
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
                        ),
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            self.engine.logger.warning(
                "entry blocked by cash reserve: cash=%.0f reserve=%.0f pct=%.2f",
                cash,
                reserve_amt,
                self.engine.cash_reserve_pct,
            )
            return EntryGateDecision(False, "cash_reserve", 0, cash, budget)

        budget = min(float(budget), float(cash_budget_cap))

        if remaining_cap <= 0:
            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="portfolio_exposure_cap",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                        context=self.build_risk_context(
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
                        ),
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            self.engine.logger.warning(
                "entry blocked by portfolio cap: exposure=%.0f cap=%.0f pct=%.2f",
                exposure,
                cap_amt,
                max_total_position_pct,
            )
            return EntryGateDecision(False, "portfolio_exposure_cap", 0, cash, budget)

        budget = min(float(budget), float(remaining_cap))

        existing_sym_exposure = 0.0
        try:
            p0 = self.engine.state_machine.get_position(symbol)
            if p0 is not None:
                px0 = self.engine.last_price.get(symbol)
                if not px0:
                    px0 = float(p0.avg_price or 0.0)
                existing_sym_exposure = float(px0) * int(p0.qty)
        except Exception:
            existing_sym_exposure = 0.0

        sym_cap_amt = float(equity_est) * float(max_symbol_position_pct)
        sym_remaining = float(sym_cap_amt) - float(existing_sym_exposure)
        if sym_remaining <= 0:
            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="symbol_exposure_cap",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                        context=self.build_risk_context(
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
                        ),
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            self.engine.logger.warning(
                "entry blocked by symbol cap: symbol=%s exposure=%.0f cap=%.0f pct=%.2f",
                symbol,
                existing_sym_exposure,
                sym_cap_amt,
                max_symbol_position_pct,
            )
            return EntryGateDecision(False, "symbol_exposure_cap", 0, cash, budget)

        budget = min(float(budget), float(sym_remaining))

        qty = min(int(budget // book.ask), self.engine.max_position_qty)
        if qty <= 0:
            try:
                if self.engine.symbol_cooldown_sec > 0:
                    self.engine._symbol_cooldown_until[symbol] = time.time() + float(
                        self.engine.symbol_cooldown_sec
                    )
            except Exception:
                pass

            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="qty=0",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                        context=self.build_risk_context(
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
                        ),
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            self.engine.logger.info(
                "entry skipped: qty=0 cash=%.0f budget=%.0f pct=%.3f ask=%.0f",
                cash,
                budget,
                self.engine.entry_budget_pct,
                book.ask,
            )
            return EntryGateDecision(False, "qty=0", 0, cash, budget)

        is_kr = bool(str(symbol).isdigit())
        trades_mkt = self.engine.trades_today_kr if is_kr else self.engine.trades_today_us
        max_mkt = self.engine.max_trades_per_day_kr if is_kr else self.engine.max_trades_per_day_us
        if trades_mkt >= max_mkt:
            self.engine.logger.warning(
                "entry blocked by max trades/day guardrail (%s %s/%s)",
                "KR" if is_kr else "US",
                trades_mkt,
                max_mkt,
            )
            return EntryGateDecision(False, "max_trades_per_day", 0, cash, budget)

        symbol_trades = int(self.engine.trades_today_by_symbol.get(symbol, 0) or 0)
        if symbol_trades >= self.engine.max_trades_per_symbol:
            try:
                if self.engine.symbol_cooldown_sec > 0:
                    self.engine._symbol_cooldown_until[symbol] = time.time() + float(
                        self.engine.symbol_cooldown_sec
                    )
            except Exception:
                pass

            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="max_trades_per_symbol",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            self.engine.logger.warning(
                "entry blocked by max_trades_per_symbol (%s %s/%s)",
                symbol,
                symbol_trades,
                self.engine.max_trades_per_symbol,
            )
            return EntryGateDecision(False, "max_trades_per_symbol", 0, cash, budget)

        max_concurrent_exposures = max(
            1, int(tcfg.get("max_concurrent_exposures", 3) or 3)
        )
        open_positions = self.engine._active_positions()
        if len(open_positions) >= max_concurrent_exposures:
            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="max_concurrent_exposures",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            self.engine.logger.warning(
                "entry blocked by max_concurrent_exposures (%s/%s)",
                len(open_positions),
                max_concurrent_exposures,
            )
            return EntryGateDecision(False, "max_concurrent_exposures", 0, cash, budget)

        if len(open_positions) >= self.engine.max_concurrent_positions:
            try:
                self.engine.event_store.append(
                    ievents.RiskDecision(
                        symbol=symbol,
                        allowed=False,
                        reason="max_concurrent_positions",
                        idempotency_key=idempotency_key,
                        correlation_id=correlation_id,
                        module="engine_orb_vwap",
                    ).to_event(run_id=self.engine.run_id)
                )
            except Exception:
                pass
            return EntryGateDecision(False, "max_concurrent_positions", 0, cash, budget)

        return EntryGateDecision(True, "ok", qty, cash, budget)
