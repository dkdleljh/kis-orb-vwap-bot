"""
Kukjang Module - 국내주식/ETF trading module.

기존 main.py의 국내주식 거래 로직을 모듈화합니다.

Live gate:
- KIS_LIVE_ENABLED=1
- KIS_LIVE_CONFIRM=YES
- KIS_KILL_SWITCH=0 and STOP_TRADING.flag absent
"""

import asyncio
import json
import os
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

from modules.base import BaseTradingModule, ModuleContext
from models import Position, OrderBookTop, TradeTick, Bar1m, OrderResult
from kis_rest_orders import KISRestOrders
from perfect_strategy import Perfect100Strategy, State, PerfectSignal
from indicators import rsi, sma, bollinger_bands, envelope, atr, ema, macd
from bars_vwap import BarBuilder1m, VwapCalculator
from fee_calculator import FeeCalculator
from news import NewsSentimentAnalyzer
from premarket_collector import PremarketCollector, CollectStats
from universe_builder import build_universe, resolve_universe_config


KST = ZoneInfo("Asia/Seoul")


def get_kr_market_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(tz=KST)
    is_weekend = now.weekday() >= 5
    hhmm = now.hour * 60 + now.minute
    pre_start = 8 * 60
    pre_end = 9 * 60
    regular_end = 15 * 60 + 30

    is_pre_market = (not is_weekend) and (pre_start <= hhmm < pre_end)
    is_open = (not is_weekend) and (pre_end <= hhmm < regular_end)
    return {
        "is_open": is_open,
        "is_pre_market": is_pre_market,
        "is_weekend": is_weekend,
        "market_open_kst": "09:00:00",
        "market_close_kst": "15:30:00",
        "pre_market_start_kst": "08:00:00",
        "pre_market_end_kst": "09:00:00",
    }


