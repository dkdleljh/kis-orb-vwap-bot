"""Compute a best-effort US entry score *now* (offline snapshot).

Why:
- USSwingModule score normally requires live 1m ticks/bars.
- This script fetches recent 15m bars (yfinance) and approximates book/vwap
  to run Perfect100Strategy scoring and prints SignalScore lines.

Caveats:
- Yahoo/yfinance may rate-limit. If fetch fails, prints an error.
- This is a diagnostic tool; it does not place orders.

Usage:
  cd ~/Desktop/kis_orb_vwap_bot && venv/bin/python scripts/score_now_us.py

Env:
- US_SCORE_SYMBOLS="AAPL,MSFT,NVDA,TSLA"
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import csv
import urllib.request

from indicators import atr, ema, macd, rsi, sma
from models import OrderBookTop
from perfect_strategy import Perfect100Strategy, State
from scoring import ScoreBreakdown, SignalScore
from strategy_profiles import get_recommended_threshold


def _symbols() -> list[str]:
    raw = os.environ.get("US_SCORE_SYMBOLS", "AAPL,MSFT,NVDA,TSLA")
    out = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return out[:10] or ["AAPL"]


def _fetch_daily_stooq(symbol: str) -> list[dict]:
    """Fetch daily bars from Stooq CSV (no API key).

    Symbol mapping: AAPL -> aapl.us
    """
    s = symbol.lower()
    url = f"https://stooq.com/q/d/l/?s={s}.us&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode("utf-8", errors="replace")
    rows = list(csv.DictReader(text.splitlines()))
    # keep last ~120 rows
    return rows[-160:]


def _make_book(last_px: float) -> OrderBookTop:
    # simple spread assumption 2bp
    spr = max(0.01, last_px * 0.0002)
    bid = last_px - spr / 2
    ask = last_px + spr / 2
    return OrderBookTop(
        symbol="",
        bid=float(bid),
        ask=float(ask),
        bid_size=100,
        ask_size=100,
        timestamp=datetime.now(),
    )


def _vwap_from_bars(_: Any) -> float | None:
    # Deprecated: kept for compatibility with earlier draft.
    return None


def main() -> int:
    # threshold from dynamic thresholds if present
    entry_th = int(get_recommended_threshold("US", "SWING"))

    # lightweight logger
    class _L:
        def info(self, msg):
            print(msg)
        def warning(self, msg):
            print("WARN", msg)

    logger = _L()

    strat = Perfect100Strategy(
        logger=logger,
        min_score=entry_th,
        max_spread_pct=0.010,
        min_bid_ask_ratio=0.7,
        min_win_rate=0.53,
        min_r_ratio=1.2,
    )

    results = []

    for sym in _symbols():
        try:
            rows = _fetch_daily_stooq(sym)
        except Exception as e:
            results.append({"symbol": sym, "error": f"fetch_failed:{e}"})
            continue

        if not rows or len(rows) < 40:
            results.append({"symbol": sym, "error": "no_data_or_too_short"})
            continue

        closes = [float(r.get("Close") or 0) for r in rows if float(r.get("Close") or 0) > 0]
        highs = [float(r.get("High") or 0) for r in rows if float(r.get("High") or 0) > 0]
        lows = [float(r.get("Low") or 0) for r in rows if float(r.get("Low") or 0) > 0]
        vols = [float(r.get("Volume") or 0) for r in rows]

        if len(closes) < 40:
            results.append({"symbol": sym, "error": "not_enough_closes"})
            continue

        last_price = float(closes[-1])
        # approximate vwap with 20d typical-price vwap
        try:
            tail = rows[-40:]
            tp = [ (float(r.get('High') or 0)+float(r.get('Low') or 0)+float(r.get('Close') or 0))/3.0 for r in tail ]
            tv = [ float(r.get('Volume') or 0) for r in tail ]
            den = sum(tv)
            vwap = (sum([a*b for a,b in zip(tp,tv)]) / den) if den > 0 else float(sma(closes, 20) or last_price)
        except Exception:
            vwap = float(sma(closes, 20) or last_price)

        book = _make_book(last_price)
        book.symbol = sym

        ind: dict[str, Any] = {
            "rsi": rsi(closes, 14),
            "ema9": ema(closes, 9),
            "ema21": ema(closes, 21),
            "ma20": sma(closes, 20),
            "prev_close": closes[-2],
            "volume_power": 120,
            "news_score": 0,
            "atr": atr(highs, lows, closes, 14),
        }
        _, _, m_hist = macd(closes, 12, 26, 9)
        ind["macd_hist"] = m_hist

        # craft a pseudo "bar" using last close
        from models import Bar1m

        bar = Bar1m(
            start=datetime.now(),
            open=closes[-2],
            high=highs[-1],
            low=lows[-1],
            close=closes[-1],
            volume=float(vols[-1] if vols else 0.0),
        )

        strat.set_state(State.WAIT_SIGNAL)
        strat.update_or(sym, bar)

        sig = strat.evaluate_entry(
            bar=bar,
            last_price=float(last_price),
            vwap=float(vwap),
            book=book,
            lever_symbol=sym,
            inverse_symbol=sym,
            indicators=ind,
            market_regime="NEUTRAL",
        )

        breakdown = ScoreBreakdown({"SWING": 40, "MOMO": 20, "VWAP": 10, "RR": 30})
        score = float(getattr(sig, "score", 0) or 0)

        out = SignalScore(
            symbol=sym,
            side=sig.side,
            score=score,
            threshold=float(entry_th),
            market="US",
            style="SWING",
            risk_grade="B" if score >= entry_th else "C",
            breakdown=breakdown,
            reasons=list(sig.reasons or []),
        )

        print(out.to_line())
        results.append({"symbol": sym, "score": score, "threshold": entry_th, "side": sig.side, "reasons": (sig.reasons or [])[:5]})

    # machine-friendly footer
    print(json.dumps({"ok": True, "entry_threshold": entry_th, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
