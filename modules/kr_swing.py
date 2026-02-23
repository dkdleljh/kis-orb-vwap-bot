"""KR Swing module (15m-based) with scoring output.

추천값:
- 15분봉 기반 스윙(단기 스윙)
- 진입 임계값: config.trading.scoring.kr_swing_entry_threshold (default 65)

주의:
- 현재는 Perfect100Strategy를 재사용하되, "스윙"에 맞춰 보수적으로 운용(저빈도 진입)
- 점수 표준 출력은 scoring.SignalScore로 통일
"""

from __future__ import annotations

import asyncio
import os
import statistics
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

from bars_agg import BarAggregator
from bars_vwap import VwapCalculator
from indicators import atr, ema, macd, rsi, sma
from kis_rest_orders import KISRestOrders
from models import Bar1m, OrderBookTop, OrderResult, Position, TradeTick
from modules.base import BaseTradingModule, ModuleContext
from perfect_strategy import Perfect100Strategy, State
from scoring import ScoreBreakdown, SignalScore
from core.audit_log import audit_decision
from strategy_profiles import get_recommended_threshold
from universe_builder import build_universe, resolve_universe_config


KST = ZoneInfo("Asia/Seoul")


class KRSwingModule(BaseTradingModule):
    def __init__(
        self,
        context: ModuleContext,
        logger,
        rest_client: KISRestOrders,
        config: Dict[str, Any],
    ):
        super().__init__(context, logger)
        self.rest = rest_client
        self.config = config

        tcfg = (context.config or {})
        self.symbol_lever = tcfg.get("lever", "122630")
        self.symbol_inverse = tcfg.get("inverse", "114800")

        self.entry_budget_pct = float(config.get("entry_budget_pct", 0.15))
        self.stop_loss_pct = float(config.get("stop_loss_pct", -0.02))
        self.take_profit_pct = float(config.get("take_profit_pct", 0.05))
        self.entry_retry_limit = int(config.get("entry_retry_limit", 3))

        scoring_cfg = (config.get("scoring", {}) or {})
        self.entry_threshold = int(scoring_cfg.get("kr_swing_entry_threshold", get_recommended_threshold("KR", "SWING")))
        self.dynamic_config = resolve_universe_config("SWING", context.config.get("dynamic_universe", {}))
        self.dynamic_enabled = os.environ.get("KIS_DYNAMIC_UNIVERSE", "1").strip() != "0"
        self._scanner = None
        self._current_universe: List[str] = []
        self._last_good_universe: List[str] = []
        self._last_scan_time: Optional[datetime] = None

        self._pos: Optional[Position] = None

        self.last_price: Dict[str, float] = {}
        self.last_book: Dict[str, OrderBookTop] = {}
        self.vwap_by_symbol: Dict[str, VwapCalculator] = {}

        self.hist: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "closes": deque(maxlen=400),
                "highs": deque(maxlen=400),
                "lows": deque(maxlen=400),
                "volumes": deque(maxlen=400),
            }
        )

        # 15m aggregator per symbol
        self._agg: Dict[str, BarAggregator] = {}

        self.strategy = Perfect100Strategy(
            logger=self.logger,
            min_score=self.entry_threshold,
            max_spread_pct=float(config.get("max_spread_pct", 0.008)),
            min_bid_ask_ratio=0.75,
            min_win_rate=0.53,
            min_r_ratio=1.2,
        )
        self.market_regime: Optional[str] = None

    def detect_market_regime(self, symbol: str) -> str:
        """Detect market regime using 15m KOSPI proxy vs SMA20 and slope."""
        kospi_candidates = ("KOSPI", "KOSPI200", "069500", "102110")
        closes: List[float] = []

        for candidate in kospi_candidates:
            data = self.hist.get(candidate)
            if not data:
                continue
            candidate_closes = list(data.get("closes", []))
            if len(candidate_closes) >= 21:
                closes = candidate_closes
                break

        if not closes:
            fallback = list(self.hist[symbol]["closes"])
            closes = fallback if len(fallback) >= 21 else []

        if len(closes) < 21:
            return "NEUTRAL"

        sma20_now = sma(closes, 20)
        sma20_prev = sma(closes[:-1], 20)
        if sma20_now <= 0 or sma20_prev <= 0:
            return "NEUTRAL"

        slope = (sma20_now - sma20_prev) / sma20_prev
        kospi_now = float(closes[-1])

        rets = []
        lookback = closes[-20:]
        for i in range(1, len(lookback)):
            prev = float(lookback[i - 1])
            curr = float(lookback[i])
            if prev > 0:
                rets.append((curr - prev) / prev)
        vix_proxy = statistics.pstdev(rets) * 100.0 if len(rets) >= 2 else 0.0

        if kospi_now > sma20_now * 1.01 and slope >= 0:
            return "BULL"
        if kospi_now < sma20_now * 0.99 and slope <= 0:
            return "BEAR"
        if abs(slope) < 0.0005 and vix_proxy >= 1.2:
            return "RANGE"
        return "NEUTRAL"

    def set_scanner(self, scanner) -> None:
        self._scanner = scanner

    async def _on_initialize(self) -> None:
        # Log effective entry threshold so we can confirm day overrides are applied.
        self.logger.info(f"[kr_swing] entry_threshold(min_score)={self.entry_threshold}")

        if self.dynamic_enabled:
            await self._refresh_universe()
        else:
            self._current_universe = list(self.symbols) if self.symbols else []
        await self._restore_position()

    async def _refresh_universe(self) -> None:
        try:
            universe = await build_universe(
                market="KR",
                style="SWING",
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
        interval = int(self.dynamic_config.get("scan_interval_sec", 600))
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._refresh_universe()
            except Exception as e:
                self.log_error(f"Universe refresh loop error: {e}")
                await asyncio.sleep(60)

    def get_current_universe(self) -> List[str]:
        return self._current_universe.copy()

    async def get_cash_available(self, symbol: str, price: float = 0.0) -> float:
        return await self.rest.get_cash_available(symbol, price)

    async def place_buy_order(self, symbol: str, qty: int, price: float) -> OrderResult:
        return await self.rest.place_buy_limit(symbol, qty, price)

    async def place_sell_order(self, symbol: str, qty: int, price: Optional[float] = None) -> OrderResult:
        if price is None:
            return await self.rest.place_sell_market(symbol, qty)
        return await self.rest.place_sell_limit(symbol, qty, price)

    async def get_positions(self) -> Optional[Position]:
        return await self.rest.get_positions()

    async def get_quote(self, symbol: str) -> dict:
        return await self.rest.get_quote(symbol)

    async def _restore_position(self) -> None:
        pos = await self.get_positions()
        self._pos = pos
        if pos:
            self.log_info(f"restored position: {pos.symbol} qty={pos.qty} avg={pos.avg_price}")

    def _get_agg(self, symbol: str) -> BarAggregator:
        if symbol in self._agg:
            return self._agg[symbol]

        def _on_15m_close(ab) -> None:
            # store aggregated history (as if bar)
            h = self.hist[symbol]
            h["closes"].append(float(ab.close))
            h["highs"].append(float(ab.high))
            h["lows"].append(float(ab.low))
            h["volumes"].append(float(ab.volume))
            # evaluate entry on 15m close
            asyncio.create_task(self._evaluate(symbol))

        agg = BarAggregator(15, _on_15m_close)
        self._agg[symbol] = agg
        return agg

    async def on_tick(self, tick: TradeTick) -> None:
        # Swing: we rely on 1m bars coming from upstream; tick handler is optional.
        if tick.symbol and tick.symbol not in self.vwap_by_symbol:
            self.vwap_by_symbol[tick.symbol] = VwapCalculator()
        if tick.symbol:
            self.last_price[tick.symbol] = float(tick.price)
            if tick.symbol in self.vwap_by_symbol:
                self.vwap_by_symbol[tick.symbol].update(tick)

    async def on_book(self, book: OrderBookTop) -> None:
        if book.symbol:
            self.last_book[book.symbol] = book

    async def on_bar_close(self, symbol: str, bar: Bar1m) -> None:
        # Aggregate 1m -> 15m
        self._get_agg(symbol).update(bar)

    async def _evaluate(self, symbol: str) -> None:
        if self._pos is not None:
            return

        closes = list(self.hist[symbol]["closes"])
        highs = list(self.hist[symbol]["highs"])
        lows = list(self.hist[symbol]["lows"])
        volumes = list(self.hist[symbol]["volumes"])
        if len(closes) < 40:
            return

        last_price = self.last_price.get(symbol)
        book = self.last_book.get(symbol)
        vwap = None
        vcalc = self.vwap_by_symbol.get(symbol)
        if vcalc is not None:
            vwap = vcalc.vwap()

        if last_price is None or book is None or vwap is None:
            return

        vol_sma20 = sma(volumes, 20) if len(volumes) >= 20 else 0.0
        curr_vol = float(volumes[-1]) if volumes else 0.0
        volume_power = (curr_vol / vol_sma20 * 100.0) if vol_sma20 and vol_sma20 > 0 else 100.0

        news_score = 0
        news_provider = getattr(self, "news", None)
        if news_provider is not None and hasattr(news_provider, "get_score"):
            try:
                news_score = int(news_provider.get_score(symbol))
            except Exception:
                news_score = 0

        # Minimal indicators for scoring reasons
        ind: Dict[str, Any] = {
            "rsi": rsi(closes, 14),
            "ema9": ema(closes, 9),
            "ema21": ema(closes, 21),
            "ma20": sma(closes, 20),
            "prev_close": closes[-2],
            "volume_power": volume_power,
            "news_score": news_score,
            "atr": atr(highs, lows, closes, 14),
        }
        _, _, m_hist = macd(closes, 12, 26, 9)
        ind["macd_hist"] = m_hist

        # Use a synthetic 1m bar wrapper with last close
        bar = Bar1m(
            start=datetime.now(),
            open=closes[-2],
            high=max(highs[-2], last_price),
            low=min(lows[-2], last_price),
            close=float(last_price),
            volume=float(self.hist[symbol]["volumes"][-1] if self.hist[symbol]["volumes"] else 0.0),
        )

        # Perfect100Strategy uses OR; for swing we treat it as optional. Ensure state is WAIT_SIGNAL.
        self.strategy.set_state(State.WAIT_SIGNAL)
        self.strategy.update_or(symbol, bar)  # best-effort (won't hurt)
        self.market_regime = self.detect_market_regime(symbol)
        market_regime = (self.market_regime or "").strip().upper()
        if not market_regime:
            market_regime = "NEUTRAL"
            self.log_warning(
                f"[kr_swing] market_regime missing for {symbol}; defaulting to NEUTRAL"
            )
        self.market_regime = market_regime

        sig = self.strategy.evaluate_entry(
            bar=bar,
            last_price=float(last_price),
            vwap=vwap,
            book=book,
            lever_symbol=self.symbol_lever,
            inverse_symbol=self.symbol_inverse,
            indicators=ind,
            market_regime=self.market_regime,
        )

        # Convert to standardized score output
        breakdown = ScoreBreakdown(
            {
                "SWING": 40,
                "MOMO": 20,
                "VWAP": 10,
                "RR": 30,
            }
        )
        score = float(getattr(sig, "score", 0) or 0)

        out = SignalScore(
            symbol=symbol,
            side=sig.side,
            score=score,
            threshold=float(self.entry_threshold),
            market="KR",
            style="SWING",
            risk_grade="B" if score >= self.entry_threshold else "C",
            breakdown=breakdown,
            reasons=list(sig.reasons or []),
        )
        audit_decision(
            symbol=symbol,
            kind="signal_score",
            market="KR",
            style="SWING",
            score=float(out.score),
            threshold=float(out.threshold),
            side=(out.side or None),
            reasons=list(out.reasons or []),
            extra={"breakdown": dict(out.breakdown.categories or {})},
        )

        self.log_info(out.to_line())

        if not out.is_actionable:
            return

        # Live gate
        if os.environ.get("KIS_KILL_SWITCH", "0").strip() == "1":
            return
        if os.path.exists(str((BASE_DIR / "STOP_TRADING.flag"))):
            return
        if os.environ.get("KIS_LIVE_ENABLED", "0").strip() != "1":
            return
        if os.environ.get("KIS_LIVE_CONFIRM", "").strip().upper() != "YES":
            return

        # Place live order
        cash = await self.get_cash_available(symbol, float(book.ask))
        budget = cash * self.entry_budget_pct
        qty = int(budget // float(book.ask))
        if qty <= 0:
            return

        from core.correlation import new_corr
        corr = new_corr("kr_entry")
        audit_decision(
            symbol=symbol,
            kind="entry_plan",
            correlation_id=corr,
            market="KR",
            style="SWING",
            side="BUY",
            extra={"qty": int(qty), "limit_price": float(book.ask)},
        )

        for _ in range(self.entry_retry_limit):
            o = await self.place_buy_order(symbol, qty, float(book.ask))
            audit_decision(
                symbol=symbol,
                kind="order_result",
                correlation_id=corr,
                market="KR",
                style="SWING",
                side="BUY",
                extra={"odno": getattr(o, 'order_id', None), "order": getattr(o, '__dict__', None) or str(o)},
            )
            await asyncio.sleep(2)
            pos = await self.get_positions()
            if pos and pos.symbol == symbol:
                self._pos = pos
                self.log_info(f"Entry filled: {symbol} qty={pos.qty}")
                return
            if o.order_id:
                await self.rest.cancel_order(o.order_id, symbol, qty)

    async def monitor_loop(self) -> None:
        # Swing exit management is still delegated to engine-level risk/exit.
        # For now, keep a lightweight position refresh loop.
        universe_task = None
        if self.dynamic_enabled:
            universe_task = asyncio.create_task(self._universe_refresh_loop())
        while self._running:
            try:
                self._pos = await self.get_positions()
            except Exception:
                pass
            await asyncio.sleep(10)
        if universe_task:
            universe_task.cancel()
