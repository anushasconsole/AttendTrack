import os, re, json, math, sqlite3, hashlib, base64
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_READY   = False
try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        GEMINI_READY = True
        print("✅ Gemini Vision ready — face verification active")
    else:
        print("⚠️  GEMINI_API_KEY not set — face verification in bypass mode")
except ImportError:
    print("⚠️  google-generativeai not installed — run: pip install google-generativeai")

app = FastAPI(title="AttendTrack API", version="2.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

BASE_DIR = os.path.dirname(__file__)
FRONTEND = os.path.join(BASE_DIR, "..", "frontend")
# On Render.com: use persistent disk at /data. Locally: use same folder.
_DATA_DIR = "/data" if os.path.exists("/data") else BASE_DIR
DB_PATH   = os.path.join(_DATA_DIR, "attendance.db")

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db(); c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id      TEXT UNIQUE NOT NULL,
        name             TEXT NOT NULL,
        role             TEXT NOT NULL CHECK(role IN ('labour','manager','hr')),
        password_hash    TEXT NOT NULL,
        phone            TEXT,
        department       TEXT,
        site_id          INTEGER,
        face_image_b64   TEXT,          -- stored only for labour
        face_registered  INTEGER DEFAULT 0,
        created_at       TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # Safe migration: add face columns if they don't exist yet
    for col, defn in [("face_image_b64","TEXT"), ("face_registered","INTEGER DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")
        except Exception:
            pass  # already exists

    c.execute("""CREATE TABLE IF NOT EXISTS sites (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        name           TEXT NOT NULL,
        address        TEXT,
        latitude       REAL NOT NULL,
        longitude      REAL NOT NULL,
        radius_meters  REAL DEFAULT 200,
        manager_id     INTEGER,
        created_at     TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS attendance (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id         TEXT NOT NULL,
        site_id             INTEGER NOT NULL,
        login_time          TEXT NOT NULL,
        logout_time         TEXT,
        login_lat           REAL,
        login_lng           REAL,
        logout_lat          REAL,
        logout_lng          REAL,
        hours_worked        REAL,
        status              TEXT DEFAULT 'active'
                            CHECK(status IN ('active','pending_manager','pending_hr','approved','rejected')),
        manager_verified    INTEGER DEFAULT 0,
        manager_note        TEXT,
        manager_verified_at TEXT,
        hr_verified         INTEGER DEFAULT 0,
        hr_note             TEXT,
        hr_verified_at      TEXT,
        date                TEXT NOT NULL,
        FOREIGN KEY(employee_id) REFERENCES users(employee_id),
        FOREIGN KEY(site_id)     REFERENCES sites(id)
    )""")

    conn.commit()
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        _seed(c)
    conn.commit(); conn.close()
    print("✅ Database ready")

def _hash(p): return hashlib.sha256(p.encode()).hexdigest()

def _seed(c):
    c.execute("INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (?,?,?,?,?)",
              ("Main Construction Site","12 MG Road, Bangalore",12.9716,77.5946,300))
    c.execute("INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (?,?,?,?,?)",
              ("Warehouse Zone A","56 Industrial Area, Mysore",12.2958,76.6394,250))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department) VALUES (?,?,?,?,?,?)",
              ("HR001","Priya Sharma","hr",_hash("hr123"),"9876543210","Human Resources"))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (?,?,?,?,?,?,?)",
              ("MGR001","Rajesh Kumar","manager",_hash("mgr123"),"9876543211","Operations",1))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (?,?,?,?,?,?,?)",
              ("MGR002","Suresh Nair","manager",_hash("mgr456"),"9876543212","Operations",2))
    for i,(eid,name,site) in enumerate([
        ("LAB001","Ramu Verma",1),("LAB002","Shyam Das",1),
        ("LAB003","Kiran Patil",2),("LAB004","Mohan Reddy",1),
    ]):
        c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (?,?,?,?,?,?,?)",
                  (eid,name,"labour",_hash("lab123"),f"98765432{13+i}","Labour",site))
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

