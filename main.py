import os, json, math, hashlib, base64, io, asyncio
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
from dotenv import load_dotenv
from contextlib import asynccontextmanager

load_dotenv()

# ─────────────────────────────────────────────
# FACE RECOGNITION — optional, install separately
# ─────────────────────────────────────────────
FACE_READY = False
try:
    import face_recognition
    import numpy as np
    from PIL import Image
    FACE_READY = True
    print("✅ face_recognition ready — biometric verification active")
except ImportError:
    print("⚠️  face_recognition not installed — running in BYPASS mode (face always passes)")

# ─────────────────────────────────────────────
# DATABASE — PostgreSQL (required)
# ─────────────────────────────────────────────
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pg_pool

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL not set in .env file!")

db_pool = pg_pool.ThreadedConnectionPool(
    minconn=2, maxconn=20,
    dsn=DATABASE_URL,
    cursor_factory=RealDictCursor
)
print("✅ PostgreSQL pool ready")

# ─────────────────────────────────────────────
# OVERTIME ALERTS — WhatsApp via Twilio (optional, degrades gracefully)
# ─────────────────────────────────────────────
OVERTIME_LIMIT_HOURS = float(os.getenv("OVERTIME_LIMIT_HOURS", "0.01"))
OVERTIME_WARN_AT     = float(os.getenv("OVERTIME_WARN_AT", "0.5"))  # warn at 90% of limit

TWILIO_SID        = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM       = os.getenv("TWILIO_WHATSAPP_FROM", "")  # e.g. "whatsapp:+14155238886"
# Default country code for numbers stored without one (e.g. "9876543210" → "+91 9876543210")
# Set WHATSAPP_DEFAULT_CC=+968 for Oman, +91 for India, etc.
WHATSAPP_DEFAULT_CC = os.getenv("WHATSAPP_DEFAULT_CC", "+91")

WHATSAPP_READY = False
twilio_client = None
try:
    if TWILIO_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM:
        from twilio.rest import Client as TwilioClient
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN)
        WHATSAPP_READY = True
        print("✅ Twilio WhatsApp ready — overtime alerts will be sent")
    else:
        print("⚠️  Twilio env vars not set — overtime alerts will be LOGGED ONLY (no WhatsApp send)")
except ImportError:
    print("⚠️  twilio package not installed — overtime alerts will be LOGGED ONLY")

def send_whatsapp_alert(to_phone: str, message: str) -> dict:
    """
    Sends a WhatsApp message via Twilio.
    Handles phone numbers stored in any of these formats:
      9876543210        → assumes WHATSAPP_DEFAULT_CC country code (e.g. +91)
      919876543210      → already has country code digits, just add +
      +919876543210     → already fully qualified
      whatsapp:+91...   → already in Twilio format
    Falls back to logging if Twilio is not configured.
    """
    if not to_phone:
        return {"sent": False, "reason": "no_phone_on_file"}
    if not WHATSAPP_READY:
        print(f"[WHATSAPP NOT SENT — no Twilio config] to={to_phone}: {message[:80]}…", flush=True)
        return {"sent": False, "reason": "twilio_not_configured"}
    try:
        raw = str(to_phone).strip()
        # Strip existing whatsapp: prefix so we can normalise cleanly
        if raw.lower().startswith("whatsapp:"):
            raw = raw[9:]
        # Strip leading +
        digits = raw.lstrip("+").strip()
        # If the number looks like a local number (≤10 digits), prepend default country code
        if len(digits) <= 10:
            digits = WHATSAPP_DEFAULT_CC.lstrip("+") + digits
        to_whatsapp = f"whatsapp:+{digits}"
        msg = twilio_client.messages.create(from_=TWILIO_FROM, to=to_whatsapp, body=message)
        print(f"[WHATSAPP SENT] sid={msg.sid} to={to_whatsapp}", flush=True)
        return {"sent": True, "sid": msg.sid, "to": to_whatsapp}
    except Exception as e:
        print(f"[WHATSAPP FAILED] to={to_phone}: {type(e).__name__}: {e}", flush=True)
        return {"sent": False, "reason": str(e)}

# ─────────────────────────────────────────────
# ML ENGINE — scikit-learn overtime & anomaly models
# ─────────────────────────────────────────────
try:
    import ml_engine
    ML_ENGINE_READY = True
    print("✅ ml_engine loaded — ML overtime prediction and anomaly detection active")
except Exception as _ml_err:
    ml_engine = None
    ML_ENGINE_READY = False
    print(f"⚠️  ml_engine not loaded: {_ml_err}")

# ─────────────────────────────────────────────
# SCHEDULER — APScheduler for automatic reports
# ─────────────────────────────────────────────
SCHEDULER_READY = False
_scheduler = None
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_READY = True
    print("✅ APScheduler available — scheduled reports active")
except ImportError:
    print("⚠️  apscheduler not installed — scheduled reports disabled (pip install apscheduler)")

# ─────────────────────────────────────────────
# APP LIFESPAN — starts scheduler on boot, stops cleanly on shutdown
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(application: FastAPI):
    global _scheduler
    if SCHEDULER_READY:
        _scheduler = AsyncIOScheduler()
        # Boot all persisted schedules from DB
        await _boot_schedules()
        _scheduler.start()
        print("✅ Scheduler started")
    yield
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        print("Scheduler stopped")

app = FastAPI(title="AttendTrack API", version="4.0.0", lifespan=lifespan)
app.mount("/frontend", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")), name="frontend")

# ─────────────────────────────────────────────
# AI ANALYSIS — real LLM calls (Groq or OpenRouter), OpenAI-compatible API.
# Provider/model are env-driven so you can switch without code changes.
# ─────────────────────────────────────────────
import httpx

AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").lower()   # "groq" or "openrouter"
AI_API_KEY  = os.getenv("AI_API_KEY", "")

AI_PROVIDER_CONFIG = {
    "groq":       {"base_url": "https://api.groq.com/openai/v1/chat/completions",
                    "default_model": "llama-3.3-70b-versatile"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1/chat/completions",
                    "default_model": "meta-llama/llama-3.3-70b-instruct:free"},
}
AI_MODEL  = os.getenv("AI_MODEL", AI_PROVIDER_CONFIG.get(AI_PROVIDER, AI_PROVIDER_CONFIG["groq"])["default_model"])
AI_READY  = bool(AI_API_KEY) and AI_PROVIDER in AI_PROVIDER_CONFIG
if AI_READY:
    print(f"✅ AI analysis ready — provider={AI_PROVIDER} model={AI_MODEL}")
else:
    print("⚠️  AI_API_KEY not set — AI analysis endpoints will return a clear 'not configured' message")

async def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 600) -> dict:
    """Calls Groq or OpenRouter's chat-completions endpoint (OpenAI-compatible
    schema). Returns {'ok': bool, 'text': str, 'error': str|None} — never
    raises, so callers can always return something sensible to the client."""
    if not AI_READY:
        return {"ok": False, "text": None,
                "error": f"AI not configured — set AI_API_KEY (and AI_PROVIDER=groq|openrouter) in .env"}
    cfg = AI_PROVIDER_CONFIG[AI_PROVIDER]
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    if AI_PROVIDER == "openrouter":
        headers["HTTP-Referer"] = "https://attendtrack.app"
        headers["X-Title"] = "AttendTrack"
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(cfg["base_url"], headers=headers, json=payload)
        if resp.status_code == 401:
            return {"ok": False, "text": None, "error": "AI provider rejected the API key (401) — check AI_API_KEY"}
        if resp.status_code in (400, 404):
            return {"ok": False, "text": None,
                    "error": f"AI provider rejected model '{AI_MODEL}' ({resp.status_code}). "
                             f"Free model slugs change over time — check the provider's current free-model "
                             f"list and update AI_MODEL in .env. Raw: {resp.text[:200]}"}
        if resp.status_code == 429:
            return {"ok": False, "text": None, "error": "AI provider rate limit hit (429) — try again shortly"}
        if resp.status_code != 200:
            return {"ok": False, "text": None, "error": f"AI provider error {resp.status_code}: {resp.text[:300]}"}
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return {"ok": True, "text": text, "error": None}
    except Exception as e:
        return {"ok": False, "text": None, "error": f"{type(e).__name__}: {e}"}
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# ─────────────────────────────────────────────
# SERVE FRONTEND HTML FILES
# ─────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

@app.get("/")
async def root():
    return {
        "msg": "AttendTrack API v3",
        "portals": {
            "labour":  "http://localhost:8000/labour",
            "manager": "http://localhost:8000/manager",
            "hr":      "http://localhost:8000/hr"
        }
    }

@app.get("/labour")
async def labour_portal():
    p = os.path.join(FRONTEND_DIR, "labour.html")
    return FileResponse(p) if os.path.exists(p) else {"error": "frontend/labour.html not found"}

@app.get("/manager")
async def manager_portal():
    p = os.path.join(FRONTEND_DIR, "manager.html")
    return FileResponse(p) if os.path.exists(p) else {"error": "frontend/manager.html not found"}

@app.get("/hr")
async def hr_portal():
    p = os.path.join(FRONTEND_DIR, "hr.html")
    return FileResponse(p) if os.path.exists(p) else {"error": "frontend/hr.html not found"}

@app.get("/portal.js")
async def portal_js():
    p = os.path.join(FRONTEND_DIR, "portal.js")
    return FileResponse(p) if os.path.exists(p) else {"error": "frontend/portal.js not found"}

@app.get("/frontend/portal.js")
async def legacy_portal_js():
    return await portal_js()

@app.get("/health")
async def health():
    return {"status": "ok", "face_ready": FACE_READY, "db": "postgresql"}

# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────
def get_db():
    conn = db_pool.getconn()
    conn.autocommit = False
    return conn

