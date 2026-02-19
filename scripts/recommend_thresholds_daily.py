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
from statistics import pstdev
from datetime import datetime
from pathlib import Path
from typing import Any

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


def _news_stats(scores: list[float], attempts: int) -> dict:
    if not scores:
        return {"avg": None, "count": 0, "std": None, "coverage": 0.0, "success_rate": 0.0}
    return {
        "avg": float(sum(scores) / len(scores)),
        "count": int(len(scores)),
        "std": float(pstdev(scores)) if len(scores) >= 2 else 0.0,
        "coverage": float(len(scores) / max(1, attempts)),
        "success_rate": float(len(scores) / max(1, attempts)),
    }


async def _news_kr(an: NewsSentimentAnalyzer, symbols: list[str]) -> dict:
    targets = symbols[:20]
    scores = []
    for sym in targets:
        try:
            r = await an.get_sentiment_score(sym)
            s = float(r.get("score") or 0)
            scores.append(s)
        except Exception:
            continue
    return _news_stats(scores, len(targets))


async def _news_us(an: NewsSentimentAnalyzer, symbols: list[str]) -> dict:
    targets = symbols[:10]
    scores = []
    for sym in targets:
        try:
            r = await an.get_us_sentiment_score(sym)
            s = float(r.get("score") or 0)
            scores.append(s)
        except Exception:
            continue
    return _news_stats(scores, len(targets))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _news_stats_from_cache(base_dir: Path, market: str, ymd: str, symbols: list[str], cap: int) -> dict | None:
    path = base_dir / "data" / f"news_cache_{market.lower()}" / f"{ymd}.json"
    cached = _read_json(path)
    if not cached:
        return None
    scores = cached.get("scores")
    if not isinstance(scores, dict):
        return None

    targets = symbols[: max(1, cap)]
    vals: list[float] = []
    for sym in targets:
        try:
            if sym in scores:
                vals.append(float(scores.get(sym)))
        except Exception:
            continue

    if not vals:
        for v in scores.values():
            try:
                vals.append(float(v))
            except Exception:
                continue

    attempts = len(targets) if targets else len(vals)
    return _news_stats(vals, attempts)


def _normalize_premarket_trend(value: Any) -> str | None:
    trend = str(value or "").strip().upper()
    return {"UP": "UP", "DOWN": "DOWN", "FLAT": "FLAT"}.get(trend)


def _select_premarket_metric(
    *,
    base_dir: Path,
    market: str,
    ymd: str,
    rep_symbol: str,
) -> dict[str, Any] | None:
    path = base_dir / "data" / f"premarket_metrics_{market.lower()}" / f"{ymd}.json"
    payload = _read_json(path)
    if not payload:
        return None
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return None
    rep = str(rep_symbol or "").strip().upper()
    if rep and isinstance(metrics.get(rep), dict):
        return metrics[rep]
    for v in metrics.values():
        if isinstance(v, dict):
            return v
    return None


