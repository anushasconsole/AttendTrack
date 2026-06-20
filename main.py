import os, re, json, math, hashlib, base64, io
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# FACE RECOGNITION SETUP
# ─────────────────────────────────────────────
FACE_READY = False
try:
    import face_recognition
    import numpy as np
    from PIL import Image
    FACE_READY = True
    print("✅ face_recognition ready — biometric verification active")
except ImportError:
    print("⚠️  face_recognition not installed — run: pip install face_recognition")

# ─────────────────────────────────────────────
# POSTGRESQL SETUP
# ─────────────────────────────────────────────
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL environment variable not set!")

# Connection pool — handles 2000 employees hitting the API concurrently
db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=2,
    maxconn=20,
    dsn=DATABASE_URL,
    cursor_factory=RealDictCursor
)
print("✅ PostgreSQL connection pool ready")

app = FastAPI(title="AttendTrack API", version="3.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

BASE_DIR = os.path.dirname(__file__)
FRONTEND = os.path.join(BASE_DIR, "..", "frontend")

# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────
def get_db():
    """Get a connection from the pool"""
    conn = db_pool.getconn()
    conn.autocommit = False
    return conn

def release_db(conn):
    """Return connection to pool"""
    db_pool.putconn(conn)

def init_db():
    conn = get_db()
    try:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS sites (
                id             SERIAL PRIMARY KEY,
                name           TEXT NOT NULL,
                address        TEXT,
                latitude       DOUBLE PRECISION NOT NULL,
                longitude      DOUBLE PRECISION NOT NULL,
                radius_meters  DOUBLE PRECISION DEFAULT 200,
                manager_id     INTEGER,
                created_at     TIMESTAMP DEFAULT NOW()
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id               SERIAL PRIMARY KEY,
                employee_id      TEXT UNIQUE NOT NULL,
                name             TEXT NOT NULL,
                role             TEXT NOT NULL CHECK(role IN ('labour','manager','hr')),
                password_hash    TEXT NOT NULL,
                phone            TEXT,
                department       TEXT,
                site_id          INTEGER REFERENCES sites(id),
                face_embedding   JSONB,
                face_registered  BOOLEAN DEFAULT FALSE,
                face_registered_at TIMESTAMP,
                created_at       TIMESTAMP DEFAULT NOW()
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
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
            )
        """)

        # Indexes for fast queries across years of data
        c.execute("CREATE INDEX IF NOT EXISTS idx_att_emp_date   ON attendance(employee_id, date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_att_site_date  ON attendance(site_id, date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_att_status     ON attendance(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_att_date       ON attendance(date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_role     ON users(role)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_site     ON users(site_id)")

        conn.commit()

        # Seed only if empty
        c.execute("SELECT COUNT(*) as cnt FROM users")
        if c.fetchone()["cnt"] == 0:
            _seed(c)
            conn.commit()

        print("✅ Database ready (PostgreSQL)")
    except Exception as e:
        conn.rollback()
        print(f"❌ DB init error: {e}")
        raise
    finally:
        release_db(conn)


def _hash(p): return hashlib.sha256(p.encode()).hexdigest()

def _seed(c):
    c.execute("""INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES
        ('Main Construction Site','Vijayanagar, Bangalore',23.58195,58.34720,1000),
        ('Warehouse Zone A','56 Industrial Area, Muscat',23.58195,58.34720,1000)""")

    c.execute("""INSERT INTO users (employee_id,name,role,password_hash,phone,department) VALUES
        ('HR001','Priya Sharma','hr',%s,'9876543210','Human Resources')""", (_hash("hr123"),))

    c.execute("""INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES
        ('MGR001','Rajesh Kumar','manager',%s,'9876543211','Operations',1),
        ('MGR002','Suresh Nair','manager',%s,'9876543212','Operations',2)""",
        (_hash("mgr123"), _hash("mgr456")))

    labours = [
        ("LAB001","Prashanth Kumar",1,"9876543213"),
        ("LAB002","Pranav Das",1,"9876543214"),
        ("LAB003","Shreekanth Rao",2,"9876543215"),
        ("LAB004","Mohan Kumar",2,"9876543216"),
    ]
    for eid, name, site, phone in labours:
        c.execute("""INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id)
            VALUES (%s,%s,'labour',%s,%s,'Labour',%s)""",
            (eid, name, _hash("lab123"), phone, site))

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
        if isinstance(login, str):  login  = datetime.fromisoformat(login[:19])
        if isinstance(logout, str): logout = datetime.fromisoformat(logout[:19])
        return round((logout - login).total_seconds() / 3600, 2)
    except:
        return 0.0

def clean_b64(b64: str) -> str:
    return b64.split(",")[1] if "," in b64 else b64

# ─────────────────────────────────────────────
# FACE RECOGNITION (local, offline, free)
# ─────────────────────────────────────────────
def extract_embedding(b64_image: str) -> list | None:
    """
    Convert a base64 face photo into a 128-float embedding vector.
    This vector IS the person's face identity — tiny (512 bytes), fast to compare.
    """
    if not FACE_READY:
        return None
    try:
        img_bytes  = base64.b64decode(clean_b64(b64_image))
        pil_img    = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_array  = np.array(pil_img)
        encodings  = face_recognition.face_encodings(
            img_array,
            model="large"   # "large" = more accurate; "small" = faster
        )
        if not encodings:
            return None   # no face found in image
        return encodings[0].tolist()  # 128 floats
    except Exception as e:
        print(f"Embedding error: {e}")
        return None


def compare_faces(stored_embedding: list, live_b64: str) -> dict:
    """
    Compare a live camera image against a stored 128-float embedding.
    Distance < 0.45 = same person (tune as needed).
    Returns match result with confidence score.
    """
    if not FACE_READY:
        return {
            "match": True, "confidence": 0.5,
            "reason": "Face recognition unavailable — bypass mode", "bypass": True
        }

    live_embedding = extract_embedding(live_b64)
    if live_embedding is None:
        return {
            "match": False, "confidence": 0.0,
            "reason": "No face detected in camera image. Ensure good lighting and face the camera directly.",
            "bypass": False
        }

    stored = np.array(stored_embedding)
    live   = np.array(live_embedding)

    distance   = float(face_recognition.face_distance([stored], live)[0])
    confidence = round(max(0.0, 1.0 - distance), 3)

    # Threshold tuning guide:
    #   0.35 = very strict (twins might fail)
    #   0.45 = recommended for construction site (helmet, dust, varying light)
    #   0.55 = lenient (some security risk)
    THRESHOLD = 0.45

    return {
        "match":      distance < THRESHOLD,
        "confidence": confidence,
        "distance":   round(distance, 4),
        "reason":     "Face matched ✅" if distance < THRESHOLD
                      else f"Face does not match (distance: {distance:.3f}, threshold: {THRESHOLD})",
        "bypass":     False
    }

# ─────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────
class LoginReq(BaseModel):
    employee_id: str
    password:    str

class FaceVerifyReq(BaseModel):
    employee_id: str
    live_image:  str   # base64 from webcam

class FaceRegisterReq(BaseModel):
    employee_id: str
    password:    str   # re-confirm for security
    face_image:  str   # base64

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
# FRONTEND — three separate portals, each its own URL
# ─────────────────────────────────────────────
@app.get("/")
async def root():
    return {"msg": "AttendTrack API v3 — use /labour, /manager, or /hr"}

@app.get("/labour")
async def labour_portal():
    path = os.path.join(FRONTEND, "labour.html")
    return FileResponse(path) if os.path.exists(path) else {"msg": "labour.html not found"}

@app.get("/manager")
async def manager_portal():
    path = os.path.join(FRONTEND, "manager.html")
    return FileResponse(path) if os.path.exists(path) else {"msg": "manager.html not found"}

@app.get("/hr")
async def hr_portal():
    path = os.path.join(FRONTEND, "hr.html")
    return FileResponse(path) if os.path.exists(path) else {"msg": "hr.html not found"}

@app.get("/health")
async def health():
    return {
        "status":       "ok",
        "face_ready":   FACE_READY,
        "db":           "postgresql"
    }


# ─────────────────────────────────────────────
# AUTH — STEP 1: PASSWORD LOGIN
# ─────────────────────────────────────────────
@app.post("/api/login")
async def login(req: LoginReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT u.*, s.name as site_name, s.latitude, s.longitude, s.radius_meters
            FROM users u LEFT JOIN sites s ON u.site_id = s.id
            WHERE u.employee_id = %s AND u.password_hash = %s
        """, (req.employee_id.upper(), _hash(req.password)))
        user = c.fetchone()
    finally:
        release_db(conn)

    if not user:
        raise HTTPException(401, "Invalid employee ID or password")

    return {
        "success":              True,
        "requires_face_verify": user["role"] == "labour",
        "face_registered":      bool(user["face_registered"]),
        "user": {
            "employee_id": user["employee_id"],
            "name":        user["name"],
            "role":        user["role"],
            "department":  user["department"],
            "phone":       user["phone"],
            "site_id":     user["site_id"],
            "site_name":   user["site_name"],
            "site_lat":    user["latitude"],
            "site_lng":    user["longitude"],
            "site_radius": user["radius_meters"],
        }
    }

