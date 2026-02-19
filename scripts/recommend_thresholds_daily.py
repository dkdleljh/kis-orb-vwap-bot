"""Daily dynamic threshold recommender.

- 전일/당일 뉴스 키워드 기반 감성 점수를 간단 계산
- 시장/스타일별 추천 임계값을 산정하여 logs/dynamic_thresholds.json에 기록

주의:
- 외부 뉴스 소스는 네이버금융/야후 파이낸스 HTML 파싱 기반(best-effort)
- 실패 시에는 base threshold를 그대로 유지
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ensure project root on sys.path
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from dynamic_thresholds import DEFAULT_PATH, Thresholds, write_thresholds  # noqa: E402
from news import NewsSentimentAnalyzer  # noqa: E402
from strategy_profiles import get_base_threshold  # noqa: E402
from kis_realtime_data import HistoricalDataManager  # noqa: E402
from indicators import atr, ema  # noqa: E402


def _cnn_fear_greed() -> dict:
    """Fetch CNN Fear & Greed index (best-effort).

    Returns dict keys (when ok):
      - fg_score (0..100 float)
      - fg_rating (str)
      - fg_ts_utc (str)
      - fg_sentiment (float, -100..0; extremes -> more negative)

    Notes:
    - CNN blocks naive bots (HTTP 418). We use browser-like headers.
    - If fetch fails, returns empty dict.
    """

    import requests

    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://edition.cnn.com/markets/fear-and-greed",
        "Origin": "https://edition.cnn.com",
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            return {}
        j = r.json()
        fg = (j or {}).get("fear_and_greed") or {}
        score = float(fg.get("score"))
        rating = str(fg.get("rating") or "")
        ts = str(fg.get("timestamp") or "")

        # Convert 0..100 -> -100..0 (risk penalty): neutral(50)->0, extremes->-100.
        fg_sentiment = -abs(score - 50.0) * 2.0
        fg_sentiment = max(-100.0, min(0.0, fg_sentiment))

        out = {
            "fg_score": score,
            "fg_rating": rating,
            "fg_ts_utc": ts,
            "fg_sentiment": float(fg_sentiment),
        }

        # VIX (and its 50d MA) are included in the same CNN graphdata payload.
        try:
            vix = (j or {}).get("market_volatility_vix") or {}
            vix50 = (j or {}).get("market_volatility_vix_50") or {}
            vix_score = float(vix.get("score"))
            vix_rating = str(vix.get("rating") or "")

            # last values from series
            vix_last = None
            vix_ma50_last = None
            if isinstance(vix.get("data"), list) and vix["data"]:
                vix_last = float(vix["data"][-1].get("y") or 0)
            if isinstance(vix50.get("data"), list) and vix50["data"]:
                vix_ma50_last = float(vix50["data"][-1].get("y") or 0)

            vix_gap = None
            if vix_last is not None and vix_ma50_last is not None and vix_last > 0 and vix_ma50_last > 0:
                vix_gap = float(vix_last - vix_ma50_last)

            # Convert VIX regime to -100..0 penalty.
            # - If VIX is above its 50d MA, treat as risk-off (more negative).
            # - Absolute high VIX also increases penalty.
            penalty = 0.0
            if vix_gap is not None and vix_gap > 0:
                penalty = min(100.0, vix_gap * 10.0)  # +5 gap => 50 penalty
            if vix_last is not None and vix_last > 20:
                penalty = max(penalty, min(100.0, (vix_last - 20.0) * 5.0))

            out.update(
                {
                    "vix_last": vix_last,
                    "vix_ma50": vix_ma50_last,
                    "vix_gap": vix_gap,
                    "vix_score": vix_score,
                    "vix_rating": vix_rating,
                    "vix_sentiment": -float(penalty),
                }
            )
        except Exception:
            pass

        return out
    except Exception:
        return {}


def _load_dotenv_like(path: str) -> None:
    try:
        if not os.path.exists(path):
            return
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
    except Exception:
        return


async def _avg_kr(an: NewsSentimentAnalyzer, symbols: list[str]) -> float:
    scores = []
    for sym in symbols[:20]:
        try:
            r = await an.get_sentiment_score(sym)
            s = float(r.get("score") or 0)
            scores.append(s)
        except Exception:
            continue
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


async def _avg_us(an: NewsSentimentAnalyzer, symbols: list[str]) -> float:
    scores = []
    for sym in symbols[:10]:
        try:
            r = await an.get_us_sentiment_score(sym)
            s = float(r.get("score") or 0)
            scores.append(s)
        except Exception:
            continue
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def _kr_chart_metrics(base_dir: Path, symbol: str) -> dict:
    """Compute simple chart regime metrics.

    Priority:
    1) data/intraday/<symbol>_YYYYMMDD.json (KIS intraday snapshot)
    2) data/daily_kr/<symbol>_YYYYMMDD.json (Naver daily snapshot)
    """

    # 1) intraday
    mgr = HistoricalDataManager(str(base_dir / "data" / "intraday"))
    dates = mgr.get_available_dates(symbol)
    if dates:
        last = dates[-1]
        bars = mgr.load_date_range(symbol, last, last)
        if len(bars) >= 60:
            closes = [float(b.close) for b in bars if float(b.close) > 0]
            highs = [float(b.high) for b in bars if float(b.high) > 0]
            lows = [float(b.low) for b in bars if float(b.low) > 0]
            if len(closes) >= 60:
                a = atr(highs[-120:], lows[-120:], closes[-120:], 14)
                px = closes[-1]
                atr_pct = (a / px * 100.0) if px > 0 and a > 0 else None
                e9 = ema(closes[-200:], 9)
                e21 = ema(closes[-200:], 21)
                trend = "UP" if e9 > e21 else "DOWN" if e9 < e21 else "FLAT"
                return {"kr_atr_pct": atr_pct, "kr_trend": trend, "kr_chart_date": last, "kr_chart_tf": "intraday"}

    # 2) daily_kr fallback
    daily_dir = base_dir / "data" / "daily_kr"
    files = sorted(daily_dir.glob(f"{symbol}_*.json")) if daily_dir.exists() else []
    if not files:
        return {"kr_atr_pct": None, "kr_trend": None, "kr_chart_date": None, "kr_chart_tf": None}

    path = files[-1]
    try:
        daily = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kr_atr_pct": None, "kr_trend": None, "kr_chart_date": None, "kr_chart_tf": None}

    if not isinstance(daily, list) or len(daily) < 30:
        return {"kr_atr_pct": None, "kr_trend": None, "kr_chart_date": path.stem.split("_")[-1], "kr_chart_tf": "daily"}

    closes = [float(b.get("close") or 0) for b in daily if float(b.get("close") or 0) > 0]
    highs = [float(b.get("high") or 0) for b in daily if float(b.get("high") or 0) > 0]
    lows = [float(b.get("low") or 0) for b in daily if float(b.get("low") or 0) > 0]
    if len(closes) < 30:
        return {"kr_atr_pct": None, "kr_trend": None, "kr_chart_date": path.stem.split("_")[-1], "kr_chart_tf": "daily"}

    a = atr(highs[-60:], lows[-60:], closes[-60:], 14)
    px = closes[-1]
    atr_pct = (a / px * 100.0) if px > 0 and a > 0 else None
    e9 = ema(closes[-120:], 9)
    e21 = ema(closes[-120:], 21)
    trend = "UP" if e9 > e21 else "DOWN" if e9 < e21 else "FLAT"

    return {"kr_atr_pct": atr_pct, "kr_trend": trend, "kr_chart_date": path.stem.split("_")[-1], "kr_chart_tf": "daily"}


def _us_chart_metrics(base_dir: Path, symbol: str = "VOO") -> dict:
    """Compute simple US daily regime metrics from cached yfinance snapshot."""
    data_dir = base_dir / "data" / "daily_us"
    if not data_dir.exists():
        return {"us_atr_pct": None, "us_trend": None, "us_chart_date": None, "us_rep": symbol}

    files = sorted(data_dir.glob(f"{symbol}_*.json"))
    if not files:
        return {"us_atr_pct": None, "us_trend": None, "us_chart_date": None, "us_rep": symbol}

    path = files[-1]
    try:
        bars = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"us_atr_pct": None, "us_trend": None, "us_chart_date": None, "us_rep": symbol}

    if not isinstance(bars, list) or len(bars) < 30:
        return {"us_atr_pct": None, "us_trend": None, "us_chart_date": path.stem.split("_")[-1], "us_rep": symbol}

    closes = [float(b.get("close") or 0) for b in bars if float(b.get("close") or 0) > 0]
    highs = [float(b.get("high") or 0) for b in bars if float(b.get("high") or 0) > 0]
    lows = [float(b.get("low") or 0) for b in bars if float(b.get("low") or 0) > 0]
    if len(closes) < 30:
        return {"us_atr_pct": None, "us_trend": None, "us_chart_date": path.stem.split("_")[-1], "us_rep": symbol}

    a = atr(highs[-60:], lows[-60:], closes[-60:], 14)
    px = closes[-1]
    atr_pct = (a / px * 100.0) if px > 0 and a > 0 else None

    e9 = ema(closes[-120:], 9)
    e21 = ema(closes[-120:], 21)
    trend = "UP" if e9 > e21 else "DOWN" if e9 < e21 else "FLAT"

    return {"us_atr_pct": atr_pct, "us_trend": trend, "us_chart_date": path.stem.split("_")[-1], "us_rep": symbol}


async def main() -> int:
    base_dir = BASE_DIR
    _load_dotenv_like(str(base_dir / ".env"))

    cfg = json.loads((base_dir / "config.json").read_text(encoding="utf-8"))
    modules = cfg.get("modules", {}) or {}
    kuk = modules.get("kukjang", {}) or {}
    # KR symbols: always_include + lever/inverse
    dyn = (kuk.get("dynamic_universe", {}) or {})
    kr_syms = list(dyn.get("always_include", []) or [])

    # If empty, fall back to some well-known KR symbols.
    if not kr_syms:
        kr_syms = ["005930", "000660", "035420", "035720", "051910", "068270"]

    us = modules.get("us_swing", {}) or {}
    us_syms = list(us.get("symbols", ["AAPL", "MSFT", "NVDA", "TSLA"]) or [])

    an = NewsSentimentAnalyzer(max_news_age_hours=24)

    kr_avg = await _avg_kr(an, kr_syms)
    us_avg = await _avg_us(an, us_syms)

    # CNN Fear & Greed + VIX regime -> blend into US sentiment.
    fg = await asyncio.to_thread(_cnn_fear_greed)
    if fg:
        if "fg_sentiment" in fg:
            w = float(os.environ.get("FG_BLEND_WEIGHT", "0.25") or 0.25)
            w = max(0.0, min(0.8, w))
            us_avg = (1.0 - w) * float(us_avg) + w * float(fg.get("fg_sentiment") or 0.0)
        if "vix_sentiment" in fg:
            wv = float(os.environ.get("VIX_BLEND_WEIGHT", "0.20") or 0.20)
            wv = max(0.0, min(0.8, wv))
            us_avg = (1.0 - wv) * float(us_avg) + wv * float(fg.get("vix_sentiment") or 0.0)

    # IMPORTANT: use hardcoded baselines here to avoid compounding daily adjustments.
    base = Thresholds(
        kr_scalp=get_base_threshold("KR", "SCALP"),
        kr_swing=get_base_threshold("KR", "SWING"),
        us_scalp=get_base_threshold("US", "SCALP"),
        us_swing=get_base_threshold("US", "SWING"),
    )

    # Chart regime (recommended representatives)
    kr_rep = "069500"
    kr_chart = _kr_chart_metrics(base_dir, kr_rep)
    us_chart = _us_chart_metrics(base_dir, "VOO")

    # Small extra adjustment from ATR% (high volatility -> stricter)
    kr_atr_pct = kr_chart.get("kr_atr_pct")
    if kr_atr_pct is not None:
        if kr_atr_pct >= 2.0:
            kr_avg = kr_avg - 5.0
        elif kr_atr_pct <= 1.0 and kr_chart.get("kr_trend") == "UP":
            kr_avg = kr_avg + 3.0

    us_atr_pct = us_chart.get("us_atr_pct")
    if us_atr_pct is not None:
        if us_atr_pct >= 2.5:
            us_avg = us_avg - 5.0
        elif us_atr_pct <= 1.4 and us_chart.get("us_trend") == "UP":
            us_avg = us_avg + 3.0

    extras = {**kr_chart, **us_chart, **(fg or {})}

    out = write_thresholds(
        path=str(base_dir / DEFAULT_PATH),
        base=base,
        kr_sentiment_avg=kr_avg,
        us_sentiment_avg=us_avg,
        extras=extras,
    )

    print(
        json.dumps(
            {
                "ts_kst": datetime.now().astimezone().isoformat(),
                "kr_sentiment_avg": kr_avg,
                "us_sentiment_avg": us_avg,
                "thresholds": {
                    "kr_scalp": out.kr_scalp,
                    "kr_swing": out.kr_swing,
                    "us_scalp": out.us_scalp,
                    "us_swing": out.us_swing,
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
