"""
seed_july_demo.py - 100-employee July 2025 demo seed for AttendTrack.

Modelled on the original seed_dummy_data.py cursor/connection patterns
so it works identically on Railway/Postgres.

Run:
    python seed_july_demo.py

STORY EMPLOYEES (demo these in the dashboard):
  DEMO001  Ali Hassan         - BURNOUT SPIKE     (normal June, then 14h+ days from Jul 14)
  DEMO002  Priya Mehta        - GRADUAL CREEP     (hours climbing 8.5 to 13h all July)
  DEMO003  Rashid Al Balushi  - CHRONIC LATENESS  (60-90 min late every day from Jul 13)
  DEMO004  Sunita Rao         - OVERTIME RISK     (11-12h days, way over pace)
  DEMO005  Kevin Fernandes    - CLEAN / NORMAL    (contrast - nothing flags)
  DEMO006  Tariq Ahmed        - REJECTION PATTERN (alternating rejected/pending)
  DEMO007  Deepa Nair         - LOW DATA          (4 days only - graceful fallback)
  DEMO008  Suresh Pillai      - EARLY DEPARTURE   (leaves ~3pm every day from Jul 11)

BULK EMPLOYEES:
  DEMO009-100 - 92 employees with realistic mixed patterns across 2 sites

ALL PASSWORDS: demo123
HR001 / hr123  |  MGR001 / mgr123  |  MGR002 / mgr456
"""

import os
import hashlib
import random
from datetime import date, datetime, timedelta, time

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------
TODAY      = date.today()
JUNE_START = date(2025, 6, 1)
JUNE_END   = date(2025, 6, 30)
JULY_START = date(2025, 7, 1)
JULY_END   = min(TODAY, date(2025, 7, 31))

SITE_LAT = 23.5880
SITE_LNG = 58.3829