# ─────────────────────────────────────────────
# AUTH — STEP 1b: REGISTER FACE (first-time labour)
# ─────────────────────────────────────────────
@app.post("/api/face/register")
async def face_register(req: FaceRegisterReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id, role, password_hash FROM users WHERE employee_id = %s",
                  (req.employee_id.upper(),))
        user = c.fetchone()

        if not user:
            raise HTTPException(404, "Employee not found")
        if user["role"] != "labour":
            raise HTTPException(403, "Face registration is only for labour accounts")
        if _hash(req.password) != user["password_hash"]:
            raise HTTPException(401, "Incorrect password — cannot register face")
        if not req.face_image or len(req.face_image) < 100:
            raise HTTPException(400, "Invalid face image")

        # Extract the 128-float embedding from the photo
        embedding = extract_embedding(req.face_image)
        if embedding is None and FACE_READY:
            raise HTTPException(400, "No face detected in the image. Please ensure your face is clearly visible and well-lit.")

        if embedding:
            # Store the tiny embedding vector (NOT the full image)
            c.execute("""
                UPDATE users
                SET face_embedding = %s, face_registered = TRUE, face_registered_at = NOW()
                WHERE employee_id = %s
            """, (json.dumps(embedding), req.employee_id.upper()))
        else:
            # face_recognition not installed — bypass mode
            c.execute("""
                UPDATE users SET face_registered = TRUE, face_registered_at = NOW()
                WHERE employee_id = %s
            """, (req.employee_id.upper(),))

        conn.commit()
        return {"success": True, "message": "Face registered successfully. You can now login with face verification."}
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)

