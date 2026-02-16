"""Unit tests for time utilities module."""

import pytest
import sys
import os
from datetime import datetime, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils_time import TimeRules, parse_time, now_local, is_after, is_between


class TestTimeRules:
    """Tests for TimeRules dataclass."""

    def test_time_rules_creation(self):
        """Test TimeRules can be created."""
        rules = TimeRules(
            observe_start=dt_time(8, 59, 0),
            or_start=dt_time(9, 0, 0),
            or_end=dt_time(9, 5, 0),
            entry_start=dt_time(9, 5, 5),
            force_exit=dt_time(15, 15, 0),
        )
        assert rules.observe_start == dt_time(8, 59, 0)
        assert rules.force_exit == dt_time(15, 15, 0)

    def test_time_rules_defaults(self):
        """Test TimeRules with default values."""
        rules = TimeRules(
            observe_start=dt_time(9, 0, 0),
            or_start=dt_time(9, 0, 0),
            or_end=dt_time(9, 5, 0),
            entry_start=dt_time(9, 5, 0),
            force_exit=dt_time(15, 0, 0),
        )
        assert rules is not None


class TestParseTime:
    """Tests for parse_time function."""

    def test_parse_time_hh_mm_ss(self):
        """Test parsing HH:MM:SS format."""
        result = parse_time("15:30:45")
        assert result == dt_time(15, 30, 45)

    def test_parse_time_zero(self):
        """Test parsing midnight."""
        result = parse_time("00:00:00")
        assert result == dt_time(0, 0, 0)

    def test_parse_time_single_digits(self):
        """Test parsing single digit times."""
        result = parse_time("09:05:00")
        assert result == dt_time(9, 5, 0)


class TestNowLocal:
    """Tests for now_local function."""

    def test_now_local_korea(self):
        """Test now_local returns datetime with Korea timezone."""
        result = now_local("Asia/Seoul")
        assert result.tzinfo is not None
        assert result.tzinfo.key == "Asia/Seoul"

    def test_now_local_utc(self):
        """Test now_local returns datetime with UTC timezone."""
        result = now_local("UTC")
        assert result.tzinfo is not None

    def test_now_local_returns_datetime(self):
        """Test now_local returns datetime object."""
        result = now_local("Asia/Seoul")
        assert isinstance(result, datetime)


class TestIsAfter:
    """Tests for is_after function."""

    def test_is_after_true(self):
        """Test is_after returns True when time has passed."""
        test_time = dt_time(12, 0, 0)
        test_dt = datetime(2026, 3, 15, 12, 30, 0)
        assert is_after(test_time, test_dt) is True

    def test_is_after_false(self):
        """Test is_after returns False when time hasn't passed."""
        test_time = dt_time(15, 0, 0)
        test_dt = datetime(2026, 3, 15, 12, 30, 0)
        assert is_after(test_time, test_dt) is False

    def test_is_after_exact_match(self):
        """Test is_after returns True at exact match."""
        test_time = dt_time(12, 30, 0)
        test_dt = datetime(2026, 3, 15, 12, 30, 0)
        assert is_after(test_time, test_dt) is True


class TestIsBetween:
    """Tests for is_between function."""

    def test_is_between_true(self):
        """Test is_between returns True when within range."""
        start = dt_time(9, 0, 0)
        end = dt_time(15, 0, 0)
        test_dt = datetime(2026, 3, 15, 12, 30, 0)
        assert is_between(start, end, test_dt) is True

    def test_is_between_before_start(self):
        """Test is_between returns False before start."""
        start = dt_time(9, 0, 0)
        end = dt_time(15, 0, 0)
        test_dt = datetime(2026, 3, 15, 8, 30, 0)
        assert is_between(start, end, test_dt) is False

    def test_is_between_after_end(self):
        """Test is_between returns False after end."""
        start = dt_time(9, 0, 0)
        end = dt_time(15, 0, 0)
        test_dt = datetime(2026, 3, 15, 16, 0, 0)
        assert is_between(start, end, test_dt) is False

    def test_is_between_at_start(self):
        """Test is_between returns True at exact start."""
        start = dt_time(9, 0, 0)
        end = dt_time(15, 0, 0)
        test_dt = datetime(2026, 3, 15, 9, 0, 0)
        assert is_between(start, end, test_dt) is True

    def test_is_between_at_end(self):
        """Test is_between returns False at exact end (exclusive)."""
        start = dt_time(9, 0, 0)
        end = dt_time(15, 0, 0)
        test_dt = datetime(2026, 3, 15, 15, 0, 0)
        assert is_between(start, end, test_dt) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
