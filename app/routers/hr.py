"""
app/routers/hr.py — HR portal endpoints.
"""
import io
import json
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from auth import require_hr, get_current_user, TokenData
from app.core.database import get_db, release_db, run_db, write_audit
from app.core.cache import cache_get, cache_set, cache_invalidate
from app.core.config import OVERTIME_LIMIT_HOURS
from app.services.llm import call_llm
from redis_layer import publish_event

router = APIRouter()


def _month_bounds(d: datetime):
    start = d.replace(day=1)
    if d.month == 12:
        end = d.replace(year=d.year + 1, month=1, day=1)
    else:
        end = d.replace(month=d.month + 1, day=1)
    return start.date(), end.date()


# ── Pending / all / summary ───────────────────────────────────────────────────

@router.get("/api/hr/pending/{hr_id}")
async def hr_pending(hr_id: str, current: TokenData = Depends(require_hr)):
    if current.employee_id != hr_id.upper():
        raise HTTPException(403, "You can only view your own pending queue")

    def _db():
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute("""SELECT a.*, u.name as labour_name, u.phone, u.department,
                                s.name as site_name
                         FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                         JOIN sites s ON a.site_id=s.id
                         WHERE a.status='pending_hr' AND a.manager_verified=TRUE
                         ORDER BY a.date DESC""")
            return [dict(r) for r in c.fetchall()]
        finally:
            release_db(conn)

    return {"records": await run_db(_db)}


@router.get("/api/hr/all/{hr_id}")
async def hr_all(hr_id: str, date: str = None, employee_id: str = None,
                 current: TokenData = Depends(require_hr)):
    if current.employee_id != hr_id.upper():
        raise HTTPException(403, "Forbidden")

    def _db(d, eid):
        conn = get_db()
        try:
            c = conn.cursor()
            q = """SELECT a.*, u.name as labour_name, u.phone, u.department,
                          s.name as site_name
                   FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                   JOIN sites s ON a.site_id=s.id
                   WHERE a.status IN ('pending_hr','approved','rejected')
                     AND a.manager_verified=TRUE"""
            p = []
            if d:   q += " AND a.date=%s";        p.append(d)
            if eid: q += " AND a.employee_id=%s"; p.append(eid)
            q += " ORDER BY a.date DESC LIMIT 500"
            c.execute(q, p)
            return [dict(r) for r in c.fetchall()]
        finally:
            release_db(conn)

    return {"records": await run_db(_db, date, employee_id)}


@router.get("/api/hr/summary")
async def hr_summary(current: TokenData = Depends(require_hr)):
    cache_key = "cache:hr:summary"
    if (cached := await cache_get(cache_key)):
        return cached

    today = datetime.now().strftime("%Y-%m-%d")

    def _db(t):
        conn = get_db()
        try:
            c = conn.cursor()
            c.execute("SELECT COUNT(DISTINCT employee_id) as cnt FROM attendance WHERE date=%s", (t,))
            tc = c.fetchone()["cnt"]
            c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='pending_manager'")
            pm = c.fetchone()["cnt"]
            c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='pending_hr' AND manager_verified=TRUE")
            ph = c.fetchone()["cnt"]
            c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='approved' AND date=%s", (t,))
            at = c.fetchone()["cnt"]
            c.execute("SELECT COUNT(*) as cnt FROM users WHERE role='labour'")
            te = c.fetchone()["cnt"]
            return {"today_checkins": tc, "pending_manager": pm, "pending_hr": ph,
                    "approved_today": at, "total_employees": te}
        finally:
            release_db(conn)

    result = await run_db(_db, today)
    await cache_set(cache_key, result, ttl=30)
    return result


# ── HR verify ─────────────────────────────────────────────────────────────────

