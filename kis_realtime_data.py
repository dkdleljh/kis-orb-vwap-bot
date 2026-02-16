#!/usr/bin/env python3
"""
KIS Real-Time Data Fetcher
실시간으로 분봉 데이터를 수집하고 저장합니다.
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List
from pathlib import Path

import aiohttp

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import Bar1m

from models import Bar1m


class KISRealtimeDataFetcher:
    """실시간 분봉 데이터 수집기"""
    
    def __init__(
        self,
        auth,
        base_url: str = "https://openapi.koreainvestment.com:9443",
        data_dir: str = "data/intraday",
    ):
        self.auth = auth
        self.base_url = base_url
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self._bars_cache: Dict[str, List[Bar1m]] = {}
        self._last_fetch: Dict[str, datetime] = {}
        self._is_collecting = False
    
    async def fetch_minute_bars(
        self,
        symbol: str,
        minute_type: str = "1",
        from_time: str = "",
    ) -> List[Bar1m]:
        """
        당일 분봉 데이터 조회
        minute_type: "1", "5", "10", "15", "30", "60"
        """
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_HOUR_GUBUN": minute_type,
            "FID_FROM_TIME": from_time,
        }
        
        headers = self.auth.auth_headers()
        headers["tr_id"] = "FHPTJ01000000"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        return []
                    
                    data = await resp.json()
                    
                    if data.get("rt_cd") != "0":
                        return []
                    
                    output = data.get("output", [])
                    if not output:
                        return []
                    
                    bars = self._parse_bars(output, symbol)
                    return bars
                    
        except Exception as e:
            print(f"fetch_minute_bars error: {e}")
            return []
    
    def _parse_bars(self, output: List[dict], symbol: str) -> List[Bar1m]:
        """API 응답을 Bar1m 리스트로 변환"""
        bars = []
        
        for item in output:
            try:
                time_str = item.get("stk_bsop_dt", "") + item.get("stk_mkt_tm", "")
                if len(time_str) >= 12:
                    dt = datetime.strptime(time_str[:12], "%Y%m%d%H%M%S")
                else:
                    dt = datetime.now()
                
                bar = Bar1m(
                    start=dt,
                    open=float(item.get("stk_o_prc", 0)),
                    high=float(item.get("stk_h_prc", 0)),
                    low=float(item.get("stk_l_prc", 0)),
                    close=float(item.get("stk_cl_prc", 0)),
                    volume=int(item.get("acml_vol", 0)),
                )
                
                if bar.close > 0:
                    bars.append(bar)
                    
            except Exception:
                continue
        
        return bars
    
    async def collect_realtime(
        self,
        symbol: str,
        interval_sec: int = 60,
        duration_min: int = 480,
    ) -> List[Bar1m]:
        """
        실시간으로 분봉 데이터 수집
        duration_min: 수집 기간 (분단위, 기본 480분 = 8시간)
        """
        self._is_collecting = True
        all_bars = []
        start_time = datetime.now()
        
        print(f"Starting real-time data collection for {symbol}...")
        
        while self._is_collecting:
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed >= duration_min * 60:
                break
            
            bars = await self.fetch_minute_bars(symbol, minute_type="1")
            
            for bar in bars:
                if bar not in all_bars:
                    all_bars.append(bar)
            
            self._bars_cache[symbol] = all_bars
            self._last_fetch[symbol] = datetime.now()
            
            await asyncio.sleep(interval_sec)
        
        print(f"Collected {len(all_bars)} bars for {symbol}")
        return all_bars
    
    def stop_collection(self):
        """데이터 수집 중지"""
        self._is_collecting = False
    
    def save_to_file(self, symbol: str, bars: List[Bar1m]) -> str:
        """데이터를 파일로 저장"""
        filepath = self.data_dir / f"{symbol}_{datetime.now().strftime('%Y%m%d')}.json"
        
        data = []
        for bar in bars:
            data.append({
                "start": bar.start.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            })
        
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        
        return str(filepath)
    
    def load_from_file(self, symbol: str, date: str = "") -> List[Bar1m]:
        if not date:
            date = datetime.now().strftime("%Y%m%d")
        
        filepath = self.data_dir / f"{symbol}_{date}.json"
        
        if not filepath.exists():
            return []
        
        with open(filepath, "r") as f:
            data = json.load(f)
        
        bars = []
        for item in data:
            bar = Bar1m(
                start=datetime.fromisoformat(item["start"]),
                open=item["open"],
                high=item["high"],
                low=item["low"],
                close=item["close"],
                volume=item["volume"],
            )
            bars.append(bar)
        
        return bars
    
    def get_cached_bars(self, symbol: str) -> List[Bar1m]:
        return self._bars_cache.get(symbol, [])


class HistoricalDataManager:
    """과거 데이터 관리자 - 여러 날짜의 데이터를 합성"""
    
    def __init__(self, data_dir: str = "data/intraday"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def get_available_dates(self, symbol: str) -> List[str]:
        """사용 가능한 날짜 목록"""
        pattern = f"{symbol}_*.json"
        files = self.data_dir.glob(pattern)
        
        dates = []
        for f in files:
            name = f.stem
            if "_" in name:
                date_part = name.split("_")[1]
                if len(date_part) == 8 and date_part.isdigit():
                    dates.append(date_part)
        
        return sorted(dates)
    
    def load_date_range(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> List[Bar1m]:
        """날짜 범위의 데이터 로드"""
        all_bars = []
        
        start_dt = datetime.strptime(start_date, "%Y%m%d")
        end_dt = datetime.strptime(end_date, "%Y%m%d")
        
        current = start_dt
        while current <= end_dt:
            date_str = current.strftime("%Y%m%d")
            bars = self._load_single_date(symbol, date_str)
            all_bars.extend(bars)
            current += timedelta(days=1)
        
        return all_bars
    
    def _load_single_date(self, symbol: str, date: str) -> List[Bar1m]:
        """단일 날짜 데이터 로드"""
        filepath = self.data_dir / f"{symbol}_{date}.json"
        
        if not filepath.exists():
            return []
        
        with open(filepath, "r") as f:
            data = json.load(f)
        
        bars = []
        for item in data:
            bar = Bar1m(
                start=datetime.fromisoformat(item["start"]),
                open=item["open"],
                high=item["high"],
                low=item["low"],
                close=item["close"],
                volume=item["volume"],
            )
            bars.append(bar)
        
        return bars


async def main():
    """테스트"""
    from kis_auth import KISAuth, load_auth_from_env
    
    app_key, app_secret, _ = load_auth_from_env()
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, None)
    
    await auth.fetch_token()
    
    fetcher = KISRealtimeDataFetcher(auth)
    
    print("Fetching current minute bars for 005930...")
    bars = await fetcher.fetch_minute_bars("005930", minute_type="1")
    
    print(f"Got {len(bars)} bars")
    for bar in bars[-5:]:
        print(f"  {bar.start}: O={bar.open:.0f} H={bar.high:.0f} L={bar.low:.0f} C={bar.close:.0f} V={bar.volume}")


if __name__ == "__main__":
    asyncio.run(main())
