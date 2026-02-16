"""
Mijang Module - US stock trading module.

海外株式(NASDAQ/NYSE) trading moduleです。
当日売買原則(force_exit)とニュース分析をサポートします。
"""

import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, date, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import holidays

from modules.base import BaseTradingModule, ModuleContext
from models import Position, OrderResult
from kis_rest_overseas import KISOverseasRestOrders
from fee_calculator import USFeeCalculator
from perfect_strategy import Perfect100Strategy
from news import NewsSentimentAnalyzer


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
    if is_us_dst_active():
        return dt_time(22, 30, 0), dt_time(5, 0, 0)
    else:
        return dt_time(23, 30, 0), dt_time(6, 0, 0)


def is_us_market_open() -> bool:
    now = datetime.now()
    check_date = now.date()
    weekday = now.weekday()

    if weekday >= 5:
        return False

    if is_us_market_holiday(check_date):
        return False

    hour = now.hour
    minute = now.minute
    current_time = hour * 60 + minute

    market_open, market_close = _get_market_hours_korea()
    market_open_minutes = market_open.hour * 60 + market_open.minute
    market_close_minutes = market_close.hour * 60 + market_close.minute

    if market_open_minutes > market_close_minutes:
        is_market_time = (
            current_time >= market_open_minutes or current_time < market_close_minutes
        )
    else:
        is_market_time = market_open_minutes <= current_time < market_close_minutes

    return is_market_time