def calc_hours(login: str, logout: str) -> float:
    try:
        l1 = datetime.strptime(login[:19],  "%Y-%m-%dT%H:%M:%S")
        l2 = datetime.strptime(logout[:19], "%Y-%m-%dT%H:%M:%S")
        return round((l2-l1).total_seconds()/3600, 2)
    except:
        return 0.0

def clean_b64(b64: str) -> str:
    return b64.split(",")[1] if "," in b64 else b64

def get_mime(b64: str) -> str:
    return "image/png" if "png" in b64[:30] else "image/jpeg"

# ─────────────────────────────────────────────
# GEMINI FACE COMPARISON
# ─────────────────────────────────────────────
async def gemini_compare_faces(stored_b64: str, live_b64: str) -> dict:
    """
    Use Gemini Vision to compare two face images.
    Returns {"match": bool, "confidence": float, "reason": str}
    """
    if not GEMINI_READY:
        # Bypass mode — let through but flag it
        return {"match": True, "confidence": 0.5, "reason": "AI verification unavailable — bypass mode", "bypass": True}

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "You are a biometric face verification system. "
            "Compare these two face images carefully — Image 1 is the registered employee photo, "
            "Image 2 is the live camera capture at login time.\n\n"
            "Analyze:\n"
            "- Are these the same person? Consider facial features, structure, eyes, nose, jawline.\n"
            "- Ignore differences in lighting, angle, expression.\n\n"
            "Respond ONLY with this exact JSON (no other text, no markdown):\n"
            '{"same_person": true or false, "confidence": 0.0-1.0, "reason": "brief explanation"}'
        )
        parts = [
            prompt,
            {"mime_type": get_mime(stored_b64), "data": clean_b64(stored_b64)},
            {"mime_type": get_mime(live_b64),   "data": clean_b64(live_b64)},
        ]
        resp = model.generate_content(parts)
        raw  = re.sub(r'```json\s*|```', '', resp.text.strip()).strip()
        result = json.loads(raw)
        match      = bool(result.get("same_person", False))
        confidence = float(result.get("confidence", 0.5))
        reason     = result.get("reason", "")
        return {"match": match, "confidence": confidence, "reason": reason, "bypass": False}
    except Exception as e:
        print(f"Gemini face compare error: {e}")
        # On AI error: be conservative — deny access
        return {"match": False, "confidence": 0.0, "reason": f"AI error: {str(e)}", "bypass": False}

# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────
class LoginReq(BaseModel):
    employee_id: str
    password: str

class FaceVerifyReq(BaseModel):
    employee_id: str
    live_image:  str   # base64 from webcam

class FaceRegisterReq(BaseModel):
    employee_id: str
    password:    str   # re-confirm password for security
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
# FRONTEND
# ─────────────────────────────────────────────
@app.get("/")
async def root():
    idx = os.path.join(FRONTEND, "index.html")
    return FileResponse(idx) if os.path.exists(idx) else {"msg": "AttendTrack API v2"}

@app.get("/health")
async def health():
    return {"status": "ok", "gemini": GEMINI_READY, "db": os.path.exists(DB_PATH)}