# ─────────────────────────────────────────────
# AUTH — STEP 2: FACE VERIFICATION (labour only)
# ─────────────────────────────────────────────
@app.post("/api/face/verify")
async def face_verify(req: FaceVerifyReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT employee_id, role, face_embedding, face_registered FROM users WHERE employee_id = %s",
                  (req.employee_id.upper(),))
        user = c.fetchone()
    finally:
        release_db(conn)

    if not user:
        raise HTTPException(404, "Employee not found")
    if user["role"] != "labour":
        raise HTTPException(403, "Face verification only required for labour")
    if not user["face_registered"] or not user["face_embedding"]:
        return {
            "success":          False,
            "needs_registration": True,
            "message":          "No face registered. Please register your face first."
        }

    stored_embedding = user["face_embedding"]  # already a list from JSONB
    if isinstance(stored_embedding, str):
        stored_embedding = json.loads(stored_embedding)

    result = compare_faces(stored_embedding, req.live_image)

    if result.get("bypass"):
        return {
            "success":    True,
            "match":      True,
            "confidence": result["confidence"],
            "message":    "⚠️ Face recognition unavailable — access granted (bypass mode)",
            "bypass":     True
        }

    if result["match"]:
        return {
            "success":    True,
            "match":      True,
            "confidence": result["confidence"],
            "message":    f"✅ Face verified ({int(result['confidence']*100)}% match)"
        }
    else:
        return {
            "success":    False,
            "match":      False,
            "confidence": result["confidence"],
            "reason":     result["reason"],
            "message":    f"❌ Face does not match ({int(result['confidence']*100)}% confidence). Try better lighting."
        }

