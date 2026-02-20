from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict

from bars_vwap import BarBuilder1m, VwapCalculator
from candle_analysis import Candle
from indicators import atr, bollinger_bands, ema, envelope, macd, rsi, sma
from ml_score import calculate_ml_score
from models import Bar1m, OrderBookTop, Position, TradeTick
from strategy_state_machine import State
from us_prev_day import fetch_us_prev_daily
from utils_time import is_after, is_between, now_local

from core import events as ievents


def on_tick(self, tick: TradeTick) -> None:
    if not tick.symbol:
        return

    if os.environ.get("KIS_EVENT_LOG_TICKS", "0") == "1":
        try:
            self.event_store.append(
                ievents.Event.make(
                    type="Tick",
                    symbol=tick.symbol,
                    payload={
                        "price": float(tick.price),
                        "volume": float(tick.volume),
                        "ts": tick.timestamp.isoformat(),
                    },
                )
            )
        except Exception:
            pass
    if tick.symbol not in self.vwap_by_symbol:
        self.vwap_by_symbol[tick.symbol] = VwapCalculator()
    if tick.symbol not in self.bar_builders:
        symbol_for_callback = tick.symbol

        def callback(bar):
            self.on_bar_close(symbol_for_callback, bar)

        self.bar_builders[tick.symbol] = BarBuilder1m(callback)
    self.vwap_by_symbol[tick.symbol].update(tick)
    self.last_price[tick.symbol] = tick.price
    self.bar_builders[tick.symbol].update(tick)


def on_book(self, book: OrderBookTop) -> None:
    if not book.symbol:
        return
    self.last_book[book.symbol] = book


