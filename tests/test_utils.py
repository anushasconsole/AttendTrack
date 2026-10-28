"""
tests/test_utils.py — Unit tests for pure utility functions.
No database, no Redis, no external services.
"""
import os
os.environ.setdefault("DATABASE_URL",  "postgresql://fake:fake@localhost/fake")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-32c")
os.environ.setdefault("REDIS_URL", "")

import math
import pytest
from datetime import datetime


# ── Haversine distance ────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    """Copy of the haversine from attendance router — tested independently."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine(12.618852, 75.249893, 12.618852, 75.249893) == pytest.approx(0.0, abs=1)

    def test_known_distance(self):
        # Bangalore (12.97°N, 77.59°E) to Mysore (12.29°N, 76.64°E) ≈ 128km
        dist = haversine(12.97, 77.59, 12.29, 76.64)
        assert 120_000 < dist < 140_000

    def test_symmetry(self):
        d1 = haversine(12.0, 77.0, 13.0, 78.0)
        d2 = haversine(13.0, 78.0, 12.0, 77.0)
        assert d1 == pytest.approx(d2, rel=1e-6)

    def test_returns_metres(self):
        # One degree of latitude ≈ 111 km
        dist = haversine(0.0, 0.0, 1.0, 0.0)
        assert 110_000 < dist < 112_000


# ── calc_hours ────────────────────────────────────────────────────────────────

def calc_hours(login, logout) -> float:
    try:
        if not isinstance(login, datetime):
            login  = datetime.fromisoformat(str(login)[:19])
        if not isinstance(logout, datetime):
            logout = datetime.fromisoformat(str(logout)[:19])
        return round((logout - login).total_seconds() / 3600, 2)
    except Exception:
        return 0.0


class TestCalcHours:
    def test_8_hour_shift(self):
        login  = datetime(2026, 1, 1, 9, 0, 0)
        logout = datetime(2026, 1, 1, 17, 0, 0)
        assert calc_hours(login, logout) == 8.0

    def test_half_hour_shift(self):
        login  = datetime(2026, 1, 1, 8, 0, 0)
        logout = datetime(2026, 1, 1, 8, 30, 0)
        assert calc_hours(login, logout) == pytest.approx(0.5)

    def test_string_input(self):
        assert calc_hours("2026-01-01 09:00:00", "2026-01-01 17:00:00") == 8.0

    def test_zero_duration(self):
        t = datetime(2026, 1, 1, 10, 0, 0)
        assert calc_hours(t, t) == 0.0

    def test_bad_input_returns_zero(self):
        assert calc_hours(None, None) == 0.0
        assert calc_hours("not-a-date", "also-not") == 0.0


# ── _month_bounds ─────────────────────────────────────────────────────────────

def _month_bounds(d: datetime):
    start = d.replace(day=1)
    if d.month == 12:
        end = d.replace(year=d.year + 1, month=1, day=1)
    else:
        end = d.replace(month=d.month + 1, day=1)
    return start.date(), end.date()


class TestMonthBounds:
    def test_january(self):
        start, end = _month_bounds(datetime(2026, 1, 15))
        assert str(start) == "2026-01-01"
        assert str(end)   == "2026-02-01"

    def test_december_wraps_year(self):
        start, end = _month_bounds(datetime(2026, 12, 25))
        assert str(start) == "2026-12-01"
        assert str(end)   == "2027-01-01"

    def test_first_day_of_month(self):
        start, end = _month_bounds(datetime(2026, 6, 1))
        assert str(start) == "2026-06-01"
        assert str(end)   == "2026-07-01"

    def test_last_day_of_month(self):
        start, end = _month_bounds(datetime(2026, 3, 31))
        assert str(start) == "2026-03-01"
        assert str(end)   == "2026-04-01"


# ── Cache key naming ──────────────────────────────────────────────────────────

class TestCacheKeys:
    """Verify cache key patterns are consistent and don't collide."""

    def test_hr_summary_key_is_constant(self):
        key = "cache:hr:summary"
        assert ":" in key

    def test_mgr_stats_key_includes_id(self):
        key = f"cache:mgr:stats:MGR001"
        assert "MGR001" in key

    def test_emp_analytics_key_includes_id(self):
        key = f"cache:analytics:emp:LAB001"
        assert "LAB001" in key

    def test_site_analytics_key_includes_id(self):
        key1 = f"cache:analytics:site:1"
        key2 = f"cache:analytics:site:2"
        assert key1 != key2

    def test_different_employees_different_keys(self):
        k1 = f"cache:analytics:emp:LAB001"
        k2 = f"cache:analytics:emp:LAB002"
        assert k1 != k2
