"""
app/routers/reports.py — Scheduled report CRUD, run-now, history.
"""
import json
from datetime import date, datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List

from auth import require_hr, TokenData
from app.core.database import get_db, release_db
from app.core.config import OVERTIME_LIMIT_HOURS
from app.services.llm import call_llm
from app.services.whatsapp import send_whatsapp_alert
from redis_layer import get_redis

router = APIRouter()

# APScheduler (optional)
SCHEDULER_READY = False
_scheduler = None
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_READY = True
    print("✅ APScheduler available — scheduled reports active")
except ImportError:
    print("⚠️  apscheduler not installed — scheduled reports disabled")


class ScheduleReportReq(BaseModel):
    name:        str
    report_type: str
    frequency:   str
    hour:        int = 18
    site_id:     Optional[int] = None
    recipients:  List[str] = []
    delivery:    str = "whatsapp"
    created_by:  str


def get_scheduler():
    return _scheduler


def set_scheduler(s):
    global _scheduler
    _scheduler = s


def _cron_kwargs(frequency: str, hour: int) -> dict:
    if frequency == "daily":   return {"hour": hour, "minute": 0}
    if frequency == "weekly":  return {"day_of_week": "mon", "hour": hour, "minute": 0}
    if frequency == "monthly": return {"day": 1, "hour": hour, "minute": 0}
    return {"hour": hour, "minute": 0}


