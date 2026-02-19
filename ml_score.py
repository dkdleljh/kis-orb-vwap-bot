"""Heuristic ML-style scoring helpers.

The project originally embedded this score function in main.py.
It is kept as a small, isolated module to make the trading engine easier to
read/test and to avoid a growing "god file".
"""

from __future__ import annotations


def calculate_ml_score(
    indicators: dict, last_price: float, vwap: float | None
) -> float:
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
        bb_position = (
            (last_price - bb_low) / (bb_up - bb_low) if bb_up != bb_low else 0.5
        )
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


def predict_next_bar_direction(
    closes: list[float],
    volumes: list[float],
    lookback: int = 20,
) -> tuple[str, float]:
    """Predict next bar direction based on historical patterns.

    Args:
        closes: List of closing prices
        volumes: List of volumes
        lookback: Number of bars to analyze

    Returns:
        Tuple of (direction: 'UP'|'DOWN'|'NEUTRAL', confidence: 0-1)
    """
    if not closes or len(closes) < lookback:
        return "NEUTRAL", 0.5

    recent_closes = closes[-lookback:]
    recent_volumes = volumes[-lookback:] if volumes else [1] * lookback

    price_change = (recent_closes[-1] - recent_closes[0]) / recent_closes[0]

    avg_volume = sum(recent_volumes) / len(recent_volumes)
    recent_volume = recent_volumes[-1]
    volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1.0

    momentum = 0
    for i in range(-min(5, len(recent_closes) - 1), 0):
        if recent_closes[i] > recent_closes[i - 1]:
            momentum += 1
        elif recent_closes[i] < recent_closes[i - 1]:
            momentum -= 1

    score = 0.0
    if price_change > 0.01:
        score += 0.3
    elif price_change < -0.01:
        score -= 0.3

    if volume_ratio > 1.5:
        score += 0.2 * (1 if price_change > 0 else -1)
    elif volume_ratio < 0.7:
        score -= 0.1

    if momentum > 2:
        score += 0.2
    elif momentum < -2:
        score -= 0.2

    if score > 0.2:
        direction = "UP"
        confidence = min(0.9, 0.5 + score)
    elif score < -0.2:
        direction = "DOWN"
        confidence = min(0.9, 0.5 + abs(score))
    else:
        direction = "NEUTRAL"
        confidence = 0.5

    return direction, confidence


def calculate_pattern_score(
    closes: list[float],
    pattern_type: str = "default",
) -> float:
    """Calculate pattern-based prediction score.

    Args:
        closes: List of closing prices
        pattern_type: Type of pattern to look for

    Returns:
        Score from -1 to 1
    """
    if not closes or len(closes) < 10:
        return 0.0

    recent = closes[-10:]

    if pattern_type == "default":
        highs = [max(recent[i : i + 3]) for i in range(len(recent) - 2)]
        lows = [min(recent[i : i + 3]) for i in range(len(recent) - 2)]

        if len(highs) >= 3 and len(lows) >= 3:
            if highs[-1] > highs[-2] > highs[-3]:
                return 0.5
            elif lows[-1] < lows[-2] < lows[-3]:
                return -0.5

        return 0.0

    return 0.0