FIRST_NAMES = [
    "Aakash","Aamir","Abdullah","Adeel","Ahmed","Akash","Aman","Anand","Anil","Arjun",
    "Deepak","Imran","Irfan","Javed","Khalid","Madhan","Mohammed","Naveen","Prakash","Praveen",
    "Rahul","Rajesh","Ramesh","Rehman","Sanjay","Suresh","Syed","Tariq","Vijay","Vimal",
    "Ajay","Arun","Babu","Chandra","Dinesh","Ganesh","Harish","Jagdish","Kiran","Lakshman",
    "Manoj","Naresh","Omkar","Pavan","Rajan","Satish","Umesh","Venkat","Wasim","Yogesh",
]
LAST_NAMES = [
    "Ahmed","Al Balushi","Al Harthy","Al Habsi","Al Mahrouqi","Al Rawahi",
    "Das","Fernandes","Iyer","Kumar","Nair","Pillai","Rao","Sharma","Singh",
    "Yadav","Dasgupta","Shetty","Chandra","Prasad","Patel","Mehta","Joshi",
]
DEPARTMENTS = ["Civil", "MEP", "Logistics", "Safety", "Admin"]
TRADES = [
    "Helper","Steel Fixer","Electrician","Carpenter","Mason",
    "Pipe Fitter","Welder","Painter","Driver","Forklift Operator",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def db_hash(password):
    return hashlib.sha256(password.encode()).hexdigest()


def workdays(start, end):
    """Mon-Sat, skip Sunday."""
    out, cur = [], start
    while cur <= end:
        if cur.weekday() != 6:
            out.append(cur)
        cur += timedelta(days=1)
    return out


JUNE_DAYS = workdays(JUNE_START, JUNE_END)
JULY_DAYS = workdays(JULY_START, JULY_END)


def make_att_row(employee_id, site_id, day, login_h, login_m, hours,
                 status="approved", manager_verified=True, hr_verified=True):
    login_dt  = datetime.combine(day, time(int(login_h), int(login_m)))
    logout_dt = login_dt + timedelta(hours=hours)
    return (
        employee_id, site_id,
        login_dt, logout_dt,
        SITE_LAT, SITE_LNG, SITE_LAT, SITE_LNG,
        round(hours, 2), status,
        manager_verified, None,
        login_dt if manager_verified else None,
        hr_verified, None,
        login_dt if hr_verified else None,
        day,
    )


def insert_att(cursor, row_tuple):
    cursor.execute(
        """INSERT INTO attendance (
            employee_id, site_id, login_time, logout_time,
            login_lat, login_lng, logout_lat, logout_lng,
            hours_worked, status, manager_verified, manager_note, manager_verified_at,
            hr_verified, hr_note, hr_verified_at, date
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        row_tuple,
    )


def ensure_sites(cursor):
    """Identical pattern to original seed_dummy_data.py."""
    cursor.execute("SELECT id, name FROM sites ORDER BY id")
    sites = cursor.fetchall()
    if sites:
        return sites
    for name, addr in [
        ("Main Construction Site", "Vijayanagar, Bangalore"),
        ("Warehouse Zone A",       "56 Industrial Area, Muscat"),
    ]:
        cursor.execute(
            "INSERT INTO sites (name,address,latitude,longitude,radius_meters) "
            "VALUES (%s,%s,%s,%s,%s)",
            (name, addr, SITE_LAT, SITE_LNG, 1000000000),
        )
    cursor.execute("SELECT id, name FROM sites ORDER BY id")
    return cursor.fetchall()


def ensure_user(cursor, employee_id, name, role, password, phone, department, site_id=None):
    """Identical to original."""
    cursor.execute("SELECT 1 FROM users WHERE employee_id=%s", (employee_id,))
    if cursor.fetchone():
        return False
    if site_id is None:
        cursor.execute(
            "INSERT INTO users (employee_id,name,role,password_hash,phone,department) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (employee_id, name, role, db_hash(password), phone, department),
        )
    else:
        cursor.execute(
            "INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (employee_id, name, role, db_hash(password), phone, department, site_id),
        )
    return True


# ---------------------------------------------------------------------------
# story employee row builders
# ---------------------------------------------------------------------------
def story_burnout_spike(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS:
        rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,10), round(rng.uniform(8.25,9.0),2)))
    for d in JULY_DAYS:
        if d < date(2025,7,14):
            rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,10), round(rng.uniform(8.25,9.0),2)))
        else:
            rows.append(make_att_row(eid, site_id, d, 6, 30, 14.5))
    return rows

def story_gradual_creep(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS:
        rows.append(make_att_row(eid, site_id, d, 9, 0, round(rng.uniform(8.25,9.0),2)))
    n = max(len(JULY_DAYS)-1, 1)
    for i, d in enumerate(JULY_DAYS):
        h = 8.5 + (i/n)*4.5
        rows.append(make_att_row(eid, site_id, d, 9, 0, round(h,2)))
    return rows

def story_chronic_lateness(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS:
        rows.append(make_att_row(eid, site_id, d, 9, 0, round(rng.uniform(8.0,9.0),2)))
    for d in JULY_DAYS:
        if d < date(2025,7,13):
            rows.append(make_att_row(eid, site_id, d, 9, 0, round(rng.uniform(8.0,9.0),2)))
        else:
            late = rng.randint(60,90)
            rows.append(make_att_row(eid, site_id, d, 10 + late//60, late%60, 8.0))
    return rows

def story_overtime_risk(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(make_att_row(eid, site_id, d, 7, 0, round(rng.uniform(11.0,12.5),2)))
    return rows

def story_clean_normal(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,8), round(rng.uniform(8.0,9.0),2)))
    return rows

def story_rejection_pattern(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS:
        rows.append(make_att_row(eid, site_id, d, 8, 30, round(rng.uniform(8.25,9.0),2)))
    cycle = [
        ("rejected",        False, False),
        ("pending_manager", False, False),
        ("pending_hr",      True,  False),
    ]
    for i, d in enumerate(JULY_DAYS):
        s, mv, hv = cycle[i % 3]
        rows.append(make_att_row(eid, site_id, d, 8, 30, round(rng.uniform(8.25,9.0),2),
                                 status=s, manager_verified=mv, hr_verified=hv))
    return rows

def story_low_data(eid, site_id, rng):
    rows = []
    for d in JULY_DAYS[:4]:
        rows.append(make_att_row(eid, site_id, d, 9, 0, round(rng.uniform(8.5,9.0),2)))
    return rows

def story_early_departure(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS:
        rows.append(make_att_row(eid, site_id, d, 9, 0, round(rng.uniform(8.25,9.0),2)))
    for d in JULY_DAYS:
        if d < date(2025,7,11):
            rows.append(make_att_row(eid, site_id, d, 9, 0, round(rng.uniform(8.25,9.0),2)))
        else:
            rows.append(make_att_row(eid, site_id, d, 9, 0, round(rng.uniform(5.0,6.0),2)))
    return rows


STORY_EMPLOYEES = [
    ("DEMO001", "Ali Hassan",          "97100001", "Civil",     story_burnout_spike),
    ("DEMO002", "Priya Mehta",         "97100002", "MEP",       story_gradual_creep),
    ("DEMO003", "Rashid Al Balushi",   "97100003", "Civil",     story_chronic_lateness),
    ("DEMO004", "Sunita Rao",          "97100004", "Logistics", story_overtime_risk),
    ("DEMO005", "Kevin Fernandes",     "97100005", "MEP",       story_clean_normal),
    ("DEMO006", "Tariq Ahmed",         "97100006", "Civil",     story_rejection_pattern),
    ("DEMO007", "Deepa Nair",          "97100007", "Logistics", story_low_data),
    ("DEMO008", "Suresh Pillai",       "97100008", "Civil",     story_early_departure),
]


# ---------------------------------------------------------------------------
# bulk pattern builders
# ---------------------------------------------------------------------------
def bulk_normal(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,12), round(rng.uniform(8.0,9.25),2)))
    return rows

def bulk_overtime(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(make_att_row(eid, site_id, d, 7, rng.randint(0,30), round(rng.uniform(10.0,12.5),2)))
    return rows

def bulk_lateness(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        if rng.random() < 0.5:
            late = rng.randint(30,60)
            rows.append(make_att_row(eid, site_id, d, 9+late//60, late%60, round(rng.uniform(8.0,9.0),2)))
        else:
            rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,10), round(rng.uniform(8.0,9.0),2)))
    return rows

def bulk_early_leave(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        h = round(rng.uniform(6.0,7.0),2) if rng.random() < 0.5 else round(rng.uniform(8.0,9.0),2)
        rows.append(make_att_row(eid, site_id, d, 9, 0, h))
    return rows

def bulk_burnout(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS:
        rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,15), round(rng.uniform(8.0,9.5),2)))
    spike_start = JULY_DAYS[-10] if len(JULY_DAYS) >= 10 else JULY_DAYS[0]
    for d in JULY_DAYS:
        if d < spike_start:
            rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,15), round(rng.uniform(8.0,9.5),2)))
        else:
            rows.append(make_att_row(eid, site_id, d, 6, rng.randint(0,30), round(rng.uniform(13.0,15.0),2)))
    return rows

def bulk_irregular(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        h    = round(rng.uniform(5.0,13.0),2)
        roll = rng.random()
        if roll < 0.20:
            s, mv, hv = "rejected",        False, False
        elif roll < 0.35:
            s, mv, hv = "pending_manager", False, False
        else:
            s, mv, hv = "approved",        True,  True
        rows.append(make_att_row(eid, site_id, d, rng.randint(7,10), rng.randint(0,45), h,
                                 status=s, manager_verified=mv, hr_verified=hv))
    return rows

def bulk_low_data(eid, site_id, rng):
    rows = []
    for d in JULY_DAYS[:rng.randint(5,8)]:
        rows.append(make_att_row(eid, site_id, d, 9, rng.randint(0,15), round(rng.uniform(8.0,9.0),2)))
    return rows


BULK_PATTERNS = [
    (bulk_normal,    0.35),
    (bulk_overtime,  0.15),
    (bulk_lateness,  0.15),
    (bulk_early_leave, 0.10),
    (bulk_burnout,   0.10),
    (bulk_irregular, 0.10),
    (bulk_low_data,  0.05),
]

def pick_pattern(rng):
    roll, cumulative = rng.random(), 0.0
    for fn, weight in BULK_PATTERNS:
        cumulative += weight
        if roll < cumulative:
            return fn
    return bulk_normal


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Export it or put it in .env first.")

    rng = random.Random(42)
    conn = psycopg2.connect(database_url)
    conn.autocommit = False

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:

            # 1. wipe old DEMO attendance (June + July 2025)
            cursor.execute(
                "DELETE FROM attendance WHERE employee_id LIKE 'DEMO%%' "
                "AND date BETWEEN %s AND %s",
                (JUNE_START, date(2025, 7, 31)),
            )
            print(f"  Cleared {cursor.rowcount} old DEMO attendance rows")

            # 2. wipe old DEMO users
            cursor.execute("DELETE FROM users WHERE employee_id LIKE 'DEMO%%'")
            print(f"  Cleared {cursor.rowcount} old DEMO users")

            # 3. ensure sites — using exact same pattern as original seed script
            sites = ensure_sites(cursor)
            primary_site_id   = sites[0]["id"]
            secondary_site_id = sites[1]["id"] if len(sites) > 1 else sites[0]["id"]
            print(f"  Sites: {[s['name'] for s in sites]}")

            # 4. ensure HR and managers (idempotent)
            ensure_user(cursor, "HR001",  "Priya Sharma", "hr",      "hr123",  "9876543210", "Human Resources")
            ensure_user(cursor, "MGR001", "Rajesh Kumar", "manager", "mgr123", "9876543211", "Operations", primary_site_id)
            ensure_user(cursor, "MGR002", "Suresh Nair",  "manager", "mgr456", "9876543212", "Operations", secondary_site_id)
            print("  HR and managers ready")

            # 5. story employees DEMO001-008
            total_att = 0
            for idx, (eid, name, phone, dept, builder) in enumerate(STORY_EMPLOYEES, start=1):
                site_id = primary_site_id if idx <= 4 else secondary_site_id
                ensure_user(cursor, eid, name, "labour", "demo123", phone, dept, site_id)
                for row in builder(eid, site_id, rng):
                    insert_att(cursor, row)
                    total_att += 1
            print("  Created 8 story employees (DEMO001-008)")

            # 6. bulk employees DEMO009-100
            pattern_counts = {}
            for idx in range(9, 101):
                eid     = f"DEMO{idx:03d}"
                first   = FIRST_NAMES[rng.randrange(len(FIRST_NAMES))]
                last    = LAST_NAMES[rng.randrange(len(LAST_NAMES))]
                name    = f"{first} {last}"
                phone   = f"971{idx:06d}"
                dept    = rng.choice(DEPARTMENTS)
                site_id = primary_site_id if idx % 2 == 0 else secondary_site_id
                ensure_user(cursor, eid, name, "labour", "demo123", phone, dept, site_id)
                fn = pick_pattern(rng)
                pattern_counts[fn.__name__] = pattern_counts.get(fn.__name__, 0) + 1
                for row in fn(eid, site_id, rng):
                    insert_att(cursor, row)
                    total_att += 1
            print(f"  Created 92 bulk employees (DEMO009-100)")
            print(f"  Pattern mix: {pattern_counts}")
            print(f"  Total attendance rows inserted: {total_att}")

        conn.commit()
        print()
        print("=" * 60)
        print("DONE - 100 demo employees seeded for July 2025")
        print("=" * 60)
        print()
        print("STORY EMPLOYEES to demo (password: demo123):")
        print("  DEMO001  Ali Hassan         - Burnout spike")
        print("  DEMO002  Priya Mehta        - Gradual hour creep")
        print("  DEMO003  Rashid Al Balushi  - Chronic lateness")
        print("  DEMO004  Sunita Rao         - Overtime risk")
        print("  DEMO005  Kevin Fernandes    - Clean/normal (no flags)")
        print("  DEMO006  Tariq Ahmed        - Rejection pattern")
        print("  DEMO007  Deepa Nair         - Low data (graceful fallback)")
        print("  DEMO008  Suresh Pillai      - Early departure streak")
        print()
        print("STAFF:  HR001/hr123  |  MGR001/mgr123  |  MGR002/mgr456")

    except Exception as exc:
        conn.rollback()
        import traceback
        traceback.print_exc()
        raise SystemExit(f"Seed FAILED (rolled back): {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
