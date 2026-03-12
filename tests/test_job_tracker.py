"""Unit tests for skills/job_tracker.py — date parser, sanitizers, note renderer."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills.job_tracker import parse_shooting_dates


class TestParseShootingDates:
    """Flexible date parser for Holded 'Shooting Dates:' line item descriptions."""

    def test_single_date(self):
        assert parse_shooting_dates("17/3", 2026) == ["2026-03-17"]

    def test_range_same_month(self):
        result = parse_shooting_dates("17/3-21/3", 2026)
        assert result == ["2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20", "2026-03-21"]

    def test_short_range_shared_month(self):
        assert parse_shooting_dates("17-18/3", 2026) == ["2026-03-17", "2026-03-18"]

    def test_comma_separated(self):
        assert parse_shooting_dates("17/3, 19/3, 21/3", 2026) == [
            "2026-03-17", "2026-03-19", "2026-03-21"
        ]

    def test_list_with_trailing_month(self):
        assert parse_shooting_dates("17, 18, 21/3", 2026) == [
            "2026-03-17", "2026-03-18", "2026-03-21"
        ]

    def test_cross_month_range(self):
        result = parse_shooting_dates("30/3-2/4", 2026)
        assert result == ["2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02"]

    def test_cross_year_range(self):
        result = parse_shooting_dates("28/12-3/1", 2026)
        assert result[0] == "2026-12-28"
        assert result[-1] == "2027-01-03"
        assert len(result) == 7

    def test_empty_string(self):
        assert parse_shooting_dates("", 2026) == []

    def test_none(self):
        assert parse_shooting_dates(None, 2026) == []

    def test_garbage_input(self):
        assert parse_shooting_dates("not a date", 2026) == []

    def test_whitespace_handling(self):
        assert parse_shooting_dates("  17/3 - 19/3  ", 2026) == [
            "2026-03-17", "2026-03-18", "2026-03-19"
        ]

    def test_deduplication(self):
        result = parse_shooting_dates("17/3, 17/3", 2026)
        assert result == ["2026-03-17"]
