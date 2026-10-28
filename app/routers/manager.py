"""
app/routers/manager.py — Manager portal endpoints.
"""
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional

from auth import require_manager, TokenData
from app.core.database import get_db, release_db, run_db, write_audit
from app.core.cache import cache_get, cache_set, cache_invalidate
from redis_layer import publish_event

router = APIRouter()


class ManagerVerifyReq(BaseModel):
    attendance_id: int
    manager_id:    str
    approved:      bool
    note:          Optional[str] = None


def _mgr_site(c, manager_id):
    c.execute("SELECT site_id FROM users WHERE employee_id=%s AND role='manager'", (manager_id,))
    m = c.fetchone()
    if not m:
        raise HTTPException(403, "Not a manager")
    return m["site_id"]


@router.get("/api/manager/pending/{manager_id}")
async def manager_pending(manager_id: str,
                          current: TokenData = Depends(require_manager)):
    if current.employee_id != manager_id.upper():
        raise HTTPException(403, "You can only view your own pending queue")

    def _db(mgr_id):
        conn = get_db()
        try:
            c = conn.cursor()
            sid = _mgr_site(c, mgr_id)
            c.execute(
                """SELECT a.*, u.name as labour_name, u.phone, s.name as site_name
                   FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                   JOIN sites s ON a.site_id=s.id
                   WHERE a.site_id=%s AND a.status='pending_manager'
                     AND a.logout_time IS NOT NULL
                   ORDER BY a.date DESC""",
                (sid,),
            )
            return [dict(r) for r in c.fetchall()]
        finally:
            release_db(conn)

    return {"records": await run_db(_db, manager_id)}


@router.get("/api/manager/all/{manager_id}")
async def manager_all(manager_id: str, date: str = None,
                      current: TokenData = Depends(require_manager)):
    if current.employee_id != manager_id.upper():
        raise HTTPException(403, "You can only view your own site records")

    def _db(mgr_id, d):
        conn = get_db()
        try:
            c = conn.cursor()
            sid = _mgr_site(c, mgr_id)
            q = """SELECT a.*, u.name as labour_name, u.phone, s.name as site_name
                   FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                   JOIN sites s ON a.site_id=s.id WHERE a.site_id=%s"""
            p = [sid]
            if d:
                q += " AND a.date=%s"; p.append(d)
            q += " ORDER BY a.date DESC, a.id DESC"
            c.execute(q, p)
            return [dict(r) for r in c.fetchall()]
        finally:
            release_db(conn)

    return {"records": await run_db(_db, manager_id, date)}


@router.get("/api/manager/stats/{manager_id}")
async def manager_stats(manager_id: str,
                        current: TokenData = Depends(require_manager)):
    if current.employee_id != manager_id.upper():
        raise HTTPException(403, "You can only view your own site stats")

    cache_key = f"cache:mgr:stats:{manager_id.upper()}"
    if (cached := await cache_get(cache_key)):
        return cached

    today = datetime.now().strftime("%Y-%m-%d")

    def _db(mgr_id, t):
        conn = get_db()
        try:
            c = conn.cursor()
            sid = _mgr_site(c, mgr_id)
            c.execute("SELECT COUNT(DISTINCT employee_id) as cnt FROM attendance WHERE site_id=%s AND date=%s", (sid, t))
            tc = c.fetchone()["cnt"]
            c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE site_id=%s AND status='pending_manager'", (sid,))
            pc = c.fetchone()["cnt"]
            c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE site_id=%s AND status IN ('pending_hr','approved') AND date=%s", (sid, t))
            ac = c.fetchone()["cnt"]
            c.execute("SELECT COUNT(*) as cnt FROM users WHERE site_id=%s AND role='labour'", (sid,))
            lc = c.fetchone()["cnt"]
            return {"today_unique_checkins": tc, "pending_count": pc,
                    "approved_count": ac, "total_labours_site": lc}
        finally:
            release_db(conn)

    result = await run_db(_db, manager_id, today)
    await cache_set(cache_key, result, ttl=30)
    return result


@router.post("/api/manager/verify")
async def manager_verify(req: ManagerVerifyReq, request: Request,
                         current: TokenData = Depends(require_manager)):
    if current.employee_id != req.manager_id.upper():
        raise HTTPException(403, "You can only verify records for your own site")

    conn = get_db()
    try:
        c = conn.cursor()
        sid = _mgr_site(c, req.manager_id)
        c.execute("SELECT id,status,employee_id FROM attendance WHERE id=%s AND site_id=%s",
                  (req.attendance_id, sid))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Record not found")
        old_status  = row["status"]
        employee_id = row["employee_id"]
        new_status  = "pending_hr" if req.approved else "rejected"
        c.execute(
            """UPDATE attendance
               SET manager_verified=%s,manager_note=%s,manager_verified_at=NOW(),status=%s
               WHERE id=%s""",
            (req.approved, req.note, new_status, req.attendance_id),
        )
        write_audit(c, "manager_verify", req.manager_id.upper(), "manager",
                    target_id=str(req.attendance_id), target_type="attendance",
                    old_value=old_status, new_value=new_status,
                    metadata={"approved": req.approved, "note": req.note or ""})
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)

    await publish_event({
        "type": "attendance_update", "attendance_id": req.attendance_id,
        "employee_id": employee_id, "status": new_status,
        "updated_by_role": "manager", "site_id": sid,
        "timestamp": datetime.now().isoformat(),
    })
    await cache_invalidate("cache:hr:summary", f"cache:mgr:stats:{req.manager_id.upper()}")
    return {"success": True,
            "message": f"Record {'approved → HR' if req.approved else 'rejected'}"}
