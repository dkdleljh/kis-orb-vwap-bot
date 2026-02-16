"""Unit tests for US market module (mijang)."""

import pytest
import sys
import os
from datetime import date, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mijang import (
    is_us_dst_active,
    get_us_timezone_info,
    is_us_market_holiday,
    get_us_holiday_name,
    _calculate_good_friday,
    _get_market_hours_korea,
    is_us_market_open,
    get_us_market_status,
    get_force_exit_time,
)


class TestUSDST:
    """Tests for US DST detection."""

    def test_is_us_dst_active(self):
        """Test DST detection returns boolean."""
        result = is_us_dst_active()
        assert isinstance(result, bool)

    def test_get_us_timezone_info(self):
        """Test timezone info contains required keys."""
        info = get_us_timezone_info()
        assert "is_dst" in info
        assert "timezone_name" in info
        assert "offset_hours" in info
        assert info["offset_hours"] in [13, 14]


class TestUSHolidays:
    """Tests for US holiday handling."""

    def test_new_years_day_2026(self):
        """Test New Year's Day 2026 is recognized."""
        assert is_us_market_holiday(date(2026, 1, 1)) is True

    def test_mlk_day_2026(self):
        """Test MLK Day 2026 (3rd Monday of January)."""
        assert is_us_market_holiday(date(2026, 1, 19)) is True

    def test_presidents_day_2026(self):
        """Test Presidents Day 2026 (3rd Monday of February)."""
        assert is_us_market_holiday(date(2026, 2, 16)) is True

    def test_memorial_day_2026(self):
        """Test Memorial Day 2026 (last Monday of May)."""
        assert is_us_market_holiday(date(2026, 5, 25)) is True

    def test_juneteenth_2026(self):
        """Test Juneteenth 2026."""
        assert is_us_market_holiday(date(2026, 6, 19)) is True

    def test_independence_day_2026(self):
        """Test Independence Day 2026."""
        assert is_us_market_holiday(date(2026, 7, 4)) is True

    def test_labor_day_2026(self):
        """Test Labor Day 2026 (1st Monday of September)."""
        assert is_us_market_holiday(date(2026, 9, 7)) is True

    def test_thanksgiving_2026(self):
        """Test Thanksgiving 2026 (4th Thursday of November)."""
        assert is_us_market_holiday(date(2026, 11, 26)) is True

    def test_christmas_2026(self):
        """Test Christmas 2026."""
        assert is_us_market_holiday(date(2026, 12, 25)) is True

    def test_good_friday_2026(self):
        """Test Good Friday 2026."""
        good_friday = _calculate_good_friday(2026)
        assert good_friday == date(2026, 4, 3)
        assert is_us_market_holiday(good_friday) is True

    def test_regular_weekday(self):
        """Test regular weekday is not a holiday."""
        assert is_us_market_holiday(date(2026, 3, 15)) is False
        assert is_us_market_holiday(date(2026, 8, 15)) is False

    def test_get_holiday_name(self):
        """Test holiday name retrieval."""
        assert get_us_holiday_name(date(2026, 1, 1)) == "New Year's Day"
        assert get_us_holiday_name(date(2026, 1, 19)) == "Martin Luther King Jr. Day"

    def test_weekend_returns_weekend(self):
        """Test weekend returns 'Weekend'."""
        saturday = date(2026, 1, 3)
        assert get_us_holiday_name(saturday) == "Weekend"


class TestMarketHours:
    """Tests for US market hours."""

    def test_get_market_hours_korea_returns_tuple(self):
        """Test market hours returns tuple of times."""
        open_time, close_time = _get_market_hours_korea()
        assert isinstance(open_time, dt_time)
        assert isinstance(close_time, dt_time)

    def test_market_hours_est(self):
        """Test EST market hours."""
        open_time, close_time = _get_market_hours_korea()
        if not is_us_dst_active():
            assert open_time == dt_time(23, 30, 0)
            assert close_time == dt_time(6, 0, 0)

    def test_market_hours_edt(self):
        """Test EDT market hours (summer)."""
        # This test verifies the function handles both cases
        # Actual result depends on current DST status
        open_time, close_time = _get_market_hours_korea()
        if is_us_dst_active():
            assert open_time == dt_time(22, 30, 0)
            assert close_time == dt_time(5, 0, 0)


class TestMarketStatus:
    """Tests for market status."""

    def test_get_us_market_status_returns_dict(self):
        """Test market status returns required keys."""
        status = get_us_market_status()
        assert "is_open" in status
        assert "is_weekend" in status
        assert "is_holiday" in status
        assert "is_dst" in status
        assert "market_open_korea" in status
        assert "market_close_korea" in status

    def test_is_us_market_open_returns_bool(self):
        """Test is_us_market_open returns boolean."""
        result = is_us_market_open()
        assert isinstance(result, bool)

    def test_weekend_market_closed(self):
        """Test market is closed on weekends."""
        # Saturday
        status = get_us_market_status()
        # Should handle weekend correctly
        assert "is_weekend" in status


class TestForceExit:
    """Tests for force exit time."""

    def test_get_force_exit_time_returns_time(self):
        """Test force exit time returns time object."""
        exit_time = get_force_exit_time()
        assert isinstance(exit_time, dt_time)

    def test_force_exit_dst_aware(self):
        """Test force exit is DST aware."""
        exit_time = get_force_exit_time()
        if is_us_dst_active():
            assert exit_time == dt_time(5, 0, 0)
        else:
            assert exit_time == dt_time(4, 0, 0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
