"""Unit tests for strategy_state_machine module."""

import pytest
import sys
import os
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_state_machine import State, ORState, Signal, StrategyStateMachine
from models import Bar1m, OrderBookTop, Position


class TestORState:
    """Tests for ORState dataclass."""

    def test_default_values(self):
        """Test ORState default values."""
        or_state = ORState()
        assert or_state.or_high is None
        assert or_state.or_low is None

    def test_update_first_bar(self):
        """Test updating with first bar sets high and low."""
        bar = Bar1m(
            start=datetime(2024, 1, 1, 9, 0),
            open=10000,
            high=10500,
            low=9900,
            close=10300,
            volume=1000,
        )
        or_state = ORState()
        or_state.update(bar)

        assert or_state.or_high == 10500
        assert or_state.or_low == 9900

    def test_update_continues_high_low(self):
        """Test updating continues to track high and low."""
        or_state = ORState()
        or_state.or_high = 10500
        or_state.or_low = 9900

        bar = Bar1m(
            start=datetime(2024, 1, 1, 9, 1),
            open=10300,
            high=10700,
            low=10100,
            close=10600,
            volume=1500,
        )
        or_state.update(bar)

        assert or_state.or_high == 10700  # Updated
        assert or_state.or_low == 9900  # Unchanged


class TestSignal:
    """Tests for Signal dataclass."""

    def test_default_values(self):
        """Test Signal default values."""
        signal = Signal()
        assert signal.side is None
        assert signal.symbol is None

    def test_with_values(self):
        """Test Signal with values."""
        signal = Signal(side="BUY", symbol="005930")
        assert signal.side == "BUY"
        assert signal.symbol == "005930"


