from dataclasses import dataclass
from enum import Enum
from logging import Logger
from typing import cast

from models import Bar1m, OrderBookTop, Position
from news import NewsSentimentAnalyzer


class State(str, Enum):
    """Trading state enumeration.

    Represents the current lifecycle stage for the strategy during a
    single trading session.
    """

    WAIT_OPEN = "WAIT_OPEN"
    BUILD_OR = "BUILD_OR"
    WAIT_SIGNAL = "WAIT_SIGNAL"
    ENTRY_PENDING = "ENTRY_PENDING"
    IN_POSITION = "IN_POSITION"
    EXIT_PENDING = "EXIT_PENDING"
    DONE_TODAY = "DONE_TODAY"


@dataclass
class ORState:
    """Opening range values for a symbol.

    Attributes:
        or_high: Highest observed price within the opening range window.
        or_low: Lowest observed price within the opening range window.
    """

    or_high: float | None = None
    or_low: float | None = None

    def update(self, bar: Bar1m) -> None:
        """Update opening range bounds with a new bar.

        Args:
            bar: Latest 1-minute bar used to expand opening range bounds.

        Returns:
            None: Updates the instance in place.
        """

        if self.or_high is None:
            self.or_high = bar.high
            self.or_low = bar.low
            return
        self.or_high = max(self.or_high, bar.high)
        self.or_low = min(cast(float, self.or_low), bar.low)


@dataclass
class Signal:
    """Trading signal payload.

    Attributes:
        side: Trade direction to execute (for example, "BUY").
        symbol: Symbol to trade when a signal is generated.
        score: Composite score used for the decision (best-effort).
        reasons: Human-readable reason tags that contributed to the score.
        module: Source module/strategy name that generated the signal.
    """

    side: str | None = None  # "BUY"
    symbol: str | None = None
    score: float = 0.0
    reasons: list[str] | None = None
    module: str = "state_machine"


