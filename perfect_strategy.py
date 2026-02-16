"""
Perfect 100-Point Strategy - 모든 요소를 갖춘 완벽한 전략.
"""

from dataclasses import dataclass
from typing import Optional, Dict, List
from enum import Enum

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
        if self.or_low is not None:
            self.or_low = min(self.or_low, bar.low)


@dataclass
class PerfectSignal:
    side: Optional[str] = None
    symbol: Optional[str] = None
    score: int = 0
    reasons: Optional[List[str]] = None
    expected_win_rate: float = 0.0
    expected_r: float = 0.0
    risk_reward_ratio: float = 0.0
    slippage_adjusted: bool = False
    fee_adjusted: bool = False

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []


class Perfect100Strategy:
    def __init__(
        self,
        logger=None,
        min_score: int = 60,
        max_spread_pct: float = 0.003,
        min_bid_ask_ratio: float = 0.8,
        min_win_rate: float = 0.55,
        min_r_ratio: float = 1.5,
        or_start_minute: int = 5,
        or_start_time: str = "09:00",
        or_end_time: str = "09:05",
    ):
        self.state = State.WAIT_OPEN
        self.or_state: Dict[str, ORState] = {}
        self.position: Optional[Position] = None
        self.logger = logger

        self.min_score = min_score
        self.max_spread_pct = max_spread_pct
        self.min_bid_ask_ratio = min_bid_ask_ratio
        self.min_win_rate = min_win_rate
        self.min_r_ratio = min_r_ratio

        self.or_start_minute = or_start_minute
        self.or_start_time = or_start_time
        self.or_end_time = or_end_time

        self.fee_rate = 0.00015 + 0.00015 + 0.002
        self.slippage_rate = 0.001
        self.total_cost_rate = self.fee_rate + self.slippage_rate

        self._peak_price: float = 0.0
        self._entry_price: float = 0.0

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

    def _validate_market_quality(self, book: OrderBookTop) -> bool:
        if book.ask <= 0 or book.bid <= 0:
            return False

        spread = (book.ask - book.bid) / book.ask
        if spread > self.max_spread_pct:
            return False

        bid_ask_ratio = book.bid_size / book.ask_size if book.ask_size > 0 else 0
        if bid_ask_ratio < self.min_bid_ask_ratio:
            return False

        return True

    def _calculate_risk_reward(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> tuple[float, float]:
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)

        if risk <= 0:
            return 0.0, 0.0

        rr_ratio = reward / risk

        net_reward = reward - (entry_price * self.total_cost_rate)
        net_risk = risk + (entry_price * self.total_cost_rate)

        net_rr = net_reward / net_risk if net_risk > 0 else 0.0

        return rr_ratio, net_rr

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
    ) -> PerfectSignal:
        if self.state != State.WAIT_SIGNAL:
            return PerfectSignal()
        if vwap is None:
            return PerfectSignal()

        if not self._validate_market_quality(book):
            return PerfectSignal()

        or_state = self.or_state.get(book.symbol)
        if or_state is None:
            return PerfectSignal()

        score = 0
        reasons = []

        prev_close = indicators.get("prev_close", 0)
        ma20 = indicators.get("ma20", 0)

        rsi_val = indicators.get("rsi", 50.0)
        ema9 = indicators.get("ema9", 0)
        ema21 = indicators.get("ema21", 0)
        macd_hist = indicators.get("macd_hist", 0.0)

        if rsi_val >= 72:
            reasons.append(f"RSI_OVERBOUGHT({rsi_val:.0f})")
            if score < self.min_score + 15:
                return PerfectSignal()
        elif 50 <= rsi_val <= 68:
            score += 10
            reasons.append(f"RSI_OK({rsi_val:.0f})")
        elif rsi_val < 40:
            score += 5
            reasons.append(f"RSI_OVERSOLD({rsi_val:.0f})")

        if ema9 > ema21 > 0 and last_price >= ema21:
            score += 10
            reasons.append("EMA_BULL")
        elif ema21 > ema9 > 0 and last_price <= ema21:
            score -= 10
            reasons.append("EMA_BEAR")

        if macd_hist > 0:
            score += 10
            reasons.append("MACD_BULL")
        elif macd_hist < 0:
            score -= 10
            reasons.append("MACD_BEAR")

        if prev_close > 0:
            day_return = (last_price - prev_close) / prev_close
            if day_return >= 0.003:
                score += 15
                reasons.append(f"UP({day_return:.1%})")

        if ma20 > 0 and last_price > ma20:
            score += 10
            reasons.append("MA20+")

        vol_power = indicators.get("volume_power", 100)
        if vol_power >= 150:
            score += 20
            reasons.append(f"VOL+({vol_power:.0f})")
        elif vol_power >= 120:
            score += 10

        if vwap is not None and last_price >= vwap:
            score += 10
            reasons.append("VWAP+")

        is_breakout = or_state.or_high is not None and bar.close > or_state.or_high
        is_pullback = (last_price > ma20 > 0) and (bar.close > bar.open)

        if is_breakout:
            score += 25
            reasons.append("BREAKOUT")
        elif is_pullback:
            score += 20
            reasons.append("PULLBACK")

        if book.bid_size >= book.ask_size:
            score += 10
            reasons.append("BID+")

        news_score = indicators.get("news_score", 0)
        if news_score >= 50:
            score += 20
            reasons.append(f"NEWS+({news_score})")
        elif news_score >= 20:
            score += 10
        elif news_score <= -30:
            if book.symbol != inverse_symbol:
                return PerfectSignal()

        body = abs(bar.close - bar.open)
        upper_wick = bar.high - max(bar.close, bar.open)
        if body > 0 and upper_wick > body * 2:
            score -= 25
            reasons.append("WICK-")

        if prev_close > 0 and (last_price / prev_close) > 1.15:
            score -= 30
            reasons.append("OVER+")

        if market_regime == "BEAR" and book.symbol != inverse_symbol:
            return PerfectSignal()

        atr = indicators.get("atr", 0)
        stop_loss = last_price - (atr * 2) if atr > 0 else last_price * 0.985
        take_profit = last_price + (atr * 3) if atr > 0 else last_price * 1.03

        rr_ratio, net_rr = self._calculate_risk_reward(
            last_price, stop_loss, take_profit
        )

        if net_rr < self.min_r_ratio:
            reasons.append(f"RR-({net_rr:.1f})")
            if score < self.min_score + 10:
                return PerfectSignal()

        if score >= self.min_score:
            estimated_win_rate = min(0.95, 0.40 + (score / 200) + (net_rr / 10))

            return PerfectSignal(
                side="BUY",
                symbol=book.symbol,
                score=score,
                reasons=reasons,
                expected_win_rate=estimated_win_rate,
                expected_r=net_rr * estimated_win_rate - (1 - estimated_win_rate),
                risk_reward_ratio=net_rr,
                slippage_adjusted=True,
                fee_adjusted=True,
            )

        sell_score = self._evaluate_sell_entry(
            bar=bar,
            last_price=last_price,
            vwap=vwap,
            book=book,
            or_state=or_state,
            indicators=indicators,
            market_regime=market_regime,
            inverse_symbol=inverse_symbol,
        )

        if sell_score and sell_score.score >= self.min_score:
            return sell_score

        return PerfectSignal()

    def _evaluate_sell_entry(
        self,
        bar: Bar1m,
        last_price: float,
        vwap: Optional[float],
        book: OrderBookTop,
        or_state: ORState,
        indicators: Dict,
        market_regime: str,
        inverse_symbol: str,
    ) -> Optional[PerfectSignal]:
        if book.symbol == inverse_symbol:
            return None

        if market_regime != "BEAR":
            return None

        score = 0
        reasons = []

        prev_close = indicators.get("prev_close", 0)
        ma20 = indicators.get("ma20", 0)

        rsi_val = indicators.get("rsi", 50.0)
        ema9 = indicators.get("ema9", 0)
        ema21 = indicators.get("ema21", 0)
        macd_hist = indicators.get("macd_hist", 0.0)

        if rsi_val <= 28:
            reasons.append(f"RSI_OVERSOLD({rsi_val:.0f})")
            if score < self.min_score + 15:
                return None
        elif 32 <= rsi_val <= 50:
            score += 10
            reasons.append(f"RSI_OK({rsi_val:.0f})")

        if ema21 > ema9 > 0 and last_price <= ema21:
            score += 10
            reasons.append("EMA_BEAR")
        elif ema9 > ema21 > 0 and last_price >= ema21:
            score -= 10
            reasons.append("EMA_BULL")

        if macd_hist < 0:
            score += 10
            reasons.append("MACD_BEAR")
        elif macd_hist > 0:
            score -= 10
            reasons.append("MACD_BULL")

        if prev_close > 0:
            day_return = (last_price - prev_close) / prev_close
            if day_return <= -0.003:
                score += 15
                reasons.append(f"DOWN({day_return:.1%})")

        if ma20 > 0 and last_price < ma20:
            score += 10
            reasons.append("MA20-")

        vol_power = indicators.get("volume_power", 100)
        if vol_power >= 150:
            score += 20
            reasons.append(f"VOL+({vol_power:.0f})")
        elif vol_power >= 120:
            score += 10

        if vwap is not None and last_price <= vwap:
            score += 10
            reasons.append("VWAP-")

        is_breakdown = or_state.or_low is not None and bar.close < or_state.or_low
        is_rejection = (last_price < ma20 > 0) and (bar.close < bar.open)

        if is_breakdown:
            score += 25
            reasons.append("BREAKDOWN")
        elif is_rejection:
            score += 20
            reasons.append("REJECTION")

        if book.ask_size >= book.bid_size:
            score += 10
            reasons.append("ASK+")

        news_score = indicators.get("news_score", 0)
        if news_score <= -50:
            score += 20
            reasons.append(f"NEWS-({news_score})")
        elif news_score <= -20:
            score += 10

        body = abs(bar.close - bar.open)
        lower_wick = min(bar.close, bar.open) - bar.low
        if body > 0 and lower_wick > body * 2:
            score -= 25
            reasons.append("WICK-")

        if prev_close > 0 and (last_price / prev_close) < 0.85:
            score -= 30
            reasons.append("OVER-")

        atr = indicators.get("atr", 0)
        stop_loss = last_price + (atr * 2) if atr > 0 else last_price * 1.015
        take_profit = last_price - (atr * 3) if atr > 0 else last_price * 0.97

        rr_ratio, net_rr = self._calculate_risk_reward(
            last_price, stop_loss, take_profit
        )

        if net_rr < self.min_r_ratio:
            reasons.append(f"RR-({net_rr:.1f})")
            if score < self.min_score + 10:
                return None

        if score >= self.min_score:
            estimated_win_rate = min(0.95, 0.40 + (score / 200) + (net_rr / 10))

            return PerfectSignal(
                side="SELL",
                symbol=book.symbol,
                score=score,
                reasons=reasons,
                expected_win_rate=estimated_win_rate,
                expected_r=net_rr * estimated_win_rate - (1 - estimated_win_rate),
                risk_reward_ratio=net_rr,
                slippage_adjusted=True,
                fee_adjusted=True,
            )

        return None

    def should_take_profit(
        self,
        current_price: float,
        entry_price: float,
        atr: float,
        side: str = "BUY",
    ) -> tuple[bool, str]:
        gross_pnl = (current_price - entry_price) / entry_price

        net_pnl = gross_pnl - self.total_cost_rate

        if side == "SELL":
            net_pnl = -net_pnl

        tp_levels = [
            (0.015, "TP1"),
            (0.025, "TP2"),
            (0.035, "TP3"),
        ]

        for tp_pct, label in tp_levels:
            if net_pnl >= tp_pct:
                return True, label

        return False, ""

    def should_trailing_stop(
        self,
        current_price: float,
        peak_price: float,
        entry_price: float,
        atr: float,
        side: str = "BUY",
    ) -> tuple[bool, str]:
        if side == "BUY":
            trailing_distance = peak_price - current_price
            atr_trailing = atr * 2.5
            if peak_price > entry_price and trailing_distance >= atr_trailing:
                return True, "TRAIL"
        else:
            trailing_distance = current_price - peak_price
            atr_trailing = atr * 2.5
            if peak_price < entry_price and trailing_distance >= atr_trailing:
                return True, "TRAIL"

        return False, ""

    def get_partial_tp_levels(
        self,
        current_price: float,
        entry_price: float,
        atr: float,
    ) -> list[tuple[float, float, int]]:
        levels = []

        tp_prices = [
            (entry_price * 1.015, 0.30),
            (entry_price * 1.025, 0.30),
            (entry_price * 1.035, 0.40),
        ]

        for tp_price, portion in tp_prices:
            if current_price >= tp_price:
                levels.append((tp_price, portion, int(portion * 100)))

        return levels

    def should_stop_loss(
        self,
        current_price: float,
        entry_price: float,
        atr: float,
        side: str = "BUY",
    ) -> tuple[bool, str]:
        gross_pnl = (current_price - entry_price) / entry_price

        net_pnl = gross_pnl - self.total_cost_rate

        if side == "SELL":
            net_pnl = -net_pnl

        sl_levels = [
            (-0.008, "SL1"),
            (-0.012, "SL2"),
            (-0.015, "SL3"),
        ]

        for sl_pct, label in sl_levels:
            if net_pnl <= sl_pct:
                return True, label

        if atr > 0:
            atr_stop = entry_price - (atr * 2)
            if side == "SELL":
                atr_stop = entry_price + (atr * 2)
            if current_price <= atr_stop:
                return True, "ATR_SL"

        return False, ""

    def get_adjusted_prices(
        self, book: OrderBookTop, is_buy: bool
    ) -> tuple[float, float]:
        if is_buy:
            expected_price = book.ask * (1 + self.slippage_rate)
            worst_price = book.ask * (1 + self.slippage_rate * 2)
        else:
            expected_price = book.bid * (1 - self.slippage_rate)
            worst_price = book.bid * (1 - self.slippage_rate * 2)

        return expected_price, worst_price
