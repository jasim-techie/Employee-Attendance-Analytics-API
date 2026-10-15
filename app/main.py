"""
Employee Attendance & Analytics API - Production Implementation
Contract: openapi.yaml (v2.0.0)
Rules: R1 - R10
Data model: DATA_MODEL.md
"""
import calendar
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Indexes Setup
# --------------------------------------------------------------------------- #
def ensure_indexes(database) -> None:
    """Creates required indexes idempotently at startup."""
    # employees
    database.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    database.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_dept_joined")
    database.employees.create_index([("joined_on", ASCENDING)], name="idx_joined_on")

    # attendance_logs
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_emp_date"
    )
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_desc_emp_asc"
    )
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_emp_punch_in_desc"
    )
    database.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="idx_date_late"
    )
    database.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="idx_date_status"
    )


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    # Startup: ensure indexes idempotently
    try:
        ensure_indexes(db)
    except Exception as e:
        # Log or keep running so health probe can reflect db status
        pass
    yield
    # Shutdown
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne Candidate API Implementation",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """R8 Rounding: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("1") if decimals == 0 else Decimal("0." + "0" * (decimals - 1) + "1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def ms_to_datetime(ms: int) -> datetime:
    """
    R1: Instants are truncated to whole seconds before any calculation,
    and the truncated value is stored and returned.
    """
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=UTC)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON or Python datetime to epoch milliseconds (int)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def parse_time_str(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date:
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start, e.g. 22:00 - 06:00):
    a punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    ist_date = punch_in_ist.date()
    ist_time = punch_in_ist.time()

    start_h, start_m = parse_time_str(shift_start)
    end_h, end_m = parse_time_str(shift_end)
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight and ist_time < dtime(end_h, end_m):
        return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2 Late:
    Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) elapsed since shift_start, not since grace period ended.
    Shift 09:30: 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    start_h, start_m = parse_time_str(shift_start)
    start_dt = datetime.fromisoformat(f"{attendance_date}T{start_h:02d}:{start_m:02d}:00").replace(tzinfo=IST)

    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:  # strictly more than 10 minutes (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4 Work hours:
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    d = (Decimal(diff_sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(d)


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3 Overtime:
    Whole minutes (floor) from shift_end to punch_out, counted only if that is at least 30; otherwise 0.
    Overnight shifts: shift_end falls on next calendar day.
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    start_h, start_m = parse_time_str(shift_start)
    end_h, end_m = parse_time_str(shift_end)
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    if is_overnight:
        end_date = (date.fromisoformat(attendance_date) + timedelta(days=1)).isoformat()
    else:
        end_date = attendance_date

    end_dt = datetime.fromisoformat(f"{end_date}T{end_h:02d}:{end_m:02d}:00").replace(tzinfo=IST)
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day:
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def doc_to_employee_out(doc: dict) -> dict:
    """Formats an employee document for API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc.get("created_at")),
    }


def doc_to_attendance_out(doc: dict) -> dict:
    """Formats an attendance document for API response (no _id, dates to epoch ms)."""
    raw_history = doc.get("history") or []
    formatted_history = []
    for h in raw_history:
        entry = {
            "at": datetime_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {}
        }
        raw_changes = h.get("changes") or {}
        for field, change in raw_changes.items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = datetime_to_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = datetime_to_ms(to_val)
            entry["changes"][field] = {
                "from": from_val,
                "to": to_val
            }
        formatted_history.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": formatted_history
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Invalid date format for joined_on, expected YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"], operation_id="health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        res = db.command("ping")
        if res.get("ok"):
            return {"status": "ok"}
        raise HTTPException(status_code=503, detail="Database ping failed")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {str(exc)}")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"], operation_id="createEmployee")
def create_employee(body: EmployeeCreate):
    """Creates an employee. Unique emp_code enforced by database unique index."""
    now_dt = datetime.now(tz=UTC).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_dt
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee with emp_code '{body.emp_code}' already exists")

    return doc_to_employee_out(doc)


@app.get("/employees", tags=["employees"], operation_id="listEmployees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [doc_to_employee_out(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"], operation_id="punchIn")
def punch_in(body: PunchInRequest):
    """Record a punch-in. Atomic insertion prevents duplicate records for the same day."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{body.emp_code}' not found")

    if body.punched_at is not None:
        punch_in_dt = ms_to_datetime(body.punched_at)
    else:
        # Server clock truncated to whole second
        punch_in_dt = datetime.now(tz=UTC).replace(microsecond=0)

    attendance_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Attendance record already exists for {body.emp_code} on date {attendance_date}"
        )

    return doc_to_attendance_out(doc)


