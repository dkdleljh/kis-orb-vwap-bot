"""
Hwanjeon Module - 환전 시세 조회 모듈.

KIS API에는 실제 환전 거래 API가 없으므로 시세 조회만 지원합니다.
"""

import asyncio
from typing import Optional, Dict, Any, List
import aiohttp

from modules.base import BaseTradingModule, ModuleContext
from models import Position, OrderResult


CURRENCY_SYMBOLS = {
    "USD": "USD/KRW",
    "EUR": "EUR/KRW",
    "JPY": "JPY/KRW",
    "CNY": "CNY/KRW",
    "HKD": "HKD/KRW",
}


class HwanjeonModule(BaseTradingModule):
    """
    환전 시세 조회 모듈.

    실제 거래는 지원하지 않으며, 환율 모니터링만 수행합니다.
    """

    def __init__(
        self,
        context: ModuleContext,
        logger,
        auth,
        base_url: str,
        config: Dict[str, Any],
    ):
        super().__init__(context, logger)
        self.auth = auth
        self.base_url = base_url
        self.config = config

        self.currencies = context.config.get("currencies", ["USD"])
        self.poll_interval = context.config.get("poll_interval", 60)
        self.alerts = context.config.get("alerts", {})

        self._latest_rates: Dict[str, Dict[str, Any]] = {}
        self._rate_history: Dict[str, List[float]] = {}

    async def _on_initialize(self) -> None:
        self.log_info(f"Initializing with currencies: {self.currencies}")
        for curr in self.currencies:
            self._rate_history[curr] = []

    async def get_cash_available(self, symbol: str, price: float = 0.0) -> float:
        return 0.0

    async def place_buy_order(self, symbol: str, qty: int, price: float) -> OrderResult:
        return OrderResult(
            order_id="",
            filled_qty=0,
            status="unsupported: FX trading not available via KIS API",
        )

    async def place_sell_order(
        self, symbol: str, qty: int, price: Optional[float] = None
    ) -> OrderResult:
        return OrderResult(
            order_id="",
            filled_qty=0,
            status="unsupported: FX trading not available via KIS API",
        )

    async def get_positions(self) -> Optional[Position]:
        return None

    async def get_quote(self, symbol: str) -> dict:
        result = await self._fetch_exchange_rate(symbol)
        return result if result is not None else {}

    async def monitor_loop(self) -> None:
        self.log_info("Starting FX monitoring loop...")

        while self._running:
            try:
                for curr in self.currencies:
                    rate_data = await self._fetch_exchange_rate(curr)
                    if rate_data:
                        self._update_rate(curr, rate_data)
                        await self._check_alerts(curr, rate_data)

                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                self.log_error(f"Monitor loop error: {e}")
                await asyncio.sleep(10)

    async def _fetch_exchange_rate(self, currency: str) -> Optional[Dict[str, Any]]:
        try:
            url = (
                f"{self.base_url}/uapi/overseas-stock/v1/quotations/inquire-daily-chart"
            )

            params = {
                "EXCD": "货币CODE",  # 통화 코드
                "SYMB": f"{currency}/KRW",
                "GUBUN": "D",
                "MOD": "1",
            }

            headers = self.auth.auth_headers()
            headers["tr_id"] = "HHHPRE_UND_NW:0"

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, headers=headers, timeout=10
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return self._parse_rate_response(currency, data)

            return await self._fetch_exchange_rate_fallback(currency)
        except Exception as e:
            self.log_warning(f"Rate fetch failed for {currency}: {e}")
            return await self._fetch_exchange_rate_fallback(currency)

    async def _fetch_exchange_rate_fallback(
        self, currency: str
    ) -> Optional[Dict[str, Any]]:
        try:
            url = "https://api.exchangerate.host/latest"
            params = {
                "base": "KRW",
                "symbols": currency,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = data.get("rates", {}).get(currency)
                        if rate:
                            return {
                                "currency": currency,
                                "rate": rate,
                                "timestamp": data.get("date"),
                            }
        except Exception as e:
            self.log_warning(f"Fallback rate fetch failed: {e}")

        return None

    def _parse_rate_response(
        self, currency: str, data: dict
    ) -> Optional[Dict[str, Any]]:
        try:
            output = data.get("output", [])
            if output and len(output) > 0:
                latest = output[0]
                return {
                    "currency": currency,
                    "rate": float(latest.get("clos", 0)),
                    "open": float(latest.get("open", 0)),
                    "high": float(latest.get("high", 0)),
                    "low": float(latest.get("low", 0)),
                    "volume": latest.get("vol", 0),
                }
        except Exception as e:
            self.log_warning(f"Parse error for {currency}: {e}")

        return None

    def _update_rate(self, currency: str, rate_data: Dict[str, Any]) -> None:
        self._latest_rates[currency] = rate_data

        rate = rate_data.get("rate", 0)
        if rate > 0:
            self._rate_history[currency].append(rate)
            if len(self._rate_history[currency]) > 100:
                self._rate_history[currency] = self._rate_history[currency][-100:]

            self.log_info(
                f"[{currency}] Rate: {rate:.2f} (High: {rate_data.get('high', 0):.2f}, Low: {rate_data.get('low', 0):.2f})"
            )

    async def _check_alerts(self, currency: str, rate_data: Dict[str, Any]) -> None:
        if not self.alerts:
            return

        rate = rate_data.get("rate", 0)
        history = self._rate_history.get(currency, [])

        if len(history) < 2:
            return

        prev_rate = history[-2]
        change_pct = (rate - prev_rate) / prev_rate * 100 if prev_rate > 0 else 0

        alert_above = self.alerts.get(f"{currency}_above")
        if alert_above and rate >= alert_above:
            self.log_warning(
                f"ALERT: {currency} reached {rate:.2f} (above {alert_above})"
            )

        alert_below = self.alerts.get(f"{currency}_below")
        if alert_below and rate <= alert_below:
            self.log_warning(
                f"ALERT: {currency} reached {rate:.2f} (below {alert_below})"
            )

        alert_change = self.alerts.get(f"{currency}_change")
        if alert_change and abs(change_pct) >= alert_change:
            self.log_warning(
                f"ALERT: {currency} changed {change_pct:.2f}% from previous"
            )

    def get_latest_rate(self, currency: str) -> Optional[float]:
        data = self._latest_rates.get(currency)
        if data:
            return data.get("rate")
        return None

    def get_rate_history(self, currency: str) -> List[float]:
        return self._rate_history.get(currency, [])

    def get_all_rates(self) -> Dict[str, Dict[str, Any]]:
        return self._latest_rates.copy()
