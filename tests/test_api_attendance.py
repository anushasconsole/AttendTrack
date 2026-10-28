"""
tests/test_api_attendance.py — Integration tests for attendance endpoints.
All DB and Redis calls are mocked.
"""
import os
os.environ.setdefault("DATABASE_URL",  "postgresql://fake:fake@localhost/fake")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-32c")
os.environ.setdefault("REDIS_URL", "")

import pytest
from datetime import datetime
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_access_token
from app.routers.attendance import router

# ── Shared test client ────────────────────────────────────────────────────────

def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _labour_token(emp_id="LAB001"):
    return create_access_token(emp_id, "labour", site_id=1)


# ── /api/attendance/today ─────────────────────────────────────────────────────

class TestTodayStatus:
    def test_no_token_returns_401(self):
        resp = _client().get("/api/attendance/today/LAB001")
        assert resp.status_code == 401

    def test_labour_can_see_own_record(self):
        token = _labour_token("LAB001")
        record = {"id": 1, "employee_id": "LAB001", "date": "2026-07-01",
                  "login_time": "2026-07-01T09:00:00", "logout_time": None,
                  "hours_worked": None, "status": "active", "site_name": "Main Site"}

        with patch("app.routers.attendance.run_db", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = record
            resp = _client().get(
                "/api/attendance/today/LAB001",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_record"] is True
        assert data["record"]["status"] == "active"

    def test_labour_cannot_see_other_employee(self):
        token = _labour_token("LAB001")
        resp  = _client().get(
            "/api/attendance/today/LAB002",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_no_record_returns_has_record_false(self):
        token = _labour_token("LAB001")

        with patch("app.routers.attendance.run_db", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = None
            resp = _client().get(
                "/api/attendance/today/LAB001",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert resp.json()["has_record"] is False
        assert resp.json()["record"] is None

    def test_manager_can_see_any_employee(self):
        token = create_access_token("MGR001", "manager", site_id=1)
        record = {"id": 2, "employee_id": "LAB002", "status": "active", "site_name": "Site A",
                  "date": "2026-07-01", "login_time": "2026-07-01T08:00:00",
                  "logout_time": None, "hours_worked": None}

        with patch("app.routers.attendance.run_db", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = record
            resp = _client().get(
                "/api/attendance/today/LAB002",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200


# ── /api/attendance/history ───────────────────────────────────────────────────

class TestHistory:
    def test_returns_records_list(self):
        token   = _labour_token("LAB001")
        records = [
            {"id": 1, "date": "2026-07-01", "hours_worked": 8.0, "status": "approved",
             "login_time": "2026-07-01T09:00:00", "logout_time": "2026-07-01T17:00:00",
             "site_name": "Main Site"},
        ]

        with patch("app.routers.attendance.run_db", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = records
            resp = _client().get(
                "/api/attendance/history/LAB001",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert len(resp.json()["records"]) == 1

    def test_empty_history_returns_empty_list(self):
        token = _labour_token("LAB001")

        with patch("app.routers.attendance.run_db", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = []
            resp = _client().get(
                "/api/attendance/history/LAB001",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert resp.json()["records"] == []

    def test_labour_cannot_see_other_history(self):
        token = _labour_token("LAB001")
        resp  = _client().get(
            "/api/attendance/history/LAB002",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403


# ── /api/checkin (ownership check) ───────────────────────────────────────────

class TestCheckinOwnership:
    """Test that a labour employee cannot check in as someone else."""

    def test_cannot_checkin_as_other_employee(self):
        token = _labour_token("LAB001")
        resp  = _client().post(
            "/api/checkin",
            json={"employee_id": "LAB002", "latitude": 12.62, "longitude": 75.25},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_no_token_returns_401(self):
        resp = _client().post(
            "/api/checkin",
            json={"employee_id": "LAB001", "latitude": 12.62, "longitude": 75.25},
        )
        assert resp.status_code == 401

    def test_manager_token_on_checkin_returns_403(self):
        token = create_access_token("MGR001", "manager", site_id=1)
        resp  = _client().post(
            "/api/checkin",
            json={"employee_id": "MGR001", "latitude": 12.62, "longitude": 75.25},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_checkout_ownership_check(self):
        token = _labour_token("LAB001")
        resp  = _client().post(
            "/api/checkout",
            json={"attendance_id": 1, "employee_id": "LAB002",
                  "latitude": 12.62, "longitude": 75.25},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
