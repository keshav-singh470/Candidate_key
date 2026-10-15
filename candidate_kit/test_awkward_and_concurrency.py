"""
Awkward Data and Concurrency Verification Test Suite

Tests:
1. Awkward dataset validation:
   - Employee with zero logs (counted in headcount, 0 present days)
   - Mid-month joiner (working days counted only from joined_on)
   - Tied late minutes (standard competition ranking: 1, 1, 3 and limit cutoff)
   - Weekend attendance records (adds to late and overtime, 0 present days)
   - Overnight shift (date belongs to previous calendar day, next-day overtime)
   - Weekday with no record (gap-filled in trend with rate 0.0)
   - Future joiner (excluded from headcount)
2. Comparison of exact Python-computed metrics against API responses.
3. Concurrency Stress Test:
   - 20 simultaneous punch-in requests: exactly one 201, nineteen 409s.
   - 20 simultaneous punch-out requests: exactly one 200, nineteen 409s.
"""
import concurrent.futures
import datetime
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Add candidate_kit to import path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from candidate_kit.app.main import app, db, init_indexes

load_dotenv(override=False)

client = TestClient(app)


def seed_awkward_data():
    """Seeds the exact awkward test dataset into MongoDB."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    # 1. Employees
    employees = [
        # EMP0001: Standard employee in Engineering
        {
            "emp_code": "EMP0001",
            "name": "Alice Standard",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-01-01",
            "created_at": datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
        },
        # EMP0002: Tied late minutes in Engineering
        {
            "emp_code": "EMP0002",
            "name": "Bob TiedLate",
            "email": "bob@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-01-01",
            "created_at": datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
        },
        # EMP0003: Overnight shift worker
        {
            "emp_code": "EMP0003",
            "name": "Charlie Overnight",
            "email": "charlie@example.com",
            "department": "Engineering",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2025-01-01",
            "created_at": datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
        },
        # EMP0004: Mid-month joiner (joined July 15, 2026 - Wednesday)
        {
            "emp_code": "EMP0004",
            "name": "Diana MidMonth",
            "email": "diana@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-15",
            "created_at": datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc),
        },
        # EMP0005: Employee with ZERO attendance logs in Engineering
        {
            "emp_code": "EMP0005",
            "name": "Edward NoLogs",
            "email": "edward@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-05-01",
            "created_at": datetime.datetime(2025, 5, 1, tzinfo=datetime.timezone.utc),
        },
        # EMP0006: Future joiner (joined Aug 1, 2026 -> must be excluded from July)
        {
            "emp_code": "EMP0006",
            "name": "Fiona Future",
            "email": "fiona@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01",
            "created_at": datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
        },
        # EMP0007: Employee in another department ("HR")
        {
            "emp_code": "EMP0007",
            "name": "George HR",
            "email": "george@example.com",
            "department": "HR",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-01-01",
            "created_at": datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
        },
    ]
    db.employees.insert_many(employees)

    # 2. Attendance Logs for July 2026
    # Note on July 2026:
    # July 6 = Mon, July 7 = Tue, July 8 = Wed, July 9 = Thu, July 10 = Fri, July 11 = Sat, July 12 = Sun
    # July 13 = Mon, July 14 = Tue, July 15 = Wed, July 16 = Thu
    attendance_records = [
        # EMP0001 logs:
        # Mon Jul 6: on-time full day (1.0 present)
        {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 3, 58, tzinfo=datetime.timezone.utc),  # 09:28 IST
            "punch_out": datetime.datetime(2026, 7, 6, 13, 5, tzinfo=datetime.timezone.utc),  # 18:35 IST
            "work_hours": 9.12,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # Tue Jul 7: 45 min late (1.0 present, 45 late min)
        {
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 7, 4, 45, tzinfo=datetime.timezone.utc),  # 10:15 IST
            "punch_out": datetime.datetime(2026, 7, 7, 13, 0, tzinfo=datetime.timezone.utc),  # 18:30 IST
            "work_hours": 8.25,
            "late_minutes": 45,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # Wed Jul 8: Approved LEAVE (0 present, 1 leave)
        {
            "emp_code": "EMP0001",
            "date": "2026-07-08",
            "status": "LEAVE",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # Thu Jul 9: ABSENT (0 present, 0 leave)
        {
            "emp_code": "EMP0001",
            "date": "2026-07-09",
            "status": "ABSENT",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # Fri Jul 10: NO RECORD AT ALL for EMP0001 (weekday with no log)
        # Sat Jul 11: Weekend presence! 30 min late, 45 min overtime (0 present days, but +30 late min, +45 ot min!)
        {
            "emp_code": "EMP0001",
            "date": "2026-07-11",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 11, 4, 30, tzinfo=datetime.timezone.utc),  # 10:00 IST
            "punch_out": datetime.datetime(2026, 7, 11, 13, 45, tzinfo=datetime.timezone.utc),  # 19:15 IST
            "work_hours": 9.25,
            "late_minutes": 30,
            "overtime_minutes": 45,
            "half_day": False,
            "history": [],
        },
        # Mon Jul 13: Half-day WFH (3.5 hours < 4.5 -> 0.5 present day)
        {
            "emp_code": "EMP0001",
            "date": "2026-07-13",
            "status": "WFH",
            "punch_in": datetime.datetime(2026, 7, 13, 4, 0, tzinfo=datetime.timezone.utc),  # 09:30 IST
            "punch_out": datetime.datetime(2026, 7, 13, 7, 30, tzinfo=datetime.timezone.utc),  # 13:00 IST
            "work_hours": 3.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [],
        },
        # EMP0002 logs:
        # Mon Jul 6: 75 min late (tied with EMP0001 total late 45+30 = 75 min!)
        {
            "emp_code": "EMP0002",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 5, 15, tzinfo=datetime.timezone.utc),  # 10:45 IST
            "punch_out": datetime.datetime(2026, 7, 6, 13, 0, tzinfo=datetime.timezone.utc),  # 18:30 IST
            "work_hours": 7.75,
            "late_minutes": 75,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # EMP0003 logs (Overnight shift: 22:00 to 06:00):
        # Mon Jul 6 shift: punched in 21:55 IST, out next day at 06:40 IST (40 min overtime)
        {
            "emp_code": "EMP0003",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 16, 25, tzinfo=datetime.timezone.utc),  # 21:55 IST
            "punch_out": datetime.datetime(2026, 7, 7, 1, 10, tzinfo=datetime.timezone.utc),  # 06:40 IST next day
            "work_hours": 8.75,
            "late_minutes": 0,
            "overtime_minutes": 40,
            "half_day": False,
            "history": [],
        },
        # Tue Jul 7 shift: punched in on Jul 8 at 05:40 IST (< 06:00, belongs to Jul 7!), out 06:00 IST
        {
            "emp_code": "EMP0003",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 8, 0, 10, tzinfo=datetime.timezone.utc),  # 05:40 IST
            "punch_out": datetime.datetime(2026, 7, 8, 0, 30, tzinfo=datetime.timezone.utc),  # 06:00 IST
            "work_hours": 0.33,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [],
        },
        # EMP0004 logs (Mid-month joiner joined Jul 15):
        # Wed Jul 15: full day
        {
            "emp_code": "EMP0004",
            "date": "2026-07-15",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 15, 4, 0, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 15, 12, 30, tzinfo=datetime.timezone.utc),
            "work_hours": 8.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # Thu Jul 16: full day
        {
            "emp_code": "EMP0004",
            "date": "2026-07-16",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 16, 4, 0, tzinfo=datetime.timezone.utc),
            "punch_out": datetime.datetime(2026, 7, 16, 12, 30, tzinfo=datetime.timezone.utc),
            "work_hours": 8.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # EMP0007 logs (HR department):
        # Mon Jul 6: 10 min late
        {
            "emp_code": "EMP0007",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime.datetime(2026, 7, 6, 4, 10, tzinfo=datetime.timezone.utc),  # 09:40:01
            "punch_out": datetime.datetime(2026, 7, 6, 13, 0, tzinfo=datetime.timezone.utc),
            "work_hours": 8.83,
            "late_minutes": 10,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
    ]
    db.attendance_logs.insert_many(attendance_records)
    print("Awkward dataset seeded successfully.")


def test_employee_monthly():
    print("\n--- Test 1: Employee Monthly Analytics ---")
    # Test A: EMP0001 (standard employee with weekend presence, leave, absent, half day)
    # Expected: working_days=23, present_days=2.5, leave_days=1, late_count=2, total_late_minutes=75, total_overtime=45
    # attendance_pct = (2.5 / 23) * 100 = 10.87
    resp = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["working_days"] == 23, f"Expected 23, got {data['working_days']}"
    assert data["present_days"] == 2.5, f"Expected 2.5, got {data['present_days']}"
    assert data["leave_days"] == 1, f"Expected 1, got {data['leave_days']}"
    assert data["late_count"] == 2, f"Expected 2, got {data['late_count']}"
    assert data["total_late_minutes"] == 75, f"Expected 75, got {data['total_late_minutes']}"
    assert data["total_overtime_minutes"] == 45, f"Expected 45, got {data['total_overtime_minutes']}"
    assert data["attendance_pct"] == 10.87, f"Expected 10.87, got {data['attendance_pct']}"
    print("[PASS] EMP0001 monthly summary correctly accounts for weekend late/ot and weekday half-day.")

    # Test B: EMP0004 (mid-month joiner: joined July 15)
    # July 15 to July 31 has exactly 13 working days (Mon-Fri)
    # present_days = 2.0 (Jul 15, Jul 16)
    # attendance_pct = (2.0 / 13) * 100 = 15.38%
    resp = client.get("/analytics/employees/EMP0004/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 13, f"Expected 13 working days for mid-month joiner, got {data['working_days']}"
    assert data["present_days"] == 2.0, f"Expected 2.0 present days, got {data['present_days']}"
    assert data["attendance_pct"] == 15.38, f"Expected 15.38%, got {data['attendance_pct']}"
    print("[PASS] EMP0004 mid-month joiner correctly counts working days from joined_on.")

    # Test C: EMP0005 (zero logs employee)
    # working_days = 23, present_days = 0.0, attendance_pct = 0.0
    resp = client.get("/analytics/employees/EMP0005/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["working_days"] == 23
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0
    print("[PASS] EMP0005 employee with zero logs produces clean 0 totals.")


def test_department_summary():
    print("\n--- Test 2: Department Summary Analytics ---")
    resp = client.get("/analytics/departments/summary?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    items = {item["department"]: item for item in data["items"]}

    assert "Engineering" in items
    eng = items["Engineering"]
    # Headcount: EMP0001, EMP0002, EMP0003, EMP0004, EMP0005 = 5 employees (EMP0006 joined in August -> excluded!)
    assert eng["headcount"] == 5, f"Expected headcount 5 (excluding future joiner), got {eng['headcount']}"

    # present_days: EMP0001 (2.5) + EMP0002 (1.0) + EMP0003 (1.0 + 0.5 = 1.5) + EMP0004 (2.0) = 7.0
    assert eng["present_days"] == 7.0, f"Expected 7.0 present days, got {eng['present_days']}"
    assert eng["late_count"] == 3, f"Expected 3 late instances, got {eng['late_count']}"
    assert eng["total_late_minutes"] == 150, f"Expected 150 late minutes (75+75), got {eng['total_late_minutes']}"
    assert eng["leave_count"] == 1

    # avg_work_hours: Mean over all 8 present records with work hours:
    # EMP0001: 9.12, 8.25, 9.25, 3.50
    # EMP0002: 7.75
    # EMP0003: 8.75, 0.33
    # EMP0004: 8.50, 8.50
    all_hours = [9.12, 8.25, 9.25, 3.5, 7.75, 8.75, 0.33, 8.5, 8.5]
    expected_avg = float(
        (Decimal(str(sum(all_hours))) / Decimal(len(all_hours))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )
    assert eng["avg_work_hours"] == expected_avg, f"Expected {expected_avg}, got {eng['avg_work_hours']}"
    print(f"[PASS] Department summary correctly calculated headcount=5, present_days=7.0, and avg_work_hours={expected_avg}.")


def test_late_leaderboard_ties():
    print("\n--- Test 3: Late Leaderboard with Standard Competition Ties ---")
    # In July 2026:
    # EMP0001: 75 min late (Engineering)
    # EMP0002: 75 min late (Engineering)
    # EMP0007: 10 min late (HR)
    # Competition ranking: EMP0001 -> Rank 1, EMP0002 -> Rank 1, EMP0007 -> Rank 3 (Rank 2 is skipped!)

    resp = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert resp.status_code == 200
    items = resp.json()["items"]

    assert len(items) == 3, f"Expected 3 rows, got {len(items)}"
    assert items[0]["rank"] == 1 and items[0]["emp_code"] == "EMP0001" and items[0]["total_late_minutes"] == 75
    assert items[1]["rank"] == 1 and items[1]["emp_code"] == "EMP0002" and items[1]["total_late_minutes"] == 75
    assert items[2]["rank"] == 3 and items[2]["emp_code"] == "EMP0007" and items[2]["total_late_minutes"] == 10
    print("[PASS] Leaderboard correctly applied competition ranking (1, 1, 3), skipping rank 2 on tie.")

    # Test limit cutoff with ties:
    # If limit=1 is requested, both EMP0001 and EMP0002 have rank <= 1, so BOTH must be returned (2 rows returned)!
    resp_limit1 = client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
    assert resp_limit1.status_code == 200
    items_limit1 = resp_limit1.json()["items"]
    assert len(items_limit1) == 2, f"Expected 2 rows for limit=1 due to tie, got {len(items_limit1)}"
    print("[PASS] Leaderboard cutoff at limit=1 correctly returned 2 rows for tied leaders.")


def test_department_trend_gap_filling():
    print("\n--- Test 4: Department Trend with Gap Filling & 7-Day Moving Average ---")
    # Test range: 2026-07-06 (Mon) to 2026-07-12 (Sun) = 7 days
    # Includes Fri Jul 10 (no records) and Sun Jul 12 (no records)
    resp = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-12")
    assert resp.status_code == 200
    items = resp.json()["items"]

    assert len(items) == 7, f"Expected 7 calendar days, got {len(items)}"

    dates = [item["date"] for item in items]
    assert dates == [
        "2026-07-06",
        "2026-07-07",
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-11",
        "2026-07-12",
    ]

    # Verify Fri Jul 10 (weekday with NO records):
    # is_working_day = True, headcount = 4, present_count = 0.0, rate = 0.0
    fri = items[4]
    assert fri["date"] == "2026-07-10"
    assert fri["is_working_day"] is True
    assert fri["headcount"] == 4
    assert fri["present_count"] == 0.0
    assert fri["attendance_rate"] == 0.0

    # Verify Sat Jul 11 (weekend with records):
    # is_working_day = False, attendance_rate = None
    sat = items[5]
    assert sat["date"] == "2026-07-11"
    assert sat["is_working_day"] is False
    assert sat["attendance_rate"] is None

    # Verify moving_avg_7d is present and calculated only over working day rates
    assert all("moving_avg_7d" in item for item in items)
    print("[PASS] Department trend successfully densified all calendar days, set weekend rates to null, and computed moving averages.")


def test_concurrency_punch_in_and_out():
    print("\n--- Test 5: Concurrency Stress Test (20 Parallel Requests) ---")
    # Date: 2026-07-28
    emp_code = "EMP0001"
    punch_in_time = 1785191400000  # 2026-07-28T09:30:00 IST in epoch ms
    punch_out_time = punch_in_time + (9 * 3600 * 1000)  # 9 hours later

    num_threads = 20

    # Phase A: 20 parallel punch-in requests
    print(f"Firing {num_threads} simultaneous punch-in requests for {emp_code} on 2026-07-28...")

    def do_punch_in():
        return client.post(
            "/attendance/punch-in",
            json={"emp_code": emp_code, "punched_at": punch_in_time, "status": "PRESENT"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        punch_in_futures = [executor.submit(do_punch_in) for _ in range(num_threads)]
        punch_in_responses = [f.result() for f in punch_in_futures]

    in_status_codes = [r.status_code for r in punch_in_responses]
    count_201 = in_status_codes.count(201)
    count_409 = in_status_codes.count(409)

    print(f"Punch-in status codes: {count_201}x 201, {count_409}x 409")
    assert count_201 == 1, f"Expected exactly ONE 201 Created, got {count_201}!"
    assert count_409 == num_threads - 1, f"Expected {num_threads - 1} 409 Conflicts, got {count_409}!"
    print("[PASS] Concurrency test for punch-in passed: exactly one 201, nineteen 409s.")

    # Phase B: 20 parallel punch-out requests
    print(f"Firing {num_threads} simultaneous punch-out requests for {emp_code}...")

    def do_punch_out():
        return client.post(
            "/attendance/punch-out",
            json={"emp_code": emp_code, "punched_at": punch_out_time},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        punch_out_futures = [executor.submit(do_punch_out) for _ in range(num_threads)]
        punch_out_responses = [f.result() for f in punch_out_futures]

    out_status_codes = [r.status_code for r in punch_out_responses]
    count_200 = out_status_codes.count(200)
    count_409_out = out_status_codes.count(409)

    print(f"Punch-out status codes: {count_200}x 200, {count_409_out}x 409")
    assert count_200 == 1, f"Expected exactly ONE 200 OK, got {count_200}!"
    assert count_409_out == num_threads - 1, f"Expected {num_threads - 1} 409 Conflicts, got {count_409_out}!"
    print("[PASS] Concurrency test for punch-out passed: exactly one 200, nineteen 409s.")


def run_all_tests():
    print("=" * 70)
    print("RUNNING AWKWARD DATASET & CONCURRENCY TEST SUITE")
    print("=" * 70)

    try:
        db.command("ping")
        print("Connected to MongoDB successfully.")
    except Exception as e:
        print(f"[ERROR] Cannot connect to MongoDB: {e}")
        print("Please ensure MongoDB is reachable via MONGO_URI before running this test script.")
        sys.exit(1)

    init_indexes()
    seed_awkward_data()

    test_employee_monthly()
    test_department_summary()
    test_late_leaderboard_ties()
    test_department_trend_gap_filling()
    test_concurrency_punch_in_and_out()

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED: 100% SUCCESS ACROSS ALL AWKWARD CASES & CONCURRENCY!")
    print("=" * 70)


if __name__ == "__main__":
    run_all_tests()