# ─────────────────────────────────────────────
# AUTH — STEP 1: PASSWORD LOGIN
# ─────────────────────────────────────────────
@app.post("/api/login")
async def login(req: LoginReq):
    conn = get_db()
    user = conn.execute(
        "SELECT u.*, s.name as site_name, s.latitude, s.longitude, s.radius_meters "
        "FROM users u LEFT JOIN sites s ON u.site_id=s.id "
        "WHERE u.employee_id=? AND u.password_hash=?",
        (req.employee_id.upper(), _hash(req.password))
    ).fetchone()
    conn.close()
    if not user:
        raise HTTPException(401, "Invalid employee ID or password")

    resp = {
        "success": True,
        "requires_face_verify": user["role"] == "labour",   # ← only labour needs face step
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
    return resp

# ─────────────────────────────────────────────
# AUTH — STEP 1b: REGISTER FACE (first-time labour)
# ─────────────────────────────────────────────
@app.post("/api/face/register")
async def face_register(req: FaceRegisterReq):
    conn = get_db()
    user = conn.execute(
        "SELECT employee_id, role, password_hash FROM users WHERE employee_id=?",
        (req.employee_id.upper(),)
    ).fetchone()

    if not user:
        conn.close(); raise HTTPException(404, "Employee not found")
    if user["role"] != "labour":
        conn.close(); raise HTTPException(403, "Face registration is only for labour accounts")
    if _hash(req.password) != user["password_hash"]:
        conn.close(); raise HTTPException(401, "Incorrect password — cannot register face")
    if not req.face_image or len(req.face_image) < 100:
        conn.close(); raise HTTPException(400, "Invalid face image")

    # Store up to 30KB of base64 — enough for Gemini comparison
    face_store = req.face_image[:40000]
    conn.execute(
        "UPDATE users SET face_image_b64=?, face_registered=1 WHERE employee_id=?",
        (face_store, req.employee_id.upper())
    )
    conn.commit(); conn.close()
    return {"success": True, "message": "Face registered successfully. You can now login with face verification."}

# ─────────────────────────────────────────────
# AUTH — STEP 2: FACE VERIFICATION (labour only)
# ─────────────────────────────────────────────
@app.post("/api/face/verify")
async def face_verify(req: FaceVerifyReq):
    conn = get_db()
    user = conn.execute(
        "SELECT employee_id, role, face_image_b64, face_registered FROM users WHERE employee_id=?",
        (req.employee_id.upper(),)
    ).fetchone()
    conn.close()

    if not user:
        raise HTTPException(404, "Employee not found")
    if user["role"] != "labour":
        raise HTTPException(403, "Face verification only required for labour")

    # If no face registered yet — return special flag
    if not user["face_registered"] or not user["face_image_b64"]:
        return {
            "success": False,
            "needs_registration": True,
            "message": "No face registered. Please register your face first."
        }

    # Compare with Gemini
    result = await gemini_compare_faces(user["face_image_b64"], req.live_image)

    # Confidence threshold: 0.70 — adjust if needed
    THRESHOLD = 0.70
    if result["bypass"]:
        # Gemini unavailable — allow access with warning
        return {
            "success":    True,
            "match":      True,
            "confidence": result["confidence"],
            "message":    "⚠️ AI verification unavailable — access granted (bypass mode)",
            "bypass":     True
        }

    if result["match"] and result["confidence"] >= THRESHOLD:
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
            "message":    f"❌ Face does not match ({int(result['confidence']*100)}% confidence). Please try again in good lighting."
        }

# ─────────────────────────────────────────────
# LABOUR — CHECK IN (face must already be verified client-side)
# ─────────────────────────────────────────────
@app.post("/api/checkin")
async def checkin(req: CheckinReq):
    conn = get_db()
    user = conn.execute(
        "SELECT u.*, s.latitude as slat, s.longitude as slng, s.radius_meters as srad, s.id as sid "
        "FROM users u JOIN sites s ON u.site_id=s.id WHERE u.employee_id=?",
        (req.employee_id,)
    ).fetchone()

    if not user:
        conn.close(); raise HTTPException(404, "Employee not found or no site assigned")

    # Labour: only ONE check-in per day (any status)
    today = datetime.now().strftime("%Y-%m-%d")
    any_today = conn.execute(
        "SELECT id FROM attendance WHERE employee_id=? AND date=?",
        (req.employee_id, today)
    ).fetchone()
    if any_today:
        conn.close(); raise HTTPException(400, "You have already checked in today. Only one check-in per day is allowed.")

    # Geofence
    dist   = haversine(req.latitude, req.longitude, user["slat"], user["slng"])
    in_zone = dist <= user["srad"]
    if not in_zone:
        conn.close()
        raise HTTPException(400, f"You are {int(dist)}m from your site. Must be within {int(user['srad'])}m to check in.")

    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO attendance (employee_id,site_id,login_time,login_lat,login_lng,date,status) VALUES (?,?,?,?,?,?,?)",
        (req.employee_id, user["sid"], now, req.latitude, req.longitude, today, "active")
    )
    conn.commit()
    att_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {
        "success": True,
        "attendance_id": att_id,
        "message": f"✅ Checked in at {datetime.now().strftime('%H:%M:%S')}",
        "login_time": now,
        "distance_from_site": int(dist)
    }