# ─────────────────────────────────────────────
# LABOUR — CHECK IN
# ─────────────────────────────────────────────
@app.post("/api/checkin")
async def checkin(req: CheckinReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT u.*, s.latitude as slat, s.longitude as slng,
                   s.radius_meters as srad, s.id as sid
            FROM users u JOIN sites s ON u.site_id = s.id
            WHERE u.employee_id = %s
        """, (req.employee_id,))
        user = c.fetchone()

        if not user:
            raise HTTPException(404, "Employee not found or no site assigned")

        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT id FROM attendance WHERE employee_id = %s AND date = %s",
                  (req.employee_id, today))
        if c.fetchone():
            raise HTTPException(400, "You have already checked in today. Only one check-in per day is allowed.")

        dist    = haversine(req.latitude, req.longitude, user["slat"], user["slng"])
        in_zone = dist <= user["srad"]
        if not in_zone:
            raise HTTPException(400, f"You are {int(dist)}m from your site. Must be within {int(user['srad'])}m to check in.")

        now = datetime.now()
        c.execute("""
            INSERT INTO attendance (employee_id, site_id, login_time, login_lat, login_lng, date, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'active')
            RETURNING id
        """, (req.employee_id, user["sid"], now, req.latitude, req.longitude, today))
        att_id = c.fetchone()["id"]
        conn.commit()

        return {
            "success":            True,
            "attendance_id":      att_id,
            "message":            f"✅ Checked in at {now.strftime('%H:%M:%S')}",
            "login_time":         now.isoformat(),
            "distance_from_site": int(dist)
        }
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)

# ─────────────────────────────────────────────
# LABOUR — CHECK OUT
# ─────────────────────────────────────────────
@app.post("/api/checkout")
async def checkout(req: CheckoutReq):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT a.*, s.latitude as slat, s.longitude as slng, s.radius_meters as srad
            FROM attendance a JOIN sites s ON a.site_id = s.id
            WHERE a.id = %s AND a.employee_id = %s AND a.logout_time IS NULL
        """, (req.attendance_id, req.employee_id))
        att = c.fetchone()

        if not att:
            raise HTTPException(404, "Active attendance record not found")

        dist  = haversine(req.latitude, req.longitude, att["slat"], att["slng"])
        now   = datetime.now()
        hours = calc_hours(att["login_time"], now)

        c.execute("""
            UPDATE attendance
            SET logout_time=%s, logout_lat=%s, logout_lng=%s, hours_worked=%s, status='pending_manager'
            WHERE id=%s
        """, (now, req.latitude, req.longitude, hours, req.attendance_id))
        conn.commit()

        return {
            "success":            True,
            "message":            f"✅ Checked out. You worked {hours} hours.",
            "hours_worked":       hours,
            "logout_time":        now.isoformat(),
            "distance_from_site": int(dist)
        }
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)

# ─────────────────────────────────────────────
# LABOUR — TODAY / HISTORY
# ─────────────────────────────────────────────
@app.get("/api/attendance/today/{employee_id}")
async def today_status(employee_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn  = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT a.*, s.name as site_name FROM attendance a
            JOIN sites s ON a.site_id = s.id
            WHERE a.employee_id = %s AND a.date = %s
            ORDER BY a.id DESC LIMIT 1
        """, (employee_id, today))
        row = c.fetchone()
    finally:
        release_db(conn)

    if not row:
        return {"has_record": False}
    return {"has_record": True, "record": dict(row)}

@app.get("/api/attendance/history/{employee_id}")
async def history(employee_id: str, limit: int = 30):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT a.*, s.name as site_name FROM attendance a
            JOIN sites s ON a.site_id = s.id
            WHERE a.employee_id = %s
            ORDER BY a.date DESC, a.id DESC LIMIT %s
        """, (employee_id, limit))
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"records": [dict(r) for r in rows]}

