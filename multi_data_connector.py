#!/usr/bin/env python3
"""
Multi-Source Data Connector
다중 데이터 소스 연동 - KIS, Yahoo Finance, Alpha Vantage
"""

import aiohttp
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass


@dataclass
class MarketData:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str


class MultiSourceDataConnector:
    def __init__(self, logger=None):
        self.logger = logger
        self._cache: Dict[str, List[MarketData]] = {}
        self._cache_ttl = 300
    
    async def get_historical_data(
        self,
        symbol: str,
        days: int = 30,
        source: str = "auto",
    ) -> List[MarketData]:
        if source == "auto":
            sources = ["yahoo", "kis"]
        else:
            sources = [source]
        
        for src in sources:
            try:
                if src == "yahoo":
                    data = await self._fetch_yahoo(symbol, days)
                    if data:
                        return data
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"{src} failed for {symbol}: {e}")
        
        return []
    
    async def _fetch_yahoo(self, symbol: str, days: int) -> List[MarketData]:
        
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            "period1": int((datetime.now() - timedelta(days=days)).timestamp()),
            "period2": int(datetime.now().timestamp()),
            "interval": "1d",
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return []
                
                data = await resp.json()
                if "chart" not in data or "result" not in data["chart"]:
                    return []
                
                result = data["chart"]["result"][0]
                if not result:
                    return []
                
                timestamps = result.get("timestamp", [])
                indicators = result.get("indicators", {})
                quote = indicators.get("quote", [{}])[0]
                
                results = []
                for i, ts in enumerate(timestamps):
                    results.append(MarketData(
                        symbol=symbol,
                        timestamp=datetime.fromtimestamp(ts),
                        open=quote["open"][i],
                        high=quote["high"][i],
                        low=quote["low"][i],
                        close=quote["close"][i],
                        volume=quote["volume"][i],
                        source="yahoo",
                    ))
                
                return results
    
    async def get_realtime_quote(self, symbol: str) -> Optional[Dict]:
        sources = [
            self._fetch_yahoo_quote,
        ]
        
        for fetch_func in sources:
            try:
                result = await fetch_func(symbol)
                if result:
                    return result
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Quote fetch failed: {e}")
        
        return None
    
    async def _fetch_yahoo_quote(self, symbol: str) -> Optional[Dict]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status != 200:
                    return None
                
                data = await resp.json()
                result = data.get("chart", {}).get("result", [])
                if not result:
                    return None
                
                meta = result[0].get("meta", {})
                price = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [0])[-1]
                
                return {
                    "symbol": symbol,
                    "price": price or meta.get("regularMarketPrice", 0),
                    "change": meta.get("regularMarketChange", 0),
                    "change_pct": meta.get("regularMarketChangePercent", 0),
                    "volume": meta.get("regularMarketVolume", 0),
                    "timestamp": datetime.now(),
                }


class DataAggregator:
    def __init__(self, logger=None):
        self.logger = logger
        self.connector = MultiSourceDataConnector(logger)
    
    async def get_best_price(self, symbol: str) -> Optional[Dict]:
        quote = await self.connector.get_realtime_quote(symbol)
        return quote
    
    async def validate_signal(self, symbol: str, signal_data: Dict) -> bool:
        quote = await self.get_best_price(symbol)
        if not quote:
            return False
        
        price = quote.get("price", 0)
        if price <= 0:
            return False
        
        volume = quote.get("volume", 0)
        if volume < 10000:
            return False
        
        return True
