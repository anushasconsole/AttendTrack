"""
app/routers/analytics.py — Overtime analytics, AI site summary, employee list, sites.
"""
import math
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from auth import get_current_user, require_manager_or_hr, TokenData
from app.core.database import get_db, release_db
from app.core.cache import cache_get, cache_set
from app.core.config import OVERTIME_LIMIT_HOURS
from app.services.llm import call_llm

router = APIRouter()


def _month_bounds(d: datetime):
    start = d.replace(day=1)
    if d.month == 12:
        end = d.replace(year=d.year + 1, month=1, day=1)
    else:
        end = d.replace(month=d.month + 1, day=1)
    return start.date(), end.date()


# ── Employee analytics ─────────────────────────────────────────────────────────

@router.get("/api/analytics/employee/{employee_id}")
async def employee_analytics(employee_id: str, lookback_days: int = 90,
                              current: TokenData = Depends(get_current_user)):
    cache_key = f"cache:analytics:emp:{employee_id.upper()}"
    if (cached := await cache_get(cache_key)):
        return cached

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM users WHERE employee_id=%s", (employee_id,))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "Employee not found")
        c.execute("""SELECT date,hours_worked FROM attendance
                     WHERE employee_id=%s AND hours_worked IS NOT NULL
                       AND date >= CURRENT_DATE - INTERVAL '%s days'
                     ORDER BY date ASC""", (employee_id, lookback_days))
        rows = c.fetchall()
        as_of = datetime.now()
        period_start, period_end = _month_bounds(as_of)
        c.execute("""SELECT COALESCE(SUM(hours_worked),0) as total FROM attendance
                     WHERE employee_id=%s AND date>=%s AND date<%s
                       AND hours_worked IS NOT NULL""",
                  (employee_id, period_start, period_end))
        month_hours = float(c.fetchone()["total"])
    finally:
        release_db(conn)

    if not rows:
        return {"employee_id": employee_id, "name": user["name"], "data_points": 0,
                "message": "Not enough historical attendance data yet."}

    total        = sum(r["hours_worked"] for r in rows)
    avg_per_day  = total / len(rows)
    days_so_far  = (as_of.date() - period_start).days + 1
    days_in_month = (period_end - period_start).days
    days_left    = max(0, days_in_month - days_so_far)
    projected    = round(month_hours + avg_per_day * days_left, 1)
    will_exceed  = projected >= OVERTIME_LIMIT_HOURS
    days_to_limit = None
    if avg_per_day > 0 and month_hours < OVERTIME_LIMIT_HOURS:
        days_to_limit = math.ceil((OVERTIME_LIMIT_HOURS - month_hours) / avg_per_day)

    result = {
        "employee_id":                           employee_id,
        "name":                                  user["name"],
        "data_points":                           len(rows),
        "lookback_days":                         lookback_days,
        "avg_hours_per_worked_day":              round(avg_per_day, 2),
        "month_to_date_hours":                   round(month_hours, 1),
        "overtime_limit_hours":                  OVERTIME_LIMIT_HOURS,
        "projected_month_end_hours":             projected,
        "projected_to_exceed_limit":             will_exceed,
        "estimated_days_until_limit_at_current_pace": days_to_limit,
        "method": "linear projection from trailing average",
    }
    await cache_set(cache_key, result, ttl=60)
    return result


# ── Site analytics (batch, no N+1) ───────────────────────────────────────────

@router.get("/api/analytics/site/{site_id}")
async def site_analytics(site_id: int,
                         current: TokenData = Depends(require_manager_or_hr)):
    cache_key = f"cache:analytics:site:{site_id}"
    if (cached := await cache_get(cache_key)):
        return cached

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id,name FROM users WHERE site_id=%s AND role='labour'",
                  (site_id,))
        labourers = c.fetchall()
        if not labourers:
            result = {"site_id": site_id, "employees_analyzed": 0,
                      "at_risk_of_overtime": 0, "details": []}
            await cache_set(cache_key, result, ttl=60)
            return result

        emp_ids = [lab["employee_id"] for lab in labourers]
        as_of = datetime.now()
        period_start, period_end = _month_bounds(as_of)

        c.execute("""SELECT employee_id, COALESCE(SUM(hours_worked),0) as month_hours
                     FROM attendance WHERE employee_id=ANY(%s) AND date>=%s AND date<%s
                       AND hours_worked IS NOT NULL
                     GROUP BY employee_id""", (emp_ids, period_start, period_end))
        month_map = {r["employee_id"]: float(r["month_hours"]) for r in c.fetchall()}

        c.execute("""SELECT employee_id,date,hours_worked FROM attendance
                     WHERE employee_id=ANY(%s) AND hours_worked IS NOT NULL
                       AND date >= CURRENT_DATE - INTERVAL '90 days'
                     ORDER BY employee_id, date ASC""", (emp_ids,))
        records_map: dict = {}
        for row in c.fetchall():
            records_map.setdefault(row["employee_id"], []).append(row)
    finally:
        release_db(conn)

    days_so_far   = (as_of.date() - period_start).days + 1
    days_in_month = (period_end - period_start).days
    days_left     = max(0, days_in_month - days_so_far)

    details = []
    for lab in labourers:
        eid         = lab["employee_id"]
        rows        = records_map.get(eid, [])
        month_hours = month_map.get(eid, 0.0)
        if not rows:
            details.append({"employee_id": eid, "name": lab["name"], "data_points": 0,
                            "message": "No attendance data yet."})
            continue
        total       = sum(float(r["hours_worked"]) for r in rows)
        avg         = total / len(rows)
        projected   = round(month_hours + avg * days_left, 1)
        will_exceed = projected >= OVERTIME_LIMIT_HOURS
        days_to_limit = None
        if avg > 0 and month_hours < OVERTIME_LIMIT_HOURS:
            days_to_limit = math.ceil((OVERTIME_LIMIT_HOURS - month_hours) / avg)
        details.append({
            "employee_id": eid, "name": lab["name"],
            "data_points": len(rows),
            "avg_hours_per_worked_day":  round(avg, 2),
            "month_to_date_hours":       round(month_hours, 1),
            "overtime_limit_hours":      OVERTIME_LIMIT_HOURS,
            "projected_month_end_hours": projected,
            "projected_to_exceed_limit": will_exceed,
            "estimated_days_until_limit_at_current_pace": days_to_limit,
            "method": "batch linear projection",
        })

    at_risk = sum(1 for d in details if d.get("projected_to_exceed_limit"))
    result  = {"site_id": site_id, "employees_analyzed": len(details),
               "at_risk_of_overtime": at_risk, "details": details}
    await cache_set(cache_key, result, ttl=60)
    return result


