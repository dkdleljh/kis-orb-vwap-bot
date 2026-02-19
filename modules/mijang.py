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
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, date, time as dt_time, timedelta
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


KST = ZoneInfo("Asia/Seoul")

_US_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


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

        self._position: Optional[Position] = None
        self._entry_price: float = 0.0
        self._peak_pnl: float = 0.0

        self.dynamic_config = context.config.get("dynamic_universe", {})
        self.dynamic_enabled = self.dynamic_config.get("enabled", False)
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

    def set_scanner(self, scanner) -> None:
        self._scanner = scanner

    async def _on_initialize(self) -> None:
        self.log_info(f"Initialized with exchange: {self.exchange}, force_exit: {self.force_exit_time}")

        if self.dynamic_enabled:
            await self._refresh_universe()
        else:
            self._current_universe = self._apply_universe_policy(list(self.symbols) if self.symbols else [])

        await self._restore_position()

    def _is_valid_us_symbol(self, symbol: str) -> bool:
        s = (symbol or "").strip().upper()
        # Exclude KR-style or odd suffix symbols like 02850K, and anything too long.
        return bool(_US_SYMBOL_RE.match(s))

    def _apply_universe_policy(self, symbols: List[str]) -> List[str]:
        # normalize + filter
        cleaned: List[str] = []
        for sym in symbols:
            s = (sym or "").strip().upper()
            if not s:
                continue
            if not self._is_valid_us_symbol(s):
                continue
            cleaned.append(s)

        # de-dup while preserving order
        seen = set()
        uniq: List[str] = []
        for s in cleaned:
            if s in seen:
                continue
            seen.add(s)
            uniq.append(s)

        # cap
        if self._universe_cap > 0 and len(uniq) > self._universe_cap:
            uniq = uniq[: self._universe_cap]

        return uniq

    async def _refresh_universe(self) -> None:
        if not self._scanner:
            self.log_warning("US Scanner not set, using default universe")
            base = list(self.symbols) if self.symbols else []
            self._current_universe = self._apply_universe_policy(base)
            return

        try:
            top_n = int(self.dynamic_config.get("top_n", 30))
            exchanges = self.dynamic_config.get("exchanges", ["NASD", "NYSE"])
            min_price = self.dynamic_config.get("min_price_usd", 5.0)

            scanned = await self._scanner.scan(top_n=top_n, exchanges=exchanges, min_price=min_price)

            if scanned:
                scanned2 = self._apply_universe_policy(scanned)
                self._current_universe = scanned2
                self._last_good_universe = scanned2
                self._last_scan_time = datetime.now(tz=KST)
                self.log_info(f"US Universe refreshed: {len(scanned2)} stocks (cap={self._universe_cap})")
            else:
                self.log_warning("US Scanner returned empty, using last good universe")
                self._current_universe = self._last_good_universe or self._apply_universe_policy(list(self.symbols) if self.symbols else [])
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

    async def get_positions(self) -> Optional[Position]:
        try:
            return await self.rest.get_positions()
        except Exception as e:
            self.log_warning(f"Failed to get positions: {e}")
            return None

    async def _restore_position(self) -> None:
        pos = await self.get_positions()
        if pos:
            self._position = pos
            self._entry_price = pos.avg_price
            self._peak_pnl = 0.0
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
                if not status.get("is_open"):
                    # keep it quiet; no spam
                    await asyncio.sleep(30)
                    continue

                await self._check_positions()
                await self._check_force_exit()

                if self._current_universe:
                    await self._check_next_candidate()

                await asyncio.sleep(self._quote_poll_sec)
            except Exception as e:
                self.log_error(f"Monitor loop error: {e}")
                await asyncio.sleep(10)

        if universe_task:
            universe_task.cancel()

    async def _check_force_exit(self) -> None:
        now_dt = datetime.now(tz=KST)
        try:
            exit_time = datetime.strptime(self.force_exit_time, "%H:%M:%S").time()
            if now_dt.time() >= exit_time:
                if self._position:
                    self.log_warning(f"Force exit time reached ({self.force_exit_time}), closing position")
                    await self._handle_exit("force_exit")
        except Exception as e:
            self.log_warning(f"Force exit check failed: {e}")

    async def _handle_exit(self, reason: str) -> None:
        if not self._position:
            return

        pos = self._position
        self.log_info(f"Exiting position: {reason} {pos.symbol} qty={pos.qty}")

        if not self._live_confirm:
            self.log_warning("signals-only mode: exit order suppressed")
            return

        await self.place_sell_order(pos.symbol, pos.qty)
        await asyncio.sleep(3)

        pos_after = await self.get_positions()
        if not pos_after:
            self._position = None
            self._entry_price = 0.0
            self._peak_pnl = 0.0
            self.log_info(f"Position closed: {reason}")

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
        pos = await self.get_positions()
        if pos and not self._position:
            self._position = pos
            self._entry_price = pos.avg_price
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

        if self._position:
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

            # Place a live order (if enabled)
            cash = await self.get_cash_available(symbol, last_price)
            budget = cash * self.entry_budget_pct

            # 실주문 가격은 ask 기반(매수)으로 보수적으로 산정
            entry_px = float(book.ask) if book.ask > 0 else last_price
            qty = int(budget / entry_px)
            if qty < 1:
                return

            # one-entry-per-symbol-per-day safety pin: mark once we start live attempts
            self._entered_symbols.add(symbol)

            for attempt in range(self.entry_retry_limit):
                order = await self.place_buy_order(symbol, qty, entry_px)
                if order.order_id:
                    await asyncio.sleep(3)
                    pos = await self.get_positions()
                    if pos and pos.symbol == symbol:
                        self._position = pos
                        self._entry_price = entry_px
                        self._peak_pnl = 0.0
                        self.log_info(f"Order filled: {symbol} qty={pos.qty}")
                        return
                if order.order_id:
                    await self.rest.cancel_order(order.order_id, symbol, qty)

        except Exception as e:
            self.log_error(f"Strategy execution failed: {e}")