@router.post("/api/hr/verify")
async def hr_verify(req_body: dict, request: Request,
                    current: TokenData = Depends(require_hr)):
    # Accept both pydantic model and raw dict to stay compatible
    attendance_id = req_body.get("attendance_id")
    hr_id         = req_body.get("hr_id", "")
    approved      = req_body.get("approved", False)
    note          = req_body.get("note")

    if current.employee_id != hr_id.upper():
        raise HTTPException(403, "You can only verify as yourself")

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id,status,employee_id,site_id FROM attendance WHERE id=%s",
                  (attendance_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Record not found")
        old_status  = row["status"]
        employee_id = row["employee_id"]
        site_id     = row["site_id"]
        new_status  = "approved" if approved else "rejected"
        c.execute(
            """UPDATE attendance
               SET hr_verified=%s,hr_note=%s,hr_verified_at=NOW(),status=%s
               WHERE id=%s""",
            (approved, note, new_status, attendance_id),
        )
        write_audit(c, "hr_verify", hr_id.upper(), "hr",
                    target_id=str(attendance_id), target_type="attendance",
                    old_value=old_status, new_value=new_status,
                    metadata={"approved": approved, "note": note or ""})
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)

    await publish_event({
        "type": "attendance_update", "attendance_id": attendance_id,
        "employee_id": employee_id, "status": new_status,
        "updated_by_role": "hr", "site_id": site_id,
        "timestamp": datetime.now().isoformat(),
    })
    await cache_invalidate("cache:hr:summary")
    return {"success": True, "message": f"Record {new_status}."}


# ── HR Ask (AI Q&A) ───────────────────────────────────────────────────────────

class HRAskReq(BaseModel):
    hr_id:       str
    question:    str
    employee_id: Optional[str] = None