# ─────────────────────────────────────────────
# MANAGER
# ─────────────────────────────────────────────
def _get_manager_site(c, manager_id: str) -> int:
    c.execute("SELECT site_id FROM users WHERE employee_id = %s AND role = 'manager'", (manager_id,))
    mgr = c.fetchone()
    if not mgr:
        raise HTTPException(403, "Not a manager")
    return mgr["site_id"]

@app.get("/api/manager/pending/{manager_id}")
async def manager_pending(manager_id: str):
    conn = get_db()
    try:
        c = conn.cursor()
        sid = _get_manager_site(c, manager_id)
        c.execute("""
            SELECT a.*, u.name as labour_name, u.phone, s.name as site_name
            FROM attendance a
            JOIN users u ON a.employee_id = u.employee_id
            JOIN sites s ON a.site_id = s.id
            WHERE a.site_id = %s AND a.status = 'pending_manager' AND a.logout_time IS NOT NULL
            ORDER BY a.date DESC
        """, (sid,))
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"records": [dict(r) for r in rows]}

@app.get("/api/manager/approved/{manager_id}")
async def manager_approved(manager_id: str, date: str = None):
    conn = get_db()
    try:
        c = conn.cursor()
        sid = _get_manager_site(c, manager_id)
        q = """
            SELECT a.*, u.name as labour_name, u.phone, s.name as site_name
            FROM attendance a
            JOIN users u ON a.employee_id = u.employee_id
            JOIN sites s ON a.site_id = s.id
            WHERE a.site_id = %s AND a.status IN ('pending_hr','approved','rejected')
              AND a.manager_verified = TRUE
        """
        params = [sid]
        if date: q += " AND a.date = %s"; params.append(date)
        q += " ORDER BY a.date DESC, a.id DESC"
        c.execute(q, params)
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"records": [dict(r) for r in rows]}

@app.get("/api/manager/all/{manager_id}")
async def manager_all(manager_id: str, date: str = None):
    conn = get_db()
    try:
        c = conn.cursor()
        sid = _get_manager_site(c, manager_id)
        q = """
            SELECT a.*, u.name as labour_name, u.phone, s.name as site_name
            FROM attendance a
            JOIN users u ON a.employee_id = u.employee_id
            JOIN sites s ON a.site_id = s.id
            WHERE a.site_id = %s
        """
        params = [sid]
        if date: q += " AND a.date = %s"; params.append(date)
        q += " ORDER BY a.date DESC, a.id DESC"
        c.execute(q, params)
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"records": [dict(r) for r in rows]}

@app.get("/api/manager/stats/{manager_id}")
async def manager_stats(manager_id: str):
    conn  = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        c = conn.cursor()
        sid = _get_manager_site(c, manager_id)

        c.execute("SELECT COUNT(DISTINCT employee_id) as cnt FROM attendance WHERE site_id=%s AND date=%s", (sid,today))
        today_checkins = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE site_id=%s AND status='pending_manager'", (sid,))
        pending = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE site_id=%s AND status IN ('pending_hr','approved') AND date=%s", (sid,today))
        approved = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM users WHERE site_id=%s AND role='labour'", (sid,))
        total_labour = c.fetchone()["cnt"]
    finally:
        release_db(conn)

    return {
        "today_unique_checkins": today_checkins,
        "pending_count":         pending,
        "approved_count":        approved,
        "total_labours_site":    total_labour,
    }

@app.post("/api/manager/verify")
async def manager_verify(req: ManagerVerifyReq):
    conn = get_db()
    try:
        c = conn.cursor()
        sid = _get_manager_site(c, req.manager_id)

        c.execute("SELECT id FROM attendance WHERE id = %s AND site_id = %s", (req.attendance_id, sid))
        if not c.fetchone():
            raise HTTPException(404, "Record not found or not in your site")

        new_status = "pending_hr" if req.approved else "rejected"
        c.execute("""
            UPDATE attendance
            SET manager_verified=%s, manager_note=%s, manager_verified_at=NOW(), status=%s
            WHERE id=%s
        """, (req.approved, req.note, new_status, req.attendance_id))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": f"Record {'approved and sent to HR' if req.approved else 'rejected'}"}

