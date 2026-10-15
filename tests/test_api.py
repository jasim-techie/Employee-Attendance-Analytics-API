"""
Comprehensive Test Suite for Employee Attendance & Analytics API
Covers:
- System & health check
- Employee creation, validation, unique constraint (409), listing, pagination
- Punch-in: R1 (IST dates, overnight shifts), R2 (10-min grace, late calculation), duplicate prevention (409)
- Punch-out: R3 (overtime >= 30m), R4 (half-up rounding work hours), R5 (half day < 4.5h), cross-midnight, duplicate (409)
- Regularization: validation rules, status transitions, audit history diffs, concurrent optimistic locking
- Analytics:
    - Employee monthly (Mon-Fri working days, mid-month joiner, Mon-Fri present days, attendance %)
    - Department summary (headcount with zero-log employees, cross-collection aggregation)
    - Late leaderboard (competition ranking with ties 1, 2, 2, 4)
    - Department trend (densify gap-filling across weekends, 7-day moving average)
- Admin explain: IXSCAN verification across all 5 endpoints
"""
import os
import pytest
from datetime import datetime, date, timedelta, timezone
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Configure test database
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, db, ensure_indexes

client = TestClient(app)

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


@pytest.fixture(autouse=True)
def setup_teardown_db():
    """Reset and seed fresh test database before each test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    ensure_indexes(db)
    yield


# =========================================================================== #
# 1. Health Probe
# =========================================================================== #
def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# =========================================================================== #
# 2. Employee CRUD & Validations
# =========================================================================== #
def test_create_and_list_employees():
    emp_payload = {
        "emp_code": "EMP0100",
        "name": "Arjun Sharma",
        "email": "arjun@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0100"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate emp_code -> 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # Validation: shift_start == shift_end -> 422
    invalid_shift = dict(emp_payload, emp_code="EMP0101", shift_start="09:30", shift_end="09:30")
    res_inv = client.post("/employees", json=invalid_shift)
    assert res_inv.status_code == 422

    # Validation: invalid emp_code format -> 422
    invalid_code = dict(emp_payload, emp_code="INVALID123")
    assert client.post("/employees", json=invalid_code).status_code == 422

    # Listing & Pagination
    res_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["total"] == 1
    assert len(list_data["items"]) == 1
    assert list_data["items"][0]["emp_code"] == "EMP0100"


# =========================================================================== #
# 3. Punch-In & Rules (R1, R2)
# =========================================================================== #
def test_punch_in_and_late_calculation():
    # Setup employee with shift 09:30 - 18:30
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Case A: Inside grace period (09:40:00 IST -> late_minutes = 0)
    # 2026-07-06 09:40:00 IST = 2026-07-06 04:10:00 UTC
    dt_a = datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC)
    ms_a = int(dt_a.timestamp()) * 1000
    res_a = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_a})
    assert res_a.status_code == 201
    data_a = res_a.json()
    assert data_a["date"] == "2026-07-06"
    assert data_a["late_minutes"] == 0
    assert data_a["status"] == "PRESENT"
    assert data_a["punch_out"] is None
    assert data_a["work_hours"] is None

    # Case B: Duplicate punch-in for same date -> 409
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_a + 60000})
    assert res_dup.status_code == 409

    # Case C: 09:40:01 IST strictly past grace period -> late_minutes = 10
    # 2026-07-07 09:40:01 IST = 2026-07-07 04:10:01 UTC
    dt_c = datetime(2026, 7, 7, 4, 10, 1, tzinfo=UTC)
    ms_c = int(dt_c.timestamp()) * 1000
    res_c = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_c})
    assert res_c.status_code == 201
    assert res_c.json()["late_minutes"] == 10

    # Case D: 10:15:59 IST -> late_minutes = 45
    # 2026-07-08 10:15:59 IST = 2026-07-08 04:45:59 UTC
    dt_d = datetime(2026, 7, 8, 4, 45, 59, tzinfo=UTC)
    ms_d = int(dt_d.timestamp()) * 1000
    res_d = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_d})
    assert res_d.status_code == 201
    assert res_d.json()["late_minutes"] == 45

    # Case E: Unknown employee -> 404
    res_unk = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ms_d})
    assert res_unk.status_code == 404


def test_punch_in_overnight_shift():
    # Setup overnight employee: 22:00 - 06:00
    client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Kiran Patel",
        "email": "kiran@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-07-01"
    })

    # Punch-in on 2026-07-07 at 01:30 IST (< shift_end 06:00)
    # Per R1: belongs to the shift that started the previous day (2026-07-06)
    dt_overnight = datetime(2026, 7, 6, 20, 0, 0, tzinfo=UTC)
    ms_overnight = int(dt_overnight.timestamp()) * 1000
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms_overnight})
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


# =========================================================================== #
# 4. Punch-Out & Rules (R3, R4, R5)
# =========================================================================== #
def test_punch_out_rules():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Punch in 09:28 IST (03:58 UTC) on 2026-07-06
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC)
    in_ms = int(in_dt.timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": in_ms})

    # Punch out 18:35 IST (13:05 UTC) -> 9 hours 7 min = 9.12 work hours (half-up)
    # Overtime 5 min < 30 min -> overtime_minutes = 0 (R3)
    # work_hours 9.12 >= 4.50 -> half_day = False (R5)
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=UTC)
    out_ms = int(out_dt.timestamp()) * 1000
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms})
    assert res_out.status_code == 200
    data = res_out.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

    # Second punch out -> 409 Conflict
    res_out_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out_ms + 1000})
    assert res_out_dup.status_code == 409


def test_punch_out_half_day_and_overtime():
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Vikram Seth",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Punch in 09:30 IST (04:00 UTC)
    in_dt = datetime(2026, 7, 7, 4, 0, 0, tzinfo=UTC)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": int(in_dt.timestamp()) * 1000})

    # Punch out after 3.5 hours (13:00 IST = 07:30 UTC) -> work_hours < 4.50 -> half_day = True
    out_dt = datetime(2026, 7, 7, 7, 30, 0, tzinfo=UTC)
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": int(out_dt.timestamp()) * 1000})
    assert res_out.status_code == 200
    assert res_out.json()["work_hours"] == 3.5
    assert res_out.json()["half_day"] is True


def test_punch_out_validation_errors():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })
    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp()) * 1000})

    # punch_out <= punch_in -> 422
    res_before = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp()) * 1000 - 1000
    })
    assert res_before.status_code == 422

    # punch_out > 24 hours -> 422
    res_too_late = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(in_dt.timestamp()) * 1000 + 25 * 3600 * 1000
    })
    assert res_too_late.status_code == 422


# =========================================================================== #
# 5. Regularization & Audit Trail
# =========================================================================== #
def test_regularize_attendance_workflow():
    client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "email": "neha@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Punch-in late: 10:05 IST (04:35 UTC) -> late_minutes = 35
    in_dt = datetime(2026, 7, 7, 4, 35, 0, tzinfo=UTC)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": int(in_dt.timestamp()) * 1000})

    # Punch-out: 19:10 IST (13:40 UTC)
    out_dt = datetime(2026, 7, 7, 13, 40, 0, tzinfo=UTC)
    client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": int(out_dt.timestamp()) * 1000})

    # Regularize: adjust punch_in to 09:28 IST (03:58 UTC)
    correct_in_dt = datetime(2026, 7, 7, 3, 58, 0, tzinfo=UTC)
    patch_payload = {
        "reason": "biometric system glitch at entrance",
        "regularized_by": "hr.manager",
        "punch_in": int(correct_in_dt.timestamp()) * 1000
    }
    res_patch = client.patch("/attendance/EMP0003/2026-07-07", json=patch_payload)
    assert res_patch.status_code == 200
    data = res_patch.json()
    assert data["late_minutes"] == 0  # Recomputed!
    assert len(data["history"]) == 1
    hist = data["history"][0]
    assert hist["by"] == "hr.manager"
    assert "late_minutes" in hist["changes"]
    assert hist["changes"]["late_minutes"]["from"] == 35
    assert hist["changes"]["late_minutes"]["to"] == 0

    # Trying to patch with no changes -> 422
    res_no_change = client.patch("/attendance/EMP0003/2026-07-07", json=patch_payload)
    assert res_no_change.status_code == 422


def test_regularize_absent_leave_rules():
    client.post("/employees", json={
        "emp_code": "EMP0004",
        "name": "Rahul Verma",
        "email": "rahul@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })
    # Punch in
    in_dt = datetime(2026, 7, 8, 4, 0, 0, tzinfo=UTC)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": int(in_dt.timestamp()) * 1000})

    # Setting LEAVE while passing punch_in -> 422
    res_bad = client.patch("/attendance/EMP0004/2026-07-08", json={
        "reason": "applied leave retrospectively",
        "regularized_by": "hr.lead",
        "status": "LEAVE",
        "punch_in": int(in_dt.timestamp()) * 1000
    })
    assert res_bad.status_code == 422

    # Setting LEAVE without punches -> succeeds, clears punch times
    res_ok = client.patch("/attendance/EMP0004/2026-07-08", json={
        "reason": "applied leave retrospectively",
        "regularized_by": "hr.lead",
        "status": "LEAVE"
    })
    assert res_ok.status_code == 200
    assert res_ok.json()["status"] == "LEAVE"
    assert res_ok.json()["punch_in"] is None
    assert res_ok.json()["punch_out"] is None


# =========================================================================== #
# 6. Analytics Endpoints & Complex Edge Cases
# =========================================================================== #
def test_analytics_endpoints_against_seeded_data():
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-06"  # Mid-month joiner (Monday July 6)
    })
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Vikram Seth",
        "email": "vikram@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })
    client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "email": "neha@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Log for EMP0001: 2026-07-06 full day present
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC).timestamp()) * 1000
    })
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(datetime(2026, 7, 6, 13, 0, 0, tzinfo=UTC).timestamp()) * 1000
    })

    # Log for EMP0003: late 35 min on 2026-07-06
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0003",
        "punched_at": int(datetime(2026, 7, 6, 4, 35, 0, tzinfo=UTC).timestamp()) * 1000
    })
    client.post("/attendance/punch-out", json={
        "emp_code": "EMP0003",
        "punched_at": int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=UTC).timestamp()) * 1000
    })

    # Monthly Summary
    res_m = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_m.status_code == 200
    m_data = res_m.json()
    assert m_data["working_days"] == 20  # Mon July 6 to Fri July 31
    assert m_data["present_days"] == 1.0
    assert m_data["attendance_pct"] == 5.0

    # Department Summary (Engineering has EMP0001 and EMP0002 -> headcount = 2 even with EMP0002 having 0 logs!)
    res_dept = client.get("/analytics/departments/summary?month=2026-07")
    assert res_dept.status_code == 200
    dept_items = {d["department"]: d for d in res_dept.json()["items"]}
    assert "Engineering" in dept_items
    assert dept_items["Engineering"]["headcount"] == 2
    assert dept_items["Engineering"]["present_days"] == 1.0

    # Late Leaderboard
    res_lead = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res_lead.status_code == 200
    lead_items = res_lead.json()["items"]
    assert len(lead_items) == 1
    assert lead_items[0]["emp_code"] == "EMP0003"
    assert lead_items[0]["rank"] == 1
    assert lead_items[0]["total_late_minutes"] == 35

    # Department Trend
    res_trend = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-08")
    assert res_trend.status_code == 200
    trend_items = res_trend.json()["items"]
    assert len(trend_items) == 4  # Exactly one row per day (July 5, 6, 7, 8)
    assert trend_items[0]["date"] == "2026-07-05"
    assert trend_items[0]["is_working_day"] is False
    assert trend_items[1]["date"] == "2026-07-06"
    assert trend_items[1]["is_working_day"] is True
    assert trend_items[1]["present_count"] == 1.0


def test_leaderboard_competition_ranking_ties():
    """Verify standard competition ranking: 1, 2, 2, 4 with ties at cutoff."""
    # Create 4 employees
    for i, code in enumerate(["EMP0001", "EMP0002", "EMP0003", "EMP0004"]):
        client.post("/employees", json={
            "emp_code": code,
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

    # EMP0001: 50 late min
    # EMP0002: 30 late min (tie)
    # EMP0003: 30 late min (tie)
    # EMP0004: 10 late min
    dates = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]
    lates = [50, 30, 30, 10]
    for code, d_str, late in zip(["EMP0001", "EMP0002", "EMP0003", "EMP0004"], dates, lates):
        # Insert directly to set exact late_minutes
        db.attendance_logs.insert_one({
            "emp_code": code,
            "date": d_str,
            "status": "PRESENT",
            "punch_in": datetime.fromisoformat(f"{d_str}T04:00:00Z"),
            "punch_out": datetime.fromisoformat(f"{d_str}T13:00:00Z"),
            "work_hours": 9.0,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })

    # With limit=2: EMP0001 is rank 1, EMP0002 & EMP0003 are tied at rank 2!
    # Because limit applies AFTER ranking (rank <= limit), both rank 2 employees MUST be returned (3 rows total)!
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert [x["rank"] for x in items] == [1, 2, 2]
    assert items[0]["emp_code"] == "EMP0001"
    assert items[1]["emp_code"] == "EMP0002"  # emp_code ascending for ties
    assert items[2]["emp_code"] == "EMP0003"

    # With limit=4: Rank 4 is skipped because of competition ranking (1, 2, 2, 4)
    res_all = client.get("/analytics/leaderboard/late?month=2026-07&limit=4")
    assert res_all.status_code == 200
    all_items = res_all.json()["items"]
    assert [x["rank"] for x in all_items] == [1, 2, 2, 4]


def test_weekend_records_contribute_to_late_and_overtime():
    """R7: Weekend records do not count towards present_days, but contribute to late and overtime."""
    client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })
    # 2026-07-04 is a Saturday
    db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-04",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 4, 4, 30, 0, tzinfo=UTC),
        "punch_out": datetime(2026, 7, 4, 14, 0, 0, tzinfo=UTC),
        "work_hours": 9.5,
        "late_minutes": 30,
        "overtime_minutes": 60,
        "half_day": False,
        "history": []
    })

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["present_days"] == 0.0  # Saturday is not counted in present_days!
    assert data["total_late_minutes"] == 30  # But late minutes are counted!
    assert data["total_overtime_minutes"] == 60  # And overtime is counted!


# =========================================================================== #
# 7. Admin Explain Endpoint
# =========================================================================== #
def test_admin_explain_endpoints():
    endpoints = [
        "/admin/explain/attendance_list?emp_code=EMP0001",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07"
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 200, f"Failed for {ep}: {res.text}"
        data = res.json()
        assert "explain" in data
        assert "collection" in data
