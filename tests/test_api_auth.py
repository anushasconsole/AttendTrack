"""
tests/test_api_auth.py — Integration tests for auth API endpoints.

Uses FastAPI TestClient with fully mocked DB and Redis so no real
infrastructure is needed.
"""
import os
os.environ.setdefault("DATABASE_URL",  "postgresql://fake:fake@localhost/fake")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-32c")
os.environ.setdefault("REDIS_URL", "")

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(role="manager", employee_id="MGR001", password="mgr123"):
    """Return a mock user dict as psycopg2 would return."""
    from auth import hash_password
    return {
        "employee_id":    employee_id,
        "name":           "Test User",
        "role":           role,
        "password_hash":  hash_password(password),
        "department":     "Operations",
        "phone":          "9876543210",
        "site_id":        1,
        "site_name":      "Main Site",
        "latitude":       12.62,
        "longitude":      75.25,
        "radius_meters":  1000,
        "face_registered": False,
        "face_embedding": None,
    }


def _mock_db(user):
    """Patch get_db / release_db so endpoints don't touch Postgres."""
    conn  = MagicMock()
    cur   = MagicMock()
    cur.fetchone.return_value = user
    conn.cursor.return_value  = cur
    return conn


# ── Login tests ───────────────────────────────────────────────────────────────

class TestLoginEndpoint:
    """Test POST /api/login via the app router."""

    def _get_client(self):
        """Build a TestClient for the auth router only."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers.auth_routes import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=False)

    def test_login_success_returns_token(self):
        client = self._get_client()
        user   = _make_user("manager", "MGR001", "mgr123")

        with patch("app.routers.auth_routes.run_db") as mock_run_db, \
             patch("app.routers.auth_routes.get_redis", new_callable=AsyncMock) as mock_redis:

            mock_redis.return_value = None           # no Redis
            mock_run_db.return_value = (user, None)  # (user_dict, error)

            resp = client.post("/api/login", json={"employee_id": "MGR001", "password": "mgr123"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["employee_id"] == "MGR001"

    def test_login_wrong_password_returns_401(self):
        client = self._get_client()

        with patch("app.routers.auth_routes.run_db") as mock_run_db, \
             patch("app.routers.auth_routes.get_redis", new_callable=AsyncMock) as mock_redis:

            mock_redis.return_value  = None
            mock_run_db.return_value = (None, None)  # no user found

            resp = client.post("/api/login", json={"employee_id": "MGR001", "password": "wrong"})

        assert resp.status_code == 401

    def test_login_rate_limit_respected(self):
        """Rate limiter is a no-op when Redis is None — should not block."""
        client = self._get_client()
        user   = _make_user("hr", "HR001", "hr123")

        with patch("app.routers.auth_routes.run_db") as mock_run_db, \
             patch("app.routers.auth_routes.get_redis", new_callable=AsyncMock) as mock_redis:

            mock_redis.return_value  = None
            mock_run_db.return_value = (user, None)

            resp = client.post("/api/login", json={"employee_id": "HR001", "password": "hr123"})

        assert resp.status_code == 200

    def test_login_response_includes_user_fields(self):
        client = self._get_client()
        user   = _make_user("labour", "LAB001", "lab123")

        with patch("app.routers.auth_routes.run_db") as mock_run_db, \
             patch("app.routers.auth_routes.get_redis", new_callable=AsyncMock) as mock_redis:

            mock_redis.return_value  = None
            mock_run_db.return_value = (user, None)

            resp = client.post("/api/login", json={"employee_id": "LAB001", "password": "lab123"})

        data = resp.json()
        assert resp.status_code == 200
        for field in ("employee_id", "name", "role", "site_id", "site_name"):
            assert field in data["user"], f"Missing field: {field}"

    def test_login_labour_sets_requires_face_verify(self):
        client = self._get_client()
        user   = _make_user("labour", "LAB001", "lab123")

        with patch("app.routers.auth_routes.run_db") as mock_run_db, \
             patch("app.routers.auth_routes.get_redis", new_callable=AsyncMock) as mock_redis:

            mock_redis.return_value  = None
            mock_run_db.return_value = (user, None)

            resp = client.post("/api/login", json={"employee_id": "LAB001", "password": "lab123"})

        assert resp.json()["requires_face_verify"] is True

    def test_login_manager_does_not_require_face_verify(self):
        client = self._get_client()
        user   = _make_user("manager", "MGR001", "mgr123")

        with patch("app.routers.auth_routes.run_db") as mock_run_db, \
             patch("app.routers.auth_routes.get_redis", new_callable=AsyncMock) as mock_redis:

            mock_redis.return_value  = None
            mock_run_db.return_value = (user, None)

            resp = client.post("/api/login", json={"employee_id": "MGR001", "password": "mgr123"})

        assert resp.json()["requires_face_verify"] is False


# ── JWT token validation ──────────────────────────────────────────────────────

class TestJWTInRequests:
    """Test that protected endpoints reject requests without a valid token."""

    def _get_client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers.manager import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=False)

    def test_no_token_returns_401(self):
        client = self._get_client()
        resp   = client.get("/api/manager/stats/MGR001")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self):
        client = self._get_client()
        resp   = client.get(
            "/api/manager/stats/MGR001",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_valid_token_wrong_role_returns_403(self):
        """A labour token on a manager endpoint should be 403."""
        from auth import create_access_token
        client = self._get_client()
        token  = create_access_token("LAB001", "labour")

        with patch("app.routers.manager.cache_get", new_callable=AsyncMock) as mock_cache:
            mock_cache.return_value = None
            resp = client.get(
                "/api/manager/stats/LAB001",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 403

    def test_valid_manager_token_accepted(self):
        from auth import create_access_token
        client = self._get_client()
        token  = create_access_token("MGR001", "manager", site_id=1)

        with patch("app.routers.manager.cache_get", new_callable=AsyncMock) as mock_cache, \
             patch("app.routers.manager.run_db", new_callable=AsyncMock) as mock_run_db:

            mock_cache.return_value   = None
            mock_run_db.return_value  = {
                "today_unique_checkins": 5, "pending_count": 2,
                "approved_count": 3,        "total_labours_site": 10,
            }
            resp = client.get(
                "/api/manager/stats/MGR001",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert resp.json()["today_unique_checkins"] == 5