def on_bar_close(self, symbol: str, bar: Bar1m) -> None:
    self.logger.info(
        f"[Bar] {symbol} bar closed: O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume}"
    )

    try:
        self.event_store.append(
            ievents.Event.make(
                type="Bar1mClosed",
                symbol=symbol,
                payload={
                    "start": bar.start.isoformat(),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                },
            )
        )
    except Exception:
        pass

    self.bar_history[symbol]["closes"].append(bar.close)
    self.bar_history[symbol]["volumes"].append(bar.volume)
    self.bar_history[symbol]["highs"].append(bar.high)
    self.bar_history[symbol]["lows"].append(bar.low)

    try:
        switched = self.candle_analyzer.update(symbol, bar)
        if switched is not None:
            snap = self.candle_analyzer.snapshot(symbol)
            self.logger.info(
                "[Candle] %s session switched -> %s gap=%.3f range=%.3f",
                symbol,
                switched.value,
                float(snap.get("gap_pct", 0.0) or 0.0),
                float(snap.get("today_range_pct", 0.0) or 0.0),
            )
    except Exception as e:
        self.logger.debug(f"candle analyzer update failed: {e}")

    if symbol == self.symbol_lever:
        closes = list(self.bar_history[symbol]["closes"])
        highs = list(self.bar_history[symbol]["highs"])
        lows = list(self.bar_history[symbol]["lows"])

        if len(closes) >= 60:
            ma60 = sma(closes, 60)
            ma20 = sma(closes, 20) if len(closes) >= 20 else ma60

            price = bar.close
            above_ma20 = price > ma20
            above_ma60 = price > ma60
            ma20_above_ma60 = ma20 > ma60

            rsi_val = 50
            if len(closes) >= 15:
                rsi_val = rsi(closes, 14)

            rsi_bullish = rsi_val > 55
            rsi_bearish = rsi_val < 45

            if above_ma20 and above_ma60 and ma20_above_ma60 and rsi_bullish:
                self.market_regime = "BULL"
            elif (not above_ma20 or not above_ma60) and rsi_bearish:
                self.market_regime = "BEAR"
            else:
                self.market_regime = "NEUTRAL"

            self.logger.info(
                f"[Market Regime] {self.market_regime} (Price={price:.0f} MA20={ma20:.0f} MA60={ma60:.0f} RSI={rsi_val:.1f})"
            )

    now_dt = now_local(self.tz)

    if self._fallback_or_start and self._fallback_or_end:
        if self._fallback_or_start <= now_dt < self._fallback_or_end:
            self.state_machine.update_or(symbol, bar)
            return
        if (
            now_dt >= self._fallback_or_end
            and self.state_machine.state == State.BUILD_OR
        ):
            self.state_machine.set_state(State.WAIT_SIGNAL)

    if is_between(self.time_rules.or_start, self.time_rules.or_end, now_dt):
        self.state_machine.update_or(symbol, bar)
        return

    if is_after(self.time_rules.entry_start, now_dt):
        if self.state_machine.state == State.BUILD_OR:
            self.state_machine.set_state(State.WAIT_SIGNAL)

    if self.state_machine.state != State.WAIT_SIGNAL:
        self.logger.debug(f"[Entry] skipped: state={self.state_machine.state}")
        return

    if self.kill_switch_on():
        self.logger.debug("[Entry] skipped: kill switch on")
        return

    if not self.ws_connected:
        self.logger.debug("[Entry] skipped: ws not connected")
        return

    if not self.risk.can_enter():
        self.logger.debug("[Entry] skipped: risk cannot enter")
        self.state_machine.set_state(State.DONE_TODAY)
        return

    if self.state_machine.in_position(symbol):
        self.logger.debug("[Entry] skipped: already in position symbol=%s", symbol)
        return

    if self.state_machine.active_position_count() >= self.max_concurrent_positions:
        self.logger.debug(
            "[Entry] skipped: max_concurrent_positions reached (%s/%s)",
            self.state_machine.active_position_count(),
            self.max_concurrent_positions,
        )
        return

    last_price = self.last_price.get(symbol)
    book = self.last_book.get(symbol)
    vwap_calc = self.vwap_by_symbol.get(symbol)
    vwap = vwap_calc.vwap() if vwap_calc else None
    if last_price is None or book is None:
        self.logger.debug("[Entry] skipped: no price/book data")
        return
    if last_price <= 0 or book.ask <= 0 or book.bid <= 0:
        self.logger.debug(
            f"[Entry] skipped: invalid price/book: last_price={last_price} ask={book.ask} bid={book.bid}"
        )
        return

    closes = list(self.bar_history[symbol]["closes"])
    volumes = list(self.bar_history[symbol]["volumes"])
    highs = list(self.bar_history[symbol]["highs"])
    lows = list(self.bar_history[symbol]["lows"])

    indicators: Dict[str, Any] = {}

    try:
        indicators["prev_close"] = float(
            self.prev_close_by_symbol.get(symbol, 0.0) or 0.0
        )
        indicators["news_score"] = int(
            self.news_score_by_symbol.get(symbol, 0) or 0
        )
        indicators["volume_power"] = 100.0

        self._maybe_refresh_news_score(symbol)

        if len(closes) >= 5:
            indicators["ma5"] = sma(closes, 5)
            indicators["vol_ma5"] = sma(volumes, 5)

        if len(closes) >= 14:
            indicators["rsi"] = rsi(closes, 14)

        if len(closes) >= 21:
            indicators["ema9"] = ema(closes, 9)
            indicators["ema21"] = ema(closes, 21)

        if len(closes) >= 35:
            macd_line, macd_signal, macd_hist = macd(closes, 12, 26, 9)
            indicators["macd_line"] = macd_line
            indicators["macd_signal"] = macd_signal
            indicators["macd_hist"] = macd_hist

        if len(closes) >= 20:
            indicators["ma20"] = sma(closes, 20)
            indicators["vol_ma20"] = sma(volumes, 20)

            bb_up, bb_mid, bb_low = bollinger_bands(closes, 20, 2.0)
            indicators["bb_up"] = bb_up
            indicators["bb_mid"] = bb_mid

            env_up, env_mid, env_low = envelope(closes, 20, 2.0)
            indicators["env_up"] = env_up
            indicators["env_low"] = env_low
        else:
            if "ma5" in indicators and "ma20" not in indicators:
                indicators["ma20"] = indicators["ma5"]
            if "vol_ma5" in indicators and "vol_ma20" not in indicators:
                indicators["vol_ma20"] = indicators["vol_ma5"]

        try:
            base_vol_ma = float(indicators.get("vol_ma20", 0) or 0)
            if base_vol_ma > 0:
                indicators["volume_power"] = float(bar.volume) / base_vol_ma * 100.0
        except Exception:
            pass

        if len(highs) >= 15 and len(lows) >= 15 and len(closes) >= 15:
            atr_value = atr(highs, lows, closes, 14)
            indicators["atr"] = atr_value
            if last_price > 0:
                indicators["atr_percent"] = (atr_value / last_price) * 100.0

        indicators["ml_score"] = calculate_ml_score(indicators, last_price, vwap)

    except Exception as e:
        self.logger.error(f"Indicator calc failed for {symbol}: {e}")
        return

    try:
        ml_score = indicators.get("ml_score", 50)
        if ml_score < 40:
            self.logger.debug(f"[ML Filter] {symbol} ML score too low: {ml_score}")
            return

        base_thr = 50.0
        try:
            sc = (self.config.get("trading", {}) or {}).get("scoring", {}) or {}
            base_thr = float(sc.get("kr_scalp_entry_threshold", 50) or 50)
        except Exception:
            base_thr = 50.0

        spread_pct = 0.0
        try:
            if book is not None and float(book.ask) > 0 and float(book.bid) > 0:
                spread_pct = (float(book.ask) - float(book.bid)) / float(book.ask)
        except Exception:
            spread_pct = 0.0
        atr_pct = float(indicators.get("atr_percent", 0) or 0)

        try:
            if float(atr_pct) > 0 and float(atr_pct) < float(self.atr_min_percent):
                self.logger.debug(
                    "[ATR Filter] %s atr_pct=%.3f < min=%.3f -> skip",
                    symbol,
                    float(atr_pct),
                    float(self.atr_min_percent),
                )
                return
        except Exception:
            pass

        adj = 0.0
        if atr_pct >= 3.0:
            adj += 5.0
        if atr_pct >= 5.0:
            adj += 5.0
        if spread_pct >= 0.003:
            adj += 5.0
        if self.market_regime == "BULL":
            adj -= 3.0
        if self.market_regime == "BEAR":
            adj += 10.0

        min_score = max(45.0, min(85.0, base_thr + adj))

        signal = self.state_machine.evaluate_entry(
            bar=bar,
            last_price=last_price,
            vwap=vwap,
            book=book,
            lever_symbol=self.symbol_lever,
            inverse_symbol=self.symbol_inverse,
            max_spread_pct=self.max_spread_pct,
            indicators=indicators,
            market_regime=self.market_regime,
            min_score=min_score,
        )
        if signal.side:
            signal_symbol = str(signal.symbol or symbol)
            signal_side = str(signal.side or "BUY")
            signal_corr_id = self._make_correlation_id(signal_symbol, signal_side)
            signal_idempotency = self._make_idempotency_key(
                signal_symbol, signal_side, bar_start=bar.start
            )
            self.logger.info(
                f"signal {signal.symbol} close={bar.close} vwap={vwap} rsi={indicators.get('rsi', 0):.1f} ma20={indicators.get('ma20', 0):.0f}"
            )
            signal_context = {
                "module": "engine_orb_vwap",
                "idempotency_key": signal_idempotency,
                "correlation_id": signal_corr_id,
                "close": float(bar.close),
                "vwap": float(vwap) if vwap is not None else None,
                "spread_pct": (
                    ((float(book.ask) - float(book.bid)) / float(book.ask))
                    if (book is not None and float(book.ask) > 0 and float(book.bid) > 0)
                    else None
                ),
                "rsi": float(indicators.get("rsi", 0) or 0),
                "ma20": float(indicators.get("ma20", 0) or 0),
                "ml_score": float(indicators.get("ml_score", 50) or 50),
                "score": float(getattr(signal, "score", 0.0) or 0.0),
                "min_score": float(min_score),
                "reasons": list(getattr(signal, "reasons", []) or []),
                "reason_short": ",".join(list(getattr(signal, "reasons", []) or [])[:6]),
                "atr": float(indicators.get("atr", 0) or 0),
                "atr_percent": float(indicators.get("atr_percent", 0) or 0),
                "market_regime": str(self.market_regime or ""),
            }
            self._last_signal_context_by_symbol[signal_symbol] = dict(signal_context)
            try:
                self.event_store.append(
                    ievents.Signal(
                        symbol=str(signal.symbol),
                        side=str(signal.side),
                        strength=float(indicators.get("ml_score", 50) or 50)
                        / 100.0,
                        reason="state_machine",
                        model="ml_score_heuristic",
                        correlation_id=signal_corr_id,
                        context=signal_context,
                    ).to_event(run_id=self.run_id)
                )
            except Exception:
                pass

            asyncio.create_task(
                self.handle_entry(signal, book, bar_start=bar.start)
            )
    except Exception as e:
        self.logger.error(f"Evaluate entry failed for {symbol}: {e}")