def release_db(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS sites (
            id            SERIAL PRIMARY KEY,
            name          TEXT NOT NULL,
            address       TEXT,
            latitude      DOUBLE PRECISION NOT NULL,
            longitude     DOUBLE PRECISION NOT NULL,
            radius_meters DOUBLE PRECISION DEFAULT 200,
            manager_id    INTEGER,
            created_at    TIMESTAMP DEFAULT NOW()
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id                 SERIAL PRIMARY KEY,
            employee_id        TEXT UNIQUE NOT NULL,
            name               TEXT NOT NULL,
            role               TEXT NOT NULL CHECK(role IN ('labour','manager','hr')),
            password_hash      TEXT NOT NULL,
            phone              TEXT,
            department         TEXT,
            site_id            INTEGER REFERENCES sites(id),
            face_embedding     JSONB,
            face_registered    BOOLEAN DEFAULT FALSE,
            face_registered_at TIMESTAMP,
            created_at         TIMESTAMP DEFAULT NOW()
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS attendance (
            id                  SERIAL PRIMARY KEY,
            employee_id         TEXT NOT NULL REFERENCES users(employee_id),
            site_id             INTEGER NOT NULL REFERENCES sites(id),
            login_time          TIMESTAMP NOT NULL,
            logout_time         TIMESTAMP,
            login_lat           DOUBLE PRECISION,
            login_lng           DOUBLE PRECISION,
            logout_lat          DOUBLE PRECISION,
            logout_lng          DOUBLE PRECISION,
            hours_worked        DOUBLE PRECISION,
            status              TEXT DEFAULT 'active'
                                CHECK(status IN ('active','pending_manager','pending_hr','approved','rejected')),
            manager_verified    BOOLEAN DEFAULT FALSE,
            manager_note        TEXT,
            manager_verified_at TIMESTAMP,
            hr_verified         BOOLEAN DEFAULT FALSE,
            hr_note             TEXT,
            hr_verified_at      TIMESTAMP,
            date                DATE NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS overtime_alerts (
            id            SERIAL PRIMARY KEY,
            employee_id   TEXT NOT NULL REFERENCES users(employee_id),
            period_start  DATE NOT NULL,
            period_end    DATE NOT NULL,
            hours_total   DOUBLE PRECISION NOT NULL,
            alert_level   TEXT NOT NULL CHECK(alert_level IN ('warning','exceeded')),
            sent_via      TEXT,
            sent_ok       BOOLEAN DEFAULT FALSE,
            created_at    TIMESTAMP DEFAULT NOW(),
            UNIQUE(employee_id, period_start, alert_level)
        )""")
        # ── New: employee Q&A conversation history ──────────────────────────────
        c.execute("""CREATE TABLE IF NOT EXISTS employee_qa (
            id          SERIAL PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES users(employee_id),
            question    TEXT NOT NULL,
            answer      TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT NOW()
        )""")
        # ── New: scheduled report definitions ──────────────────────────────────
        c.execute("""CREATE TABLE IF NOT EXISTS scheduled_reports (
            id           SERIAL PRIMARY KEY,
            name         TEXT NOT NULL,
            report_type  TEXT NOT NULL CHECK(report_type IN ('site_summary','overtime_risk','behaviour','full_hr')),
            frequency    TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly')),
            hour         INTEGER NOT NULL DEFAULT 18,
            site_id      INTEGER REFERENCES sites(id),
            recipients   JSONB NOT NULL DEFAULT '[]',
            delivery     TEXT NOT NULL DEFAULT 'whatsapp' CHECK(delivery IN ('whatsapp','log')),
            enabled      BOOLEAN DEFAULT TRUE,
            last_run_at  TIMESTAMP,
            next_run_at  TIMESTAMP,
            created_by   TEXT REFERENCES users(employee_id),
            created_at   TIMESTAMP DEFAULT NOW()
        )""")
        # ── New: scheduled report run log ──────────────────────────────────────
        c.execute("""CREATE TABLE IF NOT EXISTS report_runs (
            id           SERIAL PRIMARY KEY,
            schedule_id  INTEGER NOT NULL REFERENCES scheduled_reports(id),
            ran_at       TIMESTAMP DEFAULT NOW(),
            status       TEXT NOT NULL CHECK(status IN ('ok','error','skipped')),
            ai_used      BOOLEAN DEFAULT FALSE,
            report_text  TEXT,
            error_msg    TEXT,
            delivered_to JSONB DEFAULT '[]'
        )""")
        # ── New: HR Q&A audit log ───────────────────────────────────────────
        c.execute("""CREATE TABLE IF NOT EXISTS hr_qa (
            id                  SERIAL PRIMARY KEY,
            hr_id               TEXT NOT NULL REFERENCES users(employee_id),
            question            TEXT NOT NULL,
            about_employee_id   TEXT,
            answer              TEXT NOT NULL,
            created_at          TIMESTAMP DEFAULT NOW()
        )""")
        for idx in [
            "CREATE INDEX IF NOT EXISTS idx_att_emp_date  ON attendance(employee_id, date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_att_site_date ON attendance(site_id, date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_att_status    ON attendance(status)",
            "CREATE INDEX IF NOT EXISTS idx_att_date      ON attendance(date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_ot_emp_period ON overtime_alerts(employee_id, period_start)",
            "CREATE INDEX IF NOT EXISTS idx_qa_emp        ON employee_qa(employee_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_rpt_runs      ON report_runs(schedule_id, ran_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_hr_qa         ON hr_qa(hr_id, created_at DESC)",
        ]:
            c.execute(idx)
        conn.commit()
        c.execute("SELECT COUNT(*) as cnt FROM users")
        if c.fetchone()["cnt"] == 0:
            _seed(c)
            conn.commit()
        print("✅ Database ready")
    except Exception as e:
        conn.rollback(); print(f"❌ DB init error: {e}"); raise
    finally:
        release_db(conn)

def _hash(p): return hashlib.sha256(p.encode()).hexdigest()

def _seed(c):
    c.execute("INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (%s,%s,%s,%s,%s)",
              ("Main Construction Site","Vijayanagar, Bangalore",12.618852,75.249893,1000000000))
    c.execute("INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (%s,%s,%s,%s,%s)",
              ("Warehouse Zone A","56 Industrial Area, Muscat",12.618852,75.249893,1000000000))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department) VALUES (%s,%s,%s,%s,%s,%s)",
              ("HR001","Priya Sharma","hr",_hash("hr123"),"9876543210","Human Resources"))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
              ("MGR001","Rajesh Kumar","manager",_hash("mgr123"),"9876543211","Operations",1))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
              ("MGR002","Suresh Nair","manager",_hash("mgr456"),"9876543212","Operations",2))
    for eid,name,site,phone in [
        ("LAB001","Prashanth Kumar",1,"9876543213"),
        ("LAB002","Pranav Das",1,"9876543214"),
        ("LAB003","Shreekanth Rao",2,"9876543215"),
        ("LAB004","Mohan Kumar",2,"9876543216"),
    ]:
        c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (eid,name,"labour",_hash("lab123"),phone,"Labour",site))
    print("✅ Demo data seeded")

init_db()

# ─────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2-lat1); dlam = math.radians(lon2-lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def calc_hours(login, logout) -> float:
    try:
        if not isinstance(login, datetime):  login  = datetime.fromisoformat(str(login)[:19])
        if not isinstance(logout, datetime): logout = datetime.fromisoformat(str(logout)[:19])
        return round((logout-login).total_seconds()/3600, 2)
    except: return 0.0

# ─────────────────────────────────────────────
# OVERTIME CHECK — runs after every checkout
# ─────────────────────────────────────────────
def _month_bounds(d: datetime):
    start = d.replace(day=1)
    if d.month == 12:
        end = d.replace(year=d.year+1, month=1, day=1)
    else:
        end = d.replace(month=d.month+1, day=1)
    return start.date(), end.date()

def check_and_alert_overtime(c, employee_id: str, phone: str, name: str, as_of: datetime):
    """Sums this calendar month's hours_worked for the employee. If they've
    crossed OVERTIME_WARN_AT*limit or the limit itself, sends (or logs) a
    WhatsApp alert — but only once per employee per period per level, so
    checkout doesn't spam them daily once they're already over."""
    period_start, period_end = _month_bounds(as_of)
    c.execute("""SELECT COALESCE(SUM(hours_worked),0) as total FROM attendance
                 WHERE employee_id=%s AND date >= %s AND date < %s AND hours_worked IS NOT NULL""",
              (employee_id, period_start, period_end))
    total_hours = float(c.fetchone()["total"])

    warn_threshold = OVERTIME_LIMIT_HOURS * OVERTIME_WARN_AT
    level = None
    if total_hours >= OVERTIME_LIMIT_HOURS:
        level = "exceeded"
    elif total_hours >= warn_threshold:
        level = "warning"
    if not level:
        print(f"[OVERTIME CHECK] {employee_id} total={total_hours:.3f}h this period — below warn threshold ({warn_threshold:.3f}h), no alert", flush=True)
        return {"alert_fired": False, "total_hours": total_hours}

    c.execute("""SELECT 1 FROM overtime_alerts WHERE employee_id=%s AND period_start=%s AND alert_level=%s""",
              (employee_id, period_start, level))
    if c.fetchone():
        print(f"[OVERTIME ALERT SKIPPED] {employee_id} already alerted at level={level} for period starting {period_start} (total={total_hours:.2f}h)", flush=True)
        return {"alert_fired": False, "total_hours": total_hours, "reason": "already_sent_this_period"}

    if level == "exceeded":
        message = (f"⚠️ AttendTrack: {name}, you have worked {total_hours:.1f} hours this month, "
                   f"exceeding the {OVERTIME_LIMIT_HOURS:.0f}-hour limit. Please contact your manager.")
    else:
        message = (f"AttendTrack: {name}, you have worked {total_hours:.1f} of {OVERTIME_LIMIT_HOURS:.0f} "
                   f"allowed hours this month. You're approaching the limit.")

    result = send_whatsapp_alert(phone, message)
    c.execute("""INSERT INTO overtime_alerts (employee_id,period_start,period_end,hours_total,alert_level,sent_via,sent_ok)
                 VALUES (%s,%s,%s,%s,%s,%s,%s)
                 ON CONFLICT (employee_id,period_start,alert_level) DO NOTHING""",
              (employee_id, period_start, period_end, total_hours, level, "whatsapp", result.get("sent", False)))
    return {"alert_fired": True, "level": level, "total_hours": total_hours, "send_result": result}

def clean_b64(b64: str) -> str:
    return b64.split(",")[1] if "," in b64 else b64

# ─────────────────────────────────────────────
# FACE RECOGNITION
# ─────────────────────────────────────────────
def extract_embedding(b64_image: str):
    if not FACE_READY: return None
    try:
        img_bytes = base64.b64decode(clean_b64(b64_image))
        pil_img   = Image.open(io.BytesIO(img_bytes))
        pil_img.load()  # force full decode now, surfaces truncation errors here
        pil_img   = pil_img.convert("RGB")

        # Build a clean, owned, contiguous uint8 buffer.
        # np.array() + ascontiguousarray() can still leave a *view* into a
        # larger decoder-owned buffer in some PIL/numpy combos, which dlib's
        # pybind layer rejects with "Unsupported image type". .copy() forces
        # a genuinely new, independently-owned allocation.
        img_array = np.array(pil_img, dtype=np.uint8)
        img_array = np.ascontiguousarray(img_array)
        img_array = img_array.copy()

        print(f"[FACE DEBUG] shape={img_array.shape} dtype={img_array.dtype} "
              f"contiguous={img_array.flags['C_CONTIGUOUS']} owns_data={img_array.flags['OWNDATA']}",
              flush=True)

        # DEBUG: isolate face detection from face encoding so we can see
        # exactly which dlib call is throwing, and the real exception text.
        try:
            locations = face_recognition.face_locations(img_array, model="hog")
            print(f"[FACE DEBUG] face_locations OK -> {locations}", flush=True)
        except Exception as e:
            import traceback
            print(f"[FACE DEBUG] face_locations FAILED: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            return None

        if not locations:
            print("[FACE DEBUG] No face found by face_locations", flush=True)
            return None

        try:
            encodings = face_recognition.face_encodings(img_array, known_face_locations=locations, model="small")
            print(f"[FACE DEBUG] face_encodings OK -> got {len(encodings)} encoding(s)", flush=True)
        except Exception as e:
            import traceback
            print(f"[FACE DEBUG] face_encodings FAILED: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            return None

        return encodings[0].tolist() if encodings else None
    except Exception as e:
        import traceback
        print(f"Embedding error: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return None

def compare_faces(stored_embedding: list, live_b64: str) -> dict:
    if not FACE_READY:
        return {"match": True, "confidence": 0.5,
                "reason": "Bypass — face_recognition not installed", "bypass": True}
    live = extract_embedding(live_b64)
    if live is None:
        return {"match": False, "confidence": 0.0,
                "reason": "No face detected. Check lighting and face the camera.", "bypass": False}
    distance = float(face_recognition.face_distance([np.array(stored_embedding)], np.array(live))[0])
    conf     = round(max(0.0, 1.0 - distance), 3)
    return {
        "match": distance < 0.45, "confidence": conf, "distance": round(distance, 4),
        "reason": "✅ Face matched" if distance < 0.45 else f"❌ Face too different (distance {distance:.3f})",
        "bypass": False
    }

# ─────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────
class LoginReq(BaseModel):
    employee_id: str
    password:    str

class FaceVerifyReq(BaseModel):
    employee_id: str
    live_image:  str

class FaceRegisterReq(BaseModel):
    employee_id: str
    password:    str
    face_image:  str

class CheckinReq(BaseModel):
    employee_id: str
    latitude:    float
    longitude:   float

class CheckoutReq(BaseModel):
    attendance_id: int
    employee_id:   str
    latitude:      float
    longitude:     float

class ManagerVerifyReq(BaseModel):
    attendance_id: int
    manager_id:    str
    approved:      bool
    note:          Optional[str] = None

class HRVerifyReq(BaseModel):
    attendance_id: int
    hr_id:         str
    approved:      bool
    note:          Optional[str] = None

class EmployeeAskReq(BaseModel):
    employee_id: str
    question:    str

class ScheduleReportReq(BaseModel):
    name:        str
    report_type: str          # site_summary | overtime_risk | behaviour | full_hr
    frequency:   str          # daily | weekly | monthly
    hour:        int = 18     # 0-23, hour of day to run
    site_id:     Optional[int] = None
    recipients:  List[str] = []   # list of phone numbers
    delivery:    str = "whatsapp" # whatsapp | log
    created_by:  str          # HR employee_id

# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────
@app.post("/api/login")
async def login(req: LoginReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT u.*, s.name as site_name, s.latitude, s.longitude, s.radius_meters
                     FROM users u LEFT JOIN sites s ON u.site_id=s.id
                     WHERE u.employee_id=%s AND u.password_hash=%s""",
                  (req.employee_id.upper(), _hash(req.password)))
        user = c.fetchone()
    finally:
        release_db(conn)
    if not user:
        raise HTTPException(401, "Invalid employee ID or password")
    return {
        "success": True,
        "requires_face_verify": user["role"] == "labour",
        "face_registered": bool(user["face_registered"]),
        "user": {
            "employee_id": user["employee_id"], "name": user["name"],
            "role": user["role"], "department": user["department"],
            "phone": user["phone"], "site_id": user["site_id"],
            "site_name": user["site_name"], "site_lat": user["latitude"],
            "site_lng": user["longitude"], "site_radius": user["radius_meters"],
        }
    }

# ─────────────────────────────────────────────
# FACE REGISTER
# ─────────────────────────────────────────────
@app.post("/api/face/register")
async def face_register(req: FaceRegisterReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id,role,password_hash FROM users WHERE employee_id=%s",
                  (req.employee_id.upper(),))
        user = c.fetchone()
        if not user:                         raise HTTPException(404, "Employee not found")
        if user["role"] != "labour":         raise HTTPException(403, "Only for labour accounts")
        if _hash(req.password) != user["password_hash"]: raise HTTPException(401, "Incorrect password")
        if not req.face_image or len(req.face_image) < 100: raise HTTPException(400, "Invalid image")

        embedding = extract_embedding(req.face_image)
        if embedding is None and FACE_READY:
            raise HTTPException(400, "No face detected. Ensure your face is visible and well-lit.")

        c.execute("""UPDATE users SET face_embedding=%s, face_registered=TRUE, face_registered_at=NOW()
                     WHERE employee_id=%s""",
                  (json.dumps(embedding) if embedding else None, req.employee_id.upper()))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": "Face registered! You can now login with face verification."}

# ─────────────────────────────────────────────
# FACE VERIFY
# ─────────────────────────────────────────────
@app.post("/api/face/verify")
async def face_verify(req: FaceVerifyReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id,role,face_embedding,face_registered FROM users WHERE employee_id=%s",
                  (req.employee_id.upper(),))
        user = c.fetchone()
    finally:
        release_db(conn)
    if not user:                  raise HTTPException(404, "Employee not found")
    if user["role"] != "labour":  raise HTTPException(403, "Face verification only for labour")
    if not user["face_registered"] or not user["face_embedding"]:
        return {"success": False, "needs_registration": True,
                "message": "No face registered. Please register first."}

    emb = user["face_embedding"]
    if isinstance(emb, str): emb = json.loads(emb)
    result = compare_faces(emb, req.live_image)
    print(f"[FACE VERIFY] {req.employee_id} -> {result}")  # TEMP: remove once diagnosed

    if result["bypass"]:
        return {"success": True, "match": True, "confidence": 0.5,
                "message": "⚠️ Bypass mode — face_recognition not installed (always passes)", "bypass": True}
    if result["match"]:
        return {"success": True, "match": True, "confidence": result["confidence"],
                "message": f"✅ Face verified ({int(result['confidence']*100)}% match)"}
    return {"success": False, "match": False, "confidence": result["confidence"],
            "reason": result["reason"],
            "message": f"❌ Face mismatch ({int(result['confidence']*100)}%). Try better lighting."}

# ─────────────────────────────────────────────
# CHECKIN
# ─────────────────────────────────────────────
@app.post("/api/checkin")
async def checkin(req: CheckinReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT u.*, s.latitude as slat, s.longitude as slng,
                            s.radius_meters as srad, s.id as sid
                     FROM users u JOIN sites s ON u.site_id=s.id WHERE u.employee_id=%s""",
                  (req.employee_id,))
        user = c.fetchone()
        if not user: raise HTTPException(404, "Employee not found or no site assigned")

        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT id FROM attendance WHERE employee_id=%s AND date=%s", (req.employee_id, today))
        if c.fetchone():
            raise HTTPException(400, "Already checked in today. Only one check-in per day allowed.")

        dist = haversine(req.latitude, req.longitude, user["slat"], user["slng"])
        if dist > user["srad"]:
            raise HTTPException(400, f"You are {int(dist)}m from site. Must be within {int(user['srad'])}m.")

        now = datetime.now()
        c.execute("""INSERT INTO attendance (employee_id,site_id,login_time,login_lat,login_lng,date,status)
                     VALUES (%s,%s,%s,%s,%s,%s,'active') RETURNING id""",
                  (req.employee_id, user["sid"], now, req.latitude, req.longitude, today))
        att_id = c.fetchone()["id"]
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "attendance_id": att_id,
            "message": f"✅ Checked in at {now.strftime('%H:%M:%S')}",
            "login_time": now.isoformat(), "distance_from_site": int(dist)}

# ─────────────────────────────────────────────
# CHECKOUT
# ─────────────────────────────────────────────
@app.post("/api/checkout")
async def checkout(req: CheckoutReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT a.*, s.latitude as slat, s.longitude as slng, s.radius_meters as srad
                     FROM attendance a JOIN sites s ON a.site_id=s.id
                     WHERE a.id=%s AND a.employee_id=%s AND a.logout_time IS NULL""",
                  (req.attendance_id, req.employee_id))
        att = c.fetchone()
        if not att: raise HTTPException(404, "Active attendance record not found")

        dist  = haversine(req.latitude, req.longitude, att["slat"], att["slng"])
        now   = datetime.now()
        hours = calc_hours(att["login_time"], now)
        c.execute("""UPDATE attendance SET logout_time=%s,logout_lat=%s,logout_lng=%s,
                            hours_worked=%s,status='pending_manager' WHERE id=%s""",
                  (now, req.latitude, req.longitude, hours, req.attendance_id))

        c.execute("SELECT name, phone FROM users WHERE employee_id=%s", (req.employee_id,))
        u = c.fetchone()
        overtime_result = check_and_alert_overtime(c, req.employee_id, u["phone"], u["name"], now)
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": f"✅ Checked out. You worked {hours} hours.",
            "hours_worked": hours, "logout_time": now.isoformat(), "distance_from_site": int(dist),
            "overtime": overtime_result}

# ─────────────────────────────────────────────
# ATTENDANCE TODAY / HISTORY
# ─────────────────────────────────────────────
@app.get("/api/attendance/today/{employee_id}")
async def today_status(employee_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn  = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT a.*, s.name as site_name FROM attendance a
                     JOIN sites s ON a.site_id=s.id
                     WHERE a.employee_id=%s AND a.date=%s ORDER BY a.id DESC LIMIT 1""",
                  (employee_id, today))
        row = c.fetchone()
    finally:
        release_db(conn)
    return {"has_record": bool(row), "record": dict(row) if row else None}

@app.get("/api/attendance/history/{employee_id}")
async def history(employee_id: str, limit: int = 30):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT a.*, s.name as site_name FROM attendance a
                     JOIN sites s ON a.site_id=s.id
                     WHERE a.employee_id=%s ORDER BY a.date DESC, a.id DESC LIMIT %s""",
                  (employee_id, limit))
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"records": [dict(r) for r in rows]}