# ─────────────────────────────────────────────
# LABOUR — CHECK OUT
# ─────────────────────────────────────────────
@app.post("/api/checkout")
async def checkout(req: CheckoutReq):
    conn = get_db()
    att = conn.execute(
        "SELECT a.*, s.latitude as slat, s.longitude as slng, s.radius_meters as srad "
        "FROM attendance a JOIN sites s ON a.site_id=s.id "
        "WHERE a.id=? AND a.employee_id=? AND a.logout_time IS NULL",
        (req.attendance_id, req.employee_id)
    ).fetchone()
    if not att:
        conn.close(); raise HTTPException(404, "Active attendance record not found")

    dist  = haversine(req.latitude, req.longitude, att["slat"], att["slng"])
    now   = datetime.now().isoformat()
    hours = calc_hours(att["login_time"], now)
    conn.execute(
        "UPDATE attendance SET logout_time=?,logout_lat=?,logout_lng=?,hours_worked=?,status=? WHERE id=?",
        (now, req.latitude, req.longitude, hours, "pending_manager", req.attendance_id)
    )
    conn.commit(); conn.close()
    return {
        "success": True,
        "message": f"✅ Checked out. You worked {hours} hours.",
        "hours_worked": hours,
        "logout_time": now,
        "distance_from_site": int(dist)
    }