class KukjangModule(BaseTradingModule):
    """
    국내주식/ETF trading module.

    기존 main.py의TradingEngine 로직을 모듈화합니다.
    """

    def __init__(
        self,
        context: ModuleContext,
        logger,
        rest_client: KISRestOrders,
        auth,
        config: Dict[str, Any],
        time_rules: Any,
    ):
        super().__init__(context, logger)
        self.rest = rest_client
        self.auth = auth
        self.config = config
        self.time_rules = time_rules

        self.entry_budget_pct = float(config.get("entry_budget_pct", 0.20))
        self.stop_loss_pct = float(config.get("stop_loss_pct", -0.015))
        self.take_profit_pct = float(config.get("take_profit_pct", 0.030))
        self.entry_retry_limit = int(config.get("entry_retry_limit", 5))
        self.max_spread_pct = float(config.get("max_spread_pct", 0.005))

        max_entries = int(config.get("max_entries_per_day", 10))
        daily_loss_limit = float(config.get("daily_loss_limit_pct", -0.05))
        max_consecutive_stop = int(config.get("max_consecutive_stop", 3))
        self.risk = RiskManager(max_entries, daily_loss_limit, max_consecutive_stop)

        # Perfect100Strategy handles state management and entry evaluation with fee adjustments
        # self.perfect_strategy is initialized below after fee_calculator

        self.symbol_lever = context.config.get("lever", "122630")
        self.symbol_inverse = context.config.get("inverse", "114800")

        self.bar_builders: Dict[str, BarBuilder1m] = {}
        self.vwap_by_symbol: Dict[str, VwapCalculator] = {}
        self.last_price: Dict[str, float] = {}
        self.last_book: Dict[str, OrderBookTop] = {}
        self.bar_history: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "closes": deque(maxlen=600),
                "highs": deque(maxlen=600),
                "lows": deque(maxlen=600),
                "volumes": deque(maxlen=600),
            }
        )

        self.peak_pnl_pct: Dict[str, float] = {}
        self._fallback_or_start = None
        self._fallback_or_end = None

        self.prev_close_by_symbol: Dict[str, float] = {}
        self._prev_close_loaded_ymd: str = ""

        self.news_score_by_symbol: Dict[str, int] = {}
        self.news_score_updated_at: Dict[str, float] = {}

        self.market_regime = "NEUTRAL"

        self.dynamic_config = resolve_universe_config("SCALP", context.config.get("dynamic_universe", {}))
        self.dynamic_enabled = os.environ.get("KIS_DYNAMIC_UNIVERSE", "1").strip() != "0"
        self._scanner = None
        self._current_universe: List[str] = []
        self._last_good_universe: List[str] = []
        self._miss_count: Dict[str, int] = {}
        self._last_scan_time: Optional[datetime] = None

        self.news_analyzer = NewsSentimentAnalyzer()
        self._data_root = Path(os.environ.get("KIS_DATA_DIR", "data"))

        # KR pre-market data/analysis mode (08:00~09:00 KST, data collection only)
        self._premarket_enabled = os.environ.get("KIS_KR_PREMARKET_COLLECT", "1").strip() == "1"
        self._premarket_poll_sec = max(15.0, float(os.environ.get("KIS_KR_PREMARKET_POLL_SEC", "60")))
        default_premarket_max = int(self.dynamic_config.get("max_symbols", 0) or 0)
        self._premarket_max_symbols = int(
            os.environ.get("KIS_KR_PREMARKET_MAX_SYMBOLS", str(default_premarket_max))
        )
        self._premarket_summary_interval_sec = int(os.environ.get("KIS_KR_PREMARKET_SUMMARY_SEC", "600"))
        self._premarket_news_interval_sec = int(os.environ.get("KIS_KR_PREMARKET_NEWS_SEC", "300"))
        self._premarket_metrics_interval_sec = int(os.environ.get("KIS_KR_PREMARKET_METRICS_SEC", "300"))
        self._premarket_last_summary_ts = 0.0
        self._premarket_last_news_ts = 0.0
        self._premarket_last_metrics_ts = 0.0
        self._premarket_mode_active = False
        self._premarket_collector: Optional[PremarketCollector] = None
        if self._premarket_enabled:
            self._premarket_collector = PremarketCollector(
                rest_client=self.rest,
                logger=self.logger,
                data_root=self._data_root,
                market="KR",
                poll_sec=int(self._premarket_poll_sec),
            )

        self.fee_calculator = FeeCalculator(
            commission_rate=0.00015,
            commission_min=1000,
            slippage_rate=0.001,
            tax_rate=0.002,
            is_overseas=False,
        )

        # scoring threshold (recommended)
        scoring_cfg = (config.get("scoring", {}) or {})
        kr_scalp_th = int(scoring_cfg.get("kr_scalp_entry_threshold", 72))

        self.perfect_strategy = Perfect100Strategy(
            logger=self.logger,
            min_score=kr_scalp_th,
            max_spread_pct=self.max_spread_pct,
            min_bid_ask_ratio=0.8,
            min_win_rate=0.55,
            min_r_ratio=1.5,
        )

    def set_scanner(self, scanner) -> None:
        self._scanner = scanner

    async def _on_initialize(self) -> None:
        if self.dynamic_enabled:
            await self._refresh_universe()
        else:
            self._current_universe = list(self.symbols)
        await self._load_prev_closes_if_needed(self._current_universe)

    async def _refresh_universe(self) -> None:
        try:
            universe = await build_universe(
                market="KR",
                style="SCALP",
                scanner=self._scanner,
                base_symbols=list(self.symbols) if self.symbols else [],
                config=self.dynamic_config,
            )
            if universe:
                self._current_universe = universe
                self._last_good_universe = universe
                self._last_scan_time = datetime.now(tz=KST)
                self.log_info(f"Universe refreshed: {len(universe)} stocks")
            else:
                self.log_warning("Scanner returned empty, using last good universe")
                self._current_universe = self._last_good_universe or []

        except Exception as e:
            self.log_error(f"Universe refresh failed: {e}")
            self._current_universe = self._last_good_universe or []

    async def _universe_refresh_loop(self) -> None:
        interval = self.dynamic_config.get("scan_interval_sec", 90)

        while self._running:
            try:
                now = datetime.now(tz=KST)
                if (
                    self._last_scan_time
                    and (now - self._last_scan_time).total_seconds() >= interval
                ):
                    await self._refresh_universe()
                    await self._load_prev_closes_if_needed(self._current_universe)
            except Exception as e:
                self.log_error(f"Universe refresh loop error: {e}")
            await asyncio.sleep(30)

    def get_current_universe(self) -> List[str]:
        return self._current_universe.copy()

    async def get_cash_available(self, symbol: str, price: float = 0.0) -> float:
        return await self.rest.get_cash_available(symbol, price)

    async def place_buy_order(self, symbol: str, qty: int, price: float) -> OrderResult:
        return await self.rest.place_buy_limit(symbol, qty, price)

    async def place_sell_order(
        self, symbol: str, qty: int, price: Optional[float] = None
    ) -> OrderResult:
        if price is None:
            return await self.rest.place_sell_market(symbol, qty)
        return await self.rest.place_sell_limit(symbol, qty, price)

    async def get_positions(self) -> Optional[Position]:
        return await self.rest.get_positions()

    async def get_quote(self, symbol: str) -> dict:
        return await self.rest.get_quote(symbol)

    async def on_tick(self, tick: TradeTick) -> None:
        if not tick.symbol:
            return
        if tick.symbol not in self.vwap_by_symbol:
            self.vwap_by_symbol[tick.symbol] = VwapCalculator()
        if tick.symbol not in self.bar_builders:
            symbol_for_callback = tick.symbol

            def callback(bar):
                asyncio.create_task(self.on_bar_close(symbol_for_callback, bar))

            self.bar_builders[tick.symbol] = BarBuilder1m(callback)  # type: ignore[call-arg]
        self.vwap_by_symbol[tick.symbol].update(tick)
        self.last_price[tick.symbol] = tick.price
        self.bar_builders[tick.symbol].update(tick)

    async def on_book(self, book: OrderBookTop) -> None:
        if not book.symbol:
            return
        self.last_book[book.symbol] = book

    async def on_bar_close(self, symbol: str, bar: Bar1m) -> None:
        self.log_info(
            f"Bar closed: {symbol} O={bar.open} H={bar.high} L={bar.low} C={bar.close}"
        )

        self.bar_history[symbol]["closes"].append(bar.close)
        self.bar_history[symbol]["highs"].append(bar.high)
        self.bar_history[symbol]["lows"].append(bar.low)
        self.bar_history[symbol]["volumes"].append(bar.volume)

        if symbol == self.symbol_lever:
            closes = list(self.bar_history[symbol]["closes"])
            if len(closes) >= 60:
                ma60 = sma(closes, 60)
                if bar.close > ma60:
                    self.market_regime = "BULL"
                else:
                    self.market_regime = "BEAR"
                self.log_info(f"Market Regime: {self.market_regime}")

        now_dt = datetime.now()

        if self._fallback_or_start and self._fallback_or_end:
            if self._fallback_or_start <= now_dt < self._fallback_or_end:
                self.perfect_strategy.update_or(symbol, bar)
                return
            if (
                now_dt >= self._fallback_or_end
                and self.perfect_strategy.state == State.BUILD_OR
            ):
                self.perfect_strategy.set_state(State.WAIT_SIGNAL)

        from utils_time import is_between, is_after

        if is_between(self.time_rules.or_start, self.time_rules.or_end, now_dt):
            self.perfect_strategy.update_or(symbol, bar)
            return

        if is_after(self.time_rules.entry_start, now_dt):
            if self.perfect_strategy.state == State.BUILD_OR:
                self.perfect_strategy.set_state(State.WAIT_SIGNAL)

        if self.perfect_strategy.state != State.WAIT_SIGNAL:
            return

        if not self.risk.can_enter():
            self.perfect_strategy.set_state(State.DONE_TODAY)
            return

        last_price = self.last_price.get(symbol)
        book = self.last_book.get(symbol)
        vwap_calc = self.vwap_by_symbol.get(symbol)
        vwap = vwap_calc.vwap() if vwap_calc else None

        # If websocket orderbook is missing/stale, fall back to REST quote so
        # signal generation doesn't completely stall after a restart.
        if book is None or (getattr(book, "ask", 0) or 0) <= 0 or (getattr(book, "bid", 0) or 0) <= 0:
            try:
                q = await self.get_quote(symbol)
                out = (q or {}).get("output", {}) or {}
                ask = float(out.get("askp1") or 0)
                bid = float(out.get("bidp1") or 0)
                if ask > 0 and bid > 0:
                    from models import OrderBookTop

                    book = OrderBookTop(
                        symbol=symbol,
                        bid=bid,
                        ask=ask,
                        bid_size=0.0,
                        ask_size=0.0,
                        timestamp=datetime.now(),
                    )
            except Exception:
                pass

        if last_price is None or book is None or vwap is None:
            return
        if last_price <= 0 or book.ask <= 0 or book.bid <= 0:
            return

        indicators = await self._calculate_indicators(symbol, bar)
        if not indicators:
            return

        try:
            signal = self.perfect_strategy.evaluate_entry(
                bar=bar,
                last_price=last_price,
                vwap=vwap,
                book=book,
                lever_symbol=self.symbol_lever,
                inverse_symbol=self.symbol_inverse,
                indicators=indicators,
                market_regime=self.market_regime,
            )
            if signal.side:
                # standardized score line
                from scoring import ScoreBreakdown, SignalScore
                from core.audit_log import audit_decision

                bd = ScoreBreakdown({"MQ": 25, "MOMO": 25, "VWAP": 20, "OR": 20, "RR": 10})
                out = SignalScore(
                    symbol=str(signal.symbol),
                    side=str(signal.side),
                    score=float(getattr(signal, "score", bd.total) or bd.total),
                    threshold=float(self.perfect_strategy.min_score),
                    market="KR",
                    style="SCALP",
                    risk_grade="B",
                    breakdown=bd,
                    reasons=list(signal.reasons or []),
                )
                self.log_info(out.to_line())
                await self._handle_entry(signal, book)
        except Exception as e:
            self.log_error(f"Entry evaluation failed: {e}")

    async def _calculate_indicators(self, symbol: str, bar: Bar1m) -> Optional[Dict]:
        try:
            indicators = {}
            indicators["prev_close"] = float(
                self.prev_close_by_symbol.get(symbol, 0.0) or 0.0
            )
            indicators["news_score"] = int(
                self.news_score_by_symbol.get(symbol, 0) or 0
            )
            indicators["volume_power"] = 100.0

            closes = list(self.bar_history[symbol]["closes"])
            volumes = list(self.bar_history[symbol]["volumes"])

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

                highs = list(self.bar_history[symbol]["highs"])
                lows = list(self.bar_history[symbol]["lows"])
                if len(highs) >= 20 and len(lows) >= 20:
                    indicators["atr"] = atr(highs, lows, closes, 20)
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

            return indicators
        except Exception as e:
            self.log_error(f"Indicator calculation failed: {e}")
            return None

    def _live_allowed(self) -> bool:
        # KR live gate
        if os.environ.get("KIS_KILL_SWITCH", "0").strip() == "1":
            return False
        if os.path.exists(os.path.join(os.path.dirname(__file__), "..", "STOP_TRADING.flag")):
            return False
        if os.environ.get("KIS_LIVE_ENABLED", "0").strip() != "1":
            return False
        return os.environ.get("KIS_LIVE_CONFIRM", "").strip().upper() == "YES"

    async def _handle_entry(self, signal: PerfectSignal, book: OrderBookTop) -> None:
        if self.perfect_strategy.state != State.WAIT_SIGNAL:
            return

        if not self._live_allowed():
            self.log_warning("KR live ordering suppressed (set KIS_LIVE_ENABLED=1 and KIS_LIVE_CONFIRM=YES; ensure kill switch OFF)")
            return

        symbol: str = signal.symbol if signal.symbol else ""
        if not symbol:
            self.log_info("entry skipped: no symbol in signal")
            return

        self.perfect_strategy.set_state(State.ENTRY_PENDING)

        if book.ask <= 0:
            self.log_info("entry skipped: ask=0")
            self.perfect_strategy.set_state(State.WAIT_SIGNAL)
            return

        cash = await self.get_cash_available(symbol, book.ask)
        if cash <= 0:
            # 현금조회 실패를 '0으로 진행'하면 하루 종일 조용히 매수가 막히거나(혹은 엉뚱한 기본금액으로 매수)
            # 위험해질 수 있으므로, 진입을 중단하고 원인을 로그로 노출합니다.
            self.log_error("cash query failed (cash_available<=0). entry aborted")
            self.perfect_strategy.set_state(State.WAIT_SIGNAL)
            return

        budget = cash * self.entry_budget_pct
        qty = int(budget // book.ask)

        if qty <= 0:
            self.log_info(f"entry skipped: qty=0 cash={cash:.0f} budget={budget:.0f}")
            self.perfect_strategy.set_state(State.WAIT_SIGNAL)
            return

        self.risk.record_entry()

        for i in range(self.entry_retry_limit):
            order = await self.place_buy_order(symbol, qty, book.ask)
            self.log_info(f"Order submitted: {symbol} qty={qty} id={order.order_id}")
            await asyncio.sleep(2)
            pos = await self.get_positions()
            if pos and pos.symbol == symbol and pos.qty >= qty:
                self.perfect_strategy.position = pos
                self.perfect_strategy.set_state(State.IN_POSITION)
                self.log_info(f"Entry filled: {symbol} qty={pos.qty}")
                self.peak_pnl_pct[symbol] = -0.01
                return
            if order.order_id:
                await self.rest.cancel_order(order.order_id, symbol, qty)

        self.perfect_strategy.set_state(State.WAIT_SIGNAL)
        self.log_info("Entry failed after retries")
        await asyncio.sleep(30)

    def _log_market_mode_transition(self, status: Dict[str, Any]) -> None:
        is_pre = bool(status.get("is_pre_market"))
        if is_pre and not self._premarket_mode_active:
            self._premarket_mode_active = True
            ymd = datetime.now(tz=KST).strftime("%Y%m%d")
            root = self._data_root / "premarket_kr" / ymd
            self.log_info(
                f"Entered KR pre-market collection mode: poll={int(self._premarket_poll_sec)}s "
                f"max_symbols={self._premarket_max_symbols} path={root}"
            )
        elif (not is_pre) and self._premarket_mode_active:
            self._premarket_mode_active = False
            self.log_info("Exited KR pre-market collection mode")

    def _premarket_symbols(self) -> List[str]:
        symbols = self._current_universe or list(self.symbols)
        if not symbols:
            return []
        cap = self._premarket_max_symbols if self._premarket_max_symbols > 0 else len(symbols)
        return symbols[:cap]

    def _news_cache_path(self, now: datetime) -> Path:
        return self._data_root / "news_cache_kr" / f"{now.strftime('%Y%m%d')}.json"

    def _metrics_path(self, now: datetime) -> Path:
        return self._data_root / "premarket_metrics_kr" / f"{now.strftime('%Y%m%d')}.json"

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")

    async def _update_premarket_news(self, symbols: List[str], now: datetime) -> None:
        updated = 0
        for symbol in symbols:
            try:
                result = await self.news_analyzer.get_sentiment_score(symbol)
                score = int(float(result.get("score", 0)))
                self.news_score_by_symbol[symbol] = score
                self.news_score_updated_at[symbol] = now.timestamp()
                updated += 1
            except Exception as e:
                self.log_warning(f"pre-market news update failed: {symbol} err={e}")

        payload = {
            "updated_at": now.isoformat(),
            "scores": self.news_score_by_symbol,
            "updated_at_epoch": self.news_score_updated_at,
        }
        self._write_json(self._news_cache_path(now), payload)
        if updated:
            self.log_info(f"pre-market news updated: {updated} symbols")

    def _build_symbol_premarket_metrics(self, symbol: str) -> Dict[str, Any]:
        if not self._premarket_collector:
            return {}
        samples = self._premarket_collector.recent_samples(symbol)
        if len(samples) < 2:
            return {}

        last = float(samples[-1].get("last") or 0.0)
        first = float(samples[0].get("last") or 0.0)
        if first <= 0 or last <= 0:
            return {}

        premarket_change_pct = ((last - first) / first) * 100.0
        trend = "flat"
        if premarket_change_pct > 0.2:
            trend = "up"
        elif premarket_change_pct < -0.2:
            trend = "down"

        highs: List[float] = []
        lows: List[float] = []
        closes: List[float] = []
        for s in samples:
            px = float(s.get("last") or 0.0)
            if px <= 0:
                continue
            bid = float(s.get("bid") or px)
            ask = float(s.get("ask") or px)
            highs.append(max(px, ask))
            lows.append(min(px, bid))
            closes.append(px)

        if not closes:
            return {}

        atr_val = atr(highs, lows, closes, 14)
        atr_pct = (atr_val / last) * 100.0 if (atr_val > 0 and last > 0) else 0.0
        return {
            "last": last,
            "premarket_change_pct": round(premarket_change_pct, 4),
            "premarket_atr_pct_est": round(atr_pct, 4),
            "premarket_trend": trend,
            "news_score": int(self.news_score_by_symbol.get(symbol, 0)),
            "sample_count": len(samples),
        }

    async def _update_premarket_metrics(self, symbols: List[str], now: datetime) -> None:
        metrics_by_symbol: Dict[str, Any] = {}
        for symbol in symbols:
            m = self._build_symbol_premarket_metrics(symbol)
            if m:
                metrics_by_symbol[symbol] = m

        payload = {
            "updated_at": now.isoformat(),
            "symbol_count": len(metrics_by_symbol),
            "metrics": metrics_by_symbol,
        }
        self._write_json(self._metrics_path(now), payload)

    async def _run_premarket_cycle(self) -> None:
        now = datetime.now(tz=KST)
        now_ts = now.timestamp()

        if not self._current_universe:
            if self.dynamic_enabled:
                await self._refresh_universe()
            else:
                self._current_universe = list(self.symbols)

        if self.dynamic_enabled:
            interval = int(self.dynamic_config.get("scan_interval_sec", 300))
            due = (self._last_scan_time is None) or ((now - self._last_scan_time).total_seconds() >= interval)
            if due:
                await self._refresh_universe()
                await self._load_prev_closes_if_needed(self._current_universe)

        symbols = self._premarket_symbols()
        if not symbols:
            return

        stats = CollectStats()
        if self._premarket_collector:
            stats = await self._premarket_collector.collect_once(symbols)

        if (now_ts - self._premarket_last_news_ts) >= self._premarket_news_interval_sec:
            await self._update_premarket_news(symbols, now)
            self._premarket_last_news_ts = now_ts

        if (now_ts - self._premarket_last_metrics_ts) >= self._premarket_metrics_interval_sec:
            await self._update_premarket_metrics(symbols, now)
            self._premarket_last_metrics_ts = now_ts

        if (now_ts - self._premarket_last_summary_ts) >= self._premarket_summary_interval_sec:
            fail_rate = 0.0
            if stats.requested > 0:
                fail_rate = (stats.failed / float(stats.requested)) * 100.0
            self.log_info(
                f"pre-market summary symbols={len(symbols)} collected={stats.collected}/{stats.requested} "
                f"failed={stats.failed} cooldown={stats.skipped_cooldown} fail_rate={fail_rate:.1f}%"
            )
            self._premarket_last_summary_ts = now_ts

    async def monitor_loop(self) -> None:
        universe_task = None
        if self.dynamic_enabled:
            universe_task = asyncio.create_task(self._universe_refresh_loop())

        while self._running:
            try:
                status = get_kr_market_status(datetime.now(tz=KST))
                self._log_market_mode_transition(status)
                if status.get("is_pre_market") and self._premarket_enabled:
                    # pre-market mode: collection/news/metrics/universe refresh only
                    await self._run_premarket_cycle()
                    await asyncio.sleep(self._premarket_poll_sec)
                    continue

                await self._check_tp_sl()
                await self._check_force_exit()
                await asyncio.sleep(1)
            except Exception as e:
                self.log_error(f"Monitor loop error: {e}")
                await asyncio.sleep(5)

        if universe_task:
            universe_task.cancel()

    async def _check_tp_sl(self) -> None:
        if not self.perfect_strategy.in_position():
            return
        pos = self.perfect_strategy.position
        if not pos:
            return

        last_price = self.last_price.get(pos.symbol)
        if last_price is None:
            return

        gross_pnl_pct = (last_price - pos.avg_price) / pos.avg_price

        entry_costs = self.fee_calculator.calculate_entry_cost(pos.avg_price, pos.qty)
        exit_costs = self.fee_calculator.calculate_exit_cost(last_price, pos.qty)
        total_costs_pct = (entry_costs.total_cost + exit_costs.total_cost) / (
            pos.avg_price * pos.qty
        )

        net_pnl_pct = gross_pnl_pct - total_costs_pct
        pos.unrealized_pnl_pct = net_pnl_pct

        if net_pnl_pct >= 0.015 and not pos.tp1_done and pos.qty > 1:
            half_qty = int(pos.qty * 0.5)
            if half_qty > 0:
                self.log_info(
                    f"TP1 Scale-out: {pos.symbol} PnL={net_pnl_pct:.2%} (gross={gross_pnl_pct:.2%})"
                )
                from core.correlation import new_corr
                corr = new_corr("kr_exit")
                audit_decision(
                    symbol=pos.symbol,
                    kind="exit_plan",
                    correlation_id=corr,
                    market="KR",
                    style="SCALP",
                    side="SELL",
                    extra={"reason": "tp1_scale_out", "qty": int(half_qty)},
                )
                r = await self.rest.place_sell_market(pos.symbol, half_qty)
                audit_decision(
                    symbol=pos.symbol,
                    kind="order_result",
                    correlation_id=corr,
                    market="KR",
                    style="SCALP",
                    side="SELL",
                    extra={"reason": "tp1_scale_out", "odno": getattr(r, 'order_id', None), "order": getattr(r, '__dict__', None) or str(r)},
                )
                pos.qty -= half_qty
                pos.tp1_done = True

        if pos.symbol in self.peak_pnl_pct:
            self.peak_pnl_pct[pos.symbol] = max(
                self.peak_pnl_pct[pos.symbol], net_pnl_pct
            )
        else:
            self.peak_pnl_pct[pos.symbol] = net_pnl_pct

        peak_pnl = self.peak_pnl_pct[pos.symbol]

        if peak_pnl >= 0.012 and net_pnl_pct < 0.003:
            await self._handle_exit("breakeven_stop")
            return

        if peak_pnl >= 0.030 and (peak_pnl - net_pnl_pct) >= 0.010:
            await self._handle_exit("trailing_stop")
            return

        if net_pnl_pct >= self.take_profit_pct:
            await self._handle_exit("take_profit")
        elif net_pnl_pct <= self.stop_loss_pct:
            await self._handle_exit("stop_loss")

    async def _check_force_exit(self) -> None:
        from utils_time import is_after

        now_dt = datetime.now()

        if is_after(self.time_rules.force_exit, now_dt):
            if self.perfect_strategy.in_position():
                await self._handle_exit("force_exit")

    async def _handle_exit(self, reason: str) -> None:
        if self.perfect_strategy.state not in (State.IN_POSITION, State.EXIT_PENDING):
            return
        pos = self.perfect_strategy.position
        if not pos:
            return

        self.perfect_strategy.set_state(State.EXIT_PENDING)

        current_book = self.last_book.get(pos.symbol)

        if current_book and current_book.bid > 0:
            exit_price = current_book.bid
            from core.correlation import new_corr
            corr = new_corr("kr_exit")
            audit_decision(
                symbol=pos.symbol,
                kind="exit_plan",
                correlation_id=corr,
                market="KR",
                style="SCALP",
                side="SELL",
                extra={"reason": reason, "qty": int(pos.qty), "limit_price": float(current_book.bid)},
            )
            r = await self.rest.place_sell_limit(pos.symbol, pos.qty, current_book.bid)
            audit_decision(
                symbol=pos.symbol,
                kind="order_result",
                correlation_id=corr,
                market="KR",
                style="SCALP",
                side="SELL",
                extra={"reason": reason, "odno": getattr(r, 'order_id', None), "order": getattr(r, '__dict__', None) or str(r)},
            )
            self.log_info(f"Exit order: {reason} price={current_book.bid}")
        else:
            exit_price = self.last_price.get(pos.symbol, pos.avg_price)
            from core.correlation import new_corr
            corr = new_corr("kr_exit")
            audit_decision(
                symbol=pos.symbol,
                kind="exit_plan",
                correlation_id=corr,
                market="KR",
                style="SCALP",
                side="SELL",
                extra={"reason": reason, "qty": int(pos.qty), "order_type": "MKT"},
            )
            r = await self.rest.place_sell_market(pos.symbol, pos.qty)
            audit_decision(
                symbol=pos.symbol,
                kind="order_result",
                correlation_id=corr,
                market="KR",
                style="SCALP",
                side="SELL",
                extra={"reason": reason, "odno": getattr(r, 'order_id', None), "order": getattr(r, '__dict__', None) or str(r)},
            )
            self.log_info(f"Exit order (market): {reason}")

        await asyncio.sleep(2)
        pos_after = await self.get_positions()

        if not pos_after:
            exit_price = self.last_price.get(pos.symbol, exit_price)

            gross_pnl = (exit_price - pos.avg_price) * pos.qty
            entry_costs = self.fee_calculator.calculate_entry_cost(
                pos.avg_price, pos.qty
            )
            exit_costs = self.fee_calculator.calculate_exit_cost(exit_price, pos.qty)
            net_pnl = gross_pnl - entry_costs.total_cost - exit_costs.total_cost
            net_pnl_pct = net_pnl / (pos.avg_price * pos.qty)

            is_stop = net_pnl_pct <= self.stop_loss_pct
            self.risk.record_exit(net_pnl_pct, is_stop)
            self.perfect_strategy.position = None
            self.perfect_strategy.set_state(State.WAIT_SIGNAL)
            self.log_info(
                f"Exit done: {reason} pnl={net_pnl_pct:.4f} (gross={(exit_price - pos.avg_price) / pos.avg_price:.4f})"
            )

    async def _load_prev_closes_if_needed(self, symbols: List[str]) -> None:
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

                candidates = ["stck_sdpr", "prdy_clpr", "stck_prdy_clpr", "stck_clpr"]
                prev = 0.0
                for k in candidates:
                    v = out.get(k)
                    if v:
                        try:
                            prev = float(v)
                            break
                        except Exception:
                            continue

                if prev > 0:
                    self.prev_close_by_symbol[sym] = prev
                else:
                    try:
                        cur = float(out.get("stck_prpr", 0) or 0)
                    except Exception:
                        cur = 0
                    if cur > 0:
                        self.prev_close_by_symbol[sym] = cur
            except Exception as e:
                self.log_warning(f"prev_close load failed for {sym}: {e}")


class RiskManager:
    def __init__(
        self, max_entries: int, daily_loss_limit: float, max_consecutive_stop: int
    ):
        self.max_entries = max_entries
        self.daily_loss_limit = daily_loss_limit
        self.max_consecutive_stop = max_consecutive_stop
        self.entries_today = 0
        self.daily_pnl = 0.0
        self.consecutive_stops = 0

    def can_enter(self) -> bool:
        if self.entries_today >= self.max_entries:
            return False
        if self.daily_pnl <= self.daily_loss_limit:
            return False
        if self.consecutive_stops >= self.max_consecutive_stop:
            return False
        return True

    def record_entry(self) -> None:
        self.entries_today += 1

    def record_exit(self, pnl_pct: float, is_stop: bool) -> None:
        self.daily_pnl += pnl_pct
        if is_stop:
            self.consecutive_stops += 1
        else:
            self.consecutive_stops = 0
