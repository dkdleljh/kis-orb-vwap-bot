"""Unit tests for risk_manager module."""

import pytest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_manager import RiskManager, RiskState


class TestRiskState:
    """Tests for RiskState dataclass."""

    def test_default_values(self):
        """Test that RiskState has correct default values."""
        state = RiskState()
        assert state.entries_today == 0
        assert state.daily_pnl_pct == 0.0
        assert state.consecutive_stop == 0
        assert state.done_today is False

    def test_custom_values(self):
        """Test RiskState with custom values."""
        state = RiskState(
            entries_today=5, daily_pnl_pct=-0.03, consecutive_stop=2, done_today=False
        )
        assert state.entries_today == 5
        assert state.daily_pnl_pct == -0.03
        assert state.consecutive_stop == 2
        assert state.done_today is False


class TestRiskManager:
    """Tests for RiskManager class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.risk = RiskManager(
            max_entries=10, daily_loss_limit_pct=-0.05, max_consecutive_stop=3
        )

    def test_can_enter_initially_true(self):
        """Test that can_enter returns True initially."""
        assert self.risk.can_enter() is True

    def test_can_enter_max_entries_reached(self):
        """Test can_enter returns False when max entries reached."""
        # Record 10 entries
        for _ in range(10):
            self.risk.record_entry()

        assert self.risk.can_enter() is False
        assert self.risk.state.done_today is True

    def test_can_enter_daily_loss_limit(self):
        """Test can_enter returns False when daily loss limit exceeded."""
        # Record a large loss (-6%)
        self.risk.record_exit(pnl_pct=-0.06, is_stop=True)

        assert self.risk.can_enter() is False
        assert self.risk.state.done_today is True

    def test_can_enter_consecutive_stops(self):
        """Test can_enter returns False when consecutive stops exceeded."""
        # Record 3 consecutive stop losses
        for _ in range(3):
            self.risk.record_exit(pnl_pct=-0.02, is_stop=True)

        assert self.risk.can_enter() is False
        assert self.risk.state.done_today is True

    def test_can_enter_done_today_true(self):
        """Test can_enter returns False when done_today is True."""
        self.risk.force_done()

        assert self.risk.can_enter() is False

    def test_record_entry_increments(self):
        """Test record_entry increments entries_today."""
        initial = self.risk.state.entries_today
        self.risk.record_entry()
        assert self.risk.state.entries_today == initial + 1

    def test_record_entry_multiple(self):
        """Test multiple record_entry calls."""
        for _ in range(5):
            self.risk.record_entry()
        assert self.risk.state.entries_today == 5

    def test_record_exit_profit(self):
        """Test record_exit with profit resets consecutive_stop."""
        # First record a stop loss
        self.risk.record_exit(pnl_pct=-0.02, is_stop=True)
        assert self.risk.state.consecutive_stop == 1

        # Then record a profit
        self.risk.record_exit(pnl_pct=0.03, is_stop=False)

        assert abs(self.risk.state.daily_pnl_pct - 0.01) < 0.0001  # -0.02 + 0.03
        assert self.risk.state.consecutive_stop == 0  # Reset

    def test_record_exit_stop_loss_increments(self):
        """Test record_exit with stop loss increments consecutive_stop."""
        self.risk.record_exit(pnl_pct=-0.02, is_stop=True)

        assert self.risk.state.consecutive_stop == 1
        assert self.risk.state.daily_pnl_pct == -0.02

    def test_record_exit_multiple_stops(self):
        """Test multiple stop losses."""
        for _ in range(3):
            self.risk.record_exit(pnl_pct=-0.01, is_stop=True)

        assert self.risk.state.consecutive_stop == 3

    def test_force_done(self):
        """Test force_done sets done_today to True."""
        assert self.risk.state.done_today is False

        self.risk.force_done()

        assert self.risk.state.done_today is True
        assert self.risk.can_enter() is False

    def test_custom_limits(self):
        """Test RiskManager with custom limits."""
        risk = RiskManager(
            max_entries=5, daily_loss_limit_pct=-0.02, max_consecutive_stop=2
        )

        # Should fail with 2% loss
        risk.record_exit(pnl_pct=-0.02, is_stop=True)
        assert risk.can_enter() is False

    def test_profit_does_not_trigger_limit(self):
        """Test that profit doesn't trigger any limits."""
        self.risk.record_exit(pnl_pct=0.05, is_stop=False)

        assert self.risk.can_enter() is True
        assert self.risk.state.done_today is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