# ─────────────────────────────────────────────
# LABOUR — TODAY / HISTORY
# ─────────────────────────────────────────────
@app.get("/api/attendance/today/{employee_id}")
async def today_status(employee_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    conn  = get_db()
    row   = conn.execute(
        "SELECT a.*, s.name as site_name FROM attendance a JOIN sites s ON a.site_id=s.id "
        "WHERE a.employee_id=? AND a.date=? ORDER BY a.id DESC LIMIT 1",
        (employee_id, today)
    ).fetchone()
    conn.close()
    if not row:
        return {"has_record": False}
    return {"has_record": True, "record": dict(row)}

@app.get("/api/attendance/history/{employee_id}")
async def history(employee_id: str, limit: int = 30):
    conn = get_db()
    rows = conn.execute(
        "SELECT a.*, s.name as site_name FROM attendance a JOIN sites s ON a.site_id=s.id "
        "WHERE a.employee_id=? ORDER BY a.date DESC, a.id DESC LIMIT ?",
        (employee_id, limit)
    ).fetchall()
    conn.close()
    return {"records": [dict(r) for r in rows]}

# ─────────────────────────────────────────────
# MANAGER
# ─────────────────────────────────────────────
@app.get("/api/manager/pending/{manager_id}")
async def manager_pending(manager_id: str):
    conn = get_db()
    mgr  = conn.execute("SELECT site_id FROM users WHERE employee_id=? AND role='manager'", (manager_id,)).fetchone()
    if not mgr:
        conn.close(); raise HTTPException(403, "Not a manager")
    rows = conn.execute(
        "SELECT a.*, u.name as labour_name, u.phone, s.name as site_name "
        "FROM attendance a JOIN users u ON a.employee_id=u.employee_id JOIN sites s ON a.site_id=s.id "
        "WHERE a.site_id=? AND a.status='pending_manager' AND a.logout_time IS NOT NULL ORDER BY a.date DESC",
        (mgr["site_id"],)
    ).fetchall()
    conn.close()
    return {"records": [dict(r) for r in rows]}

@app.get("/api/manager/approved/{manager_id}")
async def manager_approved(manager_id: str, date: str = None):
    conn = get_db()
    mgr  = conn.execute("SELECT site_id FROM users WHERE employee_id=? AND role='manager'", (manager_id,)).fetchone()
    if not mgr:
        conn.close(); raise HTTPException(403, "Not a manager")
    q = ("SELECT a.*, u.name as labour_name, u.phone, s.name as site_name "
         "FROM attendance a JOIN users u ON a.employee_id=u.employee_id JOIN sites s ON a.site_id=s.id "
         "WHERE a.site_id=? AND a.status IN ('pending_hr','approved','rejected') AND a.manager_verified=1")
    params = [mgr["site_id"]]
    if date: q += " AND a.date=?"; params.append(date)
    q += " ORDER BY a.date DESC, a.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {"records": [dict(r) for r in rows]}

@app.get("/api/manager/all/{manager_id}")
async def manager_all(manager_id: str, date: str = None):
    conn = get_db()
    mgr  = conn.execute("SELECT site_id FROM users WHERE employee_id=? AND role='manager'", (manager_id,)).fetchone()
    if not mgr:
        conn.close(); raise HTTPException(403, "Not a manager")
    q = ("SELECT a.*, u.name as labour_name, u.phone, s.name as site_name "
         "FROM attendance a JOIN users u ON a.employee_id=u.employee_id JOIN sites s ON a.site_id=s.id "
         "WHERE a.site_id=?")
    params = [mgr["site_id"]]
    if date: q += " AND a.date=?"; params.append(date)
    q += " ORDER BY a.date DESC, a.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {"records": [dict(r) for r in rows]}

@app.get("/api/manager/stats/{manager_id}")
async def manager_stats(manager_id: str):
    conn  = get_db()
    mgr   = conn.execute("SELECT site_id FROM users WHERE employee_id=? AND role='manager'", (manager_id,)).fetchone()
    if not mgr:
        conn.close(); raise HTTPException(403, "Not a manager")
    today = datetime.now().strftime("%Y-%m-%d"); sid = mgr["site_id"]
    stats = {
        "today_unique_checkins": conn.execute("SELECT COUNT(DISTINCT employee_id) FROM attendance WHERE site_id=? AND date=?", (sid,today)).fetchone()[0],
        "pending_count":         conn.execute("SELECT COUNT(*) FROM attendance WHERE site_id=? AND status='pending_manager'", (sid,)).fetchone()[0],
        "approved_count":        conn.execute("SELECT COUNT(*) FROM attendance WHERE site_id=? AND status IN ('pending_hr','approved') AND date=?", (sid,today)).fetchone()[0],
        "total_labours_site":    conn.execute("SELECT COUNT(*) FROM users WHERE site_id=? AND role='labour'", (sid,)).fetchone()[0],
    }
    conn.close(); return stats

@app.post("/api/manager/verify")
async def manager_verify(req: ManagerVerifyReq):
    conn = get_db()
    mgr  = conn.execute("SELECT site_id FROM users WHERE employee_id=? AND role='manager'", (req.manager_id,)).fetchone()
    if not mgr:
        conn.close(); raise HTTPException(403, "Not a manager")
    att  = conn.execute("SELECT * FROM attendance WHERE id=? AND site_id=?", (req.attendance_id, mgr["site_id"])).fetchone()
    if not att:
        conn.close(); raise HTTPException(404, "Record not found or not in your site")
    new_status = "pending_hr" if req.approved else "rejected"
    conn.execute(
        "UPDATE attendance SET manager_verified=?,manager_note=?,manager_verified_at=?,status=? WHERE id=?",
        (1 if req.approved else 0, req.note, datetime.now().isoformat(), new_status, req.attendance_id)
    )
    conn.commit(); conn.close()
    return {"success": True, "message": f"Record {'approved and sent to HR' if req.approved else 'rejected'}"}

# ─────────────────────────────────────────────
# HR
# ─────────────────────────────────────────────
@app.get("/api/hr/pending/{hr_id}")
async def hr_pending(hr_id: str):
    conn = get_db()
    hr   = conn.execute("SELECT id FROM users WHERE employee_id=? AND role='hr'", (hr_id,)).fetchone()
    if not hr:
        conn.close(); raise HTTPException(403, "Not HR")
    rows = conn.execute(
        "SELECT a.*, u.name as labour_name, u.phone, u.department, s.name as site_name "
        "FROM attendance a JOIN users u ON a.employee_id=u.employee_id JOIN sites s ON a.site_id=s.id "
        "WHERE a.status='pending_hr' AND a.manager_verified=1 ORDER BY a.date DESC"
    ).fetchall()
    conn.close()
    return {"records": [dict(r) for r in rows]}

@app.get("/api/hr/all/{hr_id}")
async def hr_all(hr_id: str, date: str = None, employee_id: str = None):
    conn = get_db()
    hr   = conn.execute("SELECT id FROM users WHERE employee_id=? AND role='hr'", (hr_id,)).fetchone()
    if not hr:
        conn.close(); raise HTTPException(403, "Not HR")
    q = ("SELECT a.*, u.name as labour_name, u.phone, u.department, s.name as site_name "
         "FROM attendance a JOIN users u ON a.employee_id=u.employee_id JOIN sites s ON a.site_id=s.id "
         "WHERE a.status IN ('pending_hr','approved','rejected') AND a.manager_verified=1")
    params = []
    if date:        q += " AND a.date=?";        params.append(date)
    if employee_id: q += " AND a.employee_id=?"; params.append(employee_id)
    q += " ORDER BY a.date DESC LIMIT 300"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {"records": [dict(r) for r in rows]}

@app.post("/api/hr/verify")
async def hr_verify(req: HRVerifyReq):
    conn = get_db()
    hr   = conn.execute("SELECT id FROM users WHERE employee_id=? AND role='hr'", (req.hr_id,)).fetchone()
    if not hr:
        conn.close(); raise HTTPException(403, "Not HR")
    new_status = "approved" if req.approved else "rejected"
    conn.execute(
        "UPDATE attendance SET hr_verified=?,hr_note=?,hr_verified_at=?,status=? WHERE id=?",
        (1 if req.approved else 0, req.note, datetime.now().isoformat(), new_status, req.attendance_id)
    )
    conn.commit(); conn.close()
    return {"success": True, "message": f"Record {new_status} and recorded in system."}

@app.get("/api/hr/summary")
async def hr_summary():
    conn  = get_db(); today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "today_checkins":  conn.execute("SELECT COUNT(DISTINCT employee_id) FROM attendance WHERE date=?", (today,)).fetchone()[0],
        "pending_manager": conn.execute("SELECT COUNT(*) FROM attendance WHERE status='pending_manager'").fetchone()[0],
        "pending_hr":      conn.execute("SELECT COUNT(*) FROM attendance WHERE status='pending_hr' AND manager_verified=1").fetchone()[0],
        "approved_today":  conn.execute("SELECT COUNT(*) FROM attendance WHERE status='approved' AND date=?", (today,)).fetchone()[0],
        "total_employees": conn.execute("SELECT COUNT(*) FROM users WHERE role='labour'").fetchone()[0],
    }
    conn.close(); return stats

@app.get("/api/employees")
async def get_employees():
    conn = get_db()
    rows = conn.execute(
        "SELECT u.employee_id,u.name,u.role,u.phone,u.department,u.face_registered,s.name as site_name "
        "FROM users u LEFT JOIN sites s ON u.site_id=s.id ORDER BY u.role,u.name"
    ).fetchall()
    conn.close()
    return {"employees": [dict(r) for r in rows]}

@app.get("/api/sites")
async def get_sites():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sites").fetchall()
    conn.close()
    return {"sites": [dict(r) for r in rows]}