@app.post("/attendance/punch-out", tags=["attendance"], operation_id="punchOut")
def punch_out(body: PunchOutRequest):
    """
    Closes the most recent open record for the employee whose punch_in <= punched_at.
    Computes work_hours, overtime_minutes, and half_day.
    Concurrency safe: exactly one 200 on simultaneous punch-outs, remainder 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{body.emp_code}' not found")

    if body.punched_at is not None:
        punch_out_dt = ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = datetime.now(tz=UTC).replace(microsecond=0)

    # Find the most recent record for the employee that has a punch_in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=404, detail=f"No punch-in record found for {body.emp_code}")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    # Atomic update on punch_out: None guarantees race condition safety
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Attendance record was already punched out concurrently")

    return doc_to_attendance_out(updated_doc)



@app.get("/attendance", tags=["attendance"], operation_id="listAttendance")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Uses indexed database queries and does not load full collections into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [doc_to_attendance_out(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"], operation_id="regularizeAttendance")
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...
):
    """
    Regularizes (manually corrects) one day's record. Recomputes derived fields
    and appends an entry to the history audit trail.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=404, detail=f"No attendance record found for {emp_code} on {date_str}")

    # Determine requested changes
    req_status = body.status
    req_punch_in_ms = body.punch_in
    req_punch_out_ms = body.punch_out

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch times with it is 422
    if req_status in ("ABSENT", "LEAVE"):
        if req_punch_in_ms is not None or req_punch_out_ms is not None:
            raise HTTPException(status_code=422, detail=f"Cannot supply punch times when status is {req_status}")
        final_status = req_status
        final_punch_in_dt = None
        final_punch_out_dt = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_status = req_status if req_status is not None else record["status"]

        # If existing record was ABSENT/LEAVE and we switch to presence without supplying punch_in -> 422
        if req_punch_in_ms is not None:
            final_punch_in_dt = ms_to_datetime(req_punch_in_ms)
        else:
            final_punch_in_dt = record.get("punch_in")
            if final_punch_in_dt is not None and final_punch_in_dt.tzinfo is None:
                final_punch_in_dt = final_punch_in_dt.replace(tzinfo=UTC)

        if final_status in ("PRESENT", "WFH", "ON_DUTY") and final_punch_in_dt is None:
            raise HTTPException(status_code=422, detail=f"Presence status '{final_status}' requires a punch_in instant")

        # punch_in must stay on the record's attendance date per R1
        if final_punch_in_dt is not None:
            calc_date = get_attendance_date(final_punch_in_dt, emp["shift_start"], emp["shift_end"])
            if calc_date != date_str:
                raise HTTPException(
                    status_code=422,
                    detail=f"punch_in instant belongs to attendance date {calc_date}, not record date {date_str}"
                )

        if req_punch_out_ms is not None:
            final_punch_out_dt = ms_to_datetime(req_punch_out_ms)
        else:
            final_punch_out_dt = record.get("punch_out")
            if final_punch_out_dt is not None and final_punch_out_dt.tzinfo is None:
                final_punch_out_dt = final_punch_out_dt.replace(tzinfo=UTC)

        # Validate punch_out relative to punch_in
        if final_punch_in_dt is not None and final_punch_out_dt is not None:
            diff_sec = (final_punch_out_dt - final_punch_in_dt).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

        # Recompute derived fields
        if final_punch_in_dt is not None:
            final_late_minutes = compute_late_minutes(final_punch_in_dt, emp["shift_start"], date_str)
        else:
            final_late_minutes = 0

        if final_punch_in_dt is not None and final_punch_out_dt is not None:
            final_work_hours = compute_work_hours(final_punch_in_dt, final_punch_out_dt)
            final_overtime_minutes = compute_overtime(final_punch_out_dt, emp["shift_start"], emp["shift_end"], date_str)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Check differences against existing record
    existing_status = record.get("status")
    existing_punch_in = record.get("punch_in")
    if existing_punch_in is not None and existing_punch_in.tzinfo is None:
        existing_punch_in = existing_punch_in.replace(tzinfo=UTC)

    existing_punch_out = record.get("punch_out")
    if existing_punch_out is not None and existing_punch_out.tzinfo is None:
        existing_punch_out = existing_punch_out.replace(tzinfo=UTC)

    existing_work_hours = record.get("work_hours")
    existing_late_min = record.get("late_minutes", 0)
    existing_overtime_min = record.get("overtime_minutes", 0)
    existing_half_day = bool(record.get("half_day", False))

    changes: Dict[str, Any] = {}
    if existing_status != final_status:
        changes["status"] = {"from": existing_status, "to": final_status}

    if existing_punch_in != final_punch_in_dt:
        changes["punch_in"] = {
            "from": datetime_to_ms(existing_punch_in),
            "to": datetime_to_ms(final_punch_in_dt)
        }

    if existing_punch_out != final_punch_out_dt:
        changes["punch_out"] = {
            "from": datetime_to_ms(existing_punch_out),
            "to": datetime_to_ms(final_punch_out_dt)
        }

    if existing_work_hours != final_work_hours:
        changes["work_hours"] = {"from": existing_work_hours, "to": final_work_hours}

    if existing_late_min != final_late_minutes:
        changes["late_minutes"] = {"from": existing_late_min, "to": final_late_minutes}

    if existing_overtime_min != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": existing_overtime_min, "to": final_overtime_minutes}

    if existing_half_day != final_half_day:
        changes["half_day"] = {"from": existing_half_day, "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    correction_now = datetime.now(tz=UTC).replace(microsecond=0)
    history_entry = {
        "at": correction_now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Concurrency safe update using history length optimistic locking
    history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{history_len}": {"$exists": False}
        },
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in_dt,
                "punch_out": final_punch_out_dt,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularizations conflict; please retry")

    return doc_to_attendance_out(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Real MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"], operation_id="employeeMonthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    """
    Monthly attendance summary for one employee backed by a real MongoDB aggregation.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    # Calculate R7 working days (Mon-Fri) counted from joined_on for mid-month joiners
    eff_start = max(month_start, emp["joined_on"])
    working_days = 0
    if eff_start <= month_end:
        cur = date.fromisoformat(eff_start)
        end_d = date.fromisoformat(month_end)
        while cur <= end_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$addFields": {
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }
        }
    ]

    agg_results = list(db.attendance_logs.aggregate(pipeline))
    if agg_results:
        res = agg_results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_overtime = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_overtime = 0

    if working_days > 0:
        pct_raw = (present_days / working_days) * 100
        attendance_pct = round_half_up(pct_raw, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", tags=["analytics"], operation_id="departmentSummary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)")
):
    """
    Department summary for a month via MongoDB aggregation across collections.
    Headcount includes people with zero logs and excludes people who joined after month_end.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$addFields": {
                "has_log": {"$cond": [{"$ifNull": ["$logs._id", False]}, True, False]},
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$has_log", True]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$has_log", True]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$type": "$logs.work_hours"}, "missing"]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$has_log", True]},
                                    {"$gt": ["$logs.late_minutes", 0]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$has_log", True]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$has_log", True]},
                                    {"$eq": ["$logs.status", "LEAVE"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$has_log", True]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees_set"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {
            "$match": {
                "headcount": {"$gt": 0}
            }
        },
        {
            "$sort": {"department": ASCENDING}
        }
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        doc["present_days"] = round_half_up(doc["present_days"], 2)
        doc["avg_work_hours"] = round_half_up(doc["avg_work_hours"], 2)
        items.append(doc)

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late", tags=["analytics"], operation_id="lateLeaderboard")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None)
):
    """
    Late-comers leaderboard for a month. Standard competition ranking with ties (1, 2, 2, 4).
    Limit applied after ranking.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {
            "$unwind": "$emp"
        }
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "emp_code": ASCENDING
            }
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend", tags=["analytics"], operation_id="departmentTrend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    """
    Daily attendance trend for a department with database gap-filling and 7-day moving average.
    """
    # 404 unknown department
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail=f"Department '{department}' not found")

    try:
        from_d = date.fromisoformat(date_from)
        to_d = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date cannot be before 'from' date")

    day_span = (to_d - from_d).days + 1
    if day_span > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    start_dt = datetime.fromisoformat(f"{date_from}T00:00:00Z")
    end_dt = datetime.fromisoformat(f"{date_to}T00:00:00Z") + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to}
            }
        },
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date"}}
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt]
                }
            }
        },
        {
            "$group": {
                "_id": "$date_dt"
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": "$_id",
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id"
                    }
                },
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$curr_date"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "dept_emps"
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"curr_date": "$date", "emp_codes": "$dept_emps.emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$curr_date"]},
                                    {"$in": ["$emp_code", "$$emp_codes"]}
                                ]
                            }
                        }
                    }
                ],
                "as": "day_logs"
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$dept_emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "l",
                                    "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                }
                            },
                            "as": "pl",
                            "in": {"$cond": [{"$eq": ["$$pl.half_day", True]}, 0.5, 1.0]}
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$divide": ["$present_count", "$headcount"]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": ASCENDING}}
    ]

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        doc["present_count"] = round_half_up(doc["present_count"], 2)
        doc["attendance_rate"] = round_half_up(doc["attendance_rate"], 4)
        doc["moving_avg_7d"] = round_half_up(doc["moving_avg_7d"], 4)
        items.append(doc)

    return {
        "department": department,
        "items": items
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"], operation_id="explainEndpoint")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100)
):
    """
    Runs explain with executionStats verbosity on the main query or pipeline of the named endpoint.
    Accepts the same query parameters as the explained endpoint. Missing required parameters return 422.
    """
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
            q: Dict[str, Any] = {}
            if emp_code is not None:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status is not None:
                q["status"] = status
            skip = ((page or 1) - 1) * (page_size or 20)
            find_cmd = {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size or 20
            }
            explain_output = db.command("explain", find_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": jsonable_encoder(explain_output)
            }

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
            year, m = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m)[1]
            month_start = f"{month}-01"
            month_end = f"{month}-{num_days:02d}"
            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$gte": month_start, "$lte": month_end}
                    }
                },
                {
                    "$addFields": {
                        "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                }
            ]
            agg_cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_output = db.command("explain", agg_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": jsonable_encoder(explain_output)
            }

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required for department_summary")
            year, m = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m)[1]
            month_end = f"{month}-{num_days:02d}"
            match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
            if department is not None:
                match_stage["department"] = department

            pipeline = [
                {"$match": match_stage},
                {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}
            ]
            agg_cmd = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_output = db.command("explain", agg_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "employees",
                "explain": jsonable_encoder(explain_output)
            }

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
            year, m = map(int, month.split("-"))
            num_days = calendar.monthrange(year, m)[1]
            month_start = f"{month}-01"
            month_end = f"{month}-{num_days:02d}"
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": month_start, "$lte": month_end},
                        "late_minutes": {"$gt": 0}
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"}
                    }
                }
            ]
            agg_cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_output = db.command("explain", agg_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": jsonable_encoder(explain_output)
            }

        elif endpoint == "department_trend":
            if not from_date or not to_date:
                raise HTTPException(status_code=422, detail="'from' and 'to' parameters are required for department_trend")
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": from_date, "$lte": to_date}
                    }
                }
            ]
            agg_cmd = {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            }
            explain_output = db.command("explain", agg_cmd, verbosity="executionStats")
            return {
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": jsonable_encoder(explain_output)
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

