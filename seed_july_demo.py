"""
seed_july_demo.py — 100-employee July 2025 demo seed for AttendTrack.

Run with:
    python seed_july_demo.py

WHAT IT DOES:
  1. Wipes all DEMO* attendance rows for June + July 2025
  2. Wipes all DEMO* users
  3. Creates exactly 100 employees across 2 sites and 5 departments
  4. Generates June 2025 attendance (ML baseline — model needs ≥10 days)
  5. Generates July 1–today attendance with clear patterns

EMPLOYEE BREAKDOWN:
  DEMO001–008   8 named "story" employees — one per feature being demoed
  DEMO009–100  92 bulk employees — realistic variation across departments

STORY EMPLOYEES (these are the ones to click on in the demo):
  DEMO001  Ali Hassan         — BURNOUT SPIKE        (sudden 14h+ days from Jul 14)
  DEMO002  Priya Mehta        — GRADUAL CREEP        (hours climbing 8.5→13h all July)
  DEMO003  Rashid Al Balushi  — CHRONIC LATENESS     (60-90 min late every day Jul 13+)
  DEMO004  Sunita Rao         — OVERTIME RISK        (11-12h days, will blow past cap)
  DEMO005  Kevin Fernandes    — CLEAN / NORMAL       (contrast — nothing flags)
  DEMO006  Tariq Ahmed        — REJECTION PATTERN    (alternating rejected/pending)
  DEMO007  Deepa Nair         — LOW DATA             (4 days only — graceful fallback)
  DEMO008  Suresh Pillai      — EARLY DEPARTURE STREAK (leaves ~3pm every day Jul 11+)

BULK EMPLOYEE PATTERNS (randomly assigned):
  normal        — steady 8-9h, on time, mostly approved  (~35%)
  overtime_risk — 10-12h days, pace > 1.0               (~15%)
  lateness      — arrives 30-60 min late 3x/week         (~15%)
  early_leave   — leaves 1-2h early 3x/week              (~10%)
  burnout       — normal then spike last 10 days          (~10%)
  irregular     — high variance hours + some rejections   (~10%)
  low_data      — only 5-8 July days (new joiner)         (~5%)

LOGINS:
  All DEMO employees: password = demo123
  HR001  / hr123    — HR dashboard
  MGR001 / mgr123   — Manager dashboard (Site 1)
  MGR002 / mgr456   — Manager dashboard (Site 2)
"""

import os, hashlib, random
from datetime import date, datetime, time, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

# ── dates ────────────────────────────────────────────────────────────────────
TODAY      = date.today()
JUNE_START = date(2025, 6, 1)
JUNE_END   = date(2025, 6, 30)
JULY_START = date(2025, 7, 1)
JULY_END   = min(TODAY, date(2025, 7, 31))

# ── site coords (both sites use same coords for the demo) ────────────────────
SITE_COORDS = (23.5880, 58.3829)

# ── name pools ───────────────────────────────────────────────────────────────
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
    "Mishra","Tiwari","Pandey","Gupta","Verma","Shah","Ansari",
]
DEPARTMENTS = ["Civil","MEP","Logistics","Safety","Admin"]
TRADES = [
    "Helper","Steel Fixer","Electrician","Carpenter","Mason",
    "Pipe Fitter","Welder","Painter","Driver","Forklift Operator",
    "Lab Technician","Survey Assistant","Supervisor Assistant","Cleaner",
]

# ── story employee definitions ───────────────────────────────────────────────
STORY_USERS = [
    ("DEMO001","Ali Hassan",          "97100001","Civil"),
    ("DEMO002","Priya Mehta",         "97100002","MEP"),
    ("DEMO003","Rashid Al Balushi",   "97100003","Civil"),
    ("DEMO004","Sunita Rao",          "97100004","Logistics"),
    ("DEMO005","Kevin Fernandes",     "97100005","MEP"),
    ("DEMO006","Tariq Ahmed",         "97100006","Civil"),
    ("DEMO007","Deepa Nair",          "97100007","Logistics"),
    ("DEMO008","Suresh Pillai",       "97100008","Civil"),
]

