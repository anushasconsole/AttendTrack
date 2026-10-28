"""
app/routers/employee_qa.py — Labour self-service AI Q&A.
"""
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel

from auth import require_labour, TokenData
from app.core.database import get_db, release_db
from app.core.config import OVERTIME_LIMIT_HOURS
from app.services.llm import call_llm
from redis_layer import get_redis, RateLimiter

router = APIRouter()

ATTENDANCE_POLICY = f"""
AttendTrack Attendance Policy:

1. CHECK-IN / CHECK-OUT
   - Each employee must check in once per working day via the Labour Portal.
   - Face verification is required before every check-in.
   - Location must be within the geofence of the assigned site.

2. APPROVAL WORKFLOW
   - After checkout: pending_manager → pending_hr → approved/rejected
   - Only approved records count toward your monthly total.

3. OVERTIME CAP
   - The monthly cap is {OVERTIME_LIMIT_HOURS:.0f} hours.
   - A WhatsApp alert is sent when you approach or exceed the cap.

4. STATUS MEANINGS
   - active: currently checked in.
   - pending_manager: waiting for manager approval.
   - pending_hr: waiting for HR final approval.
   - approved: fully counted.
   - rejected: contact your manager.
""".strip()


class EmployeeAskReq(BaseModel):
    employee_id: str
    question:    str


@router.post("/api/employee/ask")
async def employee_ask(req: EmployeeAskReq, request: Request,
                       current: TokenData = Depends(require_labour)):
    if current.employee_id != req.employee_id.upper():
        raise HTTPException(403, "You can only ask questions about your own data")
    if not req.question or not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")
    if len(req.question) > 500:
        raise HTTPException(400, "Question too long (max 500 characters)")

    r = await get_redis()
    rl = RateLimiter(r)
    try:
        await rl.check("ai_ask", req.employee_id.upper())
    except RateLimiter.RateLimitExceeded as e:
        raise HTTPException(429, detail=str(e))

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name,employee_id FROM users WHERE employee_id=%s AND role='labour'",
                  (req.employee_id.upper(),))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "Employee not found")

        c.execute("""SELECT a.date,a.login_time,a.logout_time,a.hours_worked,a.status,
                            s.name as site_name
                     FROM attendance a JOIN sites s ON a.site_id=s.id
                     WHERE a.employee_id=%s ORDER BY a.date DESC,a.id DESC LIMIT 30""",
                  (req.employee_id.upper(),))
        records = c.fetchall()

        as_of = datetime.now()
        from app.services.overtime import _month_bounds
        ps, pe = _month_bounds(as_of)
        c.execute("""SELECT COALESCE(SUM(hours_worked),0) as total, COUNT(*) as days
                     FROM attendance WHERE employee_id=%s AND date>=%s AND date<%s
                       AND hours_worked IS NOT NULL""",
                  (req.employee_id.upper(), ps, pe))
        mrow = c.fetchone()
    finally:
        release_db(conn)

    month_hours = float(mrow["total"])
    month_days  = int(mrow["days"])

    records_text = ""
    for r in records:
        lo = str(r["logout_time"])[:16] if r["logout_time"] else "–"
        li = str(r["login_time"])[:16]  if r["login_time"]  else "–"
        records_text += (f"  {str(r['date'])[:10]} | In:{li[-5:]} Out:{lo[-5:]} "
                         f"| {r['hours_worked'] or 0:.1f}h | {r['status']} | {r['site_name']}\n")
    if not records_text:
        records_text = "  (no attendance records yet)\n"

    system_prompt = (
        f"You are an attendance assistant helping {user['name']} "
        f"(ID: {user['employee_id']}) understand their attendance records.\n\n"
        f"POLICY:\n{ATTENDANCE_POLICY}\n\n"
        "Answer only from the data given. Be concise (≤ 4 sentences). "
        "Never reveal other employees' data."
    )
    user_prompt = (
        f"Employee: {user['name']} ({user['employee_id']})\n"
        f"This month: {month_hours:.1f}h across {month_days} day(s) "
        f"(cap: {OVERTIME_LIMIT_HOURS:.0f}h)\n\n"
        f"Recent records:\n{records_text}\n"
        f"Question: {req.question.strip()}"
    )

    result = await call_llm(system_prompt, user_prompt, max_tokens=300)
    answer = result["text"] if result["ok"] else (
        f"Sorry, I'm unable to process your question right now. "
        f"Please contact your manager or HR directly.")

    conn2 = get_db()
    try:
        c2 = conn2.cursor()
        c2.execute("INSERT INTO employee_qa (employee_id,question,answer) VALUES (%s,%s,%s)",
                   (req.employee_id.upper(), req.question.strip(), answer))
        conn2.commit()
    except Exception:
        conn2.rollback()
    finally:
        release_db(conn2)

    return {"employee_id": req.employee_id.upper(), "question": req.question.strip(),
            "answer": answer, "ai_used": result["ok"],
            "context": {"month_hours": round(month_hours, 1), "month_days": month_days,
                        "records_shown": len(records)}}


@router.get("/api/employee/qa-history/{employee_id}")
async def employee_qa_history(employee_id: str, limit: int = 20,
                              current: TokenData = Depends(require_labour)):
    if current.employee_id != employee_id.upper():
        raise HTTPException(403, "You can only view your own Q&A history")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE employee_id=%s AND role='labour'",
                  (employee_id.upper(),))
        if not c.fetchone():
            raise HTTPException(404, "Employee not found")
        c.execute("""SELECT question,answer,created_at FROM employee_qa
                     WHERE employee_id=%s ORDER BY created_at DESC LIMIT %s""",
                  (employee_id.upper(), limit))
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"employee_id": employee_id.upper(), "history": [dict(r) for r in rows]}