async def check_tp_sl(self) -> None:
    positions = list(self._active_positions().values())
    if not positions:
        return

    for pos in positions:
        await self._check_tp_sl_for_symbol(pos)


async def _check_tp_sl_for_symbol(self, pos: Position) -> None:
    if not self.state_machine.in_position(pos.symbol):
        return

    last_price = self.last_price.get(pos.symbol)
    if last_price is None and not self.ws_connected:
        quote = await self.rest.get_quote(pos.symbol)
        try:
            out = quote.get("output", {}) or {}
            price_candidates = ["stck_prpr", "stck_clpr", "prdy_clpr", "stck_sdpr"]
            for k in price_candidates:
                v = out.get(k)
                if v:
                    last_price = float(v)
                    break
        except Exception:
            last_price = None

    if not last_price:
        return

    try:
        if pos.entry_time is not None:
            held = (now_local(self.tz) - pos.entry_time).total_seconds()
            if held < float(self.min_hold_sec):
                return
    except Exception:
        pass

    gross_pnl_pct = (last_price - pos.avg_price) / pos.avg_price
    net_pnl_pct = self.fee_calculator.get_net_pnl_percent(pos.avg_price, last_price)

    pnl_net = net_pnl_pct
    pnl_gross = gross_pnl_pct
    pnl_exit = pnl_gross

    pos.unrealized_pnl_pct = pnl_net
    pos.unrealized_gross_pnl_pct = pnl_gross

    self.logger.debug(
        f"[Fee Adjust] {pos.symbol} Gross={gross_pnl_pct:.4f} Net={net_pnl_pct:.4f}"
    )

    bar_hist = self.bar_history.get(pos.symbol)
    if bar_hist:
        highs = list(bar_hist.get("highs", []))
        lows = list(bar_hist.get("lows", []))
        closes_for_atr = list(bar_hist.get("closes", []))
        if len(highs) >= 15 and len(lows) >= 15 and len(closes_for_atr) >= 15:
            latest_atr = atr(highs, lows, closes_for_atr, 14)
            if latest_atr > 0 and pos.avg_price > 0:
                atr_multiplier_sl = float(self.atr_multiplier_sl)
                atr_multiplier_tp = atr_multiplier_sl * 1.5

                dynamic_stop_loss_pct = (
                    -(latest_atr / pos.avg_price) * atr_multiplier_sl
                )
                dynamic_take_profit_pct = (
                    latest_atr / pos.avg_price
                ) * atr_multiplier_tp

                if pnl_exit >= dynamic_take_profit_pct:
                    self.logger.info(f"🎯 SMART PROFIT (ATR): {pos.symbol} GrossPnL={pnl_exit:.2%} Target={dynamic_take_profit_pct:.2%} (Net={pnl_net:.2%})")
                    await self.handle_exit("take_profit (ATR)", symbol=pos.symbol)
                    return
                elif pnl_exit <= dynamic_stop_loss_pct:
                    self.logger.info(f"🛡 SMART STOP (ATR): {pos.symbol} GrossPnL={pnl_exit:.2%} Limit={dynamic_stop_loss_pct:.2%} (Net={pnl_net:.2%})")
                    await self.handle_exit("stop_loss (ATR)", symbol=pos.symbol)
                    return

    if pnl_exit >= self.quick_profit_pct and not pos.tp1_done and pos.qty > 1:
        half_qty = int(pos.qty * 0.5)
        if half_qty > 0:
            if self.kill_switch_on():
                self.logger.warning(
                    "TP1 skipped by kill switch symbol=%s qty=%s",
                    pos.symbol,
                    half_qty,
                )
                return
            self.logger.info(
                f"💰 TP1 (Scale-out): {pos.symbol} GrossPnL={pnl_exit:.2%} Qty={half_qty} (Net={pnl_net:.2%})"
            )
            await self.rest.place_sell_market(pos.symbol, half_qty)
            pos.qty -= half_qty
            pos.tp1_done = True
            pos.profit_locked = True
            return

    if pnl_exit >= self.quick_profit_pct and not pos.profit_locked:
        self.logger.info(
            f" PROFIT TARGET🎯 QUICK REACHED: {pos.symbol} GrossPnL={pnl_exit:.2%} (Net={pnl_net:.2%})"
        )
        pos.profit_locked = True
        await self.handle_exit("quick_profit_1pct", symbol=pos.symbol)
        return

    if pos.profit_locked and pnl_exit < self.min_profit_for_guarantee_pct:
        self.logger.info(
            f"🔒 PROFIT LOCKED - Protecting gains: {pos.symbol} GrossPnL={pnl_exit:.2%} (Net={pnl_net:.2%})"
        )
        await self.handle_exit("profit_lock_protection", symbol=pos.symbol)
        return

    if pos.symbol in self.peak_pnl_pct:
        self.peak_pnl_pct[pos.symbol] = max(self.peak_pnl_pct[pos.symbol], pnl_exit)
    else:
        self.peak_pnl_pct[pos.symbol] = pnl_exit

    peak_pnl = self.peak_pnl_pct[pos.symbol]

    if peak_pnl >= 0.012 and pnl_exit < 0.003:
        await self.handle_exit("breakeven_stop", symbol=pos.symbol)
        return

    if peak_pnl >= 0.030 and (peak_pnl - pnl_exit) >= 0.010:
        await self.handle_exit(
            f"trailing_stop (peak={peak_pnl:.2%})", symbol=pos.symbol
        )
        return

    if pnl_exit >= self.take_profit_pct:
        await self.handle_exit("take_profit", symbol=pos.symbol)
    elif pnl_exit <= self.stop_loss_pct:
        await self.handle_exit("stop_loss", symbol=pos.symbol)


