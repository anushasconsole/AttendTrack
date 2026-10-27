"""
auth.py — AttendTrack JWT authentication layer
================================================
Uses:
  - bcrypt (direct, no passlib) for password hashing — compatible with bcrypt 4.x
  - python-jose for JWT (HS256)

Passlib was dropped because passlib 1.7.4 is incompatible with bcrypt 4.x
(AttributeError: module 'bcrypt' has no attribute '__about__').
"""

import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
import bcrypt

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

JWT_SECRET_KEY   = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_in_production_use_random_32chars")
JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))

if JWT_SECRET_KEY == "CHANGE_ME_in_production_use_random_32chars":
    print("⚠️  JWT_SECRET_KEY is using the default dev value — set a strong secret in .env for production!")
else:
    print("✅ auth module loaded — JWT + bcrypt active")

# ─────────────────────────────────────────────
# PASSWORD HASHING  (bcrypt 4.x direct)
# ─────────────────────────────────────────────
# bcrypt hard-truncates at 72 bytes. We pre-truncate explicitly
# so the behaviour is deterministic and never raises an error.
_BCRYPT_MAX = 72


def _prep(plain: str) -> bytes:
    """Encode and truncate to 72 bytes — bcrypt's hard limit."""
    return plain.encode("utf-8")[:_BCRYPT_MAX]


def hash_password(plain: str) -> str:
    """Return a bcrypt hash string for *plain*."""
    hashed = bcrypt.hashpw(_prep(plain), bcrypt.gensalt(rounds=12))
    return hashed.decode("utf-8")


def _old_sha256(plain: str) -> str:
    """Legacy SHA-256 hash — used only for migration detection."""
    return hashlib.sha256(plain.encode()).hexdigest()


def verify_password(plain: str, stored_hash: str) -> tuple[bool, Optional[str]]:
    """
    Verify *plain* against *stored_hash*.

    Handles two formats automatically:
      - bcrypt  ($2b$...) → verified directly
      - SHA-256 (64 hex)  → accepted once, returns new bcrypt hash to upgrade

    Returns:
        (ok: bool, new_bcrypt_hash: str | None)
        new_bcrypt_hash is only set when a legacy hash was matched and should
        be upgraded in the DB by the caller.
    """
    # Detect legacy SHA-256: 64 lowercase hex chars, no $ prefix
    if len(stored_hash) == 64 and stored_hash.isalnum() and stored_hash == stored_hash.lower():
        if _old_sha256(plain) == stored_hash:
            return True, hash_password(plain)   # matched — upgrade it
        return False, None

    # bcrypt path
    try:
        ok = bcrypt.checkpw(_prep(plain), stored_hash.encode("utf-8"))
        return ok, None
    except Exception:
        return False, None


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
        self.jti         = jti


def create_access_token(employee_id: str, role: str,
                        name: str = "", site_id: Optional[int] = None) -> str:
    """Issue a signed HS256 JWT."""
    now    = datetime.now(tz=timezone.utc)
    expire = now + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub":  employee_id,
        "role": role,
        "name": name,
        "sid":  site_id,
        "jti":  str(uuid.uuid4()),
        "iat":  now,
        "exp":  expire,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    """Decode and validate JWT. Raises 401 on any failure."""
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
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
    """Store revoked token jti in Redis. No-op if Redis unavailable."""
    from redis_layer import get_redis
    r = await get_redis()
    if r:
        try:
            await r.setex(f"blacklist:jti:{jti}", ttl_seconds, "1")
        except Exception as e:
            print(f"[AUTH] blacklist_token failed: {e}", flush=True)


async def _is_blacklisted(jti: str) -> bool:
    """Return True if the token jti has been blacklisted."""
    from redis_layer import get_redis
    r = await get_redis()
    if not r:
        return False
    try:
        return await r.exists(f"blacklist:jti:{jti}") == 1
    except Exception:
        return False


# ─────────────────────────────────────────────
# FASTAPI BEARER EXTRACTOR
# ─────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> TokenData:
    """Validate Bearer token and return TokenData. Raises 401 if invalid."""
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Include 'Authorization: Bearer <token>' header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = _decode_token(creds.credentials)
    emp_id  = payload.get("sub")
    role    = payload.get("role")
    jti     = payload.get("jti", "")

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
# ROLE GUARDS
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