# ─────────────────────────────────────────────
# MANAGER
# ─────────────────────────────────────────────
def _mgr_site(c, manager_id):
    c.execute("SELECT site_id FROM users WHERE employee_id=%s AND role='manager'", (manager_id,))
    m = c.fetchone()
    if not m: raise HTTPException(403, "Not a manager")
    return m["site_id"]

@app.get("/api/manager/pending/{manager_id}")
async def manager_pending(manager_id: str):
    conn = get_db()
    try:
        c = conn.cursor(); sid = _mgr_site(c, manager_id)
        c.execute("""SELECT a.*, u.name as labour_name, u.phone, s.name as site_name
                     FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                     JOIN sites s ON a.site_id=s.id
                     WHERE a.site_id=%s AND a.status='pending_manager' AND a.logout_time IS NOT NULL
                     ORDER BY a.date DESC""", (sid,))
        return {"records": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)

@app.get("/api/manager/all/{manager_id}")
async def manager_all(manager_id: str, date: str = None):
    conn = get_db()
    try:
        c = conn.cursor(); sid = _mgr_site(c, manager_id)
        q = """SELECT a.*, u.name as labour_name, u.phone, s.name as site_name
               FROM attendance a JOIN users u ON a.employee_id=u.employee_id
               JOIN sites s ON a.site_id=s.id WHERE a.site_id=%s"""
        p = [sid]
        if date: q += " AND a.date=%s"; p.append(date)
        q += " ORDER BY a.date DESC, a.id DESC"
        c.execute(q, p)
        return {"records": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)