async def _load_prev_closes_if_needed(self, symbols: list[str]) -> None:
    from datetime import datetime

    ymd = datetime.now().strftime("%Y%m%d")
    if self._prev_close_loaded_ymd != ymd:
        self.prev_close_by_symbol.clear()
        self._prev_close_loaded_ymd = ymd

    for sym in symbols:
        if sym in self.prev_close_by_symbol:
            continue
        try:
            q = await self.rest.get_quote(sym)
            out = (q or {}).get("output", {}) or {}

            candidates = [
                "stck_sdpr",
                "prdy_clpr",
                "stck_prdy_clpr",
                "stck_clpr",
            ]
            prev = 0.0
            for k in candidates:
                v = out.get(k)
                if v is None or v == "":
                    continue
                try:
                    prev = float(v)
                    break
                except Exception:
                    continue

            if prev > 0:
                self.prev_close_by_symbol[sym] = prev

                try:
                    prev_o = float(out.get("prdy_oprc") or 0)
                    prev_h = float(out.get("prdy_hgpr") or 0)
                    prev_l = float(out.get("prdy_lwpr") or 0)
                    prev_c = float(prev)
                    if prev_o > 0 and prev_h > 0 and prev_l > 0 and prev_c > 0:
                        self.candle_analyzer.set_prev_day_candle(
                            sym,
                            Candle(
                                open=prev_o,
                                high=prev_h,
                                low=prev_l,
                                close=prev_c,
                                volume=0.0,
                            ),
                        )
                except Exception:
                    pass
            else:
                try:
                    cur = float(out.get("stck_prpr", 0) or 0)
                except Exception:
                    cur = 0
                if cur > 0:
                    self.prev_close_by_symbol[sym] = cur

        except Exception as e:
            self.logger.warning(f"prev_close load failed for {sym}: {e}")


