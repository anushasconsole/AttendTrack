"""
app/core/database.py — PostgreSQL connection pool, helpers, schema init.
"""
import asyncio
import json
import os

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pg_pool

from app.core.config import DATABASE_URL

# ── TCP keepalive so Railway/Render dropped connections are detected fast ──────
_DSN_EXTRA = "keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=5"
_dsn = DATABASE_URL + ("&" if "?" in DATABASE_URL else "?") + _DSN_EXTRA

# Pool is created lazily on first call to get_db() so tests can import
# this module without needing a running Postgres server.
_db_pool = None

def _get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = pg_pool.ThreadedConnectionPool(
            minconn=2, maxconn=20,
            dsn=_dsn,
            cursor_factory=RealDictCursor,
        )
        print("✅ PostgreSQL pool ready")
    return _db_pool


def get_db():
    """
    Borrow a connection from the pool with a liveness check.
    Replaces stale (Railway-dropped) connections automatically.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        if conn.closed:
            raise psycopg2.OperationalError("connection closed")
        conn.cursor().execute("SELECT 1")
        conn.rollback()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = psycopg2.connect(dsn=_dsn, cursor_factory=RealDictCursor)
        print("[DB] Stale pool connection replaced", flush=True)
    conn.autocommit = False
    return conn


def release_db(conn):
    pool = _get_pool()
    try:
        pool.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


async def run_db(fn, *args, **kwargs):
    """
    Run a synchronous DB function in a thread pool so psycopg2 never
    blocks the asyncio event loop.

        result = await run_db(_my_sync_fn, arg1, arg2)
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def write_audit(c, action: str, actor_id: str, actor_role: str,
                target_id: str = None, target_type: str = None,
                old_value: str = None, new_value: str = None,
                metadata: dict = None, ip_address: str = None):
    """Append one row to audit_log inside the caller's open transaction."""
    c.execute(
        """INSERT INTO audit_log
               (action, actor_id, actor_role, target_id, target_type,
                old_value, new_value, metadata, ip_address)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (action, actor_id, actor_role, target_id, target_type,
         old_value, new_value,
         json.dumps(metadata or {}),
         ip_address),
    )


# ── Schema creation + seed ────────────────────────────────────────────────────

def init_db():
    """Create all tables and indexes; seed demo data if users table is empty."""
    from app.core.config import OVERTIME_LIMIT_HOURS  # avoid circular at module level
    from auth import hash_password

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
        c.execute("""CREATE TABLE IF NOT EXISTS employee_qa (
            id          SERIAL PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES users(employee_id),
            question    TEXT NOT NULL,
            answer      TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT NOW()
        )""")
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
        c.execute("""CREATE TABLE IF NOT EXISTS hr_qa (
            id                  SERIAL PRIMARY KEY,
            hr_id               TEXT NOT NULL REFERENCES users(employee_id),
            question            TEXT NOT NULL,
            about_employee_id   TEXT,
            answer              TEXT NOT NULL,
            created_at          TIMESTAMP DEFAULT NOW()
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log (
            id           SERIAL PRIMARY KEY,
            action       TEXT NOT NULL,
            actor_id     TEXT NOT NULL,
            actor_role   TEXT NOT NULL,
            target_id    TEXT,
            target_type  TEXT,
            old_value    TEXT,
            new_value    TEXT,
            metadata     JSONB DEFAULT '{}',
            ip_address   TEXT,
            created_at   TIMESTAMP DEFAULT NOW()
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
            "CREATE INDEX IF NOT EXISTS idx_users_role    ON users(role)",
            "CREATE INDEX IF NOT EXISTS idx_users_site    ON users(site_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_actor   ON audit_log(actor_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_target  ON audit_log(target_id, created_at DESC)",
        ]:
            c.execute(idx)
        conn.commit()
        c.execute("SELECT COUNT(*) as cnt FROM users")
        if c.fetchone()["cnt"] == 0:
            _seed(c, hash_password)
            conn.commit()
        print("✅ Database ready")
    except Exception as e:
        conn.rollback()
        print(f"❌ DB init error: {e}")
        raise
    finally:
        release_db(conn)


def _seed(c, hash_password_fn):
    c.execute("INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (%s,%s,%s,%s,%s)",
              ("Main Construction Site", "Vijayanagar, Bangalore", 12.618852, 75.249893, 1000000000))
    c.execute("INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (%s,%s,%s,%s,%s)",
              ("Warehouse Zone A", "56 Industrial Area, Muscat", 12.618852, 75.249893, 1000000000))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department) VALUES (%s,%s,%s,%s,%s,%s)",
              ("HR001", "Priya Sharma", "hr", hash_password_fn("hr123"), "9876543210", "Human Resources"))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
              ("MGR001", "Rajesh Kumar", "manager", hash_password_fn("mgr123"), "9876543211", "Operations", 1))
    c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
              ("MGR002", "Suresh Nair", "manager", hash_password_fn("mgr456"), "9876543212", "Operations", 2))
    for eid, name, site, phone in [
        ("LAB001", "Prashanth Kumar", 1, "9876543213"),
        ("LAB002", "Pranav Das",       1, "9876543214"),
        ("LAB003", "Shreekanth Rao",   2, "9876543215"),
        ("LAB004", "Mohan Kumar",      2, "9876543216"),
    ]:
        c.execute("INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (eid, name, "labour", hash_password_fn("lab123"), phone, "Labour", site))
    print("✅ Demo data seeded (bcrypt passwords)")