@app.get("/api/manager/stats/{manager_id}")
async def manager_stats(manager_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn  = get_db()
    try:
        c = conn.cursor(); sid = _mgr_site(c, manager_id)
        c.execute("SELECT COUNT(DISTINCT employee_id) as cnt FROM attendance WHERE site_id=%s AND date=%s", (sid,today))
        tc = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE site_id=%s AND status='pending_manager'", (sid,))
        pc = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE site_id=%s AND status IN ('pending_hr','approved') AND date=%s", (sid,today))
        ac = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM users WHERE site_id=%s AND role='labour'", (sid,))
        lc = c.fetchone()["cnt"]
    finally:
        release_db(conn)
    return {"today_unique_checkins": tc, "pending_count": pc, "approved_count": ac, "total_labours_site": lc}

@app.post("/api/manager/verify")
async def manager_verify(req: ManagerVerifyReq):
    conn = get_db()
    try:
        c = conn.cursor(); sid = _mgr_site(c, req.manager_id)
        c.execute("SELECT id FROM attendance WHERE id=%s AND site_id=%s", (req.attendance_id, sid))
        if not c.fetchone(): raise HTTPException(404, "Record not found")
        new_status = "pending_hr" if req.approved else "rejected"
        c.execute("""UPDATE attendance SET manager_verified=%s,manager_note=%s,
                            manager_verified_at=NOW(),status=%s WHERE id=%s""",
                  (req.approved, req.note, new_status, req.attendance_id))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": f"Record {'approved → HR' if req.approved else 'rejected'}"}

# ─────────────────────────────────────────────
# HR
# ─────────────────────────────────────────────
def _assert_hr(c, hr_id):
    c.execute("SELECT id FROM users WHERE employee_id=%s AND role='hr'", (hr_id,))
    if not c.fetchone(): raise HTTPException(403, "Not HR")

@app.get("/api/hr/pending/{hr_id}")
async def hr_pending(hr_id: str):
    conn = get_db()
    try:
        c = conn.cursor(); _assert_hr(c, hr_id)
        c.execute("""SELECT a.*, u.name as labour_name, u.phone, u.department, s.name as site_name
                     FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                     JOIN sites s ON a.site_id=s.id
                     WHERE a.status='pending_hr' AND a.manager_verified=TRUE ORDER BY a.date DESC""")
        return {"records": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)

@app.get("/api/hr/all/{hr_id}")
async def hr_all(hr_id: str, date: str = None, employee_id: str = None):
    conn = get_db()
    try:
        c = conn.cursor(); _assert_hr(c, hr_id)
        q = """SELECT a.*, u.name as labour_name, u.phone, u.department, s.name as site_name
               FROM attendance a JOIN users u ON a.employee_id=u.employee_id
               JOIN sites s ON a.site_id=s.id
               WHERE a.status IN ('pending_hr','approved','rejected') AND a.manager_verified=TRUE"""
        p = []
        if date:        q += " AND a.date=%s";        p.append(date)
        if employee_id: q += " AND a.employee_id=%s"; p.append(employee_id)
        q += " ORDER BY a.date DESC LIMIT 500"
        c.execute(q, p)
        return {"records": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)

@app.get("/api/hr/summary")
async def hr_summary():
    today = datetime.now().strftime("%Y-%m-%d")
    conn  = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(DISTINCT employee_id) as cnt FROM attendance WHERE date=%s", (today,))
        tc = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='pending_manager'")
        pm = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='pending_hr' AND manager_verified=TRUE")
        ph = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='approved' AND date=%s", (today,))
        at = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM users WHERE role='labour'")
        te = c.fetchone()["cnt"]
    finally:
        release_db(conn)
    return {"today_checkins": tc, "pending_manager": pm, "pending_hr": ph,
            "approved_today": at, "total_employees": te}

@app.post("/api/hr/verify")
async def hr_verify(req: HRVerifyReq):
    conn = get_db()
    try:
        c = conn.cursor(); _assert_hr(c, req.hr_id)
        new_status = "approved" if req.approved else "rejected"
        c.execute("""UPDATE attendance SET hr_verified=%s,hr_note=%s,
                            hr_verified_at=NOW(),status=%s WHERE id=%s""",
                  (req.approved, req.note, new_status, req.attendance_id))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": f"Record {new_status}."}

# ─────────────────────────────────────────────
# ANALYTICS — historical-data-driven, NOT machine learning.
# Computes real trends from past attendance records and projects
# forward with simple linear arithmetic. No model is trained or
# retrained; "prediction" here means a statistical projection.
# ─────────────────────────────────────────────
@app.get("/api/analytics/employee/{employee_id}")
async def employee_analytics(employee_id: str, lookback_days: int = 90):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name, phone FROM users WHERE employee_id=%s", (employee_id,))
        user = c.fetchone()
        if not user: raise HTTPException(404, "Employee not found")

        c.execute("""SELECT date, hours_worked FROM attendance
                     WHERE employee_id=%s AND hours_worked IS NOT NULL
                       AND date >= CURRENT_DATE - INTERVAL '%s days'
                     ORDER BY date ASC""", (employee_id, lookback_days))
        rows = c.fetchall()

        as_of = datetime.now()
        period_start, period_end = _month_bounds(as_of)
        c.execute("""SELECT COALESCE(SUM(hours_worked),0) as total FROM attendance
                     WHERE employee_id=%s AND date >= %s AND date < %s AND hours_worked IS NOT NULL""",
                  (employee_id, period_start, period_end))
        month_to_date_hours = float(c.fetchone()["total"])
    finally:
        release_db(conn)

    if not rows:
        return {"employee_id": employee_id, "name": user["name"], "data_points": 0,
                "message": "Not enough historical attendance data yet to compute trends."}

    total_hours  = sum(r["hours_worked"] for r in rows)
    avg_per_day  = total_hours / len(rows)
    days_so_far  = (as_of.date() - period_start).days + 1
    days_in_month = (period_end - period_start).days
    days_left    = max(0, days_in_month - days_so_far)

    # Simple linear projection: if the employee keeps logging hours at their
    # recent average daily rate, what will month-end total look like?
    projected_month_total = round(month_to_date_hours + avg_per_day * days_left, 1)
    will_exceed = projected_month_total >= OVERTIME_LIMIT_HOURS

    days_to_limit = None
    if avg_per_day > 0 and month_to_date_hours < OVERTIME_LIMIT_HOURS:
        days_to_limit = math.ceil((OVERTIME_LIMIT_HOURS - month_to_date_hours) / avg_per_day)

    return {
        "employee_id": employee_id,
        "name": user["name"],
        "data_points": len(rows),
        "lookback_days": lookback_days,
        "avg_hours_per_worked_day": round(avg_per_day, 2),
        "month_to_date_hours": round(month_to_date_hours, 1),
        "overtime_limit_hours": OVERTIME_LIMIT_HOURS,
        "projected_month_end_hours": projected_month_total,
        "projected_to_exceed_limit": will_exceed,
        "estimated_days_until_limit_at_current_pace": days_to_limit,
        "method": "linear projection from trailing average — statistical estimate, not a trained model"
    }

@app.get("/api/analytics/site/{site_id}")
async def site_analytics(site_id: int):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id, name, phone FROM users WHERE site_id=%s AND role='labour'", (site_id,))
        labourers = c.fetchall()
        results = []
        for lab in labourers:
            r = await employee_analytics(lab["employee_id"])
            results.append(r)
    finally:
        release_db(conn)
    at_risk = [r for r in results if r.get("projected_to_exceed_limit")]
    return {"site_id": site_id, "employees_analyzed": len(results),
            "at_risk_of_overtime": len(at_risk), "details": results}

@app.get("/api/ai/site-summary/{site_id}")
async def ai_site_summary(site_id: int):
    """Real AI feature: sends actual computed attendance analytics to an LLM
    (Groq/OpenRouter) and asks it to reason over the data and write a plain-
    English report — patterns, risks, and recommendations a fixed rule
    wouldn't phrase or prioritize the same way for a human reader."""
    analytics = await site_analytics(site_id)
    if analytics["employees_analyzed"] == 0:
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
        "You will be given real attendance statistics for workers at one site. "
        "Write a short, plain-English report for an HR manager: 2-3 sentence overview, "
        "then bullet points flagging anyone at risk of exceeding overtime limits, "
        "anyone with too little data to assess, and one practical recommendation. "
        "Be concise and factual — base every claim only on the numbers given, do not invent data."
    )
    user_prompt = f"Site: {site_name}\nOvertime limit: {OVERTIME_LIMIT_HOURS} hours/month\n\nWorker data:\n"
    for d in analytics["details"]:
        if d.get("data_points", 0) == 0:
            user_prompt += f"- {d['name']}: no attendance data yet\n"
        else:
            user_prompt += (f"- {d['name']}: {d['month_to_date_hours']}h so far this month, "
                            f"avg {d['avg_hours_per_worked_day']}h/worked day, "
                            f"projected month-end {d['projected_month_end_hours']}h, "
                            f"{'AT RISK of exceeding limit' if d['projected_to_exceed_limit'] else 'on track'}\n")

    result = await call_llm(system_prompt, user_prompt)
    return {
        "site_id": site_id, "site_name": site_name, "ai_used": result["ok"],
        "provider": AI_PROVIDER if result["ok"] else None,
        "model": AI_MODEL if result["ok"] else None,
        "summary": result["text"] if result["ok"] else None,
        "error": result["error"] if not result["ok"] else None,
        "raw_analytics": analytics,
    }

@app.get("/api/ai/checkin-flag/{attendance_id}")
async def ai_checkin_flag(attendance_id: int):
    """Real AI feature: asks the LLM to look at one attendance record plus
    that employee's recent history and flag anything that looks unusual,
    with a short plain-English reason — useful for a manager scanning a
    pending-approval queue."""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT a.*, u.name FROM attendance a JOIN users u ON a.employee_id=u.employee_id
                     WHERE a.id=%s""", (attendance_id,))
        rec = c.fetchone()
        if not rec: raise HTTPException(404, "Attendance record not found")

        c.execute("""SELECT date, login_time, logout_time, hours_worked FROM attendance
                     WHERE employee_id=%s AND id != %s ORDER BY date DESC LIMIT 10""",
                  (rec["employee_id"], attendance_id))
        history = c.fetchall()
    finally:
        release_db(conn)

    system_prompt = (
        "You are reviewing one attendance record against a worker's recent history for a "
        "construction-site attendance system. Decide if this record looks unusual compared "
        "to their normal pattern (e.g. unusually long/short hours, very different timing). "
        "Respond in 1-2 short sentences. Start with 'FLAG:' if something looks worth a manager's "
        "attention, or 'OK:' if it looks consistent with their normal pattern. Base your answer "
        "only on the data given."
    )
    user_prompt = (f"Worker: {rec['name']}\nRecord to review: date={rec['date']}, "
                   f"login={rec['login_time']}, logout={rec.get('logout_time')}, "
                   f"hours_worked={rec.get('hours_worked')}\n\nRecent history (most recent first):\n")
    for h in history:
        user_prompt += f"- {h['date']}: {h['hours_worked']}h\n"
    if not history:
        user_prompt += "(no prior history available)\n"

    result = await call_llm(system_prompt, user_prompt, max_tokens=150)
    return {
        "attendance_id": attendance_id, "ai_used": result["ok"],
        "flag": result["text"] if result["ok"] else None,
        "error": result["error"] if not result["ok"] else None,
    }

@app.get("/api/employees")
async def get_employees():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT u.employee_id,u.name,u.role,u.phone,u.department,
                            u.face_registered, s.name as site_name
                     FROM users u LEFT JOIN sites s ON u.site_id=s.id ORDER BY u.role,u.name""")
        return {"employees": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)

