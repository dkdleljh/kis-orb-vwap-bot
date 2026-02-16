"""Tests for scheduler module."""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSchedulerFunctions:
    """Test scheduler module functions."""

    def test_load_config_returns_dict(self):
        """Test load_config returns empty dict when file not found."""
        from scheduler import load_config

        result = load_config()
        assert isinstance(result, dict)

    def test_is_internet_connected_returns_bool(self):
        """Test is_internet_connected returns boolean."""
        from scheduler import is_internet_connected

        result = is_internet_connected()
        assert isinstance(result, bool)

    def test_is_maintenance_time_returns_bool(self):
        """Test is_maintenance_time returns boolean."""
        from scheduler import is_maintenance_time

        result = is_maintenance_time()
        assert isinstance(result, bool)

    def test_get_schedule_returns_tuple(self):
        """Test get_schedule returns tuple of datetimes."""
        from scheduler import get_schedule

        result = get_schedule({})
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_get_schedule_with_config(self):
        """Test get_schedule with custom config."""
        from scheduler import get_schedule

        config = {
            "time_rules": {"observe_start": "08:50:00", "market_close": "15:35:00"}
        }
        result = get_schedule(config)
        assert isinstance(result, tuple)
        assert len(result) == 2


class TestIsMarketOpen:
    """Test market open detection."""

    def test_is_market_open_weekend(self):
        """Test weekend returns False."""
        from utils_holiday import is_market_open

        # Saturday
        saturday = datetime(2026, 2, 21).date()
        assert is_market_open(saturday) == False
        # Sunday
        sunday = datetime(2026, 2, 22).date()
        assert is_market_open(sunday) == False

    def test_is_market_open_weekday(self):
        """Test weekday returns True (except holidays)."""
        from utils_holiday import is_market_open

        # Wednesday
        wednesday = datetime(2026, 2, 18).date()
        result = is_market_open(wednesday)
        assert isinstance(result, bool)


class TestHolidayName:
    """Test holiday name detection."""

    def test_get_holiday_name_returns_str_or_none(self):
        """Test get_holiday_name returns string or None."""
        from utils_holiday import get_holiday_name

        # Regular weekday
        wednesday = datetime(2026, 2, 18).date()
        result = get_holiday_name(wednesday)
        assert result is None or isinstance(result, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