async def _bootstrap_us_prev_day_candles(self, symbols: list[str]) -> None:
    try:
        from datetime import datetime

        ymd = datetime.now().strftime("%Y%m%d")
        key = f"us_prev_day_bootstrap:{ymd}"
        if getattr(self, "_bootstrapped_flags", None) is None:
            self._bootstrapped_flags = set()
        if key in self._bootstrapped_flags:
            return
        self._bootstrapped_flags.add(key)

        us_syms: list[str] = []
        tasks = []
        for sym in symbols:
            if sym.isdigit():
                continue
            if sym in self.candle_analyzer.prev_day_by_symbol:
                continue
            us_syms.append(sym)
            tasks.append(fetch_us_prev_daily(sym))

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sym, res in zip(us_syms, results):
            if isinstance(res, Exception) or res is None:
                continue
            self.candle_analyzer.set_prev_day_candle(sym, res)
    except Exception as e:
        self.logger.debug(f"us prev-day bootstrap failed: {e}")


def _maybe_refresh_news_score(self, symbol: str) -> None:
    if symbol == self.symbol_inverse:
        return

    now_ts = time.time()
    last_ts = float(self.news_score_updated_at.get(symbol, 0.0) or 0.0)
    if (now_ts - last_ts) < 600:
        return

    self.news_score_updated_at[symbol] = now_ts

    async def _worker() -> None:
        try:
            res = await self.state_machine.news_analyzer.get_sentiment_score(symbol)
            score = int(res.get("score", 0) or 0)
            self.news_score_by_symbol[symbol] = score
            self.logger.info(f"[News Cache] {symbol} score={score}")
        except Exception as e:
            self.logger.debug(f"[News Cache] update failed {symbol}: {e}")

    try:
        asyncio.create_task(_worker())
    except Exception:
        pass