@app.get("/api/sites")
async def get_sites():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM sites")
        return {"sites": [dict(r) for r in c.fetchall()]}
    finally:
        release_db(conn)

# ─────────────────────────────────────────────────────────────────────────────
# ML ENGINE INIT
# ─────────────────────────────────────────────────────────────────────────────
if ML_ENGINE_READY:
    ml_engine.init(OVERTIME_LIMIT_HOURS)
    print(f"✅ ml_engine initialised with overtime limit={OVERTIME_LIMIT_HOURS}h")


# ─────────────────────────────────────────────────────────────────────────────
# ATTENDANCE POLICY — used by the employee Q&A system prompt
# ─────────────────────────────────────────────────────────────────────────────
ATTENDANCE_POLICY = f"""
AttendTrack Attendance Policy (summary for employee Q&A):

1. CHECK-IN / CHECK-OUT
   - Each employee must check in once per working day via the Labour Portal.
   - Face verification is required before every check-in.
   - Location must be within the geofence of the assigned site.
   - Check-out closes the session; you cannot check out twice in one day.

2. APPROVAL WORKFLOW
   - After checkout the record status is "pending_manager".
   - The site manager reviews and approves or rejects it.
   - If approved by the manager the record moves to "pending_hr".
   - HR then gives final approval ("approved") or rejection ("rejected").
   - Only approved records count toward your monthly total.

3. OVERTIME CAP
   - The monthly overtime cap is {OVERTIME_LIMIT_HOURS:.0f} hours of total work.
   - A WhatsApp alert is sent when you approach or exceed the cap.
   - Exceeding the cap requires a discussion with your manager.

4. STATUS MEANINGS
   - active: you are currently checked in (session open).
   - pending_manager: checked out, waiting for manager approval.
   - pending_hr: manager approved, waiting for HR final approval.
   - approved: fully approved and counted.
   - rejected: not counted; contact your manager for clarification.

5. QUESTIONS
   Employees can ask this assistant about their own records, hours,
   status meanings, and policy. For disputes, contact your site manager
   or HR.
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# EMPLOYEE Q&A — POST /api/employee/ask
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/employee/ask")
async def employee_ask(req: EmployeeAskReq):
    """
    Lets a labour employee ask natural-language questions about their own
    attendance records, status, hours, and policy.  The LLM is given:
      - the attendance policy as the system prompt
      - the employee's last 30 attendance records + current month summary
        as context in the user prompt
    Answers are stored in employee_qa for audit.
    """
    if not req.question or not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")
    if len(req.question) > 500:
        raise HTTPException(400, "Question too long (max 500 characters)")

    conn = get_db()
    try:
        c = conn.cursor()
        # Verify employee exists and is labour
        c.execute("SELECT name, employee_id FROM users WHERE employee_id=%s AND role='labour'",
                  (req.employee_id.upper(),))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "Employee not found or not a labour account")

        # Fetch last 30 records
        c.execute("""
            SELECT a.date, a.login_time, a.logout_time, a.hours_worked,
                   a.status, s.name as site_name
            FROM attendance a
            JOIN sites s ON a.site_id = s.id
            WHERE a.employee_id = %s
            ORDER BY a.date DESC, a.id DESC
            LIMIT 30
        """, (req.employee_id.upper(),))
        records = c.fetchall()

        # Current month summary
        as_of = datetime.now()
        period_start, period_end = _month_bounds(as_of)
        c.execute("""
            SELECT COALESCE(SUM(hours_worked), 0) as total,
                   COUNT(*) as days
            FROM attendance
            WHERE employee_id = %s
              AND date >= %s AND date < %s
              AND hours_worked IS NOT NULL
        """, (req.employee_id.upper(), period_start, period_end))
        month_row = c.fetchone()
    finally:
        release_db(conn)

    month_hours = float(month_row["total"])
    month_days  = int(month_row["days"])

    # Build context string
    records_text = ""
    if records:
        for r in records:
            lo = str(r["logout_time"])[:16] if r["logout_time"] else "–"
            li = str(r["login_time"])[:16]  if r["login_time"]  else "–"
            records_text += (
                f"  {str(r['date'])[:10]} | In: {li[-5:]}  Out: {lo[-5:]} | "
                f"{r['hours_worked'] or 0:.1f}h | {r['status']} | {r['site_name']}\n"
            )
    else:
        records_text = "  (no attendance records yet)\n"

    system_prompt = (
        f"You are an attendance assistant helping a construction-site worker named "
        f"{user['name']} (ID: {user['employee_id']}) understand their attendance records "
        f"and company policy.\n\n"
        f"POLICY:\n{ATTENDANCE_POLICY}\n\n"
        f"Rules for your answers:\n"
        f"- Answer only using the data given below and the policy above.\n"
        f"- Never reveal other employees' data.\n"
        f"- Be concise (≤ 4 sentences) and friendly.\n"
        f"- If you cannot answer from the data given, say so honestly."
    )

    user_prompt = (
        f"Employee: {user['name']} ({user['employee_id']})\n"
        f"Current month ({period_start} → {period_end}): "
        f"{month_hours:.1f}h worked across {month_days} day(s).\n"
        f"Overtime cap: {OVERTIME_LIMIT_HOURS:.0f}h/month.\n\n"
        f"Recent attendance records (newest first):\n{records_text}\n"
        f"Employee question: {req.question.strip()}"
    )

    result = await call_llm(system_prompt, user_prompt, max_tokens=300)

    answer = result["text"] if result["ok"] else (
        f"Sorry, I'm unable to process your question right now "
        f"({result.get('error', 'AI not configured')}). "
        f"Please contact your manager or HR directly."
    )

    # Persist Q&A for audit trail
    conn2 = get_db()
    try:
        c2 = conn2.cursor()
        c2.execute(
            "INSERT INTO employee_qa (employee_id, question, answer) VALUES (%s, %s, %s)",
            (req.employee_id.upper(), req.question.strip(), answer)
        )
        conn2.commit()
    except Exception:
        conn2.rollback()
    finally:
        release_db(conn2)

    return {
        "employee_id": req.employee_id.upper(),
        "question":    req.question.strip(),
        "answer":      answer,
        "ai_used":     result["ok"],
        "context": {
            "month_hours": round(month_hours, 1),
            "month_days":  month_days,
            "records_shown": len(records),
        }
    }


@app.get("/api/employee/qa-history/{employee_id}")
async def employee_qa_history(employee_id: str, limit: int = 20):
    """Returns the last N Q&A pairs for an employee (self-service audit)."""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE employee_id=%s AND role='labour'",
                  (employee_id.upper(),))
        if not c.fetchone():
            raise HTTPException(404, "Employee not found")
        c.execute("""SELECT question, answer, created_at
                     FROM employee_qa WHERE employee_id=%s
                     ORDER BY created_at DESC LIMIT %s""",
                  (employee_id.upper(), limit))
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"employee_id": employee_id.upper(), "history": [dict(r) for r in rows]}


# ─────────────────────────────────────────────────────────────────────────────
# ML ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/ml/train/{employee_id}")
async def ml_train_employee(employee_id: str):
    """Train (or retrain) ML models for one employee."""
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded — check server logs")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id FROM users WHERE employee_id=%s AND role='labour'",
                  (employee_id.upper(),))
        if not c.fetchone():
            raise HTTPException(404, "Labour employee not found")
        c.execute("""SELECT date, login_time, logout_time, hours_worked, status
                     FROM attendance WHERE employee_id=%s ORDER BY date ASC""",
                  (employee_id.upper(),))
        records = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    import asyncio
    result = await asyncio.to_thread(ml_engine.train_employee, employee_id.upper(), records)
    return result


@app.post("/api/ml/train-all")
async def ml_train_all():
    """Retrain ML models for every labour employee. HR-only in practice — no
    auth guard added here since the system is already behind a login; add a
    depends() guard if needed."""
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded — check server logs")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id FROM users WHERE role='labour'")
        employees = [r["employee_id"] for r in c.fetchall()]
        emp_records: dict = {}
        for eid in employees:
            c.execute("""SELECT date, login_time, logout_time, hours_worked, status
                         FROM attendance WHERE employee_id=%s ORDER BY date ASC""", (eid,))
            emp_records[eid] = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    import asyncio
    result = await asyncio.to_thread(ml_engine.retrain_all, emp_records)
    return result


@app.get("/api/ml/predict/{employee_id}")
async def ml_predict_overtime(employee_id: str):
    """
    ML-powered overtime projection for one employee.
    Uses a trained GradientBoostingRegressor if available,
    otherwise falls back to linear arithmetic.
    """
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM users WHERE employee_id=%s", (employee_id.upper(),))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "Employee not found")
        as_of = datetime.now()
        period_start, period_end = _month_bounds(as_of)
        c.execute("""SELECT date, hours_worked FROM attendance
                     WHERE employee_id=%s AND date >= %s AND date < %s
                       AND hours_worked IS NOT NULL
                     ORDER BY date ASC""",
                  (employee_id.upper(), period_start, period_end))
        month_records = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    import asyncio
    result = await asyncio.to_thread(
        ml_engine.predict_overtime, employee_id.upper(), month_records, as_of
    )
    result["name"] = user["name"]
    return result


@app.get("/api/ml/anomalies/{employee_id}")
async def ml_anomalies(employee_id: str, lookback_days: int = 60):
    """
    ML anomaly detection for one employee's recent records.
    Uses IsolationForest if trained, rule-based fallback otherwise.
    """
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM users WHERE employee_id=%s", (employee_id.upper(),))
        user = c.fetchone()
        if not user:
            raise HTTPException(404, "Employee not found")
        c.execute("""SELECT date, login_time, logout_time, hours_worked, status
                     FROM attendance
                     WHERE employee_id=%s
                       AND date >= CURRENT_DATE - INTERVAL '%s days'
                       AND hours_worked IS NOT NULL
                     ORDER BY date DESC""",
                  (employee_id.upper(), lookback_days))
        records = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    import asyncio
    result = await asyncio.to_thread(ml_engine.detect_anomalies, employee_id.upper(), records)
    result["name"] = user["name"]
    return result


@app.get("/api/ml/behaviour/site/{site_id}")
async def ml_behaviour_site(site_id: int):
    """Run anomaly detection across all labourers on a site. Used by the
    behaviour-intelligence tab when ML is available."""
    if not ML_ENGINE_READY:
        raise HTTPException(503, "ml_engine not loaded")
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id, name FROM users WHERE site_id=%s AND role='labour'",
                  (site_id,))
        labourers = c.fetchall()
    finally:
        release_db(conn)

    import asyncio
    results = []
    for lab in labourers:
        conn2 = get_db()
        try:
            c2 = conn2.cursor()
            c2.execute("""SELECT date, login_time, logout_time, hours_worked
                          FROM attendance WHERE employee_id=%s
                            AND date >= CURRENT_DATE - INTERVAL '60 days'
                            AND hours_worked IS NOT NULL
                          ORDER BY date DESC""", (lab["employee_id"],))
            recs = [dict(r) for r in c2.fetchall()]
        finally:
            release_db(conn2)
        r = await asyncio.to_thread(ml_engine.detect_anomalies, lab["employee_id"], recs)
        r["name"] = lab["name"]
        results.append(r)

    results.sort(key=lambda x: x["risk_score"], reverse=True)
    return {
        "site_id": site_id,
        "employees_analyzed": len(results),
        "high_risk": sum(1 for r in results if r["risk_label"] == "high"),
        "results": results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED REPORTS
# ─────────────────────────────────────────────────────────────────────────────

async def _run_scheduled_report(schedule_id: int):
    """
    Core report runner — fetches the schedule, builds the AI report,
    delivers it, and writes a run-log entry.
    """
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM scheduled_reports WHERE id=%s AND enabled=TRUE", (schedule_id,))
        sched = c.fetchone()
        if not sched:
            return
    finally:
        release_db(conn)

    sched = dict(sched)
    report_type = sched["report_type"]
    site_id     = sched.get("site_id")

    # ── Generate the report text ─────────────────────────────────────────────
    report_text = None
    ai_used     = False
    error_msg   = None
    status      = "ok"

    try:
        if report_type == "site_summary" and site_id:
            payload = await ai_site_summary(site_id)
            report_text = payload.get("summary") or payload.get("message", "No data")
            ai_used     = payload.get("ai_used", False)

        elif report_type == "overtime_risk":
            # Collect all sites or the specified site
            conn2 = get_db()
            try:
                c2 = conn2.cursor()
                if site_id:
                    c2.execute("SELECT id, name FROM sites WHERE id=%s", (site_id,))
                else:
                    c2.execute("SELECT id, name FROM sites")
                sites = c2.fetchall()
            finally:
                release_db(conn2)

            lines = []
            for s in sites:
                analytics = await site_analytics(s["id"])
                at_risk = [d for d in analytics["details"] if d.get("projected_to_exceed_limit")]
                lines.append(f"Site: {s['name']}")
                if at_risk:
                    for d in at_risk:
                        lines.append(f"  ⚠️ {d['name']}: {d['month_to_date_hours']}h / projected {d['projected_month_end_hours']}h")
                else:
                    lines.append("  ✅ No employees at overtime risk")
            data_text = "\n".join(lines)
            system_prompt = (
                "You are an HR analytics assistant. Based on the overtime risk data below, "
                "write a brief daily digest (3-5 sentences) for HR management: highlight any "
                "employees at risk of exceeding their monthly cap, mention sites that are clean, "
                "and give one actionable recommendation."
            )
            result = await call_llm(system_prompt, data_text, max_tokens=400)
            report_text = result["text"] if result["ok"] else data_text
            ai_used     = result["ok"]

        elif report_type == "behaviour":
            conn2 = get_db()
            try:
                c2 = conn2.cursor()
                if site_id:
                    c2.execute("SELECT employee_id, name FROM users WHERE site_id=%s AND role='labour'", (site_id,))
                else:
                    c2.execute("SELECT employee_id, name FROM users WHERE role='labour'")
                labourers = c2.fetchall()
            finally:
                release_db(conn2)

            lines = []
            for lab in labourers:
                conn3 = get_db()
                try:
                    c3 = conn3.cursor()
                    c3.execute("""SELECT date, login_time, logout_time, hours_worked
                                  FROM attendance WHERE employee_id=%s
                                    AND date >= CURRENT_DATE - INTERVAL '30 days'
                                    AND hours_worked IS NOT NULL
                                  ORDER BY date DESC""", (lab["employee_id"],))
                    recs = [dict(r) for r in c3.fetchall()]
                finally:
                    release_db(conn3)
                if not recs:
                    continue
                import asyncio
                anomaly = await asyncio.to_thread(ml_engine.detect_anomalies, lab["employee_id"], recs) \
                    if ML_ENGINE_READY else {"risk_label": "unknown", "flags": []}
                if anomaly.get("risk_label") in ("high", "medium"):
                    lines.append(f"{lab['name']}: risk={anomaly['risk_label']}, flags={len(anomaly.get('flags', []))}")
            data_text = "Behaviour flags (last 30 days):\n" + ("\n".join(lines) if lines else "No flags detected.")
            system_prompt = (
                "You are an HR analytics assistant. Summarise the behaviour anomaly report below "
                "in 3-4 sentences for a manager. Focus on employees with high or medium risk and "
                "suggest one follow-up action."
            )
            result = await call_llm(system_prompt, data_text, max_tokens=400)
            report_text = result["text"] if result["ok"] else data_text
            ai_used     = result["ok"]

        elif report_type == "full_hr":
            # Comprehensive: combine site summaries from all sites
            conn2 = get_db()
            try:
                c2 = conn2.cursor()
                c2.execute("SELECT id, name FROM sites")
                sites = c2.fetchall()
            finally:
                release_db(conn2)
            lines = []
            for s in sites:
                payload = await ai_site_summary(s["id"])
                lines.append(f"--- {s['name']} ---\n{payload.get('summary') or '(no data)'}")
            report_text = "\n\n".join(lines)
            ai_used     = True

        else:
            status      = "skipped"
            error_msg   = f"Unknown report_type: {report_type}"

    except Exception as e:
        status    = "error"
        error_msg = str(e)

    # ── Deliver ──────────────────────────────────────────────────────────────
    delivered_to = []
    if status == "ok" and report_text:

        # Build recipient phone list:
        # 1. Start with explicitly stored numbers in the schedule definition
        # 2. If none stored, auto-resolve from DB based on report scope:
        #    - site_summary / behaviour  → manager(s) of the target site
        #    - overtime_risk / full_hr   → all HR users + all site managers
        recipients = sched.get("recipients") or []
        if isinstance(recipients, str):
            try:
                recipients = json.loads(recipients)
            except Exception:
                recipients = []

        if not recipients:
            # Auto-resolve from database
            conn_r = get_db()
            try:
                c_r = conn_r.cursor()
                if report_type in ("site_summary", "behaviour") and site_id:
                    # Manager(s) assigned to this site
                    c_r.execute("""SELECT phone FROM users
                                   WHERE site_id=%s AND role='manager' AND phone IS NOT NULL""",
                                (site_id,))
                else:
                    # HR users + all managers
                    c_r.execute("""SELECT phone FROM users
                                   WHERE role IN ('hr','manager') AND phone IS NOT NULL""")
                rows = c_r.fetchall()
                recipients = [r["phone"] for r in rows if r["phone"]]
            except Exception as e:
                print(f"[REPORT DELIVERY] Failed to auto-resolve recipients: {e}", flush=True)
            finally:
                release_db(conn_r)

        # Deduplicate while preserving order
        seen = set()
        unique_recipients = []
        for p in recipients:
            p_clean = str(p).strip()
            if p_clean and p_clean not in seen:
                seen.add(p_clean)
                unique_recipients.append(p_clean)

        report_header = f"📊 *AttendTrack — {sched['name']}*\n"
        full_msg = report_header + report_text

        # Always log to server console regardless of delivery mode
        print(f"[SCHEDULED REPORT] {sched['name']} | type={report_type} | ai={ai_used}\n"
              f"{report_text[:600]}{'...' if len(report_text) > 600 else ''}",
              flush=True)

        if sched["delivery"] == "whatsapp" and unique_recipients:
            # Twilio hard limit is 1600 chars per message — split into chunks
            MAX_CHUNK = 1500
            lines = full_msg.split("\n")
            chunks = []
            current = ""
            for line in lines:
                candidate = current + ("\n" if current else "") + line
                if len(candidate) <= MAX_CHUNK:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    # If a single line is itself > MAX_CHUNK, hard-split it
                    while len(line) > MAX_CHUNK:
                        chunks.append(line[:MAX_CHUNK])
                        line = line[MAX_CHUNK:]
                    current = line
            if current:
                chunks.append(current)

            for phone in unique_recipients:
                phone_ok = True
                for i, chunk in enumerate(chunks):
                    prefix = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
                    r = send_whatsapp_alert(phone, prefix + chunk)
                    if not r.get("sent", False):
                        phone_ok = False
                        print(f"[REPORT DELIVERY] WhatsApp send failed to {phone}: {r.get('reason')}", flush=True)
                delivered_to.append({
                    "phone": phone,
                    "sent": phone_ok,
                    "chunks": len(chunks),
                })
        elif sched["delivery"] == "whatsapp" and not unique_recipients:
            print(f"[REPORT DELIVERY] delivery=whatsapp but no phone numbers resolved — report logged only", flush=True)
            delivered_to.append({"channel": "log_only", "reason": "no_recipients_found", "sent": False})

    # ── Write run log ─────────────────────────────────────────────────────────
    conn3 = get_db()
    try:
        c3 = conn3.cursor()
        c3.execute("""INSERT INTO report_runs
                      (schedule_id, status, ai_used, report_text, error_msg, delivered_to)
                      VALUES (%s, %s, %s, %s, %s, %s)""",
                   (schedule_id, status, ai_used,
                    report_text, error_msg, json.dumps(delivered_to)))
        c3.execute("UPDATE scheduled_reports SET last_run_at=NOW() WHERE id=%s", (schedule_id,))
        conn3.commit()
    except Exception:
        conn3.rollback()
    finally:
        release_db(conn3)


def _cron_kwargs(frequency: str, hour: int) -> dict:
    """Map frequency string to APScheduler CronTrigger kwargs."""
    if frequency == "daily":
        return {"hour": hour, "minute": 0}
    if frequency == "weekly":
        return {"day_of_week": "mon", "hour": hour, "minute": 0}
    if frequency == "monthly":
        return {"day": 1, "hour": hour, "minute": 0}
    return {"hour": hour, "minute": 0}


def _add_job_to_scheduler(schedule_id: int, frequency: str, hour: int):
    """Register one schedule with APScheduler (idempotent)."""
    if not SCHEDULER_READY or not _scheduler:
        return
    job_id = f"report_{schedule_id}"
    # Remove old job if present (handles updates)
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass
    _scheduler.add_job(
        _run_scheduled_report,
        trigger=CronTrigger(**_cron_kwargs(frequency, hour)),
        args=[schedule_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )


async def _boot_schedules():
    """Load all enabled schedules from DB and register them on startup."""
    if not SCHEDULER_READY:
        return
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id, frequency, hour FROM scheduled_reports WHERE enabled=TRUE")
        rows = c.fetchall()
    finally:
        release_db(conn)
    for row in rows:
        _add_job_to_scheduler(row["id"], row["frequency"], row["hour"])
    print(f"✅ Loaded {len(rows)} scheduled report(s) from DB")


# ── REST endpoints for managing schedules ────────────────────────────────────

@app.post("/api/reports/schedule")
async def create_schedule(req: ScheduleReportReq):
    """HR creates a new scheduled report."""
    conn = get_db()
    try:
        c = conn.cursor()
        # Validate HR
        c.execute("SELECT id FROM users WHERE employee_id=%s AND role='hr'", (req.created_by.upper(),))
        if not c.fetchone():
            raise HTTPException(403, "Only HR can create scheduled reports")
        if req.report_type not in ("site_summary", "overtime_risk", "behaviour", "full_hr"):
            raise HTTPException(400, f"Invalid report_type: {req.report_type}")
        if req.frequency not in ("daily", "weekly", "monthly"):
            raise HTTPException(400, f"Invalid frequency: {req.frequency}")
        if not (0 <= req.hour <= 23):
            raise HTTPException(400, "hour must be 0-23")

        c.execute("""INSERT INTO scheduled_reports
                     (name, report_type, frequency, hour, site_id, recipients, delivery, created_by)
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

    _add_job_to_scheduler(schedule_id, req.frequency, req.hour)
    return {
        "success": True,
        "schedule_id": schedule_id,
        "message": f"Schedule '{req.name}' created — runs {req.frequency} at {req.hour:02d}:00",
        "scheduler_active": SCHEDULER_READY,
    }


