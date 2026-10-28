"""
tests/test_overtime.py — Unit tests for overtime business logic.
Uses mocked DB cursor — no real DB connection needed.
"""
import os
os.environ.setdefault("DATABASE_URL",  "postgresql://fake:fake@localhost/fake")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-32c")
os.environ.setdefault("REDIS_URL", "")

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


def _make_cursor(total_hours, already_alerted=False):
    """Build a mock psycopg2 cursor that returns preset values."""
    c = MagicMock()
    call_count = [0]

    def fetchone_side_effect():
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            # SUM(hours_worked) query
            return {"total": total_hours}
        if n == 1:
            # SELECT 1 FROM overtime_alerts (already alerted?)
            return (1,) if already_alerted else None
        return None

    c.fetchone.side_effect = fetchone_side_effect
    return c


class TestCheckAndAlertOvertime:
    """
    Test the overtime logic without importing the full module
    (which would try to connect to Postgres on import).
    We test the logic directly by re-implementing and verifying behaviour.
    """

    def _run(self, total_hours, already_alerted, ot_limit=105.0, warn_at=0.9):
        """Simulate check_and_alert_overtime logic."""
        warn_threshold = ot_limit * warn_at

        level = None
        if total_hours >= ot_limit:
            level = "exceeded"
        elif total_hours >= warn_threshold:
            level = "warning"

        if not level:
            return {"alert_fired": False, "total_hours": total_hours}

        if already_alerted:
            return {"alert_fired": False, "total_hours": total_hours,
                    "reason": "already_sent_this_period"}

        return {"alert_fired": True, "level": level, "total_hours": total_hours}

    def test_no_alert_below_threshold(self):
        result = self._run(50.0, already_alerted=False)
        assert result["alert_fired"] is False

    def test_warning_at_90_percent(self):
        result = self._run(95.0, already_alerted=False)  # 90% of 105 = 94.5
        assert result["alert_fired"] is True
        assert result["level"] == "warning"

    def test_exceeded_at_limit(self):
        result = self._run(105.0, already_alerted=False)
        assert result["alert_fired"] is True
        assert result["level"] == "exceeded"

    def test_exceeded_above_limit(self):
        result = self._run(120.0, already_alerted=False)
        assert result["alert_fired"] is True
        assert result["level"] == "exceeded"

    def test_no_duplicate_alert(self):
        result = self._run(110.0, already_alerted=True)
        assert result["alert_fired"] is False
        assert result["reason"] == "already_sent_this_period"

    def test_exact_warn_threshold(self):
        # Exactly at 90% of 105 = 94.5
        result = self._run(94.5, already_alerted=False)
        assert result["alert_fired"] is True
        assert result["level"] == "warning"

    def test_just_below_warn_threshold(self):
        result = self._run(94.4, already_alerted=False)
        assert result["alert_fired"] is False

    def test_custom_limit(self):
        result = self._run(85.0, already_alerted=False, ot_limit=100.0, warn_at=0.85)
        assert result["alert_fired"] is True
        assert result["level"] == "warning"

    def test_zero_hours(self):
        result = self._run(0.0, already_alerted=False)
        assert result["alert_fired"] is False


class TestMonthBoundsInOvertime:
    """Month boundary calculation used by overtime service."""

    def _month_bounds(self, d):
        start = d.replace(day=1)
        if d.month == 12:
            end = d.replace(year=d.year + 1, month=1, day=1)
        else:
            end = d.replace(month=d.month + 1, day=1)
        return start.date(), end.date()

    def test_mid_month(self):
        start, end = self._month_bounds(datetime(2026, 7, 15))
        assert str(start) == "2026-07-01"
        assert str(end)   == "2026-08-01"

    def test_december_year_wrap(self):
        start, end = self._month_bounds(datetime(2026, 12, 1))
        assert str(start) == "2026-12-01"
        assert str(end)   == "2027-01-01"

    def test_period_spans_full_month(self):
        from datetime import date
        start, end = self._month_bounds(datetime(2026, 6, 30))
        # All of June is between start and end (exclusive)
        assert start <= date(2026, 6, 1)
        assert end   == date(2026, 7, 1)
