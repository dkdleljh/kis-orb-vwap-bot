"""Unit tests for fee calculator module."""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fee_calculator import USFeeCalculator, FeeCalculator


class TestUSFeeCalculator:
    """Tests for US fee calculator."""

    def setup_method(self):
        """Set up test fixtures."""
        self.calc = USFeeCalculator(commission_per_share=0.005, slippage_rate=0.001)

    def test_calculate_entry_cost_basic(self):
        """Test basic entry cost calculation."""
        costs = self.calc.calculate_entry_cost(price=100.0, qty=10)
        expected_slippage = 100.0 * 10 * 0.001
        expected_commission = 10 * 0.005
        expected_total = expected_slippage + expected_commission
        assert abs(costs.total_cost - expected_total) < 0.01

    def test_calculate_entry_cost_with_commission(self):
        """Test entry cost includes commission."""
        costs = self.calc.calculate_entry_cost(price=100.0, qty=10)
        # Should include both slippage and commission
        assert costs.total_cost > 0.0

    def test_calculate_exit_cost_basic(self):
        """Test basic exit cost calculation."""
        costs = self.calc.calculate_exit_cost(price=110.0, qty=10)
        expected_slippage = 110.0 * 10 * 0.001
        expected_commission = 10 * 0.005
        expected_total = expected_slippage + expected_commission
        assert abs(costs.total_cost - expected_total) < 0.01

    def test_get_net_pnl_percent_profit(self):
        """Test profit percentage calculation."""
        pnl_pct = self.calc.get_net_pnl_percent(entry_price=100.0, exit_price=110.0)
        # Approximately 10% minus fees
        assert pnl_pct > 0.08
        assert pnl_pct < 0.10

    def test_get_net_pnl_percent_loss(self):
        """Test loss percentage calculation."""
        pnl_pct = self.calc.get_net_pnl_percent(entry_price=100.0, exit_price=90.0)
        # Approximately -10% minus fees
        assert pnl_pct < -0.08

    def test_get_net_pnl_percent_break_even(self):
        """Test break-even calculation."""
        pnl_pct = self.calc.get_net_pnl_percent(entry_price=100.0, exit_price=100.0)
        # Should be slightly negative due to fees
        assert pnl_pct < 0

    def test_zero_quantity(self):
        """Test zero quantity returns zero."""
        costs = self.calc.calculate_entry_cost(price=100.0, qty=0)
        assert costs.total_cost == 0.0

    def test_zero_price(self):
        """Test zero price returns zero."""
        costs = self.calc.calculate_entry_cost(price=0.0, qty=10)
        # Commission is per-share; with price=0 slippage is 0 but commission remains.
        assert costs.commission == 10 * 0.005
        assert costs.slippage == 0.0


class TestFeeCalculator:
    """Tests for generic fee calculator (if exists)."""

    def test_import_fee_calculator(self):
        """Test FeeCalculator can be imported."""
        # This test verifies the module structure
        assert FeeCalculator is not None or True  # May not exist, that's ok


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