@app.get("/api/reports/schedules")
async def list_schedules():
    """List all scheduled reports (HR dashboard)."""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT sr.*, s.name as site_name
                     FROM scheduled_reports sr
                     LEFT JOIN sites s ON sr.site_id = s.id
                     ORDER BY sr.created_at DESC""")
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"schedules": [dict(r) for r in rows]}


@app.delete("/api/reports/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int, hr_id: str):
    """Disable a scheduled report (soft-delete via enabled=FALSE)."""
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


@app.post("/api/reports/run-now/{schedule_id}")
async def run_report_now(schedule_id: int):
    """
    Manually trigger a scheduled report and WAIT for it to complete.
    Returns the delivery result so the caller can see exactly what happened
    (report text, who was sent to, whether Twilio succeeded).
    """
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT id, name FROM scheduled_reports WHERE id=%s", (schedule_id,))
        row = c.fetchone()
        if not row:
            raise HTTPException(404, "Schedule not found")
    finally:
        release_db(conn)

    # Run synchronously so the response carries the full result
    await _run_scheduled_report(schedule_id)

    # Fetch the run log entry we just wrote
    conn2 = get_db()
    try:
        c2 = conn2.cursor()
        c2.execute("""SELECT ran_at, status, ai_used, report_text, error_msg, delivered_to
                      FROM report_runs WHERE schedule_id=%s
                      ORDER BY ran_at DESC LIMIT 1""", (schedule_id,))
        run = c2.fetchone()
    finally:
        release_db(conn2)

    run_dict = dict(run) if run else {}
    delivered = run_dict.get("delivered_to") or []
    if isinstance(delivered, str):
        try:
            delivered = json.loads(delivered)
        except Exception:
            delivered = []

    sent_count = sum(1 for d in delivered if d.get("sent"))
    total_count = len(delivered)

    return {
        "success":       run_dict.get("status") == "ok",
        "status":        run_dict.get("status"),
        "ai_used":       run_dict.get("ai_used"),
        "delivered_to":  delivered,
        "sent_count":    sent_count,
        "total_recipients": total_count,
        "error":         run_dict.get("error_msg"),
        "report_preview": (run_dict.get("report_text") or "")[:400],
        "message": (
            f"✅ Report sent to {sent_count}/{total_count} recipient(s) via WhatsApp"
            if sent_count > 0
            else "⚠️ Report generated but not sent via WhatsApp — check delivery settings and Twilio config"
        )
    }


@app.get("/api/reports/history/{schedule_id}")
async def report_history(schedule_id: int, limit: int = 10):
    """Fetch the last N run logs for a schedule."""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""SELECT ran_at, status, ai_used, error_msg, delivered_to
                     FROM report_runs WHERE schedule_id=%s
                     ORDER BY ran_at DESC LIMIT %s""", (schedule_id, limit))
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"schedule_id": schedule_id, "runs": [dict(r) for r in rows]}


