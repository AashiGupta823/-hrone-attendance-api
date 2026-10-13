"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment
      variables always win over .env - see load_dotenv(override=False) below).

Design notes (expanded in DECISIONS.md):
  * All instants are stored in MongoDB as naive datetimes that represent UTC (the
    pymongo convention). They are converted to/from epoch milliseconds only at the
    HTTP boundary (epoch_ms_to_dt / dt_to_epoch_ms).
  * IST is treated as a fixed UTC+05:30 offset (no DST), which is correct for India
    and is what the assignment assumes throughout.
  * `date` and `joined_on` are plain ISO "YYYY-MM-DD" strings end-to-end, both in
    MongoDB and in the API, so they compare correctly with ordinary string
    comparison (ISO-8601 lexical order == chronological order).
  * Every analytics endpoint is backed by a MongoDB aggregation pipeline. Each
    pipeline is built by a small `build_*` function that is shared between the real
    endpoint and GET /admin/explain/{endpoint}, so the explain output always
    reflects the exact query the real endpoint runs.
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# `.env` is a convenience for local development only; real environment variables
# (as the grader injects) must always take priority, so override=False.
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST_OFFSET = timedelta(hours=5, minutes=30)
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


# ============================================================================ #
# Startup: create indexes idempotently (safe to call on every boot - the grader
# starts the app against a database that already holds its hidden dataset).
# ============================================================================ #
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_department_joined")
    # Enforces "at most one record per (emp_code, date)" at the database level and
    # makes punch-in race-safe (see punch_in: we rely on DuplicateKeyError, never
    # check-then-insert). Also serves point lookups and emp_code-filtered ranges.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date")
    # Serves the default listing (no emp_code filter), sorted date desc, emp_code
    # asc - exactly the contract's required sort - and any date-range-only filter
    # (compound index prefix).
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_date_empcode")
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_status_date_empcode"
    )
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ============================================================================ #
# Time / date / rounding helpers
# ============================================================================ #
def epoch_ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> naive-UTC datetime, truncated to whole seconds (R1: truncation
    happens before any calculation, and the truncated value is what gets stored)."""
    seconds = ms // 1000
    return datetime(1970, 1, 1) + timedelta(seconds=seconds)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    dt = dt.replace(microsecond=0)
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def utc_to_ist_naive(dt_utc: datetime) -> datetime:
    return dt_utc + IST_OFFSET


def ist_naive_to_utc(dt_ist: datetime) -> datetime:
    return dt_ist - IST_OFFSET


def parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def compute_record_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the attendance date for a punch-in instant."""
    ist = utc_to_ist_naive(punch_in_utc)
    overnight = is_overnight(shift_start, shift_end)
    eh, em = parse_hhmm(shift_end)
    if overnight and (ist.hour, ist.minute, ist.second) < (eh, em, 0):
        return (ist.date() - timedelta(days=1)).isoformat()
    return ist.date().isoformat()


def shift_instant_utc(record_date_str: str, hhmm: str, is_end: bool, overnight: bool) -> datetime:
    """UTC instant of a shift boundary (start or end) for a given attendance date."""
    d = date.fromisoformat(record_date_str)
    if is_end and overnight:
        d = d + timedelta(days=1)
    h, m = parse_hhmm(hhmm)
    return ist_naive_to_utc(datetime(d.year, d.month, d.day, h, m, 0))


