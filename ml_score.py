"""Heuristic ML-style scoring helpers.

The project originally embedded this score function in main.py.
It is kept as a small, isolated module to make the trading engine easier to
read/test and to avoid a growing "god file".
"""

from __future__ import annotations


def calculate_ml_score(indicators: dict, last_price: float, vwap: float | None) -> float:
    """ML-inspired scoring based on technical indicators.

    Returns a score from 0-100 representing model-like confidence in entry.

    Note:
        This is not a real ML model. It is a deterministic heuristic that
        combines multiple indicator signals into a single score.
    """

    if not indicators or last_price <= 0:
        return 50.0

    score = 50.0

    rsi_val = indicators.get("rsi", 50)
    if 30 <= rsi_val <= 70:
        score += 10

    macd_hist = indicators.get("macd_hist", 0)
    if macd_hist > 0:
        score += 15
    elif macd_hist < 0:
        score -= 10

    ema9 = indicators.get("ema9", 0)
    ema21 = indicators.get("ema21", 0)
    if ema9 > ema21 > 0:
        score += 10

    ma20 = indicators.get("ma20", 0)
    if ma20 > 0 and last_price > ma20:
        score += 10

    if vwap and vwap > 0:
        vwap_distance = (last_price - vwap) / vwap
        if vwap_distance > 0:
            score += 10
        else:
            score -= 5

    bb_up = indicators.get("bb_up", 0)
    bb_low = indicators.get("bb_low", 0)
    if bb_up > 0 and bb_low > 0:
        bb_position = (last_price - bb_low) / (bb_up - bb_low) if bb_up != bb_low else 0.5
        if 0.2 <= bb_position <= 0.8:
            score += 5

    volume_power = indicators.get("volume_power", 100)
    if volume_power >= 120:
        score += 10
    elif volume_power < 80:
        score -= 10

    atr_percent = indicators.get("atr_percent", 0)
    if atr_percent >= 0.5:
        score += 5
    elif atr_percent < 0.2:
        score -= 5

    return max(0, min(100, score))
