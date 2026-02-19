"""Snapshot KR daily bars to local files (Naver Finance HTML best-effort).

추천값:
- 대표 심볼: 005930 (삼성전자)
- 보조: 069500 (KODEX 200)
- 저장: data/daily_kr/<symbol>_YYYYMMDD.json

주의:
- 네이버는 HTML 구조가 바뀔 수 있어 best-effort입니다.
- 실패 시 다음 스케줄에서 재시도합니다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


def _download_naver_daily(symbol: str, pages: int = 8) -> list[dict]:
    # Naver day price table: date, close, diff, open, high, low, volume
    rows: list[dict] = []
    seen = set()

    for page in range(1, pages + 1):
        url = f"https://finance.naver.com/item/sise_day.nhn?code={symbol}&page={page}"
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        except Exception:
            continue

        try:
            tables = pd.read_html(r.text)
        except Exception:
            continue
        if not tables:
            continue

        df = tables[0]
        df = df.dropna()
        if df.empty:
            continue

        # Normalize column names (Korean)
        # expected: ['날짜','종가','전일비','시가','고가','저가','거래량']
        for _, rec in df.iterrows():
            try:
                date = str(rec.get("날짜"))
                if not date or date == "nan":
                    continue
                if date in seen:
                    continue
                seen.add(date)

                close = float(rec.get("종가"))
                open_ = float(rec.get("시가"))
                high = float(rec.get("고가"))
                low = float(rec.get("저가"))
                vol = float(rec.get("거래량"))
                if close <= 0:
                    continue

                rows.append(
                    {
                        "date": date,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": vol,
                    }
                )
            except Exception:
                continue

    # sort ascending by date
    try:
        rows.sort(key=lambda x: x["date"])
    except Exception:
        pass
    return rows


def main() -> int:
    base_dir = Path(__file__).resolve().parents[1]
    data_dir = base_dir / "data" / "daily_kr"
    data_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y%m%d")
    # Recommended KR representatives: 069500 (KODEX 200), backup: 005930
    symbols = ["069500", "005930"]

    for sym in symbols:
        bars = _download_naver_daily(sym, pages=10)
        if not bars:
            print(f"no daily bars for {sym}")
            continue
        path = data_dir / f"{sym}_{today}.json"
        path.write_text(json.dumps(bars, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"saved {sym} bars={len(bars)} -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
