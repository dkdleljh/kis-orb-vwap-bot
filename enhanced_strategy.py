"""
Enhanced Strategy Engine - 100점 트레이더를 위한 고급 전략.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, List

from models import Bar1m, OrderBookTop, Position


class State(Enum):
    WAIT_OPEN = "WAIT_OPEN"
    BUILD_OR = "BUILD_OR"
    WAIT_SIGNAL = "WAIT_SIGNAL"
    ENTRY_PENDING = "ENTRY_PENDING"
    IN_POSITION = "IN_POSITION"
    EXIT_PENDING = "EXIT_PENDING"
    DONE_TODAY = "DONE_TODAY"


@dataclass
class ORState:
    or_high: Optional[float] = None
    or_low: Optional[float] = None

    def update(self, bar: Bar1m) -> None:
        if self.or_high is None:
            self.or_high = bar.high
            self.or_low = bar.low
            return
        self.or_high = max(self.or_high, bar.high)
        self.or_low = min(self.or_low, bar.low)


@dataclass
class Signal:
    side: Optional[str] = None
    symbol: Optional[str] = None
    score: int = 0
    reasons: List[str] = None
    atr_stop: float = 0.0
    atr_percent: float = 0.0

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


class EnhancedStrategy:
    def __init__(
        self,
        logger=None,
        min_score: int = 50,
        max_spread_pct: float = 0.005,
        min_bid_ask_ratio: float = 0.5,
        enable_time_filter: bool = True,
        enable_atr_filter: bool = True,
    ):
        self.state = State.WAIT_OPEN
        self.or_state: Dict[str, ORState] = {}
        self.position: Optional[Position] = None
        self.logger = logger
        self.news_analyzer = None
        
        self.min_score = min_score
        self.max_spread_pct = max_spread_pct
        self.min_bid_ask_ratio = min_bid_ask_ratio
        self.enable_time_filter = enable_time_filter
        self.enable_atr_filter = enable_atr_filter
    
    def set_state(self, new_state: State) -> None:
        if self.state != new_state:
            if self.logger:
                self.logger.info(f"state {self.state.value} -> {new_state.value}")
            self.state = new_state
    
    def in_position(self) -> bool:
        return self.state == State.IN_POSITION and self.position is not None
    
    def update_or(self, symbol: str, bar: Bar1m) -> None:
        if symbol not in self.or_state:
            self.or_state[symbol] = ORState()
        self.or_state[symbol].update(bar)
    
    def _is_valid_time_for_entry(self, hour: int, minute: int) -> bool:
        if not self.enable_time_filter:
            return True
        
        total_minutes = hour * 60 + minute
        
        morning_boost_start = 9 * 60 + 5
        morning_boost_end = 9 * 60 + 45
        
        afternoon_entry_end = 14 * 60 + 30
        
        if morning_boost_start <= total_minutes <= morning_boost_end:
            return True
        
        if total_minutes < afternoon_entry_end and total_minutes >= 9 * 60 + 50:
            return True
        
        return False
    
    def _calculate_spread_quality(self, book: OrderBookTop) -> float:
        if book.ask <= 0 or book.bid <= 0:
            return 0.0
        
        spread = (book.ask - book.bid) / book.ask
        
        if spread > self.max_spread_pct:
            return 0.0
        
        bid_ask_ratio = book.bid_size / book.ask_size if book.ask_size > 0 else 0
        
        quality = (1 - (spread / self.max_spread_pct)) * min(bid_ask_ratio / self.min_bid_ask_ratio, 1.0)
        return max(0.0, quality)
    
    def evaluate_entry(
        self,
        bar: Bar1m,
        last_price: float,
        vwap: Optional[float],
        book: OrderBookTop,
        lever_symbol: str,
        inverse_symbol: str,
        indicators: Dict,
        market_regime: str = "NEUTRAL",
    ) -> Signal:
        if self.state != State.WAIT_SIGNAL:
            return Signal()
        if vwap is None:
            return Signal()
        if book.ask <= 0 or book.bid <= 0:
            return Signal()
        
        spread_pct = (book.ask - book.bid) / book.ask
        if spread_pct > self.max_spread_pct:
            return Signal()
        
        or_state = self.or_state.get(book.symbol)
        if or_state is None or or_state.or_high is None or or_state.or_low is None:
            return Signal()
        
        spread_quality = self._calculate_spread_quality(book)
        if spread_quality < 0.3:
            return Signal()
        
        if self.enable_time_filter:
            from datetime import datetime
            now = datetime.now()
            if not self._is_valid_time_for_entry(now.hour, now.minute):
                return Signal()
        
        score = 0
        reasons = []
        
        from indicators import atr_percent as calc_atr_percent
        atr_val = indicators.get("atr", 0)
        atr_pct = calc_atr_percent(last_price, atr_val) if atr_val > 0 else 0
        
        if self.enable_atr_filter and atr_pct > 10:
            return Signal()
        
        rsi_val = indicators.get("rsi", 50.0)
        ema9 = indicators.get("ema9", 0)
        ema21 = indicators.get("ema21", 0)
        macd_hist = indicators.get("macd_hist", 0.0)
        
        if rsi_val >= 72:
            reasons.append(f"RSI_OVERBOUGHT({rsi_val:.0f})")
            if score < self.min_score + 15:
                return Signal()
        elif 50 <= rsi_val <= 68:
            score += 10
            reasons.append(f"RSI_OK({rsi_val:.0f})")
        
        if ema9 > ema21 > 0 and last_price >= ema21:
            score += 10
            reasons.append("EMA_BULL")
        
        if macd_hist > 0:
            score += 10
            reasons.append("MACD_BULL")
        elif macd_hist < 0:
            score -= 10
            reasons.append("MACD_BEAR")
        
        prev_close = indicators.get("prev_close", 0)
        ma20 = indicators.get("ma20", 0)
        
        if prev_close > 0:
            day_return = (last_price - prev_close) / prev_close
            if day_return >= 0.005:
                score += 15
                reasons.append(f"UP_TREND({day_return:.1%})")
        
        if ma20 > 0 and last_price > ma20:
            score += 10
            reasons.append("ABOVE_MA20")
        
        vol_power = indicators.get("volume_power", 100.0)
        if vol_power >= 120:
            score += 20
            reasons.append(f"POWER({vol_power:.0f}%)")
        
        if last_price >= vwap:
            score += 10
            reasons.append("VWAP_SUPPORT")
        
        is_breakout = (bar.close > or_state.or_high)
        is_pullback_bull = (last_price > ma20) and (bar.close > bar.open)
        
        if is_breakout:
            score += 20
            reasons.append("OR_BREAKOUT")
        elif is_pullback_bull:
            score += 20
            reasons.append("PULLBACK_BULL")
        
        if book.bid_size > book.ask_size * 0.8:
            score += 10
            reasons.append("BID_SUPPORT")
        
        score += int(spread_quality * 10)
        reasons.append(f"SPREAD_QUALITY({spread_quality:.1f})")
        
        news_score = indicators.get("news_score", 0)
        if news_score >= 50:
            score += 20
            reasons.append(f"NEWS_HOT({news_score})")
        elif news_score >= 20:
            score += 10
            reasons.append("NEWS_GOOD")
        elif news_score <= -20:
            if book.symbol != inverse_symbol:
                return Signal()
        
        body = abs(bar.close - bar.open)
        upper_wick = bar.high - max(bar.close, bar.open)
        if body > 0 and upper_wick > body * 2:
            score -= 30
            reasons.append("WICK_REJECT")
        
        if prev_close > 0 and (last_price / prev_close) > 1.20:
            score -= 50
            reasons.append("TOO_HIGH")
        
        if market_regime == "BEAR" and book.symbol != inverse_symbol:
            return Signal()
        
        if score >= self.min_score:
            atr_stop = 0
            if atr_val > 0:
                atr_stop = last_price - (atr_val * 2)
            
            return Signal(
                side="BUY",
                symbol=book.symbol,
                score=score,
                reasons=reasons,
                atr_stop=atr_stop,
                atr_percent=atr_pct,
            )
        
        return Signal()
    
    def get_stop_loss_price(
        self,
        entry_price: float,
        atr: float,
        atr_multiplier: float = 2.0,
        use_atr: bool = True,
        fixed_stop_pct: float = 0.015,
    ) -> float:
        if use_atr and atr > 0:
            return entry_price - (atr * atr_multiplier)
        return entry_price * (1 - fixed_stop_pct)
    
    def get_take_profit_price(
        self,
        entry_price: float,
        atr: float,
        atr_multiplier: float = 3.0,
        use_atr: bool = True,
        fixed_tp_pct: float = 0.030,
    ) -> float:
        if use_atr and atr > 0:
            return entry_price + (atr * atr_multiplier)
        return entry_price * (1 + fixed_tp_pct)