# ─────────────────────────────────────────────
# HR
# ─────────────────────────────────────────────
def _assert_hr(c, hr_id: str):
    c.execute("SELECT id FROM users WHERE employee_id = %s AND role = 'hr'", (hr_id,))
    if not c.fetchone():
        raise HTTPException(403, "Not HR")

@app.get("/api/hr/pending/{hr_id}")
async def hr_pending(hr_id: str):
    conn = get_db()
    try:
        c = conn.cursor()
        _assert_hr(c, hr_id)
        c.execute("""
            SELECT a.*, u.name as labour_name, u.phone, u.department, s.name as site_name
            FROM attendance a
            JOIN users u ON a.employee_id = u.employee_id
            JOIN sites s ON a.site_id = s.id
            WHERE a.status = 'pending_hr' AND a.manager_verified = TRUE
            ORDER BY a.date DESC
        """)
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"records": [dict(r) for r in rows]}

@app.get("/api/hr/all/{hr_id}")
async def hr_all(hr_id: str, date: str = None, employee_id: str = None):
    conn = get_db()
    try:
        c = conn.cursor()
        _assert_hr(c, hr_id)
        q = """
            SELECT a.*, u.name as labour_name, u.phone, u.department, s.name as site_name
            FROM attendance a
            JOIN users u ON a.employee_id = u.employee_id
            JOIN sites s ON a.site_id = s.id
            WHERE a.status IN ('pending_hr','approved','rejected') AND a.manager_verified = TRUE
        """
        params = []
        if date:        q += " AND a.date = %s";        params.append(date)
        if employee_id: q += " AND a.employee_id = %s"; params.append(employee_id)
        q += " ORDER BY a.date DESC LIMIT 500"
        c.execute(q, params)
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"records": [dict(r) for r in rows]}

@app.post("/api/hr/verify")
async def hr_verify(req: HRVerifyReq):
    conn = get_db()
    try:
        c = conn.cursor()
        _assert_hr(c, req.hr_id)
        new_status = "approved" if req.approved else "rejected"
        c.execute("""
            UPDATE attendance
            SET hr_verified=%s, hr_note=%s, hr_verified_at=NOW(), status=%s
            WHERE id=%s
        """, (req.approved, req.note, new_status, req.attendance_id))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))
    finally:
        release_db(conn)
    return {"success": True, "message": f"Record {new_status} and recorded in system."}

@app.get("/api/hr/summary")
async def hr_summary():
    conn  = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(DISTINCT employee_id) as cnt FROM attendance WHERE date=%s", (today,))
        today_checkins = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='pending_manager'")
        pending_mgr = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='pending_hr' AND manager_verified=TRUE")
        pending_hr = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM attendance WHERE status='approved' AND date=%s", (today,))
        approved_today = c.fetchone()["cnt"]

        c.execute("SELECT COUNT(*) as cnt FROM users WHERE role='labour'")
        total_emp = c.fetchone()["cnt"]
    finally:
        release_db(conn)

    return {
        "today_checkins":  today_checkins,
        "pending_manager": pending_mgr,
        "pending_hr":      pending_hr,
        "approved_today":  approved_today,
        "total_employees": total_emp,
    }

@app.get("/api/employees")
async def get_employees():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT u.employee_id, u.name, u.role, u.phone, u.department,
                   u.face_registered, s.name as site_name
            FROM users u LEFT JOIN sites s ON u.site_id = s.id
            ORDER BY u.role, u.name
        """)
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"employees": [dict(r) for r in rows]}

@app.get("/api/sites")
async def get_sites():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM sites")
        rows = c.fetchall()
    finally:
        release_db(conn)
    return {"sites": [dict(r) for r in rows]}
