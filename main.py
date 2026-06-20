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

app = FastAPI(title="AttendTrack API", version="3.0.0")
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
        for idx in [
            "CREATE INDEX IF NOT EXISTS idx_att_emp_date  ON attendance(employee_id, date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_att_site_date ON attendance(site_id, date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_att_status    ON attendance(status)",
            "CREATE INDEX IF NOT EXISTS idx_att_date      ON attendance(date DESC)",
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
              ("Main Construction Site","Vijayanagar, Bangalore",23.58195,58.34720,1000))
    c.execute("INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (%s,%s,%s,%s,%s)",
              ("Warehouse Zone A","56 Industrial Area, Muscat",23.58195,58.34720,1000))
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
        img_array = np.ascontiguousarray(np.array(pil_img, dtype=np.uint8))
        print(f"[FACE DEBUG] shape={img_array.shape} dtype={img_array.dtype} contiguous={img_array.flags['C_CONTIGUOUS']}")
        encodings = face_recognition.face_encodings(img_array, model="large")
        return encodings[0].tolist() if encodings else None
    except Exception as e:
        print(f"Embedding error: {e}"); return None

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
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": f"✅ Checked out. You worked {hours} hours.",
            "hours_worked": hours, "logout_time": now.isoformat(), "distance_from_site": int(dist)}

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
