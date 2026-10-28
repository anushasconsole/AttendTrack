"""
app/routers/auth_routes.py — Login, logout, face register/verify.
"""
import json
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional

from auth import (
    verify_password, create_access_token, blacklist_token,
    get_current_user, require_labour, TokenData, JWT_EXPIRE_HOURS,
)
from app.core.database import get_db, release_db, run_db, write_audit
from app.services.face import FACE_READY, extract_embedding, compare_faces
from redis_layer import get_redis, RateLimiter

router = APIRouter()


# ── Request models ────────────────────────────────────────────────────────────

class LoginReq(BaseModel):
    employee_id: str
    password:    str

class FaceRegisterReq(BaseModel):
    employee_id: str
    password:    str
    face_image:  str

class FaceVerifyReq(BaseModel):
    employee_id: str
    live_image:  str


async def _rate_limiter():
    r = await get_redis()
    return RateLimiter(r)


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/api/login")
async def login(req: LoginReq, request: Request):
    rl = await _rate_limiter()
    client_ip = request.headers.get(
        "X-Forwarded-For", request.client.host if request.client else "unknown")
    try:
        await rl.check("login", client_ip)
    except RateLimiter.RateLimitExceeded as e:
        raise HTTPException(429, detail=str(e))

    def _db(employee_id, password, ip):
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute(
                """SELECT u.*, s.name as site_name, s.latitude, s.longitude, s.radius_meters
                   FROM users u LEFT JOIN sites s ON u.site_id=s.id
                   WHERE u.employee_id=%s""",
                (employee_id,),
            )
            user = c.fetchone()
            if not user:
                return None, None
            ok, new_hash = verify_password(password, user["password_hash"])
            if not ok:
                return None, None
            if new_hash:
                c.execute("UPDATE users SET password_hash=%s WHERE employee_id=%s",
                          (new_hash, user["employee_id"]))
                print(f"[AUTH] Migrated {user['employee_id']} SHA-256 → bcrypt", flush=True)
            write_audit(c, "login", user["employee_id"], user["role"], ip_address=ip)
            conn.commit()
            return dict(user), None
        except Exception as e:
            conn.rollback()
            return None, str(e)
        finally:
            release_db(conn)

    try:
        user, err = await run_db(_db, req.employee_id.upper(), req.password, client_ip)
    except Exception as e:
        print(f"[LOGIN ERROR] {type(e).__name__}: {e}", flush=True)
        raise HTTPException(500, "Login failed due to a server error. Please try again.")

    if err:
        print(f"[LOGIN ERROR] {err}", flush=True)
        raise HTTPException(500, "Login failed due to a server error. Please try again.")
    if not user:
        raise HTTPException(401, "Invalid employee ID or password")

    token = create_access_token(
        employee_id=user["employee_id"], role=user["role"],
        name=user["name"],              site_id=user["site_id"],
    )
    return {
        "success":              True,
        "access_token":         token,
        "token_type":           "bearer",
        "expires_in_hours":     JWT_EXPIRE_HOURS,
        "requires_face_verify": user["role"] == "labour",
        "face_registered":      bool(user["face_registered"]),
        "user": {
            "employee_id": user["employee_id"], "name":       user["name"],
            "role":        user["role"],         "department": user["department"],
            "phone":       user["phone"],         "site_id":   user["site_id"],
            "site_name":   user["site_name"],     "site_lat":  user["latitude"],
            "site_lng":    user["longitude"],     "site_radius": user["radius_meters"],
        },
    }


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/api/logout")
async def logout(current: TokenData = Depends(get_current_user)):
    if current.jti:
        await blacklist_token(current.jti, JWT_EXPIRE_HOURS * 3600)
    return {"success": True, "message": "Logged out successfully"}


# ── Face register ─────────────────────────────────────────────────────────────

@router.post("/api/face/register")
async def face_register(req: FaceRegisterReq, request: Request,
                        current: TokenData = Depends(require_labour)):
    if current.employee_id != req.employee_id.upper():
        raise HTTPException(403, "You can only register your own face")

    rl = await _rate_limiter()
    try:
        await rl.check("face_register", req.employee_id.upper())
    except RateLimiter.RateLimitExceeded as e:
        raise HTTPException(429, detail=str(e))

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id,role,password_hash FROM users WHERE employee_id=%s",
                  (req.employee_id.upper(),))
        user = c.fetchone()
        if not user:                   raise HTTPException(404, "Employee not found")
        if user["role"] != "labour":   raise HTTPException(403, "Only for labour accounts")
        ok, new_hash = verify_password(req.password, user["password_hash"])
        if not ok:                     raise HTTPException(401, "Incorrect password")
        if new_hash:
            c.execute("UPDATE users SET password_hash=%s WHERE employee_id=%s",
                      (new_hash, req.employee_id.upper()))
        if not req.face_image or len(req.face_image) < 100:
            raise HTTPException(400, "Invalid image")

        embedding = extract_embedding(req.face_image)
        if embedding is None and FACE_READY:
            raise HTTPException(400, "No face detected. Ensure your face is visible and well-lit.")

        c.execute("""UPDATE users SET face_embedding=%s,face_registered=TRUE,
                            face_registered_at=NOW() WHERE employee_id=%s""",
                  (json.dumps(embedding) if embedding else None, req.employee_id.upper()))
        write_audit(c, "face_register", req.employee_id.upper(), "labour",
                    target_id=req.employee_id.upper(), target_type="user",
                    new_value="face_registered=true")
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": "Face registered! You can now login with face verification."}


# ── Face verify ───────────────────────────────────────────────────────────────

@router.post("/api/face/verify")
async def face_verify(req: FaceVerifyReq, request: Request,
                      current: TokenData = Depends(require_labour)):
    if current.employee_id != req.employee_id.upper():
        raise HTTPException(403, "You can only verify your own face")

    rl = await _rate_limiter()
    try:
        await rl.check("face_verify", req.employee_id.upper())
    except RateLimiter.RateLimitExceeded as e:
        raise HTTPException(429, detail=str(e))

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id,role,face_embedding,face_registered FROM users WHERE employee_id=%s",
                  (req.employee_id.upper(),))
        user = c.fetchone()
    finally:
        release_db(conn)

    if not user:                   raise HTTPException(404, "Employee not found")
    if user["role"] != "labour":   raise HTTPException(403, "Face verification only for labour")
    if not user["face_registered"] or not user["face_embedding"]:
        return {"success": False, "needs_registration": True,
                "message": "No face registered. Please register first."}

    emb = user["face_embedding"]
    if isinstance(emb, str):
        emb = json.loads(emb)
    result = compare_faces(emb, req.live_image)

    if result["bypass"]:
        return {"success": True, "match": True, "confidence": 0.5,
                "message": "⚠️ Bypass mode — face_recognition not installed", "bypass": True}
    if result["match"]:
        return {"success": True, "match": True, "confidence": result["confidence"],
                "message": f"✅ Face verified ({int(result['confidence'] * 100)}% match)"}
    return {"success": False, "match": False, "confidence": result["confidence"],
            "reason": result["reason"],
            "message": f"❌ Face mismatch ({int(result['confidence'] * 100)}%). Try better lighting."}