class TestStrategyStateMachine:
    """Tests for StrategyStateMachine class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.sm = StrategyStateMachine()

    def test_initial_state(self):
        """Test initial state is WAIT_OPEN."""
        assert self.sm.state == State.WAIT_OPEN
        assert self.sm.position is None

    def test_set_state(self):
        """Test set_state changes state."""
        self.sm.set_state(State.BUILD_OR)
        assert self.sm.state == State.BUILD_OR

    def test_in_position_no_position(self):
        """Test in_position returns False when no position."""
        assert self.sm.in_position() is False

    def test_in_position_with_position(self):
        """Test in_position returns True with position."""
        self.sm.set_state(State.IN_POSITION)
        self.sm.position = Position(symbol="005930", qty=10, avg_price=10000)
        assert self.sm.in_position() is True

    def test_update_or(self):
        """Test update_or creates OR state for symbol."""
        bar = Bar1m(
            start=datetime(2024, 1, 1, 9, 0),
            open=10000,
            high=10500,
            low=9900,
            close=10300,
            volume=1000,
        )
        self.sm.update_or("005930", bar)

        assert "005930" in self.sm.or_state
        assert self.sm.or_state["005930"].or_high == 10500
        assert self.sm.or_state["005930"].or_low == 9900

    def test_evaluate_entry_wrong_state(self):
        """Test evaluate_entry returns empty signal when not in WAIT_SIGNAL."""
        self.sm.set_state(State.WAIT_OPEN)

        result = self.sm.evaluate_entry(
            bar=Bar1m(datetime(2024, 1, 1, 9, 5), 10000, 10500, 9900, 10300, 1000),
            last_price=10300,
            vwap=10200,
            book=OrderBookTop(
                symbol="005930",
                bid=10300,
                ask=10310,
                bid_size=100,
                ask_size=100,
                timestamp=datetime.now(),
            ),
            lever_symbol="122630",
            inverse_symbol="114800",
            max_spread_pct=0.005,
        )

        assert result.side is None

    def test_evaluate_entry_no_vwap(self):
        """Test evaluate_entry returns empty signal when vwap is None."""
        self.sm.set_state(State.WAIT_SIGNAL)

        result = self.sm.evaluate_entry(
            bar=Bar1m(datetime(2024, 1, 1, 9, 5), 10000, 10500, 9900, 10300, 1000),
            last_price=10300,
            vwap=None,
            book=OrderBookTop(
                symbol="005930",
                bid=10300,
                ask=10310,
                bid_size=100,
                ask_size=100,
                timestamp=datetime.now(),
            ),
            lever_symbol="122630",
            inverse_symbol="114800",
            max_spread_pct=0.005,
        )

        assert result.side is None

    def test_evaluate_entry_spread_too_high(self):
        """Test evaluate_entry returns empty signal when spread too high."""
        self.sm.set_state(State.WAIT_SIGNAL)

        # High spread (5%)
        result = self.sm.evaluate_entry(
            bar=Bar1m(datetime(2024, 1, 1, 9, 5), 10000, 10500, 9900, 10300, 1000),
            last_price=10300,
            vwap=10200,
            book=OrderBookTop(
                symbol="005930",
                bid=10000,
                ask=10500,
                bid_size=100,
                ask_size=100,
                timestamp=datetime.now(),
            ),
            lever_symbol="122630",
            inverse_symbol="114800",
            max_spread_pct=0.005,
        )

        assert result.side is None

    def test_evaluate_entry_no_or_state(self):
        """Test evaluate_entry returns empty signal when no OR state."""
        self.sm.set_state(State.WAIT_SIGNAL)

        result = self.sm.evaluate_entry(
            bar=Bar1m(datetime(2024, 1, 1, 9, 5), 10000, 10500, 9900, 10300, 1000),
            last_price=10300,
            vwap=10200,
            book=OrderBookTop(
                symbol="005930",
                bid=10300,
                ask=10310,
                bid_size=100,
                ask_size=100,
                timestamp=datetime.now(),
            ),
            lever_symbol="122630",
            inverse_symbol="114800",
            max_spread_pct=0.005,
        )

        assert result.side is None

    def test_evaluate_entry_with_indicators_rsi_overbought(self):
        """Test entry blocked when RSI overbought."""
        self.sm.set_state(State.WAIT_SIGNAL)

        # Set up OR state
        bar = Bar1m(
            start=datetime(2024, 1, 1, 9, 0),
            open=10000,
            high=10500,
            low=9900,
            close=10300,
            volume=1000,
        )
        self.sm.update_or("005930", bar)

        # Test with high RSI (overbought)
        result = self.sm.evaluate_entry(
            bar=Bar1m(datetime(2024, 1, 1, 9, 5), 10300, 10600, 10200, 10500, 1500),
            last_price=10500,
            vwap=10400,
            book=OrderBookTop(
                symbol="005930",
                bid=10500,
                ask=10510,
                bid_size=100,
                ask_size=100,
                timestamp=datetime.now(),
            ),
            lever_symbol="122630",
            inverse_symbol="114800",
            max_spread_pct=0.005,
            indicators={"rsi": 75, "ma20": 10000, "vol_ma20": 1000},
        )

        assert result.side is None  # Blocked by RSI

    def test_evaluate_entry_breakout_signal(self):
        """Test entry signal generated on breakout."""
        self.sm.set_state(State.WAIT_SIGNAL)

        # Set up OR state
        or_bar = Bar1m(
            start=datetime(2024, 1, 1, 9, 0),
            open=10000,
            high=10500,
            low=9900,
            close=10300,
            volume=1000,
        )
        self.sm.update_or("005930", or_bar)

        # Price breaks out above OR high with good indicators
        result = self.sm.evaluate_entry(
            bar=Bar1m(datetime(2024, 1, 1, 9, 5), 10500, 10700, 10400, 10600, 2000),
            last_price=10600,
            vwap=10500,
            book=OrderBookTop(
                symbol="005930",
                bid=10600,
                ask=10610,
                bid_size=150,
                ask_size=100,
                timestamp=datetime.now(),
            ),
            lever_symbol="122630",
            inverse_symbol="114800",
            max_spread_pct=0.005,
            indicators={
                "rsi": 45,
                "ma20": 10500,
                "vol_ma20": 1500,
                "volume_power": 150,
                "prev_close": 10000,
                "news_score": 0,
                "macd_line": 100,
                "macd_signal": 80,
                "macd_hist": 20,
                "stochastic": 60,
                "atr_percent": 1.5,
            },
        )

        # Should generate signal (score >= 50)
        assert result.side == "BUY"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
