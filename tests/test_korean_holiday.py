"""Unit tests for Korean holiday module."""

import pytest
import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils_holiday import is_market_open, get_holiday_name


class TestKoreanHolidays:
    """Tests for Korean holiday handling."""

    def test_new_years_day_2026(self):
        """Test New Year's Day 2026."""
        assert is_market_open(date(2026, 1, 1)) is False

    def test_samil_independence_day(self):
        """Test Samil Independence Movement Day."""
        assert is_market_open(date(2026, 3, 1)) is False

    def test_children_day(self):
        """Test Children's Day."""
        assert is_market_open(date(2026, 5, 5)) is False

    def test_buddhas_birthday(self):
        """Test Buddha's Birthday (佛誕節) - varies by lunar calendar."""
        # 2026-05-24 is Buddha's Birthday, and 2026-05-25 is substitute holiday
        assert is_market_open(date(2026, 5, 24)) is False
        assert is_market_open(date(2026, 5, 25)) is False

    def test_memorial_day_korea(self):
        """Test Memorial Day."""
        assert is_market_open(date(2026, 6, 6)) is False

    def test_liberation_day(self):
        """Test Liberation Day."""
        assert is_market_open(date(2026, 8, 15)) is False

    def test_chuseok(self):
        """Test Chuseok (추석) - varies by lunar calendar."""
        # Sept 25, 2026 is Chuseok
        result = is_market_open(date(2026, 9, 25))
        assert result is False

    def test_national_founding_day(self):
        """Test National Founding Day."""
        assert is_market_open(date(2026, 10, 3)) is False

    def test_hangul_day(self):
        """Test Hangul Day."""
        assert is_market_open(date(2026, 10, 9)) is False

    def test_christmas(self):
        """Test Christmas."""
        assert is_market_open(date(2026, 12, 25)) is False

    def test_labor_day_korea(self):
        """Test Labor Day (May 1)."""
        assert is_market_open(date(2026, 5, 1)) is False

    def test_regular_weekday(self):
        """Test regular weekday is market open."""
        # March 16, 2026 is Monday
        assert is_market_open(date(2026, 3, 16)) is True

    def test_saturday(self):
        """Test Saturday is closed."""
        assert is_market_open(date(2026, 3, 21)) is False

    def test_sunday(self):
        """Test Sunday is closed."""
        assert is_market_open(date(2026, 3, 22)) is False


class TestGetHolidayName:
    """Tests for holiday name retrieval."""

    def test_new_years_day_name(self):
        """Test New Year's Day name."""
        name = get_holiday_name(date(2026, 1, 1))
        assert name is not None
        assert "New Year" in name or "신정" in name

    def test_saturday_returns_weekend(self):
        """Test Saturday returns 'Weekend'."""
        assert get_holiday_name(date(2026, 3, 21)) == "Weekend"

    def test_sunday_returns_weekend(self):
        """Test Sunday returns 'Weekend'."""
        assert get_holiday_name(date(2026, 3, 22)) == "Weekend"

    def test_regular_day_returns_none(self):
        """Test regular day returns None."""
        assert get_holiday_name(date(2026, 3, 16)) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