@router.post("/api/hr/ask")
async def hr_ask(req: HRAskReq, current: TokenData = Depends(require_hr)):
    if not req.question or not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")
    if len(req.question) > 800:
        raise HTTPException(400, "Question too long (max 800 characters)")

    conn = get_db()
    try:
        c = conn.cursor()
        as_of = datetime.now()
        ps, pe = _month_bounds(as_of)

        if req.employee_id:
            eid = req.employee_id.upper()
            c.execute("""SELECT u.employee_id, u.name, u.phone, u.department,
                                u.face_registered, s.name as site_name
                         FROM users u LEFT JOIN sites s ON u.site_id=s.id
                         WHERE u.employee_id=%s AND u.role='labour'""", (eid,))
            emp = c.fetchone()
            if not emp:
                raise HTTPException(404, f"Labour employee {eid} not found")
            employees = [dict(emp)]
            c.execute("""SELECT a.date,a.login_time,a.logout_time,a.hours_worked,
                                a.status,a.manager_note,a.hr_note,s.name as site_name
                         FROM attendance a JOIN sites s ON a.site_id=s.id
                         WHERE a.employee_id=%s ORDER BY a.date DESC LIMIT 60""", (eid,))
            records_map = {eid: [dict(r) for r in c.fetchall()]}
            c.execute("""SELECT COALESCE(SUM(hours_worked),0) as mh, COUNT(*) as md
                         FROM attendance WHERE employee_id=%s AND date>=%s AND date<%s
                           AND hours_worked IS NOT NULL""", (eid, ps, pe))
            mrow = c.fetchone()
            month_summary = {eid: {"hours": float(mrow["mh"]), "days": int(mrow["md"])}}
        else:
            c.execute("""SELECT u.employee_id,u.name,u.phone,u.department,
                                u.face_registered,s.name as site_name
                         FROM users u LEFT JOIN sites s ON u.site_id=s.id
                         WHERE u.role='labour' ORDER BY u.name""")
            employees = [dict(r) for r in c.fetchall()]
            c.execute("""SELECT employee_id,COALESCE(SUM(hours_worked),0) as mh,COUNT(*) as md
                         FROM attendance WHERE date>=%s AND date<%s AND hours_worked IS NOT NULL
                         GROUP BY employee_id""", (ps, pe))
            month_summary = {r["employee_id"]: {"hours": float(r["mh"]), "days": int(r["md"])}
                             for r in c.fetchall()}
            records_map = {}
            for emp in employees[:20]:
                eid = emp["employee_id"]
                c.execute("""SELECT a.date,a.login_time,a.logout_time,a.hours_worked,
                                    a.status,s.name as site_name
                             FROM attendance a JOIN sites s ON a.site_id=s.id
                             WHERE a.employee_id=%s ORDER BY a.date DESC LIMIT 30""", (eid,))
                records_map[eid] = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    context_lines = []
    for emp in employees:
        eid  = emp["employee_id"]
        ms   = month_summary.get(eid, {"hours": 0, "days": 0})
        recs = records_map.get(eid, [])
        ot_flag = "⚠️ OVERTIME RISK" if ms["hours"] >= OVERTIME_LIMIT_HOURS * 0.85 else ""
        context_lines.append(
            f"\n[{eid}] {emp['name']} | {emp.get('department') or 'Labour'} | "
            f"Site: {emp.get('site_name') or 'unassigned'} {ot_flag}"
        )
        context_lines.append(
            f"  This month: {ms['hours']:.1f}h across {ms['days']} day(s) "
            f"(cap: {OVERTIME_LIMIT_HOURS:.0f}h)")
        if recs:
            context_lines.append("  Recent records (newest first):")
            for r in recs[:10]:
                lo = str(r["logout_time"])[:16] if r["logout_time"] else "–"
                li = str(r["login_time"])[:16]  if r["login_time"]  else "–"
                context_lines.append(
                    f"    {str(r['date'])[:10]} | In:{li[-5:]} Out:{lo[-5:]} "
                    f"| {r['hours_worked'] or 0:.1f}h | {r['status']}")

    system_prompt = (
        "You are an HR intelligence assistant for a construction-site workforce management system. "
        "Answer the HR officer's question precisely using only the data provided. "
        "Be concise (max 6 sentences or a short table) and flag anomalies proactively. "
        f"Overtime cap is {OVERTIME_LIMIT_HOURS:.0f} hours/month."
    )
    user_prompt = (
        f"HR Question: {req.question.strip()}\n\n"
        f"Employee data ({len(employees)} employee(s)):\n" + "\n".join(context_lines)
    )

    result = await call_llm(system_prompt, user_prompt, max_tokens=500)
    answer = result["text"] if result["ok"] else (
        f"AI unavailable ({result.get('error', 'not configured')}). Please review manually.")

    conn2 = get_db()
    try:
        c2 = conn2.cursor()
        c2.execute("""INSERT INTO hr_qa (hr_id,question,about_employee_id,answer)
                      VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                   (req.hr_id.upper(), req.question.strip(),
                    req.employee_id.upper() if req.employee_id else None, answer))
        conn2.commit()
    except Exception:
        conn2.rollback()
    finally:
        release_db(conn2)

    return {"hr_id": req.hr_id.upper(), "question": req.question.strip(),
            "about": req.employee_id.upper() if req.employee_id else "all employees",
            "answer": answer, "ai_used": result["ok"],
            "employees_in_context": len(employees)}


# ── Audit log ─────────────────────────────────────────────────────────────────

@router.get("/api/audit-log")
async def get_audit_log(hr_id: str, limit: int = 100, offset: int = 0,
                        actor_id: str = None, target_id: str = None,
                        action: str = None,
                        current: TokenData = Depends(require_hr)):
    conn = get_db()
    try:
        c = conn.cursor()
        q = "SELECT * FROM audit_log WHERE 1=1"
        p: list = []
        if actor_id:  q += " AND actor_id=%s";  p.append(actor_id.upper())
        if target_id: q += " AND target_id=%s"; p.append(target_id)
        if action:    q += " AND action=%s";     p.append(action)
        q += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        p += [min(limit, 500), offset]
        c.execute(q, p)
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) as cnt FROM audit_log")
        total = c.fetchone()["cnt"]
    finally:
        release_db(conn)
    return {"total": total, "limit": limit, "offset": offset,
            "entries": [dict(r) for r in rows]}


# ── Excel report ──────────────────────────────────────────────────────────────

@router.get("/api/hr/report/excel")
async def hr_excel_report(
    period: str = "monthly",
    year:   int = None,
    month:  int = None,
    week:   int = None,
    hr_id:  str = "HR001",
    current: TokenData = Depends(require_hr),
):
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        import datetime as dt_mod
    except ImportError:
        raise HTTPException(503, "openpyxl not installed")

    now = datetime.now()
    yr  = year  or now.year
    mo  = month or now.month

    if period == "weekly":
        wk      = week or now.isocalendar()[1]
        d_start = dt_mod.date.fromisocalendar(yr, wk, 1)
        d_end   = d_start + dt_mod.timedelta(days=7)
        period_label = f"Week {wk}, {yr}"
    else:
        d_start = dt_mod.date(yr, mo, 1)
        d_end   = dt_mod.date(yr + 1, 1, 1) if mo == 12 else dt_mod.date(yr, mo + 1, 1)
        period_label = dt_mod.date(yr, mo, 1).strftime("%B %Y")

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT u.employee_id,u.name,u.phone,u.department,
                            s.name as site_name,s.id as site_id
                     FROM users u LEFT JOIN sites s ON u.site_id=s.id
                     WHERE u.role='labour' ORDER BY s.name,u.name""")
        employees = [dict(r) for r in c.fetchall()]
        c.execute("""SELECT a.*,u.name as emp_name,u.department,s.name as site_name
                     FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                     JOIN sites s ON a.site_id=s.id
                     WHERE a.date>=%s AND a.date<%s ORDER BY s.name,u.name,a.date""",
                  (d_start, d_end))
        att_rows = [dict(r) for r in c.fetchall()]
        c.execute("SELECT id,name FROM sites ORDER BY id")
        sites = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    # Build workbook
    wb = openpyxl.Workbook()
    HDR = PatternFill("solid", fgColor="3B0764")
    HDR_FONT = Font(bold=True, color="FFFFFF", size=10)

    def _hdr_row(ws, cols):
        for i, h in enumerate(cols, 1):
            cell = ws.cell(row=1, column=i, value=h)
            cell.fill = HDR_FONT and HDR
            cell.font = HDR_FONT

    # Sheet 1: Summary
    ws1 = wb.active
    ws1.title = "Summary"
    _hdr_row(ws1, ["Site", "Total Employees", "Total Hours", "Avg Hours/Day", "At Overtime Risk"])
    for i, s in enumerate(sites, 2):
        s_recs = [r for r in att_rows if r["site_name"] == s["name"]]
        s_emps = {r["employee_id"] for r in s_recs}
        total_h = sum(r["hours_worked"] or 0 for r in s_recs)
        days    = max(1, len({r["date"] for r in s_recs}))
        at_risk = sum(1 for eid in s_emps
                      if sum(r["hours_worked"] or 0 for r in s_recs
                             if r["employee_id"] == eid) >= OVERTIME_LIMIT_HOURS * 0.85)
        ws1.append([s["name"], len(s_emps), round(total_h, 1),
                    round(total_h / days, 2), at_risk])

    # Sheet 2: Attendance
    ws2 = wb.create_sheet("Attendance")
    _hdr_row(ws2, ["Employee ID", "Name", "Department", "Site", "Date",
                   "Login", "Logout", "Hours", "Status"])
    for r in att_rows:
        ws2.append([r["employee_id"], r["emp_name"], r["department"], r["site_name"],
                    str(r["date"])[:10],
                    str(r["login_time"])[:16] if r["login_time"] else "",
                    str(r["logout_time"])[:16] if r["logout_time"] else "",
                    round(r["hours_worked"] or 0, 2), r["status"]])

    # Sheet 3: Overtime
    ws3 = wb.create_sheet("Overtime")
    _hdr_row(ws3, ["Employee ID", "Name", "Site", "Hours Worked", "Limit", "% Used"])
    for emp in employees:
        h = sum(r["hours_worked"] or 0 for r in att_rows
                if r["employee_id"] == emp["employee_id"])
        if h >= OVERTIME_LIMIT_HOURS * 0.8:
            pct = round(h / OVERTIME_LIMIT_HOURS * 100, 1)
            ws3.append([emp["employee_id"], emp["name"], emp["site_name"],
                        round(h, 1), OVERTIME_LIMIT_HOURS, pct])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"AttendTrack_{period_label.replace(' ', '_')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