# ── AI site summary / checkin flag ───────────────────────────────────────────

@router.get("/api/ai/site-summary/{site_id}")
async def ai_site_summary(site_id: int,
                          current: TokenData = Depends(require_manager_or_hr)):
    analytics = await site_analytics(site_id, current=current)
    if not analytics["employees_analyzed"]:
        return {"site_id": site_id, "ai_used": False,
                "message": "No labour assigned to this site yet."}

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM sites WHERE id=%s", (site_id,))
        site = c.fetchone()
    finally:
        release_db(conn)
    site_name = site["name"] if site else f"Site {site_id}"

    system_prompt = (
        "You are an HR analytics assistant for a construction-site attendance system. "
        "Write a short plain-English report: 2-3 sentence overview, then bullet points "
        "flagging anyone at overtime risk and one practical recommendation. "
        "Base every claim only on the numbers given."
    )
    user_prompt = f"Site: {site_name}\nOvertime limit: {OVERTIME_LIMIT_HOURS} hours/month\n\nWorker data:\n"
    for d in analytics["details"]:
        if not d.get("data_points"):
            user_prompt += f"- {d['employee_id']}: no data yet\n"
        else:
            user_prompt += (
                f"- {d['employee_id']}: {d['month_to_date_hours']}h so far, "
                f"avg {d['avg_hours_per_worked_day']}h/day, "
                f"projected {d['projected_month_end_hours']}h, "
                f"{'AT RISK' if d['projected_to_exceed_limit'] else 'on track'}\n"
            )

    result = await call_llm(system_prompt, user_prompt)
    return {
        "site_id": site_id, "site_name": site_name,
        "ai_used":     result["ok"],
        "provider":    None if not result["ok"] else "configured",
        "summary":     result["text"],
        "error":       result["error"],
        "raw_analytics": analytics,
    }


@router.get("/api/ai/checkin-flag/{attendance_id}")
async def ai_checkin_flag(attendance_id: int,
                          current: TokenData = Depends(require_manager_or_hr)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT a.*,u.name FROM attendance a
                     JOIN users u ON a.employee_id=u.employee_id WHERE a.id=%s""",
                  (attendance_id,))
        rec = c.fetchone()
        if not rec:
            raise HTTPException(404, "Attendance record not found")
        c.execute("""SELECT date,login_time,logout_time,hours_worked FROM attendance
                     WHERE employee_id=%s AND id!=%s ORDER BY date DESC LIMIT 10""",
                  (rec["employee_id"], attendance_id))
        history = c.fetchall()
    finally:
        release_db(conn)

    system_prompt = (
        "You are reviewing one attendance record against a worker's recent history. "
        "Respond in 1-2 short sentences. Start with 'FLAG:' if something looks unusual "
        "or 'OK:' if it looks consistent. Base your answer only on the data given."
    )
    user_prompt = (
        f"Worker: {rec['name']}\nRecord: date={rec['date']}, "
        f"login={rec['login_time']}, logout={rec.get('logout_time')}, "
        f"hours={rec.get('hours_worked')}\n\nRecent history:\n"
    )
    for h in history:
        user_prompt += f"- {h['date']}: {h['hours_worked']}h\n"
    if not history:
        user_prompt += "(no prior history)\n"

    result = await call_llm(system_prompt, user_prompt, max_tokens=150)
    return {"attendance_id": attendance_id, "ai_used": result["ok"],
            "flag": result["text"], "error": result["error"]}


# ── Employee list / sites ─────────────────────────────────────────────────────

@router.get("/api/employees")
async def get_employees(current: TokenData = Depends(require_manager_or_hr)):
    cache_key = "cache:employees"
    if (cached := await cache_get(cache_key)):
        return cached
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT u.employee_id,u.name,u.role,u.phone,u.department,
                            u.face_registered,s.name as site_name
                     FROM users u LEFT JOIN sites s ON u.site_id=s.id
                     ORDER BY u.role,u.name""")
        result = {"employees": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)
    await cache_set(cache_key, result, ttl=120)
    return result


@router.get("/api/sites")
async def get_sites(current: TokenData = Depends(get_current_user)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM sites")
        return {"sites": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)