def _add_job(schedule_id: int, frequency: str, hour: int):
    if not SCHEDULER_READY or not _scheduler:
        return
    job_id = f"report_{schedule_id}"
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass
    _scheduler.add_job(
        run_scheduled_report_task,
        trigger=CronTrigger(**_cron_kwargs(frequency, hour)),
        args=[schedule_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def boot_schedules():
    """Load all enabled schedules from DB on startup."""
    if not SCHEDULER_READY:
        return
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id,frequency,hour FROM scheduled_reports WHERE enabled=TRUE")
        rows = c.fetchall()
    finally:
        release_db(conn)
    for row in rows:
        _add_job(row["id"], row["frequency"], row["hour"])
    print(f"✅ Loaded {len(rows)} scheduled report(s) from DB")


async def run_scheduled_report_task(schedule_id: int):
    """
    Core runner with Redis dedup lock.
    Called by APScheduler (cron) or run_report_now endpoint.
    """
    # ── Dedup lock ────────────────────────────────────────────────────────────
    lock_key = f"sched:{schedule_id}:{date.today().isoformat()}"
    r = await get_redis()
    if r:
        try:
            acquired = await r.set(lock_key, "1", nx=True, ex=3600)
            if not acquired:
                print(f"[SCHEDULER] Job {schedule_id} already running — skipping", flush=True)
                return
        except Exception as _le:
            print(f"[SCHEDULER] Redis lock error: {_le}", flush=True)

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM scheduled_reports WHERE id=%s AND enabled=TRUE", (schedule_id,))
        sched = c.fetchone()
        if not sched:
            return
    finally:
        release_db(conn)

    sched       = dict(sched)
    report_type = sched["report_type"]
    site_id     = sched.get("site_id")
    report_text = None
    ai_used     = False
    error_msg   = None
    status      = "ok"

    # ── Import routers here to avoid circular imports ────────────────────────
    from app.routers.analytics import ai_site_summary, site_analytics
    from auth import TokenData

    # Stub token for internal calls (no real user, just needs role)
    _internal = TokenData(employee_id="SYSTEM", role="hr")

    try:
        if report_type == "site_summary" and site_id:
            payload     = await ai_site_summary(site_id, current=_internal)
            report_text = payload.get("summary") or payload.get("message", "No data")
            ai_used     = payload.get("ai_used", False)

        elif report_type == "overtime_risk":
            conn2 = get_db()
            try:
                c2 = conn2.cursor()
                if site_id:
                    c2.execute("SELECT id,name FROM sites WHERE id=%s", (site_id,))
                else:
                    c2.execute("SELECT id,name FROM sites")
                sites = c2.fetchall()
            finally:
                release_db(conn2)

            lines = []
            for s in sites:
                analytics = await site_analytics(s["id"], current=_internal)
                at_risk   = [d for d in analytics["details"] if d.get("projected_to_exceed_limit")]
                lines.append(f"Site: {s['name']}")
                if at_risk:
                    for d in at_risk:
                        lines.append(f"  ⚠️ {d.get('name', d['employee_id'])}: "
                                     f"{d['month_to_date_hours']}h / projected {d['projected_month_end_hours']}h")
                else:
                    lines.append("  ✅ No overtime risk")
            data_text     = "\n".join(lines)
            system_prompt = (
                "You are an HR analytics assistant. Based on the overtime risk data below, "
                "write a brief daily digest (3-5 sentences) highlighting employees at risk, "
                "mentioning clean sites, and giving one actionable recommendation."
            )
            res         = await call_llm(system_prompt, data_text, max_tokens=400)
            report_text = res["text"] if res["ok"] else data_text
            ai_used     = res["ok"]

        elif report_type == "behaviour":
            from app.routers.ml import ML_ENGINE_READY, ml_engine, ml_load_model
            conn2 = get_db()
            try:
                c2 = conn2.cursor()
                if site_id:
                    c2.execute("SELECT employee_id,name FROM users WHERE site_id=%s AND role='labour'",
                               (site_id,))
                else:
                    c2.execute("SELECT employee_id,name FROM users WHERE role='labour'")
                labourers = c2.fetchall()
            finally:
                release_db(conn2)
            lines = []
            if ML_ENGINE_READY:
                import asyncio
                for lab in labourers:
                    conn3 = get_db()
                    try:
                        c3 = conn3.cursor()
                        c3.execute("""SELECT date,login_time,logout_time,hours_worked
                                      FROM attendance WHERE employee_id=%s
                                        AND date >= CURRENT_DATE - INTERVAL '30 days'
                                        AND hours_worked IS NOT NULL
                                      ORDER BY date DESC""", (lab["employee_id"],))
                        recs = [dict(r) for r in c3.fetchall()]
                    finally:
                        release_db(conn3)
                    if not recs:
                        continue
                    if lab["employee_id"] not in ml_engine._models:
                        await ml_load_model(lab["employee_id"])
                    anomaly = await asyncio.to_thread(
                        ml_engine.detect_anomalies, lab["employee_id"], recs)
                    if anomaly.get("risk_label") in ("high", "medium"):
                        lines.append(f"{lab['name']}: risk={anomaly['risk_label']}, "
                                     f"flags={len(anomaly.get('flags', []))}")
            data_text     = "Behaviour flags (last 30 days):\n" + ("\n".join(lines) if lines else "No flags detected.")
            system_prompt = (
                "Summarise the behaviour anomaly report in 3-4 sentences for a manager. "
                "Focus on high/medium risk employees and suggest one follow-up action."
            )
            res         = await call_llm(system_prompt, data_text, max_tokens=400)
            report_text = res["text"] if res["ok"] else data_text
            ai_used     = res["ok"]

        elif report_type == "full_hr":
            conn2 = get_db()
            try:
                c2 = conn2.cursor()
                c2.execute("SELECT id,name FROM sites")
                sites = c2.fetchall()
            finally:
                release_db(conn2)
            parts = []
            for s in sites:
                payload = await ai_site_summary(s["id"], current=_internal)
                parts.append(f"--- {s['name']} ---\n{payload.get('summary') or '(no data)'}")
            report_text = "\n\n".join(parts)
            ai_used     = True
        else:
            status    = "skipped"
            error_msg = f"Unknown report_type: {report_type}"

    except Exception as e:
        status    = "error"
        error_msg = str(e)

    # ── Delivery ──────────────────────────────────────────────────────────────
    delivered_to = []
    if status == "ok" and report_text:
        recipients = sched.get("recipients") or []
        if isinstance(recipients, str):
            try:
                recipients = json.loads(recipients)
            except Exception:
                recipients = []

        if not recipients:
            conn_r = get_db()
            try:
                c_r = conn_r.cursor()
                if report_type in ("site_summary", "behaviour") and site_id:
                    c_r.execute("SELECT phone FROM users WHERE site_id=%s AND role='manager' AND phone IS NOT NULL",
                                (site_id,))
                else:
                    c_r.execute("SELECT phone FROM users WHERE role IN ('hr','manager') AND phone IS NOT NULL")
                recipients = [r["phone"] for r in c_r.fetchall() if r["phone"]]
            except Exception:
                pass
            finally:
                release_db(conn_r)

        seen  = set()
        phones = [p for p in (str(x).strip() for x in recipients) if p and p not in seen and not seen.add(p)]

        full_msg  = f"📊 *AttendTrack — {sched['name']}*\n{report_text}"
        MAX_CHUNK = 1500
        print(f"[SCHEDULED REPORT] {sched['name']} type={report_type} ai={ai_used}\n{report_text[:400]}",
              flush=True)

        if sched["delivery"] == "whatsapp" and phones:
            lines  = full_msg.split("\n")
            chunks = []
            cur    = ""
            for line in lines:
                cand = cur + ("\n" if cur else "") + line
                if len(cand) <= MAX_CHUNK:
                    cur = cand
                else:
                    if cur: chunks.append(cur)
                    while len(line) > MAX_CHUNK:
                        chunks.append(line[:MAX_CHUNK]); line = line[MAX_CHUNK:]
                    cur = line
            if cur: chunks.append(cur)

            for phone in phones:
                ok = True
                for i, chunk in enumerate(chunks):
                    prefix = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
                    res = send_whatsapp_alert(phone, prefix + chunk)
                    if not res.get("sent"):
                        ok = False
                delivered_to.append({"phone": phone, "sent": ok, "chunks": len(chunks)})
        elif sched["delivery"] == "whatsapp":
            delivered_to.append({"channel": "log_only", "reason": "no_recipients", "sent": False})

    # ── Write run log ─────────────────────────────────────────────────────────
    conn3 = get_db()
    try:
        c3 = conn3.cursor()
        c3.execute("""INSERT INTO report_runs
                          (schedule_id,status,ai_used,report_text,error_msg,delivered_to)
                      VALUES (%s,%s,%s,%s,%s,%s)""",
                   (schedule_id, status, ai_used, report_text, error_msg,
                    json.dumps(delivered_to)))
        c3.execute("UPDATE scheduled_reports SET last_run_at=NOW() WHERE id=%s", (schedule_id,))
        conn3.commit()
    except Exception:
        conn3.rollback()
    finally:
        release_db(conn3)


# ── REST endpoints ─────────────────────────────────────────────────────────────

@router.post("/api/reports/schedule")
async def create_schedule(req: ScheduleReportReq,
                          current: TokenData = Depends(require_hr)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE employee_id=%s AND role='hr'",
                  (req.created_by.upper(),))
        if not c.fetchone():
            raise HTTPException(403, "Only HR can create scheduled reports")
        if req.report_type not in ("site_summary", "overtime_risk", "behaviour", "full_hr"):
            raise HTTPException(400, f"Invalid report_type: {req.report_type}")
        if req.frequency not in ("daily", "weekly", "monthly"):
            raise HTTPException(400, f"Invalid frequency: {req.frequency}")
        if not (0 <= req.hour <= 23):
            raise HTTPException(400, "hour must be 0-23")
        c.execute("""INSERT INTO scheduled_reports
                         (name,report_type,frequency,hour,site_id,recipients,delivery,created_by)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (req.name, req.report_type, req.frequency, req.hour,
                   req.site_id, json.dumps(req.recipients), req.delivery, req.created_by.upper()))
        schedule_id = c.fetchone()["id"]
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)

    _add_job(schedule_id, req.frequency, req.hour)
    return {"success": True, "schedule_id": schedule_id,
            "message": f"Schedule '{req.name}' created — runs {req.frequency} at {req.hour:02d}:00",
            "scheduler_active": SCHEDULER_READY}


@router.get("/api/reports/schedules")
async def list_schedules(current: TokenData = Depends(require_hr)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT sr.*,s.name as site_name FROM scheduled_reports sr
                     LEFT JOIN sites s ON sr.site_id=s.id ORDER BY sr.created_at DESC""")
        return {"schedules": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)


@router.delete("/api/reports/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int, hr_id: str,
                          current: TokenData = Depends(require_hr)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE employee_id=%s AND role='hr'", (hr_id.upper(),))
        if not c.fetchone():
            raise HTTPException(403, "Only HR can delete schedules")
        c.execute("UPDATE scheduled_reports SET enabled=FALSE WHERE id=%s RETURNING id",
                  (schedule_id,))
        if not c.fetchone():
            raise HTTPException(404, "Schedule not found")
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)

    if SCHEDULER_READY and _scheduler:
        try:
            _scheduler.remove_job(f"report_{schedule_id}")
        except Exception:
            pass
    return {"success": True, "message": f"Schedule {schedule_id} disabled"}


@router.post("/api/reports/run-now/{schedule_id}")
async def run_report_now(schedule_id: int,
                         current: TokenData = Depends(require_hr)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM scheduled_reports WHERE id=%s", (schedule_id,))
        if not c.fetchone():
            raise HTTPException(404, "Schedule not found")
    finally:
        release_db(conn)

    await run_scheduled_report_task(schedule_id)

    conn2 = get_db()
    try:
        c2 = conn2.cursor()
        c2.execute("""SELECT ran_at,status,ai_used,report_text,error_msg,delivered_to
                      FROM report_runs WHERE schedule_id=%s ORDER BY ran_at DESC LIMIT 1""",
                   (schedule_id,))
        run = c2.fetchone()
    finally:
        release_db(conn2)

    run_dict   = dict(run) if run else {}
    delivered  = run_dict.get("delivered_to") or []
    if isinstance(delivered, str):
        try: delivered = json.loads(delivered)
        except: delivered = []
    sent_count = sum(1 for d in delivered if d.get("sent"))

    return {"success":          run_dict.get("status") == "ok",
            "status":           run_dict.get("status"),
            "ai_used":          run_dict.get("ai_used"),
            "delivered_to":     delivered,
            "sent_count":       sent_count,
            "total_recipients": len(delivered),
            "error":            run_dict.get("error_msg"),
            "report_preview":   (run_dict.get("report_text") or "")[:400],
            "message":          (f"✅ Report sent to {sent_count}/{len(delivered)} recipient(s)"
                                 if sent_count > 0
                                 else "⚠️ Report generated but not sent — check Twilio config")}


@router.get("/api/reports/history/{schedule_id}")
async def report_history(schedule_id: int, limit: int = 10,
                         current: TokenData = Depends(require_hr)):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT ran_at,status,ai_used,error_msg,delivered_to
                     FROM report_runs WHERE schedule_id=%s ORDER BY ran_at DESC LIMIT %s""",
                  (schedule_id, limit))
        return {"schedule_id": schedule_id, "runs": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)
