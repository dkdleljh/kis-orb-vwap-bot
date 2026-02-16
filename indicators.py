import math

def sma(data: list[float], period: int) -> float:
    """단순 이동평균 (Simple Moving Average)"""
    if len(data) < period:
        return 0.0
    return sum(data[-period:]) / period

def std_dev(data: list[float], period: int) -> float:
    """표준편차"""
    if len(data) < period:
        return 0.0
    avg = sma(data, period)
    variance = sum((x - avg) ** 2 for x in data[-period:]) / period
    return math.sqrt(variance)

def rsi(data: list[float], period: int = 14) -> float:
    """RSI (Relative Strength Index)"""
    if len(data) < period + 1:
        return 50.0 # 기본값

    deltas = [data[i] - data[i - 1] for i in range(1, len(data))]
    gains = [x if x > 0 else 0 for x in deltas]
    losses = [-x if x < 0 else 0 for x in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100.0

    # Wilder's Smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def bollinger_bands(data: list[float], period: int = 20, multiplier: float = 2.0) -> tuple[float, float, float]:
    """볼린저 밴드 (Upper, Mid, Lower)"""
    if len(data) < period:
        return 0.0, 0.0, 0.0
    
    mid = sma(data, period)
    std = std_dev(data, period)
    upper = mid + (std * multiplier)
    lower = mid - (std * multiplier)
    return upper, mid, lower

def envelope(data: list[float], period: int = 20, percent: float = 2.0) -> tuple[float, float, float]:
    """엔벨로프 (Upper, Mid, Lower)"""
    if len(data) < period:
        return 0.0, 0.0, 0.0
    
    mid = sma(data, period)
    upper = mid * (1 + percent / 100.0)
    lower = mid * (1 - percent / 100.0)
    return upper, mid, lower

def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """ATR (Average True Range) - 변동성 지표"""
    if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
        return 0.0
    
    tr_values = []
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i-1])
        low_close = abs(lows[i] - closes[i-1])
        tr = max(high_low, high_close, low_close)
        tr_values.append(tr)
    
    if len(tr_values) < period:
        return 0.0
    
    return sma(tr_values, period)

def atr_percent(price: float, atr_value: float) -> float:
    """ATR을 퍼센트로 변환"""
    if price <= 0 or atr_value <= 0:
        return 0.0
    return (atr_value / price) * 100.0

def stochastic(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Stochastic Oscillator (%K)"""
    if len(highs) < period or len(lows) < period or len(closes) < period:
        return 50.0
    
    highest_high = max(highs[-period:])
    lowest_low = min(lows[-period:])
    current_close = closes[-1]
    
    if highest_high == lowest_low:
        return 50.0
    
    return ((current_close - lowest_low) / (highest_high - lowest_low)) * 100.0

def ema(data: list[float], period: int) -> float:
    """지수 이동평균 (Exponential Moving Average)"""
    if len(data) < period:
        return 0.0
    multiplier = 2 / (period + 1)
    ema_val = data[0]
    for price in data[1:]:
        ema_val = (price * multiplier) + (ema_val * (1 - multiplier))
    return ema_val


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """MACD (MACD Line, Signal Line, Histogram)"""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = fast_ema - slow_ema
    
    macd_values = []
    for i in range(slow, len(closes) + 1):
        fast_e = ema(closes[:i], fast)
        slow_e = ema(closes[:i], slow)
        macd_values.append(fast_e - slow_e)
    
    signal_line = ema(macd_values, signal) if len(macd_values) >= signal else macd_line
    histogram = macd_line - signal_line
    
    return macd_line, signal_line, histogram
