"""US previous-day daily candle bootstrap via yfinance (personal-use best effort)."""

from __future__ import annotations

import asyncio
from typing import Optional

import yfinance as yf

from candle_analysis import Candle


def _fetch_prev_daily_sync(symbol: str) -> Optional[Candle]:
    # Grab a few daily bars to survive weekends/holidays.
    df = yf.download(
        tickers=symbol,
        period="10d",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        return None

    # yfinance returns multi-index for multiple tickers; we request one.
    try:
        last = df.iloc[-1]
    except Exception:
        return None

    try:
        o = float(last["Open"])
        h = float(last["High"])
        low = float(last["Low"])
        c = float(last["Close"])
        v = float(last.get("Volume", 0.0))
    except Exception:
        return None

    if c <= 0:
        return None
    return Candle(open=o, high=h, low=low, close=c, volume=v)


async def fetch_us_prev_daily(symbol: str, timeout_sec: int = 10) -> Optional[Candle]:
    """Fetch previous daily candle for a US ticker.

    Note: This uses network I/O under the hood. It is best-effort and should
    never crash the trading engine.
    """

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_prev_daily_sync, symbol),
            timeout=timeout_sec,
        )
    except Exception:
        return None