class StrategyStateMachine:
    """Finite state machine for opening-range and VWAP entry logic.

    Tracks session state, per-symbol opening range values, and current
    position context to produce actionable entry signals.
    """

    def __init__(self, logger: Logger | None = None) -> None:
        """Initialize strategy state and dependencies.

        Args:
            logger: Logger used for state transition and signal diagnostics.

        Returns:
            None: Initializes runtime state containers.
        """

        self.state: State = State.WAIT_OPEN
        self.or_state: dict[str, ORState] = {}
        # Multi-position canonical store keyed by symbol.
        self.positions: dict[str, Position] = {}
        # Backward-compatible single-position view (first active position).
        self.position: Position | None = None
        self.entry_pending_side: str | None = None
        self.logger: Logger | None = logger
        self.news_analyzer: NewsSentimentAnalyzer = NewsSentimentAnalyzer()

    def set_state(self, new_state: State) -> None:
        """Transition to a new strategy state.

        Args:
            new_state: Next lifecycle state to set.

        Returns:
            None: Updates internal state and emits transition log when changed.
        """

        if self.state != new_state:
            if self.logger:
                self.logger.info(f"state {self.state.value} -> {new_state.value}")
            self.state = new_state

    def _sync_legacy_position(self) -> None:
        """Keep legacy ``self.position`` aligned with ``self.positions``."""
        if self.positions:
            self.position = next(iter(self.positions.values()))
        else:
            self.position = None

    def set_position(self, pos: Position) -> None:
        """Insert or update a symbol position."""
        self.positions[str(pos.symbol)] = pos
        self._sync_legacy_position()

    def get_position(self, symbol: str) -> Position | None:
        """Return position for symbol when present."""
        return self.positions.get(str(symbol))

    def remove_position(self, symbol: str) -> Position | None:
        """Remove and return a symbol position if present."""
        out = self.positions.pop(str(symbol), None)
        self._sync_legacy_position()
        return out

    def active_position_count(self) -> int:
        """Return number of active positions."""
        if self.positions:
            return len(self.positions)
        return 1 if self.position is not None else 0

    def in_position(self, symbol: str | None = None) -> bool:
        """Check whether strategy currently holds an active position.

        Returns:
            bool: True when any position (or the requested symbol) exists.
        """
        if symbol is not None:
            sym = str(symbol)
            if sym in self.positions:
                return True
            return bool(self.position is not None and str(self.position.symbol) == sym)
        return self.active_position_count() > 0

    def update_or(self, symbol: str, bar: Bar1m) -> None:
        """Update opening range data for a symbol.

        Args:
            symbol: Ticker symbol whose opening range is being tracked.
            bar: Latest 1-minute bar for the symbol.

        Returns:
            None: Stores or updates opening range values for the symbol.
        """

        if symbol not in self.or_state:
            self.or_state[symbol] = ORState()
        self.or_state[symbol].update(bar)

    def evaluate_entry(
        self,
        bar: Bar1m,
        last_price: float,
        vwap: float | None,
        book: OrderBookTop,
        lever_symbol: str,
        inverse_symbol: str,
        max_spread_pct: float,
        indicators: dict[str, float] | None = None,
        market_regime: str = "NEUTRAL",  # BULL / BEAR / NEUTRAL
        min_score: float = 50.0,
    ) -> Signal:
        """Evaluate whether current market conditions trigger an entry signal.

        Args:
            bar: Current 1-minute bar data.
            last_price: Latest traded price.
            vwap: Current volume weighted average price.
            book: Top-of-book quote snapshot for the symbol.
            lever_symbol: Leveraged ETF symbol used for bullish exposure.
            inverse_symbol: Inverse ETF symbol used for bearish exposure.
            max_spread_pct: Maximum allowed bid-ask spread ratio.
            indicators: Technical and sentiment indicators used by filters.
            market_regime: Current market regime label (BULL/BEAR/NEUTRAL).

        Returns:
            Signal: BUY signal when entry criteria pass, otherwise an empty signal.
        """

        if self.state != State.WAIT_SIGNAL:
            return Signal()
        if vwap is None:
            return Signal()
        if book.ask <= 0 or book.bid <= 0:
            return Signal()

        spread_pct = (book.ask - book.bid) / book.ask
        if spread_pct > max_spread_pct:
            return Signal()

        indicators_data = cast(dict[str, float], indicators)

        or_state = self.or_state.get(book.symbol)
        if or_state is None or or_state.or_high is None or or_state.or_low is None:
            return Signal()

        # === 강화된 기술적 분석 필터 ===

        # 1. RSI 필터: 70 이상 과매수, 30 이하 과매도
        rsi_val = indicators_data.get("rsi", 50)
        if rsi_val >= 75:
            return Signal()  # 과매수 구간
        if rsi_val <= 25 and book.symbol != inverse_symbol:
            pass  # 과매도 구간은 반등 가능성

        # 2. MACD 필터: MACD > SignalLine = 골든크로스
        macd_line = indicators_data.get("macd_line", 0)
        macd_signal = indicators_data.get("macd_signal", 0)
        macd_hist = indicators_data.get("macd_hist", 0)

        # MACDHistogram이 양수이고 증가 추세 = 강세
        macd_bullish = macd_hist > 0 and macd_line > macd_signal

        # 3. Stochastic 필터

        # 4. ATR 필터: 변동성이 너무 낮으면 진입 금지 (횡보구간)
        atr_pct = indicators_data.get("atr_percent", 0)
        if atr_pct < 0.3 and book.symbol != inverse_symbol:
            return Signal()  # 변동성 부족

        # 5. VWAP 위치 필터
        vwap_distance = (last_price - vwap) / vwap * 100 if vwap else 0
        vwap_above = last_price > vwap  # VWAP 위 = 강세

        # --- 보조지표 필터링 (Indicators Filtering) ---
        if indicators_data:
            # 추세 필터: 가격이 20이평 위에 있어야 함
            ma20 = indicators_data.get("ma20", 0)
            if ma20 > 0 and last_price < ma20 and book.symbol != inverse_symbol:
                return Signal()

            # 거래량 필터: 거래량이 이평보다 터져야 함
            vol_ma20 = indicators_data.get("vol_ma20", 0)
            if vol_ma20 > 0 and bar.volume < vol_ma20:
                if book.symbol != inverse_symbol:
                    return Signal()

        # --- 시장 추세 필터 (Market Regime Filter) ---
        if market_regime == "BEAR":
            if book.symbol == inverse_symbol:
                pass
            else:
                if self.logger:
                    self.logger.info(
                        f"Entry Blocked by Market Regime (BEAR): {book.symbol}"
                    )
                return Signal()

        # --- Phase 2: Ultimate Hunter Strategy (복합 점수제) ---
        # 100점 만점에 50점 이상일 때만 진입

        score = 0
        reasons: list[str] = []

        # 1. [Trend] 당일 추세 & 정배열 (25점)
        prev_close = indicators_data.get("prev_close", 0)
        ma20 = indicators_data.get("ma20", 0)
        if prev_close > 0:
            day_return = (last_price - prev_close) / prev_close
            if day_return >= 0.005:
                score += 15
                reasons.append(f"UP_TREND({day_return:.1%})")

        if ma20 > 0 and last_price > ma20:
            score += 10
            reasons.append("ABOVE_MA20")

        # 2. [Strength] 체결강도 & VWAP 지지 (25점)
        vol_power = indicators_data.get("volume_power", 100.0)
        if vol_power >= 120:
            score += 15
            reasons.append(f"POWER({vol_power:.0f}%)")

        if vwap_above:
            score += 10
            reasons.append(f"VWAP_SUPPORT({vwap_distance:+.1f}%)")

        # 3. [Pattern] OR 돌파 또는 눌림목 양봉 (20점)
        is_breakout = bar.close > or_state.or_high
        is_pullback_bull = (last_price > ma20) and (bar.close > bar.open)

        if is_breakout:
            score += 20
            reasons.append("OR_BREAKOUT")
        elif is_pullback_bull:
            score += 20
            reasons.append("PULLBACK_BUY")

        # 4. [MACD] MACD 강세 확인 (10점)
        if macd_bullish:
            score += 10
            reasons.append(f"MACD_BULL(macd={macd_hist:.2f})")

        # 5. [Money Flow] 호가 잔량 (10점)
        if book.bid_size > book.ask_size * 0.8:
            score += 10
            reasons.append("BID_SUPPORT")

        # --- 뉴스 점수 반영 ---
        news_score = indicators_data.get("news_score", 0)

        if news_score >= 50:
            score += 20
            reasons.append(f"NEWS_HOT({news_score})")
        elif news_score >= 20:
            score += 10
            reasons.append("NEWS_GOOD")
        elif news_score <= -20:
            if book.symbol != inverse_symbol:
                if self.logger:
                    self.logger.info(
                        f"Entry Blocked by BAD NEWS: {book.symbol} ({news_score})"
                    )
                return Signal()

        # --- 감점 요인 (Risk Factors) ---
        body = abs(bar.close - bar.open)
        upper_wick = bar.high - max(bar.close, bar.open)
        if body > 0 and upper_wick > body * 2:
            score -= 30
            reasons.append("WICK_REJECT")

        if prev_close > 0 and (last_price / prev_close) > 1.20:
            score -= 50
            reasons.append("TOO_HIGH")

        # --- 최종 판정 (Decision) ---
        # min_score는 외부에서 동적으로 조정 가능(변동성/스프레드/시장국면 기반)
        if score >= float(min_score):
            target_symbol = (
                book.symbol
                if book.symbol not in [lever_symbol, inverse_symbol]
                else lever_symbol
            )
            if self.logger:
                self.logger.info(
                    f"HUNTER SIGNAL: {target_symbol} Score={score} Reasons={reasons}"
                )
            return Signal(side="BUY", symbol=target_symbol, score=float(score), reasons=list(reasons or []))
        else:
            if score > 0 and self.logger:
                self.logger.info(
                    f"[Score Fail] {book.symbol} Score={score}/{float(min_score):.0f} Reasons={reasons} (Price={last_price} VWAP={vwap:.0f})"
                )

        # [Pyramiding Logic] 불타기
        position = self.get_position(book.symbol)
        if position is None and self.position is not None and str(self.position.symbol) == book.symbol:
            position = cast(Position, self.position)
        if position is not None:
            pnl_pct = (last_price - position.avg_price) / position.avg_price
            if pnl_pct >= 0.02 and news_score >= 20 and position.adds < 1:
                if self.logger:
                    self.logger.info(
                        f"PYRAMIDING: {book.symbol} PnL={pnl_pct:.2%} News={news_score}"
                    )
                return Signal(side="BUY", symbol=book.symbol, score=float(score), reasons=list(reasons or ["PYRAMIDING"]))

        # 인버스(헷징) 로직
        elif bar.close < or_state.or_low and last_price <= vwap:
            if book.symbol == lever_symbol:
                return Signal(side="BUY", symbol=inverse_symbol, score=50.0, reasons=["OR_BREAKDOWN","VWAP_BEAR"])

        return Signal()