# ── helpers ──────────────────────────────────────────────────────────────────
def pw_hash(pw): return hashlib.sha256(pw.encode()).hexdigest()

def make_dt(d, hour, minute=0):
    return datetime.combine(d, time(int(hour), int(minute % 60)))

def workdays(start, end):
    """Mon–Sat (skip Sunday)."""
    out, cur = [], start
    while cur <= end:
        if cur.weekday() != 6:
            out.append(cur)
        cur += timedelta(days=1)
    return out

def att(eid, site_id, day, login_h, login_m, hours,
        status="approved", mgr_v=True, hr_v=True):
    login_dt  = make_dt(day, login_h, login_m)
    logout_dt = login_dt + timedelta(hours=hours)
    return dict(
        employee_id=eid, site_id=site_id,
        login_time=login_dt, logout_time=logout_dt,
        login_lat=SITE_COORDS[0], login_lng=SITE_COORDS[1],
        logout_lat=SITE_COORDS[0], logout_lng=SITE_COORDS[1],
        hours_worked=round(hours, 2), status=status,
        manager_verified=mgr_v,  manager_note=None,
        manager_verified_at=login_dt if mgr_v else None,
        hr_verified=hr_v, hr_note=None,
        hr_verified_at=login_dt if hr_v else None,
        date=day,
    )

JUNE_DAYS = workdays(JUNE_START, JUNE_END)
JULY_DAYS = workdays(JULY_START, JULY_END)

# ── story employee row builders ───────────────────────────────────────────────

def rows_burnout_spike(eid, site_id, rng):
    """Normal June + normal early July → sudden 14.5h days from Jul 14."""
    rows = []
    for d in JUNE_DAYS:
        rows.append(att(eid, site_id, d, 9, rng.randint(0,10), round(rng.uniform(8.25,9.0),2)))
    for d in JULY_DAYS:
        if d < date(2025,7,14):
            rows.append(att(eid, site_id, d, 9, rng.randint(0,10), round(rng.uniform(8.25,9.0),2)))
        else:
            rows.append(att(eid, site_id, d, 6, 30, 14.5))
    return rows

def rows_gradual_creep(eid, site_id, rng):
    """June normal, July hours linearly climb 8.5→13h."""
    rows = []
    for d in JUNE_DAYS:
        rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(8.25,9.0),2)))
    n = max(len(JULY_DAYS)-1, 1)
    for i, d in enumerate(JULY_DAYS):
        h = 8.5 + (i/n) * 4.5
        rows.append(att(eid, site_id, d, 9, 0, round(h,2)))
    return rows

def rows_chronic_lateness(eid, site_id, rng):
    """On time all June + first half July; 60-90 min late from Jul 13."""
    rows = []
    for d in JUNE_DAYS:
        rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(8.0,9.0),2)))
    for d in JULY_DAYS:
        if d < date(2025,7,13):
            rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(8.0,9.0),2)))
        else:
            late = rng.randint(60, 90)
            rows.append(att(eid, site_id, d, 10 + late//60, late%60, 8.0))
    return rows

def rows_overtime_risk(eid, site_id, rng):
    """Consistently 11-12h days all June and July — well over pace."""
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(att(eid, site_id, d, 7, 0, round(rng.uniform(11.0,12.5),2)))
    return rows

def rows_clean_normal(eid, site_id, rng):
    """Steady 8-9h, on time, nothing flags — contrast employee."""
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(att(eid, site_id, d, 9, rng.randint(0,8), round(rng.uniform(8.0,9.0),2)))
    return rows

def rows_rejection_pattern(eid, site_id, rng):
    """Normal June; July alternates rejected/pending_manager/pending_hr."""
    rows = []
    for d in JUNE_DAYS:
        rows.append(att(eid, site_id, d, 8, 30, round(rng.uniform(8.25,9.0),2)))
    cycle = [
        ("rejected",        False, False),
        ("pending_manager", False, False),
        ("pending_hr",      True,  False),
    ]
    for i, d in enumerate(JULY_DAYS):
        s, mv, hv = cycle[i % 3]
        rows.append(att(eid, site_id, d, 8, 30, round(rng.uniform(8.25,9.0),2),
                        status=s, mgr_v=mv, hr_v=hv))
    return rows

def rows_low_data(eid, site_id, rng):
    """Only 4 July rows — triggers graceful 'not enough data' message."""
    rows = []
    for d in JULY_DAYS[:4]:
        rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(8.5,9.0),2)))
    return rows

def rows_early_departure(eid, site_id, rng):
    """Normal June + early July; then leaves ~3pm (5-6h days) from Jul 11."""
    rows = []
    for d in JUNE_DAYS:
        rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(8.25,9.0),2)))
    for d in JULY_DAYS:
        if d < date(2025,7,11):
            rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(8.25,9.0),2)))
        else:
            rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(5.0,6.0),2)))
    return rows

STORY_BUILDERS = [
    rows_burnout_spike,
    rows_gradual_creep,
    rows_chronic_lateness,
    rows_overtime_risk,
    rows_clean_normal,
    rows_rejection_pattern,
    rows_low_data,
    rows_early_departure,
]

# ── bulk employee pattern builders ───────────────────────────────────────────

def bulk_normal(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(att(eid, site_id, d, 9, rng.randint(0,12), round(rng.uniform(8.0,9.25),2)))
    return rows

def bulk_overtime_risk(eid, site_id, rng):
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        rows.append(att(eid, site_id, d, 7, rng.randint(0,30), round(rng.uniform(10.0,12.5),2)))
    return rows

def bulk_lateness(eid, site_id, rng):
    """Late 3 days per week on average."""
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        if rng.random() < 0.5:
            late = rng.randint(30, 60)
            rows.append(att(eid, site_id, d, 9+late//60, late%60, round(rng.uniform(8.0,9.0),2)))
        else:
            rows.append(att(eid, site_id, d, 9, rng.randint(0,10), round(rng.uniform(8.0,9.0),2)))
    return rows

def bulk_early_leave(eid, site_id, rng):
    """Leaves 1-2h early about half the time."""
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        if rng.random() < 0.5:
            rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(6.0,7.0),2)))
        else:
            rows.append(att(eid, site_id, d, 9, 0, round(rng.uniform(8.0,9.0),2)))
    return rows

def bulk_burnout(eid, site_id, rng):
    """Normal until last 10 July days then spikes."""
    rows = []
    for d in JUNE_DAYS:
        rows.append(att(eid, site_id, d, 9, rng.randint(0,15), round(rng.uniform(8.0,9.5),2)))
    spike_start = JULY_DAYS[-10] if len(JULY_DAYS) >= 10 else JULY_DAYS[0]
    for d in JULY_DAYS:
        if d < spike_start:
            rows.append(att(eid, site_id, d, 9, rng.randint(0,15), round(rng.uniform(8.0,9.5),2)))
        else:
            rows.append(att(eid, site_id, d, 6, rng.randint(0,30), round(rng.uniform(13.0,15.0),2)))
    return rows

def bulk_irregular(eid, site_id, rng):
    """High hour variance + ~20% rejections."""
    rows = []
    for d in JUNE_DAYS + JULY_DAYS:
        h = round(rng.uniform(5.0, 13.0), 2)
        roll = rng.random()
        if roll < 0.20:
            s, mv, hv = "rejected", False, False
        elif roll < 0.35:
            s, mv, hv = "pending_manager", False, False
        else:
            s, mv, hv = "approved", True, True
        rows.append(att(eid, site_id, d, rng.randint(7,10), rng.randint(0,45), h,
                        status=s, mgr_v=mv, hr_v=hv))
    return rows

def bulk_low_data(eid, site_id, rng):
    """New joiner — only 5-8 July days."""
    n = rng.randint(5, 8)
    rows = []
    for d in JULY_DAYS[:n]:
        rows.append(att(eid, site_id, d, 9, rng.randint(0,15), round(rng.uniform(8.0,9.0),2)))
    return rows

# Pattern pool with weights (must sum to 1.0)
BULK_PATTERNS = [
    (bulk_normal,        0.35),
    (bulk_overtime_risk, 0.15),
    (bulk_lateness,      0.15),
    (bulk_early_leave,   0.10),
    (bulk_burnout,       0.10),
    (bulk_irregular,     0.10),
    (bulk_low_data,      0.05),
]

def pick_pattern(rng):
    roll = rng.random()
    cumulative = 0.0
    for fn, weight in BULK_PATTERNS:
        cumulative += weight
        if roll < cumulative:
            return fn
    return BULK_PATTERNS[0][0]

# ── name generator ────────────────────────────────────────────────────────────
def make_name(rng):
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"

# ── SQL ───────────────────────────────────────────────────────────────────────
INSERT_ATT = """
INSERT INTO attendance (
    employee_id, site_id, login_time, logout_time,
    login_lat, login_lng, logout_lat, logout_lng,
    hours_worked, status, manager_verified, manager_note, manager_verified_at,
    hr_verified, hr_note, hr_verified_at, date
) VALUES (
    %(employee_id)s, %(site_id)s, %(login_time)s, %(logout_time)s,
    %(login_lat)s, %(login_lng)s, %(logout_lat)s, %(logout_lng)s,
    %(hours_worked)s, %(status)s, %(manager_verified)s, %(manager_note)s, %(manager_verified_at)s,
    %(hr_verified)s, %(hr_note)s, %(hr_verified_at)s, %(date)s
)
"""

