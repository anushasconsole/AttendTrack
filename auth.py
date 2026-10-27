"""
auth.py — AttendTrack JWT authentication layer
================================================

Provides:
  1. Password hashing / verification  (bcrypt via passlib)
  2. JWT issue / decode               (HS256 via python-jose)
  3. FastAPI Depends() guards:
       - get_current_user()    → any authenticated user
       - require_labour()      → labour role only
       - require_manager()     → manager role only
       - require_hr()          → hr role only
       - require_manager_or_hr() → either
  4. Token blacklist via Redis         (logout support)
  5. Graceful password migration:
       if a stored hash looks like old SHA-256 (64 hex chars),
       we accept it once and re-hash it with bcrypt on the fly.

Usage in endpoints:
    from auth import require_hr, TokenData

    @app.get("/api/hr/summary")
    async def hr_summary(current: TokenData = Depends(require_hr)):
        ...  # current.employee_id, current.role guaranteed

Environment variables:
    JWT_SECRET_KEY   — random string ≥ 32 chars (REQUIRED in prod)
    JWT_EXPIRE_HOURS — access token lifetime in hours  (default: 24)
"""

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ── python-jose JWT ──────────────────────────────────────────────────────────
from jose import JWTError, jwt

# ── passlib bcrypt ───────────────────────────────────────────────────────────
from passlib.context import CryptContext

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

JWT_SECRET_KEY   = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_in_production_use_random_32chars")
JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

if JWT_SECRET_KEY == "CHANGE_ME_in_production_use_random_32chars":
    print("⚠️  JWT_SECRET_KEY is using the default dev value — set a strong secret in .env for production!")

# ─────────────────────────────────────────────
# PASSWORD HASHING  (bcrypt)
# ─────────────────────────────────────────────

# Suppress the bcrypt 72-byte truncation warning/error — we handle the limit
# explicitly in hash_password() and verify_password() below.
try:
    _pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto",
                            bcrypt__truncate_error=False)
except TypeError:
    # Older passlib versions don't support bcrypt__truncate_error
    _pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*.
    Pre-truncates to 72 bytes (the bcrypt hard limit) so the hash is always
    deterministic regardless of passlib version behaviour."""
    plain_bytes = plain.encode("utf-8")[:72]
    return _pwd_ctx.hash(plain_bytes.decode("utf-8", errors="replace"))


def _old_sha256(plain: str) -> str:
    """Reproduces the legacy SHA-256 hash so we can detect migration candidates."""
    return hashlib.sha256(plain.encode()).hexdigest()


def verify_password(plain: str, stored_hash: str) -> tuple[bool, Optional[str]]:
    """
    Verify *plain* against *stored_hash*.

    Handles two hash formats:
      - bcrypt  → verified directly with passlib
      - SHA-256 (64-char hex) → accepted if matches, returns new bcrypt hash
                                so the caller can upgrade the stored value

    Returns:
        (ok: bool, new_bcrypt_hash: str | None)
        new_bcrypt_hash is non-None only when the legacy hash matched
        and the caller should re-store it as bcrypt.
    """
    # Detect legacy SHA-256: exactly 64 lowercase hex chars, no $ prefix
    if len(stored_hash) == 64 and all(c in "0123456789abcdef" for c in stored_hash):
        if _old_sha256(plain) == stored_hash:
            return True, hash_password(plain)   # signal: please upgrade
        return False, None

    # Normal bcrypt path — pre-truncate to match how hash_password stores it
    try:
        plain_bytes = plain.encode("utf-8")[:72]
        ok = _pwd_ctx.verify(plain_bytes.decode("utf-8", errors="replace"), stored_hash)
    except Exception:
        ok = False
    return ok, None


# ─────────────────────────────────────────────
# JWT  ISSUE / DECODE
# ─────────────────────────────────────────────

class TokenData:
    """Typed payload extracted from a validated JWT."""
    __slots__ = ("employee_id", "role", "name", "site_id", "jti")

    def __init__(self, employee_id: str, role: str,
                 name: str = "", site_id: Optional[int] = None,
                 jti: str = ""):
        self.employee_id = employee_id
        self.role        = role
        self.name        = name
        self.site_id     = site_id
        self.jti         = jti       # JWT ID — used for blacklisting


def create_access_token(employee_id: str, role: str,
                        name: str = "", site_id: Optional[int] = None) -> str:
    """
    Issue a signed JWT.

    Payload:
        sub   — employee_id  (subject, standard claim)
        role  — labour | manager | hr
        name  — display name
        sid   — site_id (int or null)
        jti   — unique token id (for blacklisting on logout)
        exp   — expiry timestamp
    """
    import uuid
    now    = datetime.now(tz=timezone.utc)
    expire = now + timedelta(hours=JWT_EXPIRE_HOURS)
    jti    = str(uuid.uuid4())

    payload = {
        "sub":  employee_id,
        "role": role,
        "name": name,
        "sid":  site_id,
        "jti":  jti,
        "iat":  now,
        "exp":  expire,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    """
    Decode and validate the JWT signature + expiry.
    Raises HTTPException 401 on any failure.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ─────────────────────────────────────────────
# TOKEN BLACKLIST  (logout via Redis)
# ─────────────────────────────────────────────

async def blacklist_token(jti: str, ttl_seconds: int):
    """
    Mark a token as revoked.  Stored in Redis as:
        blacklist:jti:<jti>  →  "1"   TTL = remaining token lifetime
    If Redis is unavailable, logout still returns 200 but the token
    won't be actively rejected until it naturally expires.
    """
    # Import here to avoid circular dependency (auth ← redis_layer ← main)
    from redis_layer import get_redis
    r = await get_redis()
    if r:
        try:
            await r.setex(f"blacklist:jti:{jti}", ttl_seconds, "1")
        except Exception as e:
            print(f"[AUTH] blacklist_token failed (Redis error): {e}", flush=True)


async def _is_blacklisted(jti: str) -> bool:
    """Return True if the token has been blacklisted (i.e. logged out)."""
    from redis_layer import get_redis
    r = await get_redis()
    if not r:
        return False
    try:
        return await r.exists(f"blacklist:jti:{jti}") == 1
    except Exception:
        return False   # Redis unavailable → don't block legitimate requests


# ─────────────────────────────────────────────
# FASTAPI BEARER EXTRACTOR
# ─────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> TokenData:
    """
    Core dependency — validates the Bearer token and returns TokenData.
    Used by all protected endpoints.
    Raises 401 if the token is missing, invalid, expired, or blacklisted.
    """
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Include 'Authorization: Bearer <token>' header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload  = _decode_token(creds.credentials)
    jti      = payload.get("jti", "")
    emp_id   = payload.get("sub")
    role     = payload.get("role")

    if not emp_id or not role:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token payload")

    if jti and await _is_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenData(
        employee_id=emp_id,
        role=role,
        name=payload.get("name", ""),
        site_id=payload.get("sid"),
        jti=jti,
    )


# ─────────────────────────────────────────────
# ROLE-SPECIFIC DEPENDENCIES
# ─────────────────────────────────────────────

async def require_labour(current: TokenData = Depends(get_current_user)) -> TokenData:
    if current.role != "labour":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Labour role required")
    return current


async def require_manager(current: TokenData = Depends(get_current_user)) -> TokenData:
    if current.role != "manager":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager role required")
    return current


async def require_hr(current: TokenData = Depends(get_current_user)) -> TokenData:
    if current.role != "hr":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "HR role required")
    return current


async def require_manager_or_hr(current: TokenData = Depends(get_current_user)) -> TokenData:
    if current.role not in ("manager", "hr"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager or HR role required")
    return current
