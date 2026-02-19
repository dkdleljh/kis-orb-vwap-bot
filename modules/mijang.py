"""Mijang Module - US stock trading module.

해외주식(미국) 단타 모듈.

Design goals (100점 운영 기준):
- 미장 휴장/서머타임(DST) 정확히 처리
- 1분봉 + VWAP 기반의 OR(오프닝 레인지) 단타 신호 생성
- 레이트리밋/과다호출 방지: low-frequency polling + 내부 캐시
- 안전: 기본값은 *신호 생성만* (자동 실주문은 별도 확인 플래그 필요)

Safety:
- 자동 실주문 활성화는 환경변수 KIS_US_LIVE_CONFIRM=YES 를 요구합니다.
  (사용자 동의 없이 주문 실행 금지)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, date, time as dt_time, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Set

from zoneinfo import ZoneInfo

import holidays

from bars_vwap import BarBuilder1m, VwapCalculator
from indicators import atr, ema, macd, rsi
from modules.base import BaseTradingModule, ModuleContext
from models import Position, OrderResult, TradeTick, OrderBookTop, Bar1m
from kis_rest_overseas import KISOverseasRestOrders
from fee_calculator import USFeeCalculator
from perfect_strategy import Perfect100Strategy
from news import NewsSentimentAnalyzer
from premarket_collector import PremarketCollector, CollectStats
from modules.us_buy_guard import evaluate_us_buy_guard, is_buy_order_failed, set_symbol_cooldown
from universe_builder import build_universe, resolve_universe_config


KST = ZoneInfo("Asia/Seoul")

# US stock market holiday and time handling
_us_holidays_cache: Dict[int, holidays.HolidayBase] = {}


def _get_us_holidays(year: int) -> holidays.HolidayBase:
    if year not in _us_holidays_cache:
        _us_holidays_cache[year] = holidays.US(years=year, observed=True)  # type: ignore[attr-defined]
    return _us_holidays_cache[year]


def is_us_dst_active() -> bool:
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_us = now_utc.astimezone(ZoneInfo("America/New_York"))
    dst_offset = now_us.dst()
    return dst_offset is not None and dst_offset.total_seconds() > 0


def get_us_timezone_info() -> Dict[str, Any]:
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_us = now_utc.astimezone(ZoneInfo("America/New_York"))

    is_dst = is_us_dst_active()
    us_market_open = dt_time(9, 30, 0)
    us_market_close = dt_time(16, 0, 0)

    offset_hours = 14 if not is_dst else 13

    return {
        "is_dst": is_dst,
        "timezone_name": now_us.tzname(),
        "us_market_open": us_market_open,
        "us_market_close": us_market_close,
        "offset_hours": offset_hours,
    }


def is_us_market_holiday(check_date: Optional[date] = None) -> bool:
    if check_date is None:
        check_date = date.today()

    us_holidays = _get_us_holidays(check_date.year)
    if check_date in us_holidays:
        return True

    # Good Friday (stock market closure)
    good_friday = _calculate_good_friday(check_date.year)
    if check_date == good_friday:
        return True

    return False


def _calculate_good_friday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    day_of_week = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * day_of_week) // 451
    month = (h + day_of_week - 7 * m + 114) // 31
    day = ((h + day_of_week - 7 * m + 114) % 31) + 1

    easter_sunday = date(year, month, day)
    return easter_sunday - timedelta(days=2)


def get_us_holiday_name(check_date: Optional[date] = None) -> Optional[str]:
    if check_date is None:
        check_date = date.today()

    if check_date.weekday() >= 5:
        return "Weekend"

    us_holidays = _get_us_holidays(check_date.year)
    return us_holidays.get(check_date)


def _get_market_hours_korea() -> tuple[dt_time, dt_time]:
    # KST open/close for US regular session
    if is_us_dst_active():
        return dt_time(22, 30, 0), dt_time(5, 0, 0)
    return dt_time(23, 30, 0), dt_time(6, 0, 0)


def is_us_market_open(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(tz=KST)

    if now.weekday() >= 5:
        return False
    if is_us_market_holiday(now.date()):
        return False

    current_time = now.hour * 60 + now.minute

    market_open, market_close = _get_market_hours_korea()
    market_open_minutes = market_open.hour * 60 + market_open.minute
    market_close_minutes = market_close.hour * 60 + market_close.minute

    if market_open_minutes > market_close_minutes:
        # crosses midnight
        return current_time >= market_open_minutes or current_time < market_close_minutes

    return market_open_minutes <= current_time < market_close_minutes


def get_us_market_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(tz=KST)

    weekday = now.weekday()
    check_date = now.date()

    is_weekend = weekday >= 5
    is_holiday = is_us_market_holiday(check_date)

    current_time = now.hour * 60 + now.minute

    market_open, market_close = _get_market_hours_korea()
    market_open_minutes = market_open.hour * 60 + market_open.minute
    market_close_minutes = market_close.hour * 60 + market_close.minute

    if market_open_minutes > market_close_minutes:
        is_market_hours = current_time >= market_open_minutes or current_time < market_close_minutes
        is_pre_market = (not is_weekend) and (not is_holiday) and (market_close_minutes <= current_time < market_open_minutes)
        is_after_hours = False
    else:
        is_market_hours = market_open_minutes <= current_time < market_close_minutes
        is_pre_market = (not is_weekend) and (not is_holiday) and (current_time < market_open_minutes)
        is_after_hours = (not is_weekend) and (not is_holiday) and (current_time >= market_close_minutes)

    tz_info = get_us_timezone_info()

    return {
        "is_open": (not is_weekend) and (not is_holiday) and is_market_hours,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "holiday_name": get_us_holiday_name(check_date) if is_holiday else None,
        "is_pre_market": is_pre_market,
        "is_after_hours": is_after_hours,
        "is_dst": tz_info["is_dst"],
        "timezone_name": tz_info["timezone_name"],
        "market_open_korea": market_open.strftime("%H:%M:%S"),
        "market_close_korea": market_close.strftime("%H:%M:%S"),
    }


def get_force_exit_time() -> dt_time:
    # KST time for force exit (user preference)
    if is_us_dst_active():
        return dt_time(5, 0, 0)
    return dt_time(4, 0, 0)


@dataclass
class _SymbolState:
    vwap: VwapCalculator
    bar_builder: BarBuilder1m
    closes: List[float]
    highs: List[float]
    lows: List[float]
    or_high: Optional[float] = None
    or_low: Optional[float] = None
    last_bar_start: Optional[datetime] = None


class MijangModule(BaseTradingModule):
    """US scalping module.

    Recommended default: signals-only.
    Enable live orders only when KIS_US_LIVE_CONFIRM=YES.
    """

    def __init__(
        self,
        context: ModuleContext,
        logger,
        rest_client: KISOverseasRestOrders,
        config: Dict[str, Any],
        time_rules: Optional[Any] = None,
    ):
        super().__init__(context, logger)
        self.rest = rest_client
        self.config = config

        self.exchange = context.exchange or "NASD"

        self.entry_budget_pct = float(config.get("entry_budget_pct", 0.20))
        self.stop_loss_pct = float(config.get("stop_loss_pct", -0.03))
        self.take_profit_pct = float(config.get("take_profit_pct", 0.05))
        self.entry_retry_limit = int(config.get("entry_retry_limit", 3))

        force_exit = get_force_exit_time()
        self.force_exit_time = force_exit.strftime("%H:%M:%S")

        self.max_positions = int(os.environ.get("KIS_US_MAX_POSITIONS", "5"))
        self.max_position_per_symbol = int(os.environ.get("KIS_US_MAX_POSITION_PER_SYMBOL", "1"))
        self.allow_existing_holdings = os.environ.get("KIS_US_ALLOW_EXISTING_HOLDINGS", "1").strip() != "0"
        self._positions: Dict[str, Position] = {}

        self.dynamic_config = resolve_universe_config("SCALP", context.config.get("dynamic_universe", {}))
        self.dynamic_enabled = os.environ.get("KIS_DYNAMIC_UNIVERSE", "1").strip() != "0"
        self._scanner = None
        self._current_universe: List[str] = []
        self._last_good_universe: List[str] = []
        self._last_scan_time: Optional[datetime] = None
        self._candidate_index = 0

        # one-entry-per-symbol-per-day (safety)
        self._entered_day: Optional[date] = None
        self._entered_symbols: Set[str] = set()

        # quote failure backoff
        self._quote_fail_streak = 0
        self._symbol_cooldown_until: Dict[str, float] = {}
        self._last_integrated_margin_warn_ts: Dict[str, float] = {}

        self.news_analyzer = NewsSentimentAnalyzer()
        self.news_score_by_symbol: Dict[str, int] = {}
        self.news_score_updated_at: Dict[str, float] = {}

        self.fee_calculator = USFeeCalculator(
            commission_per_share=0.005,
            slippage_rate=0.001,
        )

        self.perfect_strategy = Perfect100Strategy(
            logger=self.logger,
            min_score=int(config.get("min_entry_score", 50)),
            max_spread_pct=0.003,
            min_bid_ask_ratio=0.8,
            min_win_rate=0.55,
            min_r_ratio=1.5,
            # US OR window (ET 09:30~09:35) concept: handled via KST time below
            or_start_time="23:30",
            or_end_time="23:35",
        )

        # per-symbol state
        self._sym: Dict[str, _SymbolState] = {}

        # safety: signals-only unless explicitly confirmed
        self._live_confirm = os.environ.get("KIS_US_LIVE_CONFIRM", "").strip().upper() == "YES"
        if os.environ.get("KIS_KILL_SWITCH", "0").strip() == "1":
            self._live_confirm = False
        if not self._live_confirm:
            self.log_warning("US module is running in SIGNALS-ONLY mode (set KIS_US_LIVE_CONFIRM=YES and ensure KIS_KILL_SWITCH=0 to allow live orders)")

        # polling interval (seconds) to avoid API overuse
        self._quote_poll_sec = float(os.environ.get("KIS_US_QUOTE_POLL_SEC", "5"))

        # recommended universe cap
        self._universe_cap = int(os.environ.get("KIS_US_UNIVERSE_CAP", str(int(config.get("universe_cap", 30)))))

        # pre-market data/analysis mode (data collection only, no execution)
        self._premarket_enabled = os.environ.get("KIS_US_PREMARKET_COLLECT", "1").strip() == "1"
        self._premarket_poll_sec = max(15.0, float(os.environ.get("KIS_US_PREMARKET_POLL_SEC", "60")))
        self._premarket_max_symbols = int(
            os.environ.get("KIS_US_PREMARKET_MAX_SYMBOLS", str(self._universe_cap))
        )
        self._premarket_summary_interval_sec = int(os.environ.get("KIS_US_PREMARKET_SUMMARY_SEC", "600"))
        self._premarket_news_interval_sec = int(os.environ.get("KIS_US_PREMARKET_NEWS_SEC", "300"))
        self._premarket_metrics_interval_sec = int(os.environ.get("KIS_US_PREMARKET_METRICS_SEC", "300"))
        self._premarket_last_summary_ts = 0.0
        self._premarket_last_news_ts = 0.0
        self._premarket_last_metrics_ts = 0.0
        self._premarket_mode_active = False
        self._daily_cache: Dict[str, List[Dict[str, Any]]] = {}

        self._data_root = Path(os.environ.get("KIS_DATA_DIR", "data"))
        self._premarket_collector: Optional[PremarketCollector] = None
        if self._premarket_enabled:
            self._premarket_collector = PremarketCollector(
                rest_client=self.rest,
                logger=self.logger,
                data_root=self._data_root,
                market="US",
                poll_sec=int(self._premarket_poll_sec),
            )

    def set_scanner(self, scanner) -> None:
        self._scanner = scanner

    async def _on_initialize(self) -> None:
        self.log_info(f"Initialized with exchange: {self.exchange}, force_exit: {self.force_exit_time}")

        if self.dynamic_enabled:
            await self._refresh_universe()
        else:
            self._current_universe = list(self.symbols) if self.symbols else []

        await self._restore_position()

    async def _refresh_universe(self) -> None:
        try:
            dynamic_cfg = dict(self.dynamic_config)
            if self._universe_cap > 0:
                dynamic_cfg["cap"] = self._universe_cap
                dynamic_cfg["max_symbols"] = self._universe_cap
            universe = await build_universe(
                market="US",
                style="SCALP",
                scanner=self._scanner,
                base_symbols=list(self.symbols) if self.symbols else [],
                config=dynamic_cfg,
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
        while self._running:
            try:
                await asyncio.sleep(self.dynamic_config.get("scan_interval_sec", 300))
                await self._refresh_universe()
            except Exception as e:
                self.log_error(f"Universe refresh loop error: {e}")
                await asyncio.sleep(60)

    def get_current_universe(self) -> List[str]:
        return self._current_universe

    async def _estimate_cash_available_from_krw(self) -> float:
        """Estimate US buying power from KRW cash (integrated margin).

        WARNING: Estimate only. Broker-side rules may still reject orders.
        Enabled only when KIS_US_USE_INTEGRATED_MARGIN=1.
        """

        # 1) KR cash via domestic REST
        try:
            from kis_rest_orders import AccountInfo, KISRestOrders

            acct = os.environ.get("KIS_ACCOUNT_NO", "").strip()
            prdt = os.environ.get("KIS_ACCOUNT_PRODUCT_CODE", "01").strip()
            if not acct:
                return 0.0

            account = AccountInfo(account_no=acct, product_code=prdt)
            kr_rest = KISRestOrders(self.rest.base_url, self.rest.auth, account, self.logger)
            try:
                cash_krw = float(await kr_rest.get_cash_available("005930", 1.0))
            finally:
                await kr_rest.aclose()
        except Exception:
            cash_krw = 0.0

        if cash_krw <= 0:
            return 0.0

        # 2) USDKRW rate
        def _fetch_rate() -> float:
            import urllib.request, json as _json

            urls = [
                "https://open.er-api.com/v6/latest/USD",
                "https://api.exchangerate.host/latest?base=USD&symbols=KRW",
            ]
            for u in urls:
                try:
                    with urllib.request.urlopen(u, timeout=10) as r:
                        ex = _json.loads(r.read().decode("utf-8", errors="replace"))
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

        return (cash_krw / usdkrw) * 0.95

    async def get_cash_available(self, symbol: str, price: float = 0.0) -> float:
        try:
            cash = await self.rest.get_cash_available()
            cash = float(cash) if cash else 0.0
            if cash <= 0 and os.environ.get("KIS_US_USE_INTEGRATED_MARGIN", "0").strip() == "1":
                cash = await self._estimate_cash_available_from_krw()
                self.log_warning(f"[mijang] US cash_available=0 -> using integrated margin estimate: {cash:.2f} USD")
            return cash
        except Exception as e:
            self.log_warning(f"Failed to get cash: {e}")
            return 0.0

    async def place_buy_order(self, symbol: str, qty: int, price: float) -> OrderResult:
        return await self.rest.place_buy_order(symbol, qty, price)

    async def place_sell_order(self, symbol: str, qty: int, price: Optional[float] = None) -> OrderResult:
        if price:
            return await self.rest.place_sell_order(symbol, qty, price)
        return await self.rest.place_sell_market(symbol, qty)

    async def get_positions_list(self) -> List[Position]:
        try:
            getter = getattr(self.rest, "get_positions_list", None)
            if callable(getter):
                return await getter()
            pos = await self.rest.get_positions()
            return [pos] if pos else []
        except Exception as e:
            self.log_warning(f"Failed to get positions: {e}")
            return []

    async def get_positions(self) -> Optional[Position]:
        positions = await self.get_positions_list()
        return positions[0] if positions else None

    def _positions_map(self, positions: List[Position]) -> Dict[str, Position]:
        return {str(p.symbol).upper(): p for p in positions if getattr(p, "symbol", None)}

    def _entry_blocked_by_positions(self, symbol: str, positions_map: Dict[str, Position]) -> bool:
        if symbol in positions_map:
            # Current position model is one Position row per symbol (no same-symbol add yet).
            if self.max_position_per_symbol <= 1:
                return True
            return True
        if len(positions_map) >= max(1, self.max_positions):
            return True
        if (not self.allow_existing_holdings) and positions_map:
            return True
        return False

    async def _refresh_positions(self) -> Dict[str, Position]:
        self._positions = self._positions_map(await self.get_positions_list())
        return self._positions

    async def _restore_position(self) -> None:
        positions_map = await self._refresh_positions()
        self.log_info(f"Restored positions: {len(positions_map)} holdings")
        for pos in positions_map.values():
            self.log_info(f"Restored position: {pos.symbol} qty={pos.qty}")

    async def get_quote(self, symbol: str) -> dict:
        """Quote fetch with retry/backoff.

        - Handles transient disconnections
        - Adds per-symbol cooldown to avoid hammering
        """
        now = datetime.now(tz=KST).timestamp()
        cd = self._symbol_cooldown_until.get(symbol, 0.0)
        if now < cd:
            return {}

        last_err: Optional[Exception] = None
        for i in range(3):
            try:
                q = await self.rest.get_quote(symbol)
                self._quote_fail_streak = 0
                return q
            except Exception as e:
                last_err = e
                self._quote_fail_streak += 1
                # exponential backoff + jitter
                await asyncio.sleep(min(2.0, (0.25 * (2**i)) + random.random() * 0.2))

        # cooldown this symbol for 60s, and if global streak is high, pause harder
        self._symbol_cooldown_until[symbol] = datetime.now(tz=KST).timestamp() + 60.0
        if self._quote_fail_streak >= 10:
            await asyncio.sleep(30)
        if last_err:
            self.log_warning(f"get_quote({symbol}) failed after retries: {last_err}")
        return {}

    def _get_sym_state(self, symbol: str) -> _SymbolState:
        if symbol in self._sym:
            return self._sym[symbol]

        closes: List[float] = []
        highs: List[float] = []
        lows: List[float] = []

        def _on_bar_close(bar: Bar1m) -> None:
            closes.append(float(bar.close))
            highs.append(float(bar.high))
            lows.append(float(bar.low))
            # keep short memory (scalp)
            if len(closes) > 200:
                del closes[:50]
                del highs[:50]
                del lows[:50]

        st = _SymbolState(
            vwap=VwapCalculator(),
            bar_builder=BarBuilder1m(_on_bar_close),
            closes=closes,
            highs=highs,
            lows=lows,
        )
        self._sym[symbol] = st
        return st

    def _us_open_dt_kst(self, now: datetime) -> datetime:
        open_t, _ = _get_market_hours_korea()
        open_dt = now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
        # open time could be "yesterday" if now is after midnight but before close.
        if now.hour < 12 and open_t.hour > 12:
            open_dt = open_dt - timedelta(days=1)
        return open_dt

    def _in_or_window(self, now: datetime) -> bool:
        open_dt = self._us_open_dt_kst(now)
        return open_dt <= now < (open_dt + timedelta(minutes=5))

    async def monitor_loop(self) -> None:
        self.log_info("Starting US stock monitoring loop...")

        universe_task = None
        if self.dynamic_enabled:
            universe_task = asyncio.create_task(self._universe_refresh_loop())

        while self._running:
            try:
                status = get_us_market_status(datetime.now(tz=KST))
                self._log_market_mode_transition(status)
                sleep_sec = await self._run_market_iteration(status)
                await asyncio.sleep(sleep_sec)
            except Exception as e:
                self.log_error(f"Monitor loop error: {e}")
                await asyncio.sleep(10)

        if universe_task:
            universe_task.cancel()

    async def _run_market_iteration(self, status: Dict[str, Any]) -> float:
        if status.get("is_open"):
            await self._check_positions()
            await self._check_force_exit()

            if self._current_universe:
                await self._check_next_candidate()

            return self._quote_poll_sec

        if status.get("is_pre_market") and self._premarket_enabled:
            await self._run_premarket_cycle()
            return self._premarket_poll_sec

        return 30.0

    def _log_market_mode_transition(self, status: Dict[str, Any]) -> None:
        is_pre = bool(status.get("is_pre_market"))
        if is_pre and not self._premarket_mode_active:
            self._premarket_mode_active = True
            ymd = datetime.now(tz=KST).strftime("%Y%m%d")
            root = self._data_root / "premarket_us" / ymd
            self.log_info(
                f"Entered US pre-market collection mode: poll={int(self._premarket_poll_sec)}s "
                f"max_symbols={self._premarket_max_symbols} path={root}"
            )
        elif (not is_pre) and self._premarket_mode_active:
            self._premarket_mode_active = False
            self.log_info("Exited US pre-market collection mode")

    def _premarket_symbols(self) -> List[str]:
        if not self._current_universe:
            return []
        cap = self._premarket_max_symbols if self._premarket_max_symbols > 0 else len(self._current_universe)
        return self._current_universe[:cap]

    def _news_cache_path(self, now: datetime) -> Path:
        return self._data_root / "news_cache_us" / f"{now.strftime('%Y%m%d')}.json"

    def _metrics_path(self, now: datetime) -> Path:
        return self._data_root / "premarket_metrics_us" / f"{now.strftime('%Y%m%d')}.json"

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")

    async def _run_premarket_cycle(self) -> None:
        now = datetime.now(tz=KST)
        now_ts = now.timestamp()

        if not self._current_universe:
            if self.dynamic_enabled:
                await self._refresh_universe()
            else:
                self._current_universe = list(self.symbols) if self.symbols else []

        if self.dynamic_enabled:
            interval = int(self.dynamic_config.get("scan_interval_sec", 300))
            due = (self._last_scan_time is None) or ((now - self._last_scan_time).total_seconds() >= interval)
            if due:
                await self._refresh_universe()

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

    async def _update_premarket_news(self, symbols: List[str], now: datetime) -> None:
        updated = 0
        for symbol in symbols:
            try:
                result = await self.news_analyzer.get_us_sentiment_score(symbol)
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

    def _load_daily_bars(self, symbol: str) -> List[Dict[str, Any]]:
        if symbol in self._daily_cache:
            return self._daily_cache[symbol]
        daily_dir = self._data_root / "daily_us"
        bars: List[Dict[str, Any]] = []
        try:
            candidates = sorted(daily_dir.glob(f"{symbol}_*.json"))
            if candidates:
                text = candidates[-1].read_text(encoding="utf-8")
                obj = json.loads(text)
                if isinstance(obj, list):
                    bars = [x for x in obj if isinstance(x, dict)]
        except Exception:
            bars = []
        self._daily_cache[symbol] = bars
        return bars

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

        atr_val = atr(highs, lows, closes, 14)
        atr_pct = (atr_val / last) * 100.0 if (atr_val > 0 and last > 0) else 0.0

        daily_bars = self._load_daily_bars(symbol)
        daily_atr_pct = 0.0
        daily_trend = "unknown"
        if daily_bars:
            d_high: List[float] = []
            d_low: List[float] = []
            d_close: List[float] = []
            for row in daily_bars:
                h = float(row.get("high") or 0.0)
                l = float(row.get("low") or 0.0)
                c = float(row.get("close") or 0.0)
                if h > 0 and l > 0 and c > 0:
                    d_high.append(h)
                    d_low.append(l)
                    d_close.append(c)
            d_atr = atr(d_high, d_low, d_close, 14)
            daily_atr_pct = (d_atr / last) * 100.0 if (d_atr > 0 and last > 0) else 0.0
            if len(d_close) >= 20:
                ma20 = sum(d_close[-20:]) / 20.0
                daily_trend = "up" if d_close[-1] >= ma20 else "down"

        return {
            "last": last,
            "premarket_change_pct": round(premarket_change_pct, 4),
            "premarket_atr_pct_est": round(atr_pct, 4),
            "daily_atr_pct": round(daily_atr_pct, 4),
            "premarket_trend": trend,
            "daily_trend": daily_trend,
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

    def _us_session_bounds_kst(self, now_dt: datetime) -> tuple[datetime, datetime]:
        """Return (session_start_dt_kst, session_end_dt_kst) for the current US regular session.

        US regular session in KST typically spans 23:30 ~ 06:00 (cross-midnight).
        This helper prevents naive time-only comparisons that break around midnight.
        """
        status = get_us_market_status(now_dt)
        open_t = datetime.strptime(status["market_open_korea"], "%H:%M:%S").time()
        close_t = datetime.strptime(status["market_close_korea"], "%H:%M:%S").time()

        crosses_midnight = open_t > close_t
        today = now_dt.date()

        # Determine the session start date.
        if crosses_midnight:
            start_date = today if now_dt.time() >= open_t else (today - timedelta(days=1))
            end_date = start_date + timedelta(days=1)
        else:
            start_date = today
            end_date = today

        session_start = datetime.combine(start_date, open_t, tzinfo=KST)
        session_end = datetime.combine(end_date, close_t, tzinfo=KST)
        return session_start, session_end

    async def _check_force_exit(self) -> None:
        now_dt = datetime.now(tz=KST)
        try:
            status = get_us_market_status(now_dt)
            if not status.get("is_open"):
                return

            exit_time = datetime.strptime(self.force_exit_time, "%H:%M:%S").time()
            session_start, session_end = self._us_session_bounds_kst(now_dt)

            # Force-exit timestamp should be within the session window.
            open_t = session_start.time()
            crosses_midnight = session_start.date() != session_end.date()
            force_exit_date = session_start.date()
            if crosses_midnight and exit_time < open_t:
                force_exit_date = session_start.date() + timedelta(days=1)

            force_exit_dt = datetime.combine(force_exit_date, exit_time, tzinfo=KST)

            if session_start <= now_dt <= session_end and now_dt >= force_exit_dt:
                if self._positions:
                    self.log_warning(
                        f"Force exit time reached ({self.force_exit_time}), closing position"
                    )
                    await self._handle_exit("force_exit")
        except Exception as e:
            self.log_warning(f"Force exit check failed: {e}")

    async def _handle_exit(self, reason: str, symbol: Optional[str] = None) -> None:
        if not self._positions:
            return

        target_symbols = []
        if symbol:
            sym = str(symbol).upper()
            if sym in self._positions:
                target_symbols = [sym]
        else:
            target_symbols = list(self._positions.keys())
        if not target_symbols:
            return

        if not self._live_confirm:
            self.log_warning("signals-only mode: exit order suppressed")
            return

        for sym in target_symbols:
            pos = self._positions.get(sym)
            if not pos:
                continue
            self.log_info(f"Exiting position: {reason} {pos.symbol} qty={pos.qty}")
            await self.place_sell_order(pos.symbol, pos.qty)
        await asyncio.sleep(3)

        positions_after = await self._refresh_positions()
        closed_count = sum(1 for sym in target_symbols if sym not in positions_after)
        if closed_count > 0:
            self.log_info(f"Position closed: {reason} closed={closed_count}")

    def _roll_day_if_needed(self) -> None:
        today = datetime.now(tz=KST).date()
        if self._entered_day != today:
            self._entered_day = today
            self._entered_symbols = set()

    async def _check_next_candidate(self) -> None:
        if not self._current_universe:
            return

        self._roll_day_if_needed()

        # try a few symbols per loop to skip cooldown/entered symbols
        for _ in range(min(5, len(self._current_universe))):
            symbol = self._current_universe[self._candidate_index]
            self._candidate_index = (self._candidate_index + 1) % max(1, len(self._current_universe))

            if symbol in self._entered_symbols:
                continue

            now = datetime.now(tz=KST).timestamp()
            if now < self._symbol_cooldown_until.get(symbol, 0.0):
                continue

            await self.execute_strategy(symbol)
            return

    async def _check_positions(self) -> None:
        prev = set(self._positions.keys())
        curr = await self._refresh_positions()
        new_symbols = sorted(set(curr.keys()) - prev)
        for sym in new_symbols:
            pos = curr[sym]
            self.log_info(f"Position detected: {pos.symbol} qty={pos.qty} @ {pos.avg_price}")

    def _make_book_from_quote(self, symbol: str, quote: dict, last_price: float) -> OrderBookTop:
        # Best-effort: if bid/ask not available, synthesize a tiny spread.
        bid = float(quote.get("bid") or quote.get("bid_price") or (last_price * 0.9995))
        ask = float(quote.get("ask") or quote.get("ask_price") or (last_price * 1.0005))
        return OrderBookTop(
            symbol=symbol,
            bid=bid,
            ask=ask,
            bid_size=float(quote.get("bid_size") or 1.0),
            ask_size=float(quote.get("ask_size") or 1.0),
            timestamp=datetime.now(tz=KST),
        )

    def _extract_quote_fields(self, quote: dict) -> dict:
        """KIS 해외 시세 응답에서 핵심 필드를 최대한 robust하게 추출.

        - KISOverseasRestOrders.get_quote()는 통상 {output:{...}} 형태
        - 테스트/모의/다른 소스는 flat dict일 수 있어 fallback 제공
        """
        if not isinstance(quote, dict):
            return {}
        out = quote.get("output") if isinstance(quote.get("output"), dict) else quote

        def _get_float(*keys: str) -> Optional[float]:
            for k in keys:
                v = out.get(k)
                if v is None or v == "":
                    continue
                try:
                    return float(v)
                except Exception:
                    continue
            return None

        last = _get_float("last", "lastxch", "last_price", "price")
        bid = _get_float("bid", "bid_price", "bido")
        ask = _get_float("ask", "ask_price", "askp")
        vol = _get_float("tvol", "volume", "vol")

        return {"last": last, "bid": bid, "ask": ask, "vol": vol}

    async def execute_strategy(self, symbol: str) -> None:
        """US scalping strategy (OR + VWAP) signal generation.

        Live orders require KIS_US_LIVE_CONFIRM=YES.
        """

        symbol = str(symbol).upper()
        if self._entry_blocked_by_positions(symbol, self._positions):
            return

        try:
            quote = await self.get_quote(symbol)
            q = self._extract_quote_fields(quote)
            if not q or q.get("last") is None:
                return
            last_price = float(q["last"])  # type: ignore[arg-type]

            st = self._get_sym_state(symbol)

            # synthesize a tick.
            # NOTE: quote의 volume은 누적일 수 있어 VWAP을 왜곡할 수 있음 → 기본 1.0, 값이 작을 때만 사용.
            raw_vol = float(q.get("vol") or 0.0)
            vol = raw_vol if 0 < raw_vol < 1_000 else 1.0
            tick = TradeTick(symbol=symbol, price=last_price, volume=vol, timestamp=datetime.now(tz=KST))
            st.vwap.update(tick)
            st.bar_builder.update(tick)

            now_dt = datetime.now(tz=KST)

            # Update OR during first 5 minutes after open
            if self._in_or_window(now_dt) and st.bar_builder.current_bar is not None:
                b = st.bar_builder.current_bar
                st.or_high = max(st.or_high or b.high, b.high)
                st.or_low = min(st.or_low or b.low, b.low)
                # Keep strategy in build state
                self.perfect_strategy.set_state(self.perfect_strategy.state.BUILD_OR)  # type: ignore[attr-defined]
                return

            # After OR window, allow signals
            if st.or_high is None or st.or_low is None:
                return

            self.perfect_strategy.set_state(self.perfect_strategy.state.WAIT_SIGNAL)  # type: ignore[attr-defined]

            vwap_val = st.vwap.vwap()
            # merge extracted fields into quote for book synth
            quote_for_book = {**(quote.get("output") if isinstance(quote.get("output"), dict) else {}), **q}
            book = self._make_book_from_quote(symbol, quote_for_book, last_price)

            closes = st.closes
            if len(closes) < 30:
                return

            indicators: Dict[str, Any] = {}
            indicators["rsi"] = rsi(closes, 14)
            indicators["ema9"] = ema(closes, 9)
            indicators["ema21"] = ema(closes, 21)
            m_line, m_sig, m_hist = macd(closes, 12, 26, 9)
            indicators["macd_hist"] = m_hist
            indicators["ma20"] = sum(closes[-20:]) / 20.0
            indicators["prev_close"] = closes[-2]
            # ATR은 highs/lows 필요
            indicators["atr"] = atr(st.highs, st.lows, st.closes, 14)

            # TODO: 실제 체결/거래대금 기반 volume_power 연동. (현재는 보수적 기본값)
            indicators["volume_power"] = 120
            indicators["news_score"] = 0

            # Use last closed bar if available, else synthesize from last price
            bar = st.bar_builder.current_bar
            if bar is None:
                bar = Bar1m(start=now_dt.replace(second=0, microsecond=0), open=last_price, high=last_price, low=last_price, close=last_price, volume=vol)

            # Feed OR state to Perfect100Strategy
            # OR high/low는 이미 st에 누적되어 있으므로, 한 번만 update (idempotent)
            self.perfect_strategy.update_or(
                symbol,
                Bar1m(
                    start=bar.start,
                    open=bar.open,
                    high=float(st.or_high),
                    low=float(st.or_low),
                    close=bar.close,
                    volume=bar.volume,
                ),
            )

            signal = self.perfect_strategy.evaluate_entry(
                bar=bar,
                last_price=last_price,
                vwap=vwap_val,
                book=book,
                lever_symbol=symbol,
                inverse_symbol=symbol,
                indicators=indicators,
                market_regime="NEUTRAL",
            )

            if not signal.side or not signal.symbol:
                return

            # Signals-only unless confirmed
            self.log_info(f"US signal {signal.symbol} side={signal.side} score={signal.score} reasons={signal.reasons[:3]}")
            if not self._live_confirm:
                return
            if os.environ.get("KIS_KILL_SWITCH", "0").strip() == "1":
                return
            if (Path(__file__).resolve().parents[1] / "STOP_TRADING.flag").exists():
                return

            # 실주문 가격은 ask 기반(매수)으로 보수적으로 산정
            entry_px = float(book.ask) if book.ask > 0 else last_price
            cash_detail = await self.rest.get_cash_available_detail(symbol, entry_px)
            cash = float(cash_detail.get("cash_available") or 0.0)
            ord_psbl_qty: Optional[int] = None
            raw_ord_psbl_qty = cash_detail.get("ord_psbl_qty")
            if raw_ord_psbl_qty not in (None, ""):
                try:
                    ord_psbl_qty = int(float(raw_ord_psbl_qty))
                except Exception:
                    ord_psbl_qty = None
            integrated_mode = os.environ.get("KIS_US_USE_INTEGRATED_MARGIN", "0").strip() == "1"
            integrated_margin_estimate_usd = 0.0
            if integrated_mode:
                integrated_margin_estimate_usd = await self._estimate_cash_available_from_krw()
            min_usd = float(os.environ.get("KIS_US_INTEGRATED_MARGIN_MIN_USD", "100"))
            guard = evaluate_us_buy_guard(
                integrated_margin_mode=integrated_mode,
                ord_psbl_qty=ord_psbl_qty,
                integrated_margin_estimate_usd=integrated_margin_estimate_usd,
                min_usd=min_usd,
            )
            now_ts = datetime.now(tz=KST).timestamp()
            if not guard.buy_attempt_allowed:
                return
            if guard.used_integrated_margin_fallback:
                last_warn = self._last_integrated_margin_warn_ts.get(symbol, 0.0)
                if now_ts - last_warn >= 600.0:
                    self.log_warning(
                        f"[mijang] ord_psbl_qty={ord_psbl_qty} err={cash_detail.get('err')} -> using integrated margin estimate={integrated_margin_estimate_usd:.2f} USD (min={min_usd:.2f})"
                    )
                    self._last_integrated_margin_warn_ts[symbol] = now_ts
                cash = max(cash, integrated_margin_estimate_usd)

            budget = cash * self.entry_budget_pct
            qty = int(budget / entry_px)
            if qty < 1:
                return

            latest_positions = await self._refresh_positions()
            if self._entry_blocked_by_positions(symbol, latest_positions):
                return

            # one-entry-per-symbol-per-day safety pin: mark once we start live attempts
            self._entered_symbols.add(symbol)

            for attempt in range(self.entry_retry_limit):
                order = await self.place_buy_order(symbol, qty, entry_px)
                if is_buy_order_failed(getattr(order, "status", ""), getattr(order, "order_id", "")):
                    until = set_symbol_cooldown(
                        cooldown_map=self._symbol_cooldown_until,
                        symbol=symbol,
                        now_ts=datetime.now(tz=KST).timestamp(),
                        cooldown_sec=float(os.environ.get("KIS_US_BUY_REJECT_COOLDOWN_SEC", "120")),
                    )
                    self.log_warning(
                        f"[mijang] buy failed symbol={symbol} reason={getattr(order, 'status', '')} cooldown_until={int(until)}"
                    )
                if order.order_id:
                    await asyncio.sleep(3)
                    refreshed = await self._refresh_positions()
                    if symbol in refreshed:
                        self.log_info(f"Order filled: {symbol} qty={refreshed[symbol].qty}")
                        return
                if order.order_id:
                    await self.rest.cancel_order(order.order_id, symbol, qty)

        except Exception as e:
            self.log_error(f"Strategy execution failed: {e}")