def get_us_market_status() -> Dict[str, Any]:
    now = datetime.now()
    weekday = now.weekday()
    check_date = now.date()

    is_weekend = weekday >= 5
    is_holiday = is_us_market_holiday(check_date)

    hour = now.hour
    minute = now.minute
    current_time = hour * 60 + minute

    market_open, market_close = _get_market_hours_korea()
    market_open_minutes = market_open.hour * 60 + market_open.minute
    market_close_minutes = market_close.hour * 60 + market_close.minute

    if market_open_minutes > market_close_minutes:
        is_market_hours = (
            current_time >= market_open_minutes or current_time < market_close_minutes
        )
        is_pre_market = (
            not is_weekend
            and not is_holiday
            and market_close_minutes <= current_time < market_open_minutes
        )
        is_after_hours = False
    else:
        is_market_hours = market_open_minutes <= current_time < market_close_minutes
        is_pre_market = (
            not is_weekend and not is_holiday and current_time < market_open_minutes
        )
        is_after_hours = (
            not is_weekend and not is_holiday and current_time >= market_close_minutes
        )

    tz_info = get_us_timezone_info()

    return {
        "is_open": not is_weekend and not is_holiday and is_market_hours,
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
    if is_us_dst_active():
        return dt_time(5, 0, 0)
    else:
        return dt_time(4, 0, 0)


class MijangModule(BaseTradingModule):
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

        self.news_analyzer = NewsSentimentAnalyzer()
        self.news_score_by_symbol: Dict[str, int] = {}
        self.news_score_updated_at: Dict[str, float] = {}

        self.fee_calculator = USFeeCalculator(
            commission_per_share=0.005,
            slippage_rate=0.001,
        )

        self.perfect_strategy = Perfect100Strategy(
            logger=self.logger,
            min_score=50,
            max_spread_pct=0.003,
            min_bid_ask_ratio=0.8,
            min_win_rate=0.55,
            min_r_ratio=1.5,
        )

    def set_scanner(self, scanner) -> None:
        self._scanner = scanner

    async def _on_initialize(self) -> None:
        self.log_info(
            f"Initialized with exchange: {self.exchange}, force_exit: {self.force_exit_time}"
        )

        if self.dynamic_enabled:
            await self._refresh_universe()
        else:
            self._current_universe = list(self.symbols) if self.symbols else []

        await self._restore_position()

    async def _refresh_universe(self) -> None:
        if not self._scanner:
            self.log_warning("US Scanner not set, using default universe")
            self._current_universe = self.dynamic_config.get("exchanges", ["NASD"])
            return

        try:
            top_n = self.dynamic_config.get("top_n", 50)
            exchanges = self.dynamic_config.get("exchanges", ["NASD", "NYSE"])
            min_price = self.dynamic_config.get("min_price_usd", 5.0)

            scanned = await self._scanner.scan(
                top_n=top_n,
                exchanges=exchanges,
                min_price=min_price,
            )

            if scanned:
                self._current_universe = scanned
                self._last_good_universe = scanned
                self._last_scan_time = datetime.now()
                self.log_info(f"US Universe refreshed: {len(scanned)} stocks")
            else:
                self.log_warning("US Scanner returned empty, using last good universe")
                self._current_universe = (
                    self._last_good_universe
                    or self.dynamic_config.get("exchanges", ["NASD"])
                )
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

    async def get_cash_available(self, symbol: str, price: float = 0.0) -> float:
        try:
            cash = await self.rest.get_cash_available()
            return float(cash) if cash else 0.0
        except Exception as e:
            self.log_warning(f"Failed to get cash: {e}")
            return 0.0

    async def place_buy_order(self, symbol: str, qty: int, price: float) -> OrderResult:
        return await self.rest.place_buy_order(symbol, qty, price)

    async def place_sell_order(
        self, symbol: str, qty: int, price: Optional[float] = None
    ) -> OrderResult:
        if price:
            return await self.rest.place_sell_order(symbol, qty, price)
        else:
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
        return await self.rest.get_quote(symbol)

    async def monitor_loop(self) -> None:
        self.log_info("Starting US stock monitoring loop...")

        universe_task = None
        if self.dynamic_enabled:
            universe_task = asyncio.create_task(self._universe_refresh_loop())

        candidate_check_counter = 0

        while self._running:
            try:
                if not is_us_market_open():
                    status = get_us_market_status()
                    if status.get("is_weekend"):
                        self.log_info("US market is closed (weekend)")
                    elif status.get("is_holiday"):
                        self.log_info(
                            f"US market is closed (holiday): {status.get('holiday_name')}"
                        )
                    elif status.get("is_pre_market"):
                        self.log_info("US market is in pre-market hours")
                    elif status.get("is_after_hours"):
                        self.log_info("US market is in after-hours")
                    else:
                        self.log_info("US market is closed")
                    await asyncio.sleep(30)
                    continue

                await self._check_positions()

                await self._check_force_exit()

                if not self._position and self._current_universe:
                    candidate_check_counter += 1
                    if candidate_check_counter >= 12:
                        candidate_check_counter = 0
                        await self._check_next_candidate()

                await asyncio.sleep(5)
            except Exception as e:
                self.log_error(f"Monitor loop error: {e}")
                await asyncio.sleep(10)

        if universe_task:
            universe_task.cancel()

    async def _check_force_exit(self) -> None:
        now_dt = datetime.now()

        try:
            exit_time = datetime.strptime(self.force_exit_time, "%H:%M:%S").time()
            current_time = now_dt.time()

            if current_time >= exit_time:
                if self._position:
                    self.log_warning(
                        f"Force exit time reached ({self.force_exit_time}), closing position"
                    )
                    await self._handle_exit("force_exit")
        except Exception as e:
            self.log_warning(f"Force exit check failed: {e}")

    async def _handle_exit(self, reason: str) -> None:
        if not self._position:
            return

        pos = self._position
        self.log_info(f"Exiting position: {reason} {pos.symbol} qty={pos.qty}")

        await self.place_sell_order(pos.symbol, pos.qty)

        await asyncio.sleep(3)

        pos_after = await self.get_positions()
        if not pos_after:
            self._position = None
            self._entry_price = 0.0
            self._peak_pnl = 0.0
            self.log_info(f"Position closed: {reason}")

    async def _check_next_candidate(self) -> None:
        if not self._current_universe:
            return

        symbol = self._current_universe[self._candidate_index]
        self._candidate_index = (self._candidate_index + 1) % len(
            self._current_universe
        )

        await self.execute_strategy(symbol)

    async def _check_positions(self) -> None:
        pos = await self.get_positions()

        if pos and not self._position:
            self._position = pos
            self._entry_price = pos.avg_price
            self.log_info(
                f"Position detected: {pos.symbol} qty={pos.qty} @ {pos.avg_price}"
            )

    async def execute_strategy(self, symbol: str) -> None:
        if self._position:
            return

        try:
            quote = await self.get_quote(symbol)
            current_price = quote.get("last_price") or quote.get("price")

            if not current_price:
                return

            cash = await self.get_cash_available(symbol, current_price)
            budget = cash * self.entry_budget_pct
            qty = int(budget / current_price)

            if qty < 1:
                return

            for attempt in range(self.entry_retry_limit):
                order = await self.place_buy_order(symbol, qty, current_price)

                if order.order_id:
                    await asyncio.sleep(3)
                    pos = await self.get_positions()
                    if pos and pos.symbol == symbol:
                        self._position = pos
                        self._entry_price = current_price
                        self._peak_pnl = 0.0
                        self.log_info(f"Order filled: {symbol} qty={pos.qty}")
                        return

                if order.order_id:
                    await self.rest.cancel_order(order.order_id, symbol, qty)

            self.log_info("Entry failed after retries")
        except Exception as e:
            self.log_error(f"Strategy execution failed: {e}")