def _apply_cached_overrides(
    *,
    base_dir: Path,
    market: str,
    ymd: str,
    inputs: dict[str, Any],
    symbols: list[str],
    rep_symbol: str,
    news_cap: int,
) -> dict[str, Any]:
    out = dict(inputs)
    out["prev_day"] = dict(out.get("prev_day") or {})
    out["news"] = dict(out.get("news") or {})
    out["chart"] = dict(out.get("chart") or {})
    out["atr"] = dict(out.get("atr") or {})

    cached_news = _news_stats_from_cache(base_dir, market, ymd, symbols, news_cap)
    if cached_news:
        out["news"] = {
            "sentiment_avg": cached_news.get("avg"),
            "sample_count": cached_news.get("count", 0),
            "coverage": cached_news.get("coverage", 0.0),
            "success_rate": cached_news.get("success_rate", 0.0),
            "std": cached_news.get("std"),
        }

    pm = _select_premarket_metric(
        base_dir=base_dir,
        market=market,
        ymd=ymd,
        rep_symbol=rep_symbol,
    )
    if not pm:
        return out

    try:
        sample_count = int(float(pm.get("sample_count") or 0))
    except Exception:
        sample_count = 0
    if sample_count < 10:
        return out

    pre_ret = pm.get("premarket_change_pct")
    atr_pct = pm.get("premarket_atr_pct_est")
    trend = _normalize_premarket_trend(pm.get("premarket_trend"))

    if pre_ret is not None:
        out["prev_day"]["return_pct"] = pre_ret
        out["prev_day"]["sample_count"] = sample_count
    if atr_pct is not None:
        out["atr"]["atr_pct"] = atr_pct
        out["atr"]["sample_count"] = sample_count
    if trend is not None:
        out["chart"]["trend"] = trend
        out["chart"]["sample_count"] = sample_count

    return out


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
                prev_ret = ((closes[-1] / closes[-2]) - 1.0) * 100.0 if len(closes) >= 2 and closes[-2] > 0 else None
                prev_rng = ((highs[-1] - lows[-1]) / px) * 100.0 if px > 0 else None
                chart_strength = ((e9 - e21) / px) if px > 0 else None
                return {
                    "kr_atr_pct": atr_pct,
                    "kr_trend": trend,
                    "kr_chart_strength": chart_strength,
                    "kr_chart_samples": len(closes),
                    "kr_prev_day_return_pct": prev_ret,
                    "kr_prev_day_range_pct": prev_rng,
                    "kr_chart_date": last,
                    "kr_chart_tf": "intraday",
                }

    # 2) daily_kr fallback
    daily_dir = base_dir / "data" / "daily_kr"
    files = sorted(daily_dir.glob(f"{symbol}_*.json")) if daily_dir.exists() else []
    if not files:
        return {
            "kr_atr_pct": None,
            "kr_trend": None,
            "kr_chart_strength": None,
            "kr_chart_samples": 0,
            "kr_prev_day_return_pct": None,
            "kr_prev_day_range_pct": None,
            "kr_chart_date": None,
            "kr_chart_tf": None,
        }

    path = files[-1]
    try:
        daily = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "kr_atr_pct": None,
            "kr_trend": None,
            "kr_chart_strength": None,
            "kr_chart_samples": 0,
            "kr_prev_day_return_pct": None,
            "kr_prev_day_range_pct": None,
            "kr_chart_date": None,
            "kr_chart_tf": None,
        }

    if not isinstance(daily, list) or len(daily) < 30:
        return {
            "kr_atr_pct": None,
            "kr_trend": None,
            "kr_chart_strength": None,
            "kr_chart_samples": 0,
            "kr_prev_day_return_pct": None,
            "kr_prev_day_range_pct": None,
            "kr_chart_date": path.stem.split("_")[-1],
            "kr_chart_tf": "daily",
        }

    closes = [float(b.get("close") or 0) for b in daily if float(b.get("close") or 0) > 0]
    highs = [float(b.get("high") or 0) for b in daily if float(b.get("high") or 0) > 0]
    lows = [float(b.get("low") or 0) for b in daily if float(b.get("low") or 0) > 0]
    if len(closes) < 30:
        return {
            "kr_atr_pct": None,
            "kr_trend": None,
            "kr_chart_strength": None,
            "kr_chart_samples": len(closes),
            "kr_prev_day_return_pct": None,
            "kr_prev_day_range_pct": None,
            "kr_chart_date": path.stem.split("_")[-1],
            "kr_chart_tf": "daily",
        }

    a = atr(highs[-60:], lows[-60:], closes[-60:], 14)
    px = closes[-1]
    atr_pct = (a / px * 100.0) if px > 0 and a > 0 else None
    e9 = ema(closes[-120:], 9)
    e21 = ema(closes[-120:], 21)
    trend = "UP" if e9 > e21 else "DOWN" if e9 < e21 else "FLAT"

    prev_ret = ((closes[-1] / closes[-2]) - 1.0) * 100.0 if len(closes) >= 2 and closes[-2] > 0 else None
    prev_rng = ((highs[-1] - lows[-1]) / px) * 100.0 if px > 0 else None
    chart_strength = ((e9 - e21) / px) if px > 0 else None

    return {
        "kr_atr_pct": atr_pct,
        "kr_trend": trend,
        "kr_chart_strength": chart_strength,
        "kr_chart_samples": len(closes),
        "kr_prev_day_return_pct": prev_ret,
        "kr_prev_day_range_pct": prev_rng,
        "kr_chart_date": path.stem.split("_")[-1],
        "kr_chart_tf": "daily",
    }


