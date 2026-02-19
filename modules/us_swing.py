"""US Swing module (15m-based) with scoring output.

추천값:
- 15분봉 기반 스윙 (1분봉 15개 집계)
- 진입 임계값: config.trading.scoring.us_swing_entry_threshold (default 68)

실주문 게이트:
- 환경변수 KIS_US_LIVE_CONFIRM=YES 일 때만 주문 허용
- 또한 KIS_KILL_SWITCH=1 또는 STOP_TRADING.flag 존재 시 주문 거부
"""

from __future__ import annotations

import asyncio
import urllib.request
import json
import os
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from bars_agg import BarAggregator
from bars_vwap import VwapCalculator
from indicators import atr, ema, macd, rsi, sma
from kis_rest_overseas import KISOverseasRestOrders
from models import Bar1m, OrderBookTop, OrderResult, Position, TradeTick
from modules.base import BaseTradingModule, ModuleContext
from perfect_strategy import Perfect100Strategy, State
from scoring import ScoreBreakdown, SignalScore
from core.audit_log import audit_decision
from strategy_profiles import get_recommended_threshold


class USSwingModule(BaseTradingModule):
    def __init__(
        self,
        context: ModuleContext,
        logger,
        rest_client: KISOverseasRestOrders,
        config: Dict[str, Any],
    ):
        super().__init__(context, logger)
        self.rest = rest_client
        self.config = config

        self.exchange = context.exchange or "NASD"

        self.entry_budget_pct = float(config.get("entry_budget_pct", 0.12))
        self.stop_loss_pct = float(config.get("stop_loss_pct", -0.03))
        self.take_profit_pct = float(config.get("take_profit_pct", 0.06))
        self.entry_retry_limit = int(config.get("entry_retry_limit", 3))

        scoring_cfg = (config.get("scoring", {}) or {})
        self.entry_threshold = int(scoring_cfg.get("us_swing_entry_threshold", get_recommended_threshold("US", "SWING")))

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

        self._agg: Dict[str, BarAggregator] = {}

        self.strategy = Perfect100Strategy(
            logger=self.logger,
            min_score=self.entry_threshold,
            max_spread_pct=float(config.get("max_spread_pct", 0.010)),
            min_bid_ask_ratio=0.7,
            min_win_rate=0.53,
            min_r_ratio=1.2,
        )

    async def _on_initialize(self) -> None:
        await self._restore_position()

    async def get_cash_available(self, symbol: str, price: float = 0.0) -> float:
        return float(await self.rest.get_cash_available(symbol))

    async def place_buy_order(self, symbol: str, qty: int, price: float) -> OrderResult:
        return await self.rest.place_buy_order(symbol, qty, price)

    async def place_sell_order(self, symbol: str, qty: int, price: Optional[float] = None) -> OrderResult:
        if price is None:
            return await self.rest.place_sell_market(symbol, qty)
        return await self.rest.place_sell_order(symbol, qty, price)

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
            h = self.hist[symbol]
            h["closes"].append(float(ab.close))
            h["highs"].append(float(ab.high))
            h["lows"].append(float(ab.low))
            h["volumes"].append(float(ab.volume))
            asyncio.create_task(self._evaluate(symbol))

        agg = BarAggregator(15, _on_15m_close)
        self._agg[symbol] = agg
        return agg

    async def on_tick(self, tick: TradeTick) -> None:
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
        self._get_agg(symbol).update(bar)

    async def _evaluate(self, symbol: str) -> None:
        if self._pos is not None:
            return

        closes = list(self.hist[symbol]["closes"])
        highs = list(self.hist[symbol]["highs"])
        lows = list(self.hist[symbol]["lows"])
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

        ind: Dict[str, Any] = {
            "rsi": rsi(closes, 14),
            "ema9": ema(closes, 9),
            "ema21": ema(closes, 21),
            "ma20": sma(closes, 20),
            "prev_close": closes[-2],
            "volume_power": 120,
            "news_score": 0,
            "atr": atr(highs, lows, closes, 14),
        }
        _, _, m_hist = macd(closes, 12, 26, 9)
        ind["macd_hist"] = m_hist

        bar = Bar1m(
            start=datetime.now(),
            open=closes[-2],
            high=max(highs[-2], float(last_price)),
            low=min(lows[-2], float(last_price)),
            close=float(last_price),
            volume=float(self.hist[symbol]["volumes"][-1] if self.hist[symbol]["volumes"] else 0.0),
        )

        self.strategy.set_state(State.WAIT_SIGNAL)
        self.strategy.update_or(symbol, bar)

        sig = self.strategy.evaluate_entry(
            bar=bar,
            last_price=float(last_price),
            vwap=vwap,
            book=book,
            lever_symbol=symbol,
            inverse_symbol=symbol,
            indicators=ind,
            market_regime="NEUTRAL",
        )

        breakdown = ScoreBreakdown({"SWING": 40, "MOMO": 20, "VWAP": 10, "RR": 30})
        # Use strategy's computed score; do NOT fall back to breakdown.total (would always look like 100).
        score = float(getattr(sig, "score", 0) or 0)

        out = SignalScore(
            symbol=symbol,
            side=sig.side,
            score=score,
            threshold=float(self.entry_threshold),
            market="US",
            style="SWING",
            risk_grade="B" if score >= self.entry_threshold else "C",
            breakdown=breakdown,
            reasons=list(sig.reasons or []),
        )
        # audit: store score snapshot for explainability
        audit_decision(
            symbol=symbol,
            kind="signal_score",
            market="US",
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
        # US uses explicit confirm gate
        if os.environ.get("KIS_US_LIVE_CONFIRM", "").strip().upper() != "YES":
            return

        cash = await self.get_cash_available(symbol, float(book.ask))
        if cash <= 0 and os.environ.get("KIS_US_USE_INTEGRATED_MARGIN", "0").strip() == "1":
            cash = await self._estimate_cash_available_from_krw()
            self.log_warning(f"[us_swing] US cash_available=0 -> using integrated margin estimate: {cash:.2f} USD")

        budget = cash * self.entry_budget_pct
        entry_px = float(book.ask) if book.ask > 0 else float(last_price)
        qty = int(budget // entry_px)
        if qty <= 0:
            return

        from core.correlation import new_corr
        corr = new_corr("us_entry")
        audit_decision(
            symbol=symbol,
            kind="entry_plan",
            correlation_id=corr,
            market="US",
            style="SWING",
            side="BUY",
            extra={"qty": int(qty), "limit_price": float(entry_px)},
        )

        for _ in range(self.entry_retry_limit):
            o = await self.place_buy_order(symbol, qty, entry_px)
            audit_decision(
                symbol=symbol,
                kind="order_result",
                correlation_id=corr,
                market="US",
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

    async def _estimate_cash_available_from_krw(self) -> float:
        """Estimate US buying power from KRW cash (integrated margin).

        WARNING: This is an estimate only. Broker-side rules may still reject orders.
        Enabled only when KIS_US_USE_INTEGRATED_MARGIN=1.
        """

        # 1) Get KR cash via domestic REST
        try:
            from kis_rest_orders import AccountInfo, KISRestOrders

            acct = os.environ.get("KIS_ACCOUNT_NO", "").strip()
            prdt = os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01").strip()
            if not acct:
                return 0.0

            account = AccountInfo(account_no=acct, product_code=prdt)
            kr_rest = KISRestOrders(self.rest.base_url, self.rest.auth, account, self.logger)
            try:
                # domestic inquire-psbl-order needs a symbol+price; use a tiny positive price.
                cash_krw = float(await kr_rest.get_cash_available("005930", 1.0))
            finally:
                await kr_rest.aclose()
        except Exception:
            cash_krw = 0.0

        if cash_krw <= 0:
            return 0.0

        # 2) USDKRW rate (best-effort)
        def _fetch_rate() -> float:
            urls = [
                "https://open.er-api.com/v6/latest/USD",
                "https://api.exchangerate.host/latest?base=USD&symbols=KRW",
            ]
            for u in urls:
                try:
                    with urllib.request.urlopen(u, timeout=10) as r:
                        ex = json.loads(r.read().decode("utf-8", errors="replace"))
                    rates = ex.get("rates") or ex.get("conversion_rates") or {}
                    v = rates.get("KRW")
                    if v:
                        return float(v)
                except Exception:
                    continue
            return 0.0

        usdkrw = await asyncio.to_thread(_fetch_rate)
        if usdkrw <= 0:
            return 0.0

        # Conservative haircut (fees/slippage/margin rules)
        return (cash_krw / usdkrw) * 0.95

    async def monitor_loop(self) -> None:
        while self._running:
            try:
                self._pos = await self.get_positions()
            except Exception:
                pass
            await asyncio.sleep(10)
