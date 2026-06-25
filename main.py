import os, json, math, hashlib, base64, io
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

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

<<<<<<< HEAD
# ─────────────────────────────────────────────
# OVERTIME ALERTS — WhatsApp via Twilio (optional, degrades gracefully)
# ─────────────────────────────────────────────
OVERTIME_LIMIT_HOURS = float(os.getenv("OVERTIME_LIMIT_HOURS", "105"))
OVERTIME_WARN_AT     = float(os.getenv("OVERTIME_WARN_AT", "0.9"))  # warn at 90% of limit

TWILIO_SID        = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM       = os.getenv("TWILIO_WHATSAPP_FROM", "")  # e.g. "whatsapp:+14155238886"

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
    """Sends a WhatsApp message via Twilio if configured; otherwise logs and
    returns a result dict so callers can still record that an alert 'fired'."""
    if not to_phone:
        return {"sent": False, "reason": "no_phone_on_file"}
    if not WHATSAPP_READY:
        print(f"[OVERTIME ALERT - NOT SENT, no Twilio config] to={to_phone}: {message}", flush=True)
        return {"sent": False, "reason": "twilio_not_configured"}
    try:
        to_whatsapp = to_phone if to_phone.startswith("whatsapp:") else f"whatsapp:+{to_phone.lstrip('+')}"
        msg = twilio_client.messages.create(from_=TWILIO_FROM, to=to_whatsapp, body=message)
        print(f"[OVERTIME ALERT SENT] sid={msg.sid} to={to_phone}", flush=True)
        return {"sent": True, "sid": msg.sid}
    except Exception as e:
        print(f"[OVERTIME ALERT FAILED] to={to_phone}: {type(e).__name__}: {e}", flush=True)
        return {"sent": False, "reason": str(e)}

app = FastAPI(title="AttendTrack API", version="3.0.0")

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
=======
app = FastAPI(title="AttendTrack API", version="3.0.0")
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835
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
<<<<<<< HEAD
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
=======
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835
        for idx in [
            "CREATE INDEX IF NOT EXISTS idx_att_emp_date  ON attendance(employee_id, date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_att_site_date ON attendance(site_id, date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_att_status    ON attendance(status)",
            "CREATE INDEX IF NOT EXISTS idx_att_date      ON attendance(date DESC)",
<<<<<<< HEAD
            "CREATE INDEX IF NOT EXISTS idx_ot_emp_period ON overtime_alerts(employee_id, period_start)",
=======
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835
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

<<<<<<< HEAD
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
        return {"alert_fired": False, "total_hours": total_hours}

    c.execute("""SELECT 1 FROM overtime_alerts WHERE employee_id=%s AND period_start=%s AND alert_level=%s""",
              (employee_id, period_start, level))
    if c.fetchone():
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

=======
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835
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
<<<<<<< HEAD

        c.execute("SELECT name, phone FROM users WHERE employee_id=%s", (req.employee_id,))
        u = c.fetchone()
        overtime_result = check_and_alert_overtime(c, req.employee_id, u["phone"], u["name"], now)
=======
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": f"✅ Checked out. You worked {hours} hours.",
<<<<<<< HEAD
            "hours_worked": hours, "logout_time": now.isoformat(), "distance_from_site": int(dist),
            "overtime": overtime_result}
=======
            "hours_worked": hours, "logout_time": now.isoformat(), "distance_from_site": int(dist)}
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835

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

<<<<<<< HEAD
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

=======
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835
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
<<<<<<< HEAD
        release_db(conn)
=======
        release_db(conn)
>>>>>>> a5ee307f5f322edc37ba42650bca080b30442835