def _us_chart_metrics(base_dir: Path, symbol: str = "VOO") -> dict:
    """Compute simple US daily regime metrics from cached yfinance snapshot."""
    data_dir = base_dir / "data" / "daily_us"
    if not data_dir.exists():
        return {
            "us_atr_pct": None,
            "us_trend": None,
            "us_chart_strength": None,
            "us_chart_samples": 0,
            "us_prev_day_return_pct": None,
            "us_prev_day_range_pct": None,
            "us_chart_date": None,
            "us_rep": symbol,
        }

    files = sorted(data_dir.glob(f"{symbol}_*.json"))
    if not files:
        return {
            "us_atr_pct": None,
            "us_trend": None,
            "us_chart_strength": None,
            "us_chart_samples": 0,
            "us_prev_day_return_pct": None,
            "us_prev_day_range_pct": None,
            "us_chart_date": None,
            "us_rep": symbol,
        }

    path = files[-1]
    try:
        bars = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "us_atr_pct": None,
            "us_trend": None,
            "us_chart_strength": None,
            "us_chart_samples": 0,
            "us_prev_day_return_pct": None,
            "us_prev_day_range_pct": None,
            "us_chart_date": None,
            "us_rep": symbol,
        }

    if not isinstance(bars, list) or len(bars) < 30:
        return {
            "us_atr_pct": None,
            "us_trend": None,
            "us_chart_strength": None,
            "us_chart_samples": 0,
            "us_prev_day_return_pct": None,
            "us_prev_day_range_pct": None,
            "us_chart_date": path.stem.split("_")[-1],
            "us_rep": symbol,
        }

    closes = [float(b.get("close") or 0) for b in bars if float(b.get("close") or 0) > 0]
    highs = [float(b.get("high") or 0) for b in bars if float(b.get("high") or 0) > 0]
    lows = [float(b.get("low") or 0) for b in bars if float(b.get("low") or 0) > 0]
    if len(closes) < 30:
        return {
            "us_atr_pct": None,
            "us_trend": None,
            "us_chart_strength": None,
            "us_chart_samples": len(closes),
            "us_prev_day_return_pct": None,
            "us_prev_day_range_pct": None,
            "us_chart_date": path.stem.split("_")[-1],
            "us_rep": symbol,
        }

    a = atr(highs[-60:], lows[-60:], closes[-60:], 14)
    px = closes[-1]
    atr_pct = (a / px * 100.0) if px > 0 and a > 0 else None

    e9 = ema(closes[-120:], 9)
    e21 = ema(closes[-120:], 21)
    trend = "UP" if e9 > e21 else "DOWN" if e9 < e21 else "FLAT"
    prev_ret = ((closes[-1] / closes[-2]) - 1.0) * 100.0 if len(closes) >= 2 and closes[-2] > 0 else None
    prev_rng = ((highs[-1] - lows[-1]) / px) * 100.0 if px > 0 else None
    chart_strength = ((e9 - e21) / px) if px > 0 else None

    return {
        "us_atr_pct": atr_pct,
        "us_trend": trend,
        "us_chart_strength": chart_strength,
        "us_chart_samples": len(closes),
        "us_prev_day_return_pct": prev_ret,
        "us_prev_day_range_pct": prev_rng,
        "us_chart_date": path.stem.split("_")[-1],
        "us_rep": symbol,
    }


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

    kr_news = await _news_kr(an, kr_syms)
    us_news = await _news_us(an, us_syms)

    fg = await asyncio.to_thread(_cnn_fear_greed)

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
    ymd = datetime.now().strftime("%Y%m%d")

    vix_block = {"vix_last": (fg or {}).get("vix_last"), "vix_ma50": (fg or {}).get("vix_ma50"), "vix_gap": (fg or {}).get("vix_gap")}
    fg_block = {"fg_score": (fg or {}).get("fg_score"), "fg_rating": (fg or {}).get("fg_rating"), "fg_ts_utc": (fg or {}).get("fg_ts_utc")}

    kr_inputs = {
        "prev_day": {
            "return_pct": kr_chart.get("kr_prev_day_return_pct"),
            "range_pct": kr_chart.get("kr_prev_day_range_pct"),
            "sample_count": kr_chart.get("kr_chart_samples", 0),
        },
        "news": {
            "sentiment_avg": kr_news.get("avg"),
            "sample_count": kr_news.get("count", 0),
            "coverage": kr_news.get("coverage", 0.0),
            "success_rate": kr_news.get("success_rate", 0.0),
            "std": kr_news.get("std"),
        },
        "chart": {
            "trend": kr_chart.get("kr_trend"),
            "strength": kr_chart.get("kr_chart_strength"),
            "sample_count": kr_chart.get("kr_chart_samples", 0),
        },
        "vix": vix_block,
        "fg": fg_block,
        "atr": {
            "atr_pct": kr_chart.get("kr_atr_pct"),
            "sample_count": kr_chart.get("kr_chart_samples", 0),
        },
    }

    us_inputs = {
        "prev_day": {
            "return_pct": us_chart.get("us_prev_day_return_pct"),
            "range_pct": us_chart.get("us_prev_day_range_pct"),
            "sample_count": us_chart.get("us_chart_samples", 0),
        },
        "news": {
            "sentiment_avg": us_news.get("avg"),
            "sample_count": us_news.get("count", 0),
            "coverage": us_news.get("coverage", 0.0),
            "success_rate": us_news.get("success_rate", 0.0),
            "std": us_news.get("std"),
        },
        "chart": {
            "trend": us_chart.get("us_trend"),
            "strength": us_chart.get("us_chart_strength"),
            "sample_count": us_chart.get("us_chart_samples", 0),
        },
        "vix": vix_block,
        "fg": fg_block,
        "atr": {
            "atr_pct": us_chart.get("us_atr_pct"),
            "sample_count": us_chart.get("us_chart_samples", 0),
        },
    }

    kr_inputs = _apply_cached_overrides(
        base_dir=base_dir,
        market="KR",
        ymd=ymd,
        inputs=kr_inputs,
        symbols=kr_syms,
        rep_symbol=kr_rep,
        news_cap=20,
    )
    us_inputs = _apply_cached_overrides(
        base_dir=base_dir,
        market="US",
        ymd=ymd,
        inputs=us_inputs,
        symbols=us_syms,
        rep_symbol=str(us_chart.get("us_rep") or "VOO"),
        news_cap=10,
    )

    extras = {
        "market_inputs_meta": {
            "kr_rep": kr_rep,
            "us_rep": us_chart.get("us_rep"),
            "kr_chart_date": kr_chart.get("kr_chart_date"),
            "us_chart_date": us_chart.get("us_chart_date"),
            "kr_chart_tf": kr_chart.get("kr_chart_tf"),
        }
    }

    out = write_thresholds(
        path=str(base_dir / DEFAULT_PATH),
        base=base,
        kr_inputs=kr_inputs,
        us_inputs=us_inputs,
        extras=extras,
    )

    scoring = {}
    try:
        j = json.loads((base_dir / DEFAULT_PATH).read_text(encoding="utf-8"))
        scoring = j.get("scoring", {}) if isinstance(j, dict) else {}
    except Exception:
        scoring = {}

    print(
        json.dumps(
            {
                "ts_kst": datetime.now().astimezone().isoformat(),
                "kr_score": (scoring.get("kr") or {}).get("score"),
                "us_score": (scoring.get("us") or {}).get("score"),
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