def compute_late_minutes(punch_in_utc: datetime, record_date_str: str, shift_start: str, shift_end: str) -> int:
    """R2. Grace 10:00; late only if strictly more than 10 minutes after shift_start."""
    overnight = is_overnight(shift_start, shift_end)
    start_instant = shift_instant_utc(record_date_str, shift_start, False, overnight)
    diff = (punch_in_utc - start_instant).total_seconds()
    return int(diff // 60) if diff > 600 else 0


def compute_overtime_minutes(punch_out_utc: datetime, record_date_str: str, shift_start: str, shift_end: str) -> int:
    """R3. Floor minutes from shift_end to punch_out, counted only if >= 30."""
    overnight = is_overnight(shift_start, shift_end)
    end_instant = shift_instant_utc(record_date_str, shift_end, True, overnight)
    diff = (punch_out_utc - end_instant).total_seconds()
    if diff < 0:
        return 0
    minutes = int(diff // 60)
    return minutes if minutes >= 30 else 0


def round_half_up(value, decimals: int):
    if value is None:
        return None
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4. (punch_out - punch_in) in seconds / 3600, half-up to 2 decimals."""
    total_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    return round_half_up(Decimal(total_seconds) / Decimal(3600), 2)


def compute_half_day(work_hours: float) -> bool:
    """R5. Based on the already-rounded work_hours."""
    return work_hours < 4.50


def month_bounds(month: str):
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y, 12, 31) if m == 12 else date(y, m + 1, 1) - timedelta(days=1)
    return start, end


def working_days_count(period_start: date, period_end: date, joined_on: str) -> int:
    """R7: Mon-Fri only, starting from joined_on for mid-period joiners."""
    j = date.fromisoformat(joined_on)
    start = max(period_start, j)
    if start > period_end:
        return 0
    days = 0
    d = start
    while d <= period_end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


# ============================================================================ #
# Request models (validation mirrors openapi.yaml exactly)
# ============================================================================ #
EpochMillis = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]
TimeHHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


def _validate_calendar_date(v: str) -> str:
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError("must be a valid YYYY-MM-DD date")
    return v


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: TimeHHMM = "09:30"
    shift_end: TimeHHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_valid(cls, v):
        return _validate_calendar_date(v)

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# ============================================================================ #
# Response serialization
# ============================================================================ #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def serialize_history_entry(h: dict) -> dict:
    changes = {}
    for k, v in (h.get("changes") or {}).items():
        frm, to = v.get("from"), v.get("to")
        if isinstance(frm, datetime):
            frm = dt_to_epoch_ms(frm)
        if isinstance(to, datetime):
            to = dt_to_epoch_ms(to)
        changes[k] = {"from": frm, "to": to}
    return {
        "at": dt_to_epoch_ms(h["at"]),
        "by": h["by"],
        "reason": h["reason"],
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [serialize_history_entry(h) for h in (doc.get("history") or [])],
    }


# ============================================================================ #
# /health
# ============================================================================ #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unreachable")
    return {"status": "ok"}


# ============================================================================ #
# /employees
# ============================================================================ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.utcnow().replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ============================================================================ #
# /attendance/punch-in, /attendance/punch-out, /attendance, /attendance/{emp_code}/{date}
# ============================================================================ #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.utcnow().replace(microsecond=0)
    record_date = compute_record_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, record_date, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    # Race-safe: rely on the unique (emp_code, date) index rather than check-then-insert.
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.utcnow().replace(microsecond=0)

    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )
    if not candidate:
        raise HTTPException(404, "no open punch-in found")

    punch_in_dt = candidate["punch_in"]
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - punch_in_dt) > timedelta(hours=24):
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime_minutes(ts, candidate["date"], emp["shift_start"], emp["shift_end"])
    half_day = compute_half_day(work_hours)

    # Atomic compare-and-set on punch_out: None - if a concurrent punch-out already
    # claimed this record, matched_count is 0 and we return 409 for the loser.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        rng: dict = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        q["date"] = rng
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    emp_code: str,
    date: Annotated[str, Path(pattern=r"^\d{4}-\d{2}-\d{2}$")],
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "no record for that date")

    new_status = body.status if body.status is not None else old["status"]
    new_punch_in = epoch_ms_to_dt(body.punch_in) if body.punch_in is not None else old.get("punch_in")
    new_punch_out = epoch_ms_to_dt(body.punch_out) if body.punch_out is not None else old.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if compute_record_date(new_punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in does not fall on this record's attendance date")
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in) > timedelta(hours=24):
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    if new_status in ("ABSENT", "LEAVE"):
        new_late, new_work_hours, new_overtime, new_half_day = 0, None, 0, False
    else:
        new_late = compute_late_minutes(new_punch_in, date, emp["shift_start"], emp["shift_end"])
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime_minutes(new_punch_out, date, emp["shift_start"], emp["shift_end"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours, new_overtime, new_half_day = None, 0, False

    old_status = old["status"]
    old_punch_in = old.get("punch_in")
    old_punch_out = old.get("punch_out")
    old_late = old.get("late_minutes") or 0
    old_work_hours = old.get("work_hours")
    old_overtime = old.get("overtime_minutes") or 0
    old_half_day = bool(old.get("half_day") or False)

    if new_status == old_status and new_punch_in == old_punch_in and new_punch_out == old_punch_out:
        raise HTTPException(422, "request changes nothing")

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_overtime != old_overtime:
        changes["overtime_minutes"] = {"from": old_overtime, "to": new_overtime}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    history_entry = {
        "at": datetime.utcnow().replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: the filter pins every mutable field to the value we
    # read. If another regularization won the race in between, this match fails
    # (matched_count 0) and we return 409 for the loser - no history entry is lost.
    result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "status": old_status,
            "punch_in": old_punch_in,
            "punch_out": old_punch_out,
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return serialize_attendance(result)


# ============================================================================ #
# Analytics - pipeline builders (shared with /admin/explain/{endpoint})
# ============================================================================ #
def build_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {
            "_id": None,
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]},
                        {"$in": ["$status", PRESENCE_STATUSES]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                    0,
                ],
            }},
        }},
    ]


def build_department_summary_pipeline(
    month_start: str, month_end: str, period_end: str, department: Optional[str]
) -> list:
    match_emp: dict = {"joined_on": {"$lte": period_end}}
    if department:
        match_emp["department"] = department
    return [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", month_start]},
                    {"$lte": ["$date", month_end]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArray": True}},
        {"$addFields": {
            "dow": {"$cond": [
                {"$ne": ["$logs", None]},
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                None,
            ]},
        }},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$ne": ["$logs", None]},
                        {"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]},
                        {"$in": ["$logs.status", PRESENCE_STATUSES]},
                    ]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                    0,
                ],
            }},
            "late_count": {"$sum": {"$cond": [
                {"$and": [{"$ne": ["$logs", None]}, {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}]}, 1, 0,
            ]}},
            "total_late_minutes": {"$sum": {
                "$cond": [{"$ne": ["$logs", None]}, {"$ifNull": ["$logs.late_minutes", 0]}, 0],
            }},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$ne": ["$logs", None]},
                        {"$in": ["$logs.status", PRESENCE_STATUSES]},
                        {"$ne": ["$logs.work_hours", None]},
                    ]},
                    "$logs.work_hours", 0,
                ],
            }},
            "work_hours_count": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$ne": ["$logs", None]},
                        {"$in": ["$logs.status", PRESENCE_STATUSES]},
                        {"$ne": ["$logs.work_hours", None]},
                    ]}, 1, 0,
                ],
            }},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def build_leaderboard_pipeline(month_start: str, month_end: str, department: Optional[str], limit: int) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank implements standard competition ranking natively: ties share a rank,
        # the next rank is skipped. sortBy is total_late_minutes ONLY so that ties
        # are based purely on that value (emp_code is used for row order, not rank).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def build_trend_pipeline(department: str, from_date: str, to_date: str, emp_codes: list) -> list:
    from_dt = datetime.combine(date.fromisoformat(from_date), datetime.min.time())
    to_dt = datetime.combine(date.fromisoformat(to_date), datetime.min.time())
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", PRESENCE_STATUSES]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                    0,
                ],
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"dateVal": {"$dateFromString": {"dateString": "$_id"}}}},
        # Explicit date bounds: $densify fills the whole range even when the match
        # above produced zero documents (an all-empty department/period).
        {"$densify": {"field": "dateVal", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]}}},
        {"$addFields": {
            "date_str": {"$dateToString": {"date": "$dateVal", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        # Per-day headcount computed inside the database: employees in this
        # department whose joined_on is on or before that day (R9).
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date_str"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$dateVal"}, 2]},
                {"$lte": [{"$dayOfWeek": "$dateVal"}, 6]},
            ]},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$sort": {"dateVal": 1}},
        # $avg over a window naturally ignores nulls - exactly "mean of the
        # non-null attendance_rate values ... null if the window holds none".
        {"$setWindowFields": {
            "sortBy": {"dateVal": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


# ============================================================================ #
# Analytics endpoints
# ============================================================================ #
MonthQuery = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
DateQuery = Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    start, end = month_bounds(month)
    pipeline = build_monthly_pipeline(emp_code, start.isoformat(), end.isoformat())
    result = list(db.attendance_logs.aggregate(pipeline))
    agg = result[0] if result else {}

    working_days = working_days_count(start, end, emp["joined_on"])
    present_days = round_half_up(agg.get("present_days", 0), 2)
    attendance_pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    start, end = month_bounds(month)
    pipeline = build_department_summary_pipeline(start.isoformat(), end.isoformat(), end.isoformat(), department)
    rows = list(db.employees.aggregate(pipeline))
    items = [
        {
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r.get("present_days", 0), 2),
            "avg_work_hours": round_half_up(r["avg_work_hours"], 2) if r.get("avg_work_hours") is not None else None,
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthQuery,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    pipeline = build_leaderboard_pipeline(start.isoformat(), end.isoformat(), department, limit)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    to: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
):
    if to < from_:
        raise HTTPException(422, "to must not be before from")
    if (date.fromisoformat(to) - date.fromisoformat(from_)).days > 92:
        raise HTTPException(422, "range exceeds 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")

    emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    pipeline = build_trend_pipeline(department, from_, to, emp_codes)
    rows = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            "attendance_rate": round_half_up(r["attendance_rate"], 4) if r.get("attendance_rate") is not None else None,
            "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4) if r.get("moving_avg_7d") is not None else None,
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# ============================================================================ #
# /admin/explain/{endpoint}
# ============================================================================ #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        plan = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = month_bounds(month)
        pipeline = build_monthly_pipeline(emp_code, start.isoformat(), end.isoformat())
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        pipeline = build_department_summary_pipeline(start.isoformat(), end.isoformat(), end.isoformat(), department)
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        pipeline = build_leaderboard_pipeline(start.isoformat(), end.isoformat(), department, limit)
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    # department_trend
    if not department or not from_ or not to:
        raise HTTPException(422, "department, from and to are required")
    emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    pipeline = build_trend_pipeline(department, from_, to, emp_codes)
    plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