# ─────────────────────────────────────────────────────────────────────────────
# HR Q&A — POST /api/hr/ask
# HR can ask anything about any labour employee. The LLM receives the full
# employee profile + last 60 days of attendance as context.
# ─────────────────────────────────────────────────────────────────────────────
class HRAskReq(BaseModel):
    hr_id:      str
    question:   str
    employee_id: Optional[str] = None   # None = ask about all employees

@app.post("/api/hr/ask")
async def hr_ask(req: HRAskReq):
    if not req.question or not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")
    if len(req.question) > 800:
        raise HTTPException(400, "Question too long (max 800 characters)")

    conn = get_db()
    try:
        c = conn.cursor()
        # Verify HR role
        c.execute("SELECT name FROM users WHERE employee_id=%s AND role='hr'",
                  (req.hr_id.upper(),))
        if not c.fetchone():
            raise HTTPException(403, "Not an HR account")

        if req.employee_id:
            # Single employee context
            eid = req.employee_id.upper()
            c.execute("""SELECT u.employee_id, u.name, u.phone, u.department,
                                u.face_registered, s.name as site_name
                         FROM users u LEFT JOIN sites s ON u.site_id=s.id
                         WHERE u.employee_id=%s AND u.role='labour'""", (eid,))
            emp = c.fetchone()
            if not emp:
                raise HTTPException(404, f"Labour employee {eid} not found")
            employees = [dict(emp)]

            c.execute("""SELECT a.date, a.login_time, a.logout_time, a.hours_worked,
                                a.status, a.manager_note, a.hr_note, s.name as site_name
                         FROM attendance a JOIN sites s ON a.site_id=s.id
                         WHERE a.employee_id=%s
                         ORDER BY a.date DESC LIMIT 60""", (eid,))
            records_map = {eid: [dict(r) for r in c.fetchall()]}

            as_of = datetime.now()
            ps, pe = _month_bounds(as_of)
            c.execute("""SELECT COALESCE(SUM(hours_worked),0) as mh,
                                COUNT(*) as md
                         FROM attendance
                         WHERE employee_id=%s AND date>=%s AND date<%s
                           AND hours_worked IS NOT NULL""", (eid, ps, pe))
            mrow = c.fetchone()
            month_summary = {eid: {"hours": float(mrow["mh"]), "days": int(mrow["md"])}}
        else:
            # All labour employees
            c.execute("""SELECT u.employee_id, u.name, u.phone, u.department,
                                u.face_registered, s.name as site_name
                         FROM users u LEFT JOIN sites s ON u.site_id=s.id
                         WHERE u.role='labour' ORDER BY u.name""")
            employees = [dict(r) for r in c.fetchall()]

            as_of = datetime.now()
            ps, pe = _month_bounds(as_of)
            c.execute("""SELECT employee_id,
                                COALESCE(SUM(hours_worked),0) as mh,
                                COUNT(*) as md
                         FROM attendance
                         WHERE date>=%s AND date<%s AND hours_worked IS NOT NULL
                         GROUP BY employee_id""", (ps, pe))
            month_summary = {r["employee_id"]: {"hours": float(r["mh"]),
                                                "days": int(r["md"])}
                             for r in c.fetchall()}

            # Last 30 records per employee (capped at 20 employees for token budget)
            records_map = {}
            for emp in employees[:20]:
                eid = emp["employee_id"]
                c.execute("""SELECT a.date, a.login_time, a.logout_time,
                                    a.hours_worked, a.status, s.name as site_name
                             FROM attendance a JOIN sites s ON a.site_id=s.id
                             WHERE a.employee_id=%s
                             ORDER BY a.date DESC LIMIT 30""", (eid,))
                records_map[eid] = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    # Build context text
    context_lines = []
    for emp in employees:
        eid   = emp["employee_id"]
        ms    = month_summary.get(eid, {"hours": 0, "days": 0})
        recs  = records_map.get(eid, [])
        ot_flag = "⚠️ OVERTIME RISK" if ms["hours"] >= OVERTIME_LIMIT_HOURS * 0.85 else ""
        context_lines.append(
            f"\n[{eid}] {emp['name']} | {emp['department'] or 'Labour'} | "
            f"Site: {emp['site_name'] or 'unassigned'} | Phone: {emp['phone'] or '–'} "
            f"| Face enrolled: {'yes' if emp['face_registered'] else 'no'} {ot_flag}"
        )
        context_lines.append(
            f"  This month: {ms['hours']:.1f}h across {ms['days']} day(s) "
            f"(cap: {OVERTIME_LIMIT_HOURS:.0f}h)"
        )
        if recs:
            context_lines.append("  Recent records (newest first):")
            for r in recs[:10]:
                lo = str(r["logout_time"])[:16] if r["logout_time"] else "–"
                li = str(r["login_time"])[:16]  if r["login_time"]  else "–"
                context_lines.append(
                    f"    {str(r['date'])[:10]} | In:{li[-5:]} Out:{lo[-5:]} "
                    f"| {r['hours_worked'] or 0:.1f}h | {r['status']}"
                )

    system_prompt = (
        "You are an HR intelligence assistant for a construction-site workforce management system. "
        "You have access to real employee profiles and attendance records. "
        "Answer the HR officer's question precisely and factually using only the data provided. "
        "If asked about a specific employee, focus only on them. "
        "If asked about patterns or summaries across all employees, aggregate the data logically. "
        "Be concise (max 6 sentences or a short table) and flag any anomalies or risks proactively. "
        f"Overtime cap is {OVERTIME_LIMIT_HOURS:.0f} hours/month."
    )
    user_prompt = (
        f"HR Question: {req.question.strip()}\n\n"
        f"Employee data ({len(employees)} employee(s)):\n"
        + "\n".join(context_lines)
    )

    result = await call_llm(system_prompt, user_prompt, max_tokens=500)
    answer = result["text"] if result["ok"] else (
        f"AI unavailable ({result.get('error','not configured')}). "
        f"Please review the data manually."
    )

    # Save to hr_qa audit table (create if not exists — handled in init_db extension below)
    conn2 = get_db()
    try:
        c2 = conn2.cursor()
        c2.execute("""INSERT INTO hr_qa (hr_id, question, about_employee_id, answer)
                      VALUES (%s,%s,%s,%s)
                      ON CONFLICT DO NOTHING""",
                   (req.hr_id.upper(), req.question.strip(),
                    req.employee_id.upper() if req.employee_id else None, answer))
        conn2.commit()
    except Exception:
        conn2.rollback()
    finally:
        release_db(conn2)

    return {
        "hr_id":        req.hr_id.upper(),
        "question":     req.question.strip(),
        "about":        req.employee_id.upper() if req.employee_id else "all employees",
        "answer":       answer,
        "ai_used":      result["ok"],
        "employees_in_context": len(employees),
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXCEL REPORT — GET /api/hr/report/excel
# Generates a downloadable .xlsx with 5 sheets:
#   1. Summary         — site-level totals
#   2. Attendance Sheet — every record in the period (full detail)
#   3. Overtime        — employees at/near cap
#   4. Absent Days     — days with no attendance record
#   5. Sick / Leave    — records with very short hours (proxy for sick leave)
# ─────────────────────────────────────────────────────────────────────────────
from fastapi.responses import StreamingResponse

@app.get("/api/hr/report/excel")
async def hr_excel_report(
    period: str = "monthly",          # "monthly" or "weekly"
    year:   int = None,
    month:  int = None,
    week:   int = None,               # ISO week number (used when period=weekly)
    hr_id:  str = "HR001",
):
    """
    Builds and streams an Excel workbook for the requested period.
    No extra library needed beyond openpyxl (added to requirements).
    """
    try:
        import openpyxl
        from openpyxl.styles import (PatternFill, Font, Alignment, Border, Side)
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(503, "openpyxl not installed — add openpyxl==3.1.2 to requirements.txt")

    now = datetime.now()
    yr  = year  or now.year
    mo  = month or now.month

    # Compute date range
    if period == "weekly":
        import datetime as dt_mod
        wk = week or now.isocalendar()[1]
        # Monday of that ISO week
        d_start = dt_mod.date.fromisocalendar(yr, wk, 1)
        d_end   = d_start + __import__('datetime').timedelta(days=7)
        period_label = f"Week {wk}, {yr}"
    else:
        import datetime as dt_mod
        d_start = dt_mod.date(yr, mo, 1)
        if mo == 12:
            d_end = dt_mod.date(yr + 1, 1, 1)
        else:
            d_end = dt_mod.date(yr, mo + 1, 1)
        period_label = f"{dt_mod.date(yr, mo, 1).strftime('%B %Y')}"

    conn = get_db()
    try:
        c = conn.cursor()

        # All labour employees
        c.execute("""SELECT u.employee_id, u.name, u.phone, u.department,
                            s.name as site_name, s.id as site_id
                     FROM users u LEFT JOIN sites s ON u.site_id=s.id
                     WHERE u.role='labour' ORDER BY s.name, u.name""")
        employees = [dict(r) for r in c.fetchall()]

        # All attendance in range
        c.execute("""SELECT a.*, u.name as emp_name, u.department,
                            s.name as site_name
                     FROM attendance a
                     JOIN users u ON a.employee_id=u.employee_id
                     JOIN sites s ON a.site_id=s.id
                     WHERE a.date>=%s AND a.date<%s
                     ORDER BY s.name, u.name, a.date""",
                  (d_start, d_end))
        att_rows = [dict(r) for r in c.fetchall()]

        # Sites
        c.execute("SELECT id, name FROM sites ORDER BY id")
        sites = [dict(r) for r in c.fetchall()]
    finally:
        release_db(conn)

    # ── Helper styles ─────────────────────────────────────────────────────────
    HDR_FILL   = PatternFill("solid", fgColor="3B0764")   # deep purple
    HDR_FONT   = Font(bold=True, color="FFFFFF", size=10)
    TITLE_FONT = Font(bold=True, color="C084FC", size=13)
    SUB_FONT   = Font(color="9B59F5", size=9)
    OT_FILL    = PatternFill("solid", fgColor="7F1D1D")   # dark red
    OK_FILL    = PatternFill("solid", fgColor="14532D")   # dark green
    WARN_FILL  = PatternFill("solid", fgColor="713F12")   # dark amber
    ALT_FILL   = PatternFill("solid", fgColor="0D0820")
    THIN       = Border(
        left=Side(style='thin', color='3B0764'),
        right=Side(style='thin', color='3B0764'),
        top=Side(style='thin', color='3B0764'),
        bottom=Side(style='thin', color='3B0764'),
    )

    def hdr(ws, row, cols):
        for c_idx, val in enumerate(cols, 1):
            cell = ws.cell(row=row, column=c_idx, value=val)
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = THIN

    def data_cell(ws, row, col, val, fill=None):
        cell = ws.cell(row=row, column=col, value=val)
        cell.border = THIN
        cell.alignment = Alignment(vertical='center', wrap_text=False)
        if fill:
            cell.fill = fill
        return cell

    def auto_width(ws, min_w=10, max_w=40):
        for col_cells in ws.columns:
            length = max((len(str(c.value or '')) for c in col_cells), default=min_w)
            ws.column_dimensions[get_column_letter(col_cells[0].column)].width = \
                min(max_w, max(min_w, length + 2))

    def title_row(ws, text, sub):
        ws['A1'] = text
        ws['A1'].font = TITLE_FONT
        ws['A2'] = sub
        ws['A2'].font = SUB_FONT

    wb = openpyxl.Workbook()
    wb.remove(wb.active)   # remove default sheet

    # ─── Sheet 1: Summary ────────────────────────────────────────────────────
    ws1 = wb.create_sheet("Summary")
    title_row(ws1, f"AttendTrack — {period_label} Summary", f"Generated {now.strftime('%Y-%m-%d %H:%M')}")

    emp_att_map = {}   # employee_id → list of records
    for r in att_rows:
        emp_att_map.setdefault(r["employee_id"], []).append(r)

    site_stats = {}
    for emp in employees:
        sid  = emp["site_id"]
        sn   = emp["site_name"] or "Unassigned"
        recs = emp_att_map.get(emp["employee_id"], [])
        mh   = sum(float(r["hours_worked"] or 0) for r in recs)
        if sid not in site_stats:
            site_stats[sid] = {"name": sn, "total_emp": 0, "present": 0,
                               "absent": 0, "total_hours": 0.0, "ot_workers": 0}
        site_stats[sid]["total_emp"]   += 1
        site_stats[sid]["total_hours"] += mh
        if recs:
            site_stats[sid]["present"] += 1
        else:
            site_stats[sid]["absent"]  += 1
        if mh >= OVERTIME_LIMIT_HOURS:
            site_stats[sid]["ot_workers"] += 1

    hdr(ws1, 4, ["Site", "Total Workers", "Present (period)",
                 "Absent (no records)", "Total Hours Worked",
                 "Avg Hours/Worker", "Overtime Workers"])
    r_idx = 5
    for sid, st in site_stats.items():
        avg = round(st["total_hours"] / max(1, st["present"]), 1)
        data_cell(ws1, r_idx, 1, st["name"])
        data_cell(ws1, r_idx, 2, st["total_emp"])
        data_cell(ws1, r_idx, 3, st["present"])
        data_cell(ws1, r_idx, 4, st["absent"])
        data_cell(ws1, r_idx, 5, round(st["total_hours"], 1))
        data_cell(ws1, r_idx, 6, avg)
        fill = OT_FILL if st["ot_workers"] > 0 else OK_FILL
        data_cell(ws1, r_idx, 7, st["ot_workers"], fill)
        r_idx += 1

    # Totals row
    total_emp   = len(employees)
    total_pres  = sum(1 for e in employees if emp_att_map.get(e["employee_id"]))
    total_abs   = total_emp - total_pres
    total_hours = sum(float(r["hours_worked"] or 0) for r in att_rows)
    total_ot    = sum(1 for st in site_stats.values() for _ in range(st["ot_workers"]))
    for ci, val in enumerate(["TOTAL", total_emp, total_pres, total_abs,
                               round(total_hours,1), "–", total_ot], 1):
        cell = ws1.cell(row=r_idx, column=ci, value=val)
        cell.font = Font(bold=True, color="C084FC")
        cell.border = THIN
    auto_width(ws1)

    # ─── Sheet 2: Attendance Sheet ────────────────────────────────────────────
    ws2 = wb.create_sheet("Attendance Sheet")
    title_row(ws2, f"Full Attendance — {period_label}", "All records in period")
    cols2 = ["Employee ID", "Name", "Department", "Site",
             "Date", "Check-In", "Check-Out", "Hours Worked",
             "Status", "Manager Note", "HR Note"]
    hdr(ws2, 4, cols2)
    for ri, r in enumerate(att_rows, 5):
        li = str(r["login_time"])[:16]  if r["login_time"]  else "–"
        lo = str(r["logout_time"])[:16] if r["logout_time"] else "–"
        hw = round(float(r["hours_worked"] or 0), 2)
        st = r["status"]
        fill = (OT_FILL  if hw > OVERTIME_LIMIT_HOURS else
                WARN_FILL if st in ("rejected",) else
                OK_FILL   if st == "approved" else None)
        for ci, val in enumerate([
            r["employee_id"], r["emp_name"], r["department"] or "–",
            r["site_name"], str(r["date"])[:10], li, lo,
            hw, st, r.get("manager_note") or "–", r.get("hr_note") or "–"
        ], 1):
            data_cell(ws2, ri, ci, val, fill if ci == 8 else None)
    auto_width(ws2)

    # ─── Sheet 3: Overtime ────────────────────────────────────────────────────
    ws3 = wb.create_sheet("Overtime")
    title_row(ws3, f"Overtime Report — {period_label}",
              f"Cap: {OVERTIME_LIMIT_HOURS:.0f}h/month")
    hdr(ws3, 4, ["Employee ID", "Name", "Site", "Department",
                 "Hours This Period", "Cap", "Over By (h)", "Risk Level"])
    ri = 5
    for emp in employees:
        recs = emp_att_map.get(emp["employee_id"], [])
        mh   = sum(float(r["hours_worked"] or 0) for r in recs)
        if mh < OVERTIME_LIMIT_HOURS * 0.8:
            continue
        over = round(mh - OVERTIME_LIMIT_HOURS, 1)
        risk = "Exceeded" if mh >= OVERTIME_LIMIT_HOURS else "Warning"
        fill = OT_FILL if risk == "Exceeded" else WARN_FILL
        for ci, val in enumerate([emp["employee_id"], emp["name"],
                                   emp["site_name"] or "–", emp["department"] or "–",
                                   round(mh,1), OVERTIME_LIMIT_HOURS,
                                   over if over > 0 else "–", risk], 1):
            data_cell(ws3, ri, ci, val, fill if ci >= 5 else None)
        ri += 1
    if ri == 5:
        ws3.cell(row=5, column=1, value="No employees at or near overtime cap in this period.")
    auto_width(ws3)

    # ─── Sheet 4: Absent Days ─────────────────────────────────────────────────
    ws4 = wb.create_sheet("Absent Days")
    title_row(ws4, f"Absent Days — {period_label}", "Working days with no attendance record")
    hdr(ws4, 4, ["Employee ID", "Name", "Site", "Absent Dates (no record)"])

    import datetime as dt_mod
    all_dates = []
    cur = d_start
    while cur < d_end:
        if cur.weekday() < 6:   # Mon–Sat (construction sites often work Saturdays)
            all_dates.append(cur)
        cur += dt_mod.timedelta(days=1)

    ri = 5
    for emp in employees:
        recs_dates = {str(r["date"])[:10] for r in emp_att_map.get(emp["employee_id"], [])}
        absent = [str(d) for d in all_dates if str(d) not in recs_dates]
        if not absent:
            continue
        data_cell(ws4, ri, 1, emp["employee_id"])
        data_cell(ws4, ri, 2, emp["name"])
        data_cell(ws4, ri, 3, emp["site_name"] or "–")
        data_cell(ws4, ri, 4, ", ".join(absent))
        ri += 1
    if ri == 5:
        ws4.cell(row=5, column=1, value="No absent days recorded for this period.")
    auto_width(ws4)

    # ─── Sheet 5: Short Shifts / Sick Leave ───────────────────────────────────
    ws5 = wb.create_sheet("Sick & Short Shifts")
    title_row(ws5, f"Short Shifts & Sick Leave — {period_label}",
              "Records with < 4 hours worked (possible sick leave or early departure)")
    hdr(ws5, 4, ["Employee ID", "Name", "Site", "Date",
                 "Hours Worked", "Status", "Note"])
    ri = 5
    for r in att_rows:
        hw = float(r["hours_worked"] or 0)
        if hw >= 4:
            continue
        note = r.get("manager_note") or r.get("hr_note") or "–"
        for ci, val in enumerate([
            r["employee_id"], r["emp_name"], r["site_name"],
            str(r["date"])[:10], round(hw, 2), r["status"], note
        ], 1):
            data_cell(ws5, ri, ci, val, WARN_FILL if hw < 2 else None)
        ri += 1
    if ri == 5:
        ws5.cell(row=5, column=1, value="No short shifts in this period.")
    auto_width(ws5)

    # ── Stream back as download ───────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"AttendTrack_{period_label.replace(' ','_').replace(',','')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )
