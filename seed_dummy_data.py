import argparse
import os
import random
import hashlib
from datetime import datetime, timedelta, time

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv


load_dotenv()


FIRST_NAMES = [
    "Aakash", "Aamir", "Abdullah", "Adeel", "Ahmed", "Akash", "Aman", "Anand", "Anil", "Arjun",
    "Deepak", "Imran", "Irfan", "Javed", "Khalid", "Madhan", "Mohammed", "Naveen", "Prakash", "Praveen",
    "Rahul", "Rajesh", "Ramesh", "Rehman", "Sanjay", "Suresh", "Syed", "Tariq", "Vijay", "Vimal",
]

LAST_NAMES = [
    "Ahmed", "Al Balushi", "Al Harthy", "Al Habsi", "Al Mahrouqi", "Al Rawahi", "Das", "Fernandes", "Iyer",
    "Kumar", "Nair", "Pillai", "Rao", "Sharma", "Singh", "Yadav", "Dasgupta", "Shetty", "Chandra", "Prasad",
]

TRADE_ROLES = [
    "Helper", "Steel Fixer", "Electrician", "Carpenter", "Mason", "Pipe Fitter", "Welder", "Painter",
    "Driver", "Lab Technician", "Forklift Operator", "Supervisor Assistant", "Survey Assistant", "Cleaner",
]


def db_hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def choose_name(rng: random.Random, index: int) -> str:
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    last = LAST_NAMES[rng.randrange(len(LAST_NAMES))]
    return f"{first} {last}"


def ensure_sites(cursor):
    cursor.execute("SELECT id, name FROM sites ORDER BY id")
    sites = cursor.fetchall()
    if sites:
        return sites

    seed_sites = [
        ("Main Construction Site", "Vijayanagar, Bangalore", 12.618852, 75.249893, 1000000000),
        ("Warehouse Zone A", "56 Industrial Area, Muscat", 12.618852, 75.249893, 1000000000),
    ]
    for site in seed_sites:
        cursor.execute(
            "INSERT INTO sites (name,address,latitude,longitude,radius_meters) VALUES (%s,%s,%s,%s,%s)",
            site,
        )
    cursor.execute("SELECT id, name FROM sites ORDER BY id")
    return cursor.fetchall()


def ensure_user(cursor, employee_id, name, role, password, phone, department, site_id=None):
    cursor.execute("SELECT 1 FROM users WHERE employee_id=%s", (employee_id,))
    if cursor.fetchone():
        return False

    if site_id is None:
        cursor.execute(
            "INSERT INTO users (employee_id,name,role,password_hash,phone,department) VALUES (%s,%s,%s,%s,%s,%s)",
            (employee_id, name, role, db_hash(password), phone, department),
        )
    else:
        cursor.execute(
            "INSERT INTO users (employee_id,name,role,password_hash,phone,department,site_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (employee_id, name, role, db_hash(password), phone, department, site_id),
        )
    return True


def attendance_exists(cursor, employee_id: str, day: datetime.date) -> bool:
    cursor.execute("SELECT 1 FROM attendance WHERE employee_id=%s AND date=%s LIMIT 1", (employee_id, day))
    return cursor.fetchone() is not None


def insert_attendance(cursor, employee_id: str, site_id: int, day: datetime.date, rng: random.Random):
    if attendance_exists(cursor, employee_id, day):
        return False

    login_hour = rng.randint(6, 9)
    login_minute = rng.choice([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55])
    duration_hours = round(rng.uniform(7.25, 11.5), 2)
    login_dt = datetime.combine(day, time(login_hour, login_minute))
    logout_dt = login_dt + timedelta(hours=duration_hours)

    roll = rng.random()
    if roll < 0.65:
        status = "approved"
        manager_verified = True
        hr_verified = True
    elif roll < 0.82:
        status = "pending_hr"
        manager_verified = True
        hr_verified = False
    elif roll < 0.94:
        status = "pending_manager"
        manager_verified = False
        hr_verified = False
    else:
        status = "rejected"
        manager_verified = False
        hr_verified = False

    cursor.execute(
        """INSERT INTO attendance (
            employee_id, site_id, login_time, logout_time, login_lat, login_lng,
            logout_lat, logout_lng, hours_worked, status, manager_verified,
            manager_note, manager_verified_at, hr_verified, hr_note, hr_verified_at, date
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            employee_id,
            site_id,
            login_dt,
            logout_dt,
            12.618852,
            75.249893,
            12.618852,
            75.249893,
            duration_hours,
            status,
            manager_verified,
            None,
            login_dt if manager_verified else None,
            hr_verified,
            None,
            login_dt if hr_verified else None,
            day,
        ),
    )
    return True


def main():
    parser = argparse.ArgumentParser(description="Seed AttendTrack with dummy employees and attendance rows.")
    parser.add_argument("--employees", type=int, default=100, help="Number of dummy labour employees to create")
    parser.add_argument("--days", type=int, default=7, help="How many recent days of attendance to generate")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for repeatable data")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Export it or put it in .env first.")

    rng = random.Random(args.seed)
    conn = psycopg2.connect(database_url)
    conn.autocommit = False

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            sites = ensure_sites(cursor)
            cursor.execute("SELECT COUNT(*) AS cnt FROM users WHERE employee_id LIKE 'DUM%'")
            existing_dummy = int(cursor.fetchone()["cnt"])

            primary_site_id = sites[0]["id"]
            secondary_site_id = sites[1]["id"] if len(sites) > 1 else sites[0]["id"]

            ensure_user(cursor, "HR001", "Priya Sharma", "hr", "hr123", "9876543210", "Human Resources")
            ensure_user(cursor, "MGR001", "Rajesh Kumar", "manager", "mgr123", "9876543211", "Operations", primary_site_id)
            ensure_user(cursor, "MGR002", "Suresh Nair", "manager", "mgr456", "9876543212", "Operations", secondary_site_id)

            created_users = 0
            for idx in range(1, args.employees + 1):
                employee_id = f"DUM{idx:03d}"
                name = choose_name(rng, idx)
                phone = f"9988{idx:06d}"
                trade = rng.choice(TRADE_ROLES)
                site_id = sites[(idx - 1) % len(sites)]["id"]
                created_users += int(ensure_user(cursor, employee_id, name, "labour", "lab123", phone, trade, site_id))

            created_attendance = 0
            today = datetime.now().date()
            for idx in range(1, args.employees + 1):
                employee_id = f"DUM{idx:03d}"
                site_id = sites[(idx - 1) % len(sites)]["id"]
                for offset in range(args.days):
                    day = today - timedelta(days=offset)
                    created_attendance += int(insert_attendance(cursor, employee_id, site_id, day, rng))

        conn.commit()
        print(f"Seed complete: {created_users} new dummy employees, {created_attendance} attendance rows added.")
        print("Demo login for every dummy labour account: employee_id=DUM001..DUM100, password=lab123")
        print("Existing dummy employees already present:", existing_dummy)
    except Exception as exc:
        conn.rollback()
        raise SystemExit(f"Seed failed: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()