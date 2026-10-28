"""
tests/test_auth.py — Unit tests for auth.py (bcrypt + JWT).

These tests run without a database or Redis.
"""
import os
os.environ.setdefault("DATABASE_URL",  "postgresql://fake:fake@localhost/fake")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-testing-only-32c")
os.environ.setdefault("REDIS_URL", "")

import pytest
from auth import (
    hash_password,
    verify_password,
    create_access_token,
    _decode_token,
    TokenData,
)


# ── Password hashing ──────────────────────────────────────────────────────────

class TestHashPassword:
    def test_returns_bcrypt_hash(self):
        h = hash_password("secret123")
        assert h.startswith("$2b$"), "Should be a bcrypt hash"

    def test_different_passwords_produce_different_hashes(self):
        assert hash_password("aaa") != hash_password("bbb")

    def test_same_password_different_salts(self):
        # bcrypt uses random salt — two calls produce different hashes
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2

    def test_handles_empty_string(self):
        h = hash_password("")
        assert h.startswith("$2b$")

    def test_handles_unicode(self):
        h = hash_password("pässwörd!🔑")
        assert h.startswith("$2b$")

    def test_truncates_at_72_bytes(self):
        # bcrypt truncates at 72 bytes — both passwords should verify the same hash
        long_pw   = "a" * 80
        trunc_pw  = "a" * 72
        h = hash_password(long_pw)
        ok, _ = verify_password(trunc_pw, h)
        assert ok, "Truncated password should match the truncated hash"


# ── Password verification ─────────────────────────────────────────────────────

class TestVerifyPassword:
    def test_correct_password_returns_true(self):
        h = hash_password("mypassword")
        ok, new_hash = verify_password("mypassword", h)
        assert ok is True
        assert new_hash is None   # bcrypt → no upgrade needed

    def test_wrong_password_returns_false(self):
        h = hash_password("correct")
        ok, _ = verify_password("wrong", h)
        assert ok is False

    def test_legacy_sha256_accepted_and_upgrade_returned(self):
        import hashlib
        sha_hash = hashlib.sha256("lab123".encode()).hexdigest()
        ok, new_hash = verify_password("lab123", sha_hash)
        assert ok is True
        assert new_hash is not None
        assert new_hash.startswith("$2b$"), "Upgrade should be bcrypt"

    def test_legacy_sha256_wrong_password(self):
        import hashlib
        sha_hash = hashlib.sha256("correct".encode()).hexdigest()
        ok, _ = verify_password("wrong", sha_hash)
        assert ok is False

    def test_bcrypt_verify_does_not_return_new_hash(self):
        h = hash_password("pw")
        ok, new_hash = verify_password("pw", h)
        assert ok is True
        assert new_hash is None

    def test_malformed_hash_returns_false(self):
        ok, _ = verify_password("anything", "not-a-valid-hash")
        assert ok is False

    def test_empty_password_works(self):
        h = hash_password("")
        ok, _ = verify_password("", h)
        assert ok is True


# ── JWT issue / decode ────────────────────────────────────────────────────────

class TestJWT:
    def test_token_is_string(self):
        token = create_access_token("EMP001", "labour")
        assert isinstance(token, str)
        assert len(token) > 20

    def test_decode_returns_correct_payload(self):
        token = create_access_token("MGR001", "manager", name="Rajesh", site_id=1)
        payload = _decode_token(token)
        assert payload["sub"]  == "MGR001"
        assert payload["role"] == "manager"
        assert payload["name"] == "Rajesh"
        assert payload["sid"]  == 1

    def test_different_users_different_tokens(self):
        t1 = create_access_token("EMP001", "labour")
        t2 = create_access_token("EMP002", "labour")
        assert t1 != t2

    def test_token_has_jti(self):
        token = create_access_token("EMP001", "labour")
        payload = _decode_token(token)
        assert "jti" in payload
        assert len(payload["jti"]) > 10   # UUID-like

    def test_two_tokens_have_unique_jti(self):
        t1 = create_access_token("EMP001", "labour")
        t2 = create_access_token("EMP001", "labour")
        p1 = _decode_token(t1)
        p2 = _decode_token(t2)
        assert p1["jti"] != p2["jti"]

    def test_tampered_token_raises_401(self):
        from fastapi import HTTPException
        token   = create_access_token("EMP001", "labour")
        tampered = token[:-5] + "XXXXX"
        with pytest.raises(HTTPException) as exc:
            _decode_token(tampered)
        assert exc.value.status_code == 401

    def test_invalid_token_raises_401(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            _decode_token("completely.invalid.token")
        assert exc.value.status_code == 401

    def test_token_data_attributes(self):
        td = TokenData(employee_id="HR001", role="hr", name="Priya", site_id=None, jti="abc")
        assert td.employee_id == "HR001"
        assert td.role        == "hr"
        assert td.name        == "Priya"
        assert td.site_id     is None
        assert td.jti         == "abc"
