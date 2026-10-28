"""
app/routers/attendance.py — Check-in, checkout, today status, history.
"""
import math
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from auth import get_current_user, require_labour, TokenData
from app.core.database import get_db, release_db, run_db, write_audit
from app.core.cache import cache_invalidate
from app.services.overtime import check_and_alert_overtime
from redis_layer import get_redis, RedisLock, RateLimiter
from redis_layer import publish_event

router = APIRouter()


class CheckinReq(BaseModel):
    employee_id: str
    latitude:    float
    longitude:   float

class CheckoutReq(BaseModel):
    attendance_id: int
    employee_id:   str
    latitude:      float
    longitude:     float


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _calc_hours(login, logout) -> float:
    try:
        if not isinstance(login, datetime):
            login  = datetime.fromisoformat(str(login)[:19])
        if not isinstance(logout, datetime):
            logout = datetime.fromisoformat(str(logout)[:19])
        return round((logout - login).total_seconds() / 3600, 2)
    except Exception:
        return 0.0


async def _rl():
    r = await get_redis()
    return RateLimiter(r)


# ── Check-in ──────────────────────────────────────────────────────────────────

@router.post("/api/checkin")
async def checkin(req: CheckinReq, request: Request,
                  current: TokenData = Depends(require_labour)):
    if current.employee_id != req.employee_id.upper():
        raise HTTPException(403, "You can only check in for yourself")

    rl = await _rl()
    try:
        await rl.check("checkin", req.employee_id.upper())
    except RateLimiter.RateLimitExceeded as e:
        raise HTTPException(429, detail=str(e))

    r = await get_redis()
    try:
        async with RedisLock(r, f"checkin:{req.employee_id.upper()}", ttl=15):

            def _db(employee_id, lat, lng):
                conn = get_db()
                try:
                    c = conn.cursor()
                    c.execute(
                        """SELECT u.*, s.latitude as slat, s.longitude as slng,
                                  s.radius_meters as srad, s.id as sid
                           FROM users u JOIN sites s ON u.site_id=s.id
                           WHERE u.employee_id=%s""",
                        (employee_id,),
                    )
                    user = c.fetchone()
                    if not user:
                        raise HTTPException(404, "Employee not found or no site assigned")
                    today = datetime.now().strftime("%Y-%m-%d")
                    c.execute("SELECT id FROM attendance WHERE employee_id=%s AND date=%s",
                              (employee_id, today))
                    if c.fetchone():
                        raise HTTPException(400, "Already checked in today.")
                    dist = _haversine(lat, lng, user["slat"], user["slng"])
                    if dist > user["srad"]:
                        raise HTTPException(
                            400, f"You are {int(dist)}m from site. Must be within {int(user['srad'])}m.")
                    now = datetime.now()
                    c.execute(
                        """INSERT INTO attendance
                               (employee_id,site_id,login_time,login_lat,login_lng,date,status)
                           VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
                        (employee_id, user["sid"], now, lat, lng, today),
                    )
                    att_id = c.fetchone()["id"]
                    write_audit(c, "checkin", employee_id.upper(), "labour",
                                target_id=str(att_id), target_type="attendance",
                                new_value="active",
                                metadata={"lat": lat, "lng": lng, "dist_m": int(dist)})
                    conn.commit()
                    return {"att_id": att_id, "now": now, "dist": dist,
                            "sid": user["sid"], "emp_id": user["employee_id"]}
                except HTTPException:
                    conn.rollback(); raise
                except Exception as e:
                    conn.rollback(); raise HTTPException(500, str(e))
                finally:
                    release_db(conn)

            result = await run_db(_db, req.employee_id.upper(), req.latitude, req.longitude)

    except RedisLock.LockNotAcquired:
        raise HTTPException(429, "Check-in already in progress. Please wait a moment.")

    await publish_event({
        "type": "checkin", "employee_id": req.employee_id.upper(),
        "attendance_id": result["att_id"], "status": "active",
        "site_id": result["sid"], "timestamp": result["now"].isoformat(),
    })
    await cache_invalidate(
        "cache:hr:summary",
        f"cache:mgr:stats:{req.employee_id.upper()}",
        f"cache:analytics:site:{result['sid']}",
    )
    return {"success": True, "attendance_id": result["att_id"],
            "message": f"✅ Checked in at {result['now'].strftime('%H:%M:%S')}",
            "login_time": result["now"].isoformat(),
            "distance_from_site": int(result["dist"])}


# ── Checkout ──────────────────────────────────────────────────────────────────

@router.post("/api/checkout")
async def checkout(req: CheckoutReq, request: Request,
                   current: TokenData = Depends(require_labour)):
    if current.employee_id != req.employee_id.upper():
        raise HTTPException(403, "You can only check out for yourself")

    rl = await _rl()
    try:
        await rl.check("checkout", req.employee_id.upper())
    except RateLimiter.RateLimitExceeded as e:
        raise HTTPException(429, detail=str(e))

    r = await get_redis()
    try:
        async with RedisLock(r, f"checkout:{req.employee_id.upper()}", ttl=15):

            def _db(att_id, employee_id, lat, lng):
                conn = get_db()
                try:
                    c = conn.cursor()
                    c.execute(
                        """SELECT a.*, s.latitude as slat, s.longitude as slng,
                                  s.radius_meters as srad
                           FROM attendance a JOIN sites s ON a.site_id=s.id
                           WHERE a.id=%s AND a.employee_id=%s AND a.logout_time IS NULL""",
                        (att_id, employee_id),
                    )
                    att = c.fetchone()
                    if not att:
                        raise HTTPException(404, "Active attendance record not found")
                    dist  = _haversine(lat, lng, att["slat"], att["slng"])
                    now   = datetime.now()
                    hours = _calc_hours(att["login_time"], now)
                    c.execute(
                        """UPDATE attendance
                           SET logout_time=%s,logout_lat=%s,logout_lng=%s,
                               hours_worked=%s,status='pending_manager'
                           WHERE id=%s""",
                        (now, lat, lng, hours, att_id),
                    )
                    c.execute("SELECT name,phone,site_id FROM users WHERE employee_id=%s",
                              (employee_id,))
                    u = c.fetchone()
                    ot = check_and_alert_overtime(c, employee_id, u["phone"], u["name"], now)
                    write_audit(c, "checkout", employee_id.upper(), "labour",
                                target_id=str(att_id), target_type="attendance",
                                old_value="active", new_value="pending_manager",
                                metadata={"hours_worked": hours, "dist_m": int(dist)})
                    conn.commit()
                    return {"hours": hours, "dist": dist, "now": now,
                            "site_id": u["site_id"], "overtime": ot}
                except HTTPException:
                    conn.rollback(); raise
                except Exception as e:
                    conn.rollback(); raise HTTPException(500, str(e))
                finally:
                    release_db(conn)

            co = await run_db(_db, req.attendance_id, req.employee_id,
                              req.latitude, req.longitude)

    except RedisLock.LockNotAcquired:
        raise HTTPException(429, "Check-out already in progress. Please wait a moment.")

    await publish_event({
        "type": "attendance_update", "employee_id": req.employee_id.upper(),
        "attendance_id": req.attendance_id, "status": "pending_manager",
        "site_id": co["site_id"], "timestamp": co["now"].isoformat(),
    })
    await cache_invalidate(
        "cache:hr:summary",
        f"cache:mgr:stats:{req.employee_id.upper()}",
        f"cache:analytics:emp:{req.employee_id.upper()}",
        f"cache:analytics:site:{co['site_id']}",
    )
    return {"success": True,
            "message":           f"✅ Checked out. You worked {co['hours']} hours.",
            "hours_worked":      co["hours"],
            "logout_time":       co["now"].isoformat(),
            "distance_from_site": int(co["dist"]),
            "overtime":          co["overtime"]}


# ── Today status / history ────────────────────────────────────────────────────

@router.get("/api/attendance/today/{employee_id}")
async def today_status(employee_id: str,
                       current: TokenData = Depends(get_current_user)):
    if current.role == "labour" and current.employee_id != employee_id.upper():
        raise HTTPException(403, "You can only view your own attendance")
    today = datetime.now().strftime("%Y-%m-%d")

    def _db(eid, d):
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute(
                """SELECT a.*, s.name as site_name FROM attendance a
                   JOIN sites s ON a.site_id=s.id
                   WHERE a.employee_id=%s AND a.date=%s ORDER BY a.id DESC LIMIT 1""",
                (eid, d),
            )
            row = c.fetchone()
            return dict(row) if row else None
        finally:
            release_db(conn)

    row = await run_db(_db, employee_id, today)
    return {"has_record": bool(row), "record": row}


@router.get("/api/attendance/history/{employee_id}")
async def history(employee_id: str, limit: int = 30,
                  current: TokenData = Depends(get_current_user)):
    if current.role == "labour" and current.employee_id != employee_id.upper():
        raise HTTPException(403, "You can only view your own history")

    def _db(eid, lim):
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute(
                """SELECT a.*, s.name as site_name FROM attendance a
                   JOIN sites s ON a.site_id=s.id
                   WHERE a.employee_id=%s ORDER BY a.date DESC, a.id DESC LIMIT %s""",
                (eid, lim),
            )
            return [dict(r) for r in c.fetchall()]
        finally:
            release_db(conn)

    rows = await run_db(_db, employee_id, limit)
    return {"records": rows}
