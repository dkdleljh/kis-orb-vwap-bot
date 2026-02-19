"""Snapshot KR intraday bars to local files.

추천값:
- 대표 심볼: 122630 (레버)
- 저장: data/intraday/<symbol>_YYYYMMDD.json

이 파일은 '내일 임계값 산정'에 사용할 차트 레짐(ATR%, 추세)을 위해 저장합니다.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# ensure project root on sys.path
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from logger import setup_logger  # noqa: E402
from kis_auth import KISAuth, load_auth_from_env  # noqa: E402
from kis_realtime_data import KISRealtimeDataFetcher  # noqa: E402


def _load_dotenv_like(path: str) -> None:
    try:
        if not os.path.exists(path):
            return
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        return


async def main() -> int:
    base_dir = BASE_DIR
    _load_dotenv_like(str(base_dir / ".env"))

    app_key, app_secret, _ = load_auth_from_env()
    logger = setup_logger(str(base_dir / "logs" / "intraday_snapshot"), "Asia/Seoul")
    auth = KISAuth("https://openapi.koreainvestment.com:9443", app_key, app_secret, logger)
    await auth.fetch_token()

    fetcher = KISRealtimeDataFetcher(auth, data_dir=str(base_dir / "data" / "intraday"))

    # Recommended KR representative for chart regime: 005930 (삼성전자)
    symbols = [os.environ.get("KR_REGIME_SYMBOL", "005930").strip() or "005930"]

    for sym in symbols:
        bars = await fetcher.fetch_minute_bars(sym, minute_type="1")
        if bars:
            path = fetcher.save_to_file(sym, bars)
            print(f"saved {sym} bars={len(bars)} -> {path}")
        else:
            print(f"no bars for {sym} (market closed or api empty)")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