def ensure_user(cur, eid, name, role, pw, phone, dept, site_id=None):
    cur.execute("SELECT 1 FROM users WHERE employee_id=%s", (eid,))
    if cur.fetchone():
        return False
    if site_id:
        cur.execute(
            "INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (eid, name, role, pw_hash(pw), phone, dept, site_id))
    else:
        cur.execute(
            "INSERT INTO users (employee_id,name,role,password_hash,phone,department) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (eid, name, role, pw_hash(pw), phone, dept))
    return True


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set — add to .env or export it first.")

    rng = random.Random(42)   # fixed seed → same names/patterns every run
    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # ── 1. wipe old DEMO data ────────────────────────────────────────
            cur.execute(
                "DELETE FROM attendance WHERE employee_id LIKE 'DEMO%' "
                "AND date BETWEEN %s AND %s",
                (JUNE_START, date(2025, 7, 31))
            )
            print(f"  Cleared {cur.rowcount} old DEMO attendance rows")
            cur.execute("DELETE FROM users WHERE employee_id LIKE 'DEMO%'")
            print(f"  Cleared {cur.rowcount} old DEMO users")

            # ── 2. ensure sites ──────────────────────────────────────────────
            cur.execute("SELECT id, name FROM sites ORDER BY id LIMIT 2")
            sites = cur.fetchall()
            if not sites:
                cur.execute(
                    "INSERT INTO sites (name,address,latitude,longitude,radius_meters) "
                    "VALUES ('Main Construction Site','Vijayanagar',23.588,58.382,1000000000),"
                    "       ('Warehouse Zone A','Industrial Area',23.590,58.385,1000000000) "
                    "RETURNING id, name"
                )
                sites = cur.fetchall()
            site1 = sites[0]["id"]
            site2 = sites[1]["id"] if len(sites) > 1 else site1

            # ── 3. ensure managers and HR ────────────────────────────────────
            for eid, name, role, pw, phone, dept, sid in [
                ("HR001",  "Priya Sharma", "hr",      "hr123",  "9876543210", "Human Resources", None),
                ("MGR001", "Rajesh Kumar", "manager", "mgr123", "9876543211", "Operations",      site1),
                ("MGR002", "Suresh Nair",  "manager", "mgr456", "9876543212", "Operations",      site2),
            ]:
                created = ensure_user(cur, eid, name, role, pw, phone, dept, sid)
                print(f"  {'Created' if created else 'Exists '} {role}: {eid} / {name}")

            # ── 4. story employees (DEMO001–008) ─────────────────────────────
            all_att_rows = []
            for idx, ((eid, name, phone, dept), builder) in enumerate(
                zip(STORY_USERS, STORY_BUILDERS), start=1
            ):
                sid = site1 if idx <= 4 else site2
                cur.execute(
                    "INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) "
                    "VALUES (%s,%s,'labour',%s,%s,%s,%s)",
                    (eid, name, pw_hash("demo123"), phone, dept, sid)
                )
                all_att_rows.extend(builder(eid, sid, rng))

            print(f"  Created 8 story employees (DEMO001–008)")

            # ── 5. bulk employees (DEMO009–100) ──────────────────────────────
            bulk_patterns_used = {}
            for idx in range(9, 101):
                eid   = f"DEMO{idx:03d}"
                name  = make_name(rng)
                phone = f"971{idx:06d}"
                dept  = rng.choice(DEPARTMENTS)
                trade = rng.choice(TRADES)
                sid   = site1 if idx % 2 == 0 else site2
                cur.execute(
                    "INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) "
                    "VALUES (%s,%s,'labour',%s,%s,%s,%s)",
                    (eid, name, pw_hash("demo123"), phone, dept, sid)
                )
                pattern_fn = pick_pattern(rng)
                bulk_patterns_used[pattern_fn.__name__] = bulk_patterns_used.get(pattern_fn.__name__, 0) + 1
                all_att_rows.extend(pattern_fn(eid, sid, rng))

            print(f"  Created 92 bulk employees (DEMO009–100)")
            print(f"  Bulk pattern distribution: {bulk_patterns_used}")

            # ── 6. insert all attendance in one batch ─────────────────────────
            for row in all_att_rows:
                cur.execute(INSERT_ATT, row)
            print(f"  Inserted {len(all_att_rows)} total attendance rows")

        conn.commit()

        print()
        print("=" * 65)
        print("DONE — 100 demo employees seeded for July 2025")
        print("=" * 65)
        print()
        print("STORY EMPLOYEES (demo123 password for all):")
        print("  DEMO001  Ali Hassan         — Burnout spike (14h+ from Jul 14)")
        print("  DEMO002  Priya Mehta        — Gradual creep (hours climbing all July)")
        print("  DEMO003  Rashid Al Balushi  — Chronic lateness (late from Jul 13)")
        print("  DEMO004  Sunita Rao         — Overtime risk (11-12h days)")
        print("  DEMO005  Kevin Fernandes    — Clean/normal (no flags — contrast)")
        print("  DEMO006  Tariq Ahmed        — Rejection pattern (high reject rate)")
        print("  DEMO007  Deepa Nair         — Low data (4 days, graceful fallback)")
        print("  DEMO008  Suresh Pillai      — Early departure streak (leaves ~3pm)")
        print()
        print("STAFF LOGINS:")
        print("  HR001  / hr123    — HR dashboard")
        print("  MGR001 / mgr123   — Manager dashboard (Site 1)")
        print("  MGR002 / mgr456   — Manager dashboard (Site 2)")
        print()
        print("BULK employees DEMO009–100 have realistic mixed patterns.")
        print("All passwords: demo123")

    except Exception as e:
        conn.rollback()
        raise SystemExit(f"Seed FAILED (rolled back): {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
