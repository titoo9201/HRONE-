"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI and MONGO_DB are required. Real environment variables win over a local .env.

Layout of this file, top to bottom:
  1. configuration, time helpers and the business rules R1-R5, R8 (pure functions, unit tested)
  2. startup: indexes (created idempotently, so a database that already holds data is fine)
  3. request models and document -> API conversion (instants are epoch ms in the API, UTC datetimes in Mongo)
  4. endpoints: health, employees, attendance (punch-in / punch-out / list / PATCH)
  5. analytics: one build_*() function per endpoint returns (collection, aggregation pipeline)
  6. /admin/explain, which runs explain on exactly what those build_*() functions return

The contract is openapi.yaml, the stored data is described in DATA_MODEL.md.
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False by default, so real environment variables win over .env


def _require_env(name: str) -> str:
    # No hard-coded fallback: a missing setting should fail loudly at startup.
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (put it in the environment or in .env)")
    return value


# tz_aware=True makes PyMongo return UTC-aware datetimes instead of naive ones.
# The short selection timeout keeps /health from hanging when Mongo is down.
client = MongoClient(_require_env("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[_require_env("MONGO_DB")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

LATE_GRACE_SECONDS = 10 * 60      # R2
OVERTIME_MIN_MINUTES = 30         # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers (pure functions: no DB, no clock, so they are easy to unit test)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> tz-aware UTC datetime, truncated to whole seconds (R1).
    Uses integer timedelta math instead of fromtimestamp(ms / 1000) to avoid float error."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def dt_to_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds. A naive datetime (e.g. from seeded data read
    without tz_aware) is treated as UTC, which is how MongoDB stores it."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def round_half_up(value, places: int) -> float:
    """R8: Decimal ROUND_HALF_UP. Built-in round() is banker's rounding on a binary float,
    and Mongo's $round also rounds ties to even, so reported numbers go through here."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    # "HH:MM" strings compare correctly as text because they are zero-padded.
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in. For an overnight shift, a punch-in whose IST
    time is earlier than shift_end belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def _at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    return _at(date.fromisoformat(date_str), shift_start)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    """The shift end of an overnight shift falls on the next calendar day."""
    day = date.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        day += timedelta(days=1)
    return _at(day, shift_end)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start. Then floor of the
    minutes since shift_start (not since the end of the grace period).
    We anchor on the attendance date (not punch_in's own date) so that an overnight
    shift punched in after midnight is measured from the previous evening's start."""
    seconds = int((punch_in - shift_start_at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > LATE_GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor of whole minutes after shift_end, counted only when >= 30."""
    seconds = int((punch_out - shift_end_at(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals, using exact Decimal division."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: judged on the ROUNDED work_hours (4.499 -> 4.50 -> not a half day)."""
    return work_hours is not None and Decimal(str(work_hours)) < HALF_DAY_LIMIT




# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """create_index is idempotent: it is a no-op when the same index already exists,
    so this is safe to run on every start, also against a DB that already has data."""
    # Business key: one employee per emp_code. Also what makes concurrent creates safe.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # Department filter + the sort by emp_code (employee list, department headcount).
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # Headcount (R9) = department + joined_on <= day: department summary, trend. The second one
    # serves the same filter without a department (summary over all departments).
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # One record per (emp_code, date). This is the guarantee behind the punch-in race, and it
    # also serves "all records of one employee", PATCH lookup and the monthly summary.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_date_unique"
    )
    # Matches the list sort exactly (date desc, emp_code asc), so GET /attendance with a
    # date range or no filter needs no in-memory sort. Month analytics scan by date too.
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    # Punch-out: "this employee's most recent record with punch_in <= X" is an equality on
    # emp_code, a range on punch_in and a sort on punch_in desc, all answered by one index
    # walk that stops at the first hit. (emp_code, date) can't do it: date is not punch_in.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in_desc")


indexes_ready = False


def try_ensure_indexes() -> bool:
    global indexes_ready
    if not indexes_ready:
        try:
            ensure_indexes()
            indexes_ready = True
        except PyMongoError:
            pass  # Mongo not reachable yet; /health retries below
    return indexes_ready


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Don't crash if Mongo is still starting: the app comes up, /health says 503, and the
    # indexes are created on the first healthy /health call instead.
    try_ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    # If Mongo itself fails mid-request the caller gets a clean 503, never a bare 500.
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Request models and validation types
# --------------------------------------------------------------------------- #
def _check_calendar_date(value: str) -> str:
    # The regex only checks the shape; this rejects things like 2026-02-30.
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid calendar date") from None
    return value


DateStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_check_calendar_date)
]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# strict=True: floats and numeric strings are rejected, not coerced to int.
# The lower bound rejects values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


def validation_error(loc: str, msg: str, where: str = "query") -> RequestValidationError:
    """A cross-field check FastAPI cannot express: raise it as a normal 422 with the default body shape."""
    return RequestValidationError([{"type": "value_error", "loc": (where, loc), "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# Mongo document -> API shape
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def _history_value(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def attendance_out(doc: dict) -> dict:
    """Builds the response field by field so _id never leaks, instants become epoch ms,
    and legacy documents (missing late/overtime/half_day/history) get their defaults."""
    p_in, p_out = doc.get("punch_in"), doc.get("punch_out")
    history = [
        {
            "at": dt_to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {
                field: {"from": _history_value(c.get("from")), "to": _history_value(c.get("to"))}
                for field, c in h["changes"].items()
            },
        }
        for h in doc.get("history") or []
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(p_in) if p_in else None,
        "punch_out": dt_to_ms(p_out) if p_out else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def utc_now() -> datetime:
    # Whole seconds (R1); this also keeps what we store identical to what Mongo returns (ms precision).
    return datetime.now(UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable") from None
    if not try_ensure_indexes():
        raise HTTPException(503, "indexes not ready")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        # The unique index decides, not a find_one beforehand: parallel creates can't both win.
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {}
    if department is not None:
        q["department"] = department  # exact, case-sensitive
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": db.employees.count_documents(q),  # counts the filtered set (R10)
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    when = ms_to_dt(body.punched_at) if body.punched_at is not None else utc_now()
    day = attendance_date(when, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": when,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(when, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        # Unique (emp_code, date) index: of N simultaneous inserts exactly one succeeds.
        raise HTTPException(409, "already punched in for this date") from None
    return attendance_out(doc)


# Sort required by the contract. It is the same as the index key order, so Mongo reads the
# index in order and never sorts in memory. A shared constant lets /admin/explain reuse it.
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    if date_from and date_to and date_from > date_to:  # ISO dates compare correctly as text
        raise validation_error("date_from", "date_from must not be after date_to")
    q: dict = {}
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
    return q


def build_attendance_list(q: dict, page: int, page_size: int) -> dict:
    """The find() arguments of the list endpoint; /admin/explain explains exactly these."""
    return {
        "filter": q,
        "projection": {"_id": 0},
        "sort": ATTENDANCE_SORT,
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    # Sorting, skipping and limiting all happen inside MongoDB; Python only sees one page.
    cursor = db.attendance_logs.find(**build_attendance_list(q, page, page_size))
    # Always an exact count (R10). estimated_document_count() reads metadata, which can be stale.
    total = db.attendance_logs.count_documents(q)
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Punch-out and regularization (shared pieces)
# --------------------------------------------------------------------------- #
MAX_SHIFT_LENGTH = timedelta(hours=24)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
# Fields a correction may touch or recompute; these are the keys allowed in history[].changes.
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


def derived_fields(emp: dict, day: str, status: str, p_in: Optional[datetime], p_out: Optional[datetime]) -> dict:
    """The four stored derived values for a final (status, punch_in, punch_out). Used by
    punch-out and by regularization, so both always apply R2-R5 identically."""
    if status not in PRESENCE or p_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    out = {
        "late_minutes": compute_late_minutes(p_in, day, emp["shift_start"]),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if p_out is not None:
        out["work_hours"] = compute_work_hours(p_in, p_out)
        out["overtime_minutes"] = compute_overtime(p_out, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    when = ms_to_dt(body.punched_at) if body.punched_at is not None else utc_now()

    # Most recent record that started at or before the punch-out. We search by punch_in, not
    # by date, because an overnight shift's record is dated the day before the punch-out.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": when}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    p_in = rec["punch_in"]
    if when <= p_in:
        raise validation_error("punched_at", "punched_at must be after punch_in", "body")
    if when - p_in > MAX_SHIFT_LENGTH:
        raise validation_error("punched_at", "punched_at is more than 24 hours after punch_in", "body")

    d = derived_fields(emp, rec["date"], rec["status"], p_in, when)
    # The filter repeats "punch_out is null": the update itself is the check. Of two simultaneous
    # punch-outs only one finds a still-open record; the other matches nothing -> 409.
    # punch_in is in the filter too, so a correction made after we read can't invalidate our maths.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None, "punch_in": p_in},
        {
            "$set": {
                "punch_out": when,
                "work_hours": d["work_hours"],
                "overtime_minutes": d["overtime_minutes"],
                "half_day": d["half_day"],
            }
        },
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


class RegularizeRequest(BaseModel):
    # Derived fields (work_hours, late_minutes, ...) are not declared, so a client that sends them is ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def _null_is_not_allowed(cls, v):
        # The contract makes these non-nullable: "omit it" means unchanged, null is an error.
        if v is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return v


def stored_state(doc: dict) -> dict:
    """The compared fields of a stored record, with defaults for legacy documents."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = stored_state(rec)
    status = body.status or old["status"]

    if status not in PRESENCE:
        # ABSENT / LEAVE have no punch times, so any punch time in the body contradicts the status.
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error("status", "ABSENT/LEAVE cannot be combined with punch_in/punch_out", "body")
        p_in = p_out = None
    else:
        p_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        p_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if p_in is None:
            raise validation_error("punch_in", "a presence status requires a punch_in", "body")
        if body.punch_in is not None and attendance_date(p_in, emp["shift_start"], emp["shift_end"]) != date:
            raise validation_error("punch_in", "punch_in must stay on the record's attendance date", "body")
        # Time order is only checked when the caller touched a time; untouched stored data is left alone.
        if p_out is not None and (body.punch_in is not None or body.punch_out is not None):
            if p_out <= p_in:
                raise validation_error("punch_out", "punch_out must be after punch_in", "body")
            if p_out - p_in > MAX_SHIFT_LENGTH:
                raise validation_error("punch_out", "punch_out must be within 24 hours of punch_in", "body")

    new = {"status": status, "punch_in": p_in, "punch_out": p_out, **derived_fields(emp, date, status, p_in, p_out)}
    # "Changes nothing" is judged on what the caller can set. Derived values are NOT compared here:
    # a seeded record with a stale work_hours must not turn a no-op request into a 200.
    if all(old[f] == new[f] for f in ("status", "punch_in", "punch_out")):
        raise validation_error("body", "the request changes nothing", "body")
    # Once something real changed, every field that differs (derived ones too) goes into history.
    changes = {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if old[f] != new[f]}

    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic lock: only update if the record is still exactly as we read it. History only ever
    # grows, so its length detects any other correction; the fields cover a concurrent punch-out.
    # $size of $ifNull(history, []) also works for legacy documents that have no history field.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "status": old["status"],
            "punch_in": old["punch_in"],    # None also matches a missing field
            "punch_out": old["punch_out"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
        },
        {"$set": new, "$push": {"history": entry}},  # $push appends atomically, never overwrites
        projection={"_id": 0},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return attendance_out(updated)



# --------------------------------------------------------------------------- #
# Analytics
#
# Every endpoint has a build_*() function that returns (collection, pipeline). The endpoint runs
# it, and /admin/explain runs the very same pair, so what gets explained is what gets served.
# Date filters are string ranges on the IST "YYYY-MM-DD" `date` field (ISO strings sort like
# dates), which is what lets the (date, ...) and (emp_code, date) indexes be used.
# Aggregation computes the sums/counts; Python only does the final Decimal half-up rounding
# (R8) because MongoDB's $round rounds ties to even.
# --------------------------------------------------------------------------- #
def _check_month(value: str) -> str:
    if int(value[:4]) < 1:
        raise ValueError("year must be at least 0001")
    return value


Month = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    AfterValidator(_check_month),
]
MAX_TREND_DAYS = 92


def month_bounds(month: str) -> tuple[str, str]:
    """First and last IST calendar date of 'YYYY-MM' as date strings."""
    year, mon = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def weekdays_between(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). Plain calendar arithmetic, no data involved:
    whole weeks contribute 5 each, then only the leftover (<7) days are looked at."""
    days = (end - start).days + 1
    if days <= 0:
        return 0
    weeks, rest = divmod(days, 7)
    return weeks * 5 + sum(1 for i in range(rest) if (start.weekday() + i) % 7 < 5)


# Reusable aggregation expressions. $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6.
MON_FRI = [2, 3, 4, 5, 6]
IS_WEEKDAY_RECORD = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, MON_FRI]}
# R5/R6: a presence record is worth 1 present day, or 0.5 if half_day (missing half_day = false).
PRESENT_VALUE = {
    "$cond": [
        {"$in": ["$status", list(PRESENCE)]},
        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
        0,
    ]
}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}  # legacy docs may omit it
HAS_HOURS = {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$isNumber": "$work_hours"}]}

# The per-record sums shared by the employee and department summaries.
MONTH_ACCUMULATORS = {
    # present_days only counts Mon-Fri records (R7); everything else counts on any day.
    "present_days": {"$sum": {"$cond": [IS_WEEKDAY_RECORD, PRESENT_VALUE, 0]}},
    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    "late_count": {"$sum": {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}},
    "total_late_minutes": {"$sum": LATE_MINUTES},
    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
    # Average work hours = (sum, count) here and one division later. The sum is a Decimal128:
    # a double sum drifts (8.25 + 8.26 = 16.509999999999998), which would break exact ties.
    "work_hours_sum": {"$sum": {"$cond": [HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
    "work_hours_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
}


def as_decimal(value) -> Decimal:
    return value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))


# ---- 1. employee monthly ---------------------------------------------------- #
def build_employee_monthly(emp_code: str, month: str):
    first, last = month_bounds(month)
    return "attendance_logs", [
        # Equality on emp_code + a range on date: one tight scan of (emp_code, date).
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None, **MONTH_ACCUMULATORS}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    coll, pipeline = build_employee_monthly(emp_code, month)
    # No records in the month -> the $group emits nothing -> every sum is 0.
    row = next(db[coll].aggregate(pipeline), {})

    first, last = month_bounds(month)
    # R7: working days count from joined_on for someone who joined during the month.
    start = max(date.fromisoformat(first), date.fromisoformat(emp["joined_on"]))
    working_days = weekdays_between(start, date.fromisoformat(last))
    present = as_decimal(row.get("present_days", 0))
    pct = round_half_up(present / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_count", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# ---- 2. department summary --------------------------------------------------- #
def build_department_summary(month: str, department: Optional[str]):
    first, last = month_bounds(month)
    # R9: start from employees, so people with no logs still count and later joiners don't.
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return "employees", [
        {"$match": match},
        {
            # Per employee, the logs of the month are summed INSIDE the lookup, so only one small
            # document per employee comes back. foreignField=emp_code makes each probe an index lookup.
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": None, **MONTH_ACCUMULATORS}},
                ],
                "as": "m",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                # An employee without logs has m == [] -> $first is missing -> counts as 0.
                **{k: {"$sum": {"$ifNull": [{"$first": f"$m.{k}"}, 0]}} for k in MONTH_ACCUMULATORS},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    coll, pipeline = build_department_summary(month, department)
    items = []
    for row in db[coll].aggregate(pipeline):
        n = row["work_hours_n"]
        items.append(
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": round_half_up(as_decimal(row["present_days"]), 2),
                # Mean over records (not a mean of per-employee means); null when there are none.
                "avg_work_hours": round_half_up(as_decimal(row["work_hours_sum"]) / n, 2) if n else None,
                "late_count": int(row["late_count"]),
                "total_late_minutes": int(row["total_late_minutes"]),
                "leave_count": int(row["leave_count"]),
                "on_duty_count": int(row["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


# ---- 3. late leaderboard ----------------------------------------------------- #
def build_late_leaderboard(month: str, limit: int, department: Optional[str]):
    first, last = month_bounds(month)
    pipeline: list = [
        # Date range on the (date, emp_code) index; late_minutes > 0 also drops the zeros early.
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        # $unwind drops groups whose employee doesn't exist (empty array), as required.
        {"$unwind": "$emp"},
    ]
    if department is not None:
        # Filtered BEFORE ranking, so ranks are relative to that department only.
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit applies to the rank, not the row position, so tied rows at the cutoff all stay.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return "attendance_logs", pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Month,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    coll, pipeline = build_late_leaderboard(month, limit, department)
    return {
        "month": month,
        "items": [
            {**row, "total_late_minutes": int(row["total_late_minutes"])} for row in db[coll].aggregate(pipeline)
        ],
    }


# ---- 4. department trend ----------------------------------------------------- #
def build_department_trend(department: str, d_from: str, d_to: str):
    lo = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    hi = datetime.fromisoformat(d_to).replace(tzinfo=UTC) + timedelta(days=1)  # $densify's upper bound is exclusive
    return "employees", [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                    {"$project": {"_id": 0, "date": 1, "p": PRESENT_VALUE, "late": {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        # One row per day that has at least one record ...
        {"$group": {"_id": "$logs.date", "present_count": {"$sum": "$logs.p"}, "late_count": {"$sum": "$logs.late"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # ... then $densify inserts the missing days, so there is exactly one row per calendar day.
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, MON_FRI]},
                "present_count": {"$ifNull": ["$present_count", 0]},  # filled-in days have no counts yet
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            # R9 per day: employees of this department with joined_on <= that day. Index (department, joined_on).
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {
            # The rate as a whole number of ten-thousandths ("rate_e4": 0.6667 -> 6667), rounded HALF-UP.
            # The spec's attendance_rate is the 4-decimal value, and the moving average is taken over
            # THAT value, so the rounding has to happen here, inside the pipeline, before the window.
            # $round can't be used (ties go to even), and float math can misplace a tie, so this is
            # integer arithmetic: present_count is a multiple of 0.5, hence P2 = 2 * present_count is
            # an integer, and rate*10000 = 5000 * P2 / H. Half-up = floor(x + 1/2)
            # = floor((10000 * P2 + H) / (2 * H)). Integer / integer is exact enough for floor().
            # Only working days with someone employed have a rate; a working day without records is 0.0.
            "$set": {
                "rate_e4": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": [10000, {"$multiply": ["$present_count", 2]}]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {
            # Current row + up to 6 before it, within this result only (nothing before `from`).
            # Sum and count of the non-null rounded rates; the division happens in Python with Decimal,
            # because a double average can land a hair under an exact tie (0.33335 -> 0.33334999..).
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "window_sum_e4": {"$sum": "$rate_e4", "window": {"documents": [-6, 0]}},
                    "window_n": {
                        "$sum": {"$cond": [{"$eq": ["$rate_e4", None]}, 0, 1]},
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                      "rate_e4": 1, "window_sum_e4": 1, "window_n": 1}},
    ]


def validate_trend_range(date_from: str, date_to: str) -> None:
    if date_to < date_from:
        raise validation_error("to", "to must not be before from")
    days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if days > MAX_TREND_DAYS:
        raise validation_error("to", f"range must not exceed {MAX_TREND_DAYS} days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[DateStr, Query(alias="from")],
    date_to: Annotated[DateStr, Query(alias="to")],
):
    validate_trend_range(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")

    coll, pipeline = build_department_trend(department, date_from, date_to)
    # rate_e4 is already the half-up 4-decimal rate, as an integer count of 1/10000.
    rate = lambda e4: None if e4 is None else round_half_up(Decimal(int(e4)) / 10000, 4)

    def moving_avg(r):
        n = int(r["window_n"])
        # Mean of the (rounded) rates in the window, rounded half-up once more. Exact Decimal division.
        return round_half_up(Decimal(int(r["window_sum_e4"])) / (n * 10000), 4) if n else None
    return {
        "department": department,
        "items": [
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": r["headcount"],
                "present_count": round_half_up(as_decimal(r["present_count"]), 2),
                "late_count": int(r["late_count"]),
                "attendance_rate": rate(r["rate_e4"]),
                "moving_avg_7d": moving_avg(r),
            }
            for r in db[coll].aggregate(pipeline)
        ],
    }


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def require(**params) -> None:
    """422 listing every required parameter that was not sent (same body shape as FastAPI's own)."""
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing]
        )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,  # `from` is a Python keyword
    to: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    # Every branch builds the query with the SAME function the real endpoint uses, so the plan we
    # return is the plan of the query that is actually served: same filter, sort, skip and limit.
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        kw = build_attendance_list(attendance_filter(emp_code, date_from, date_to, status), page, page_size)
        command = {
            "find": coll,
            "filter": kw["filter"],
            "projection": kw["projection"],
            "sort": dict(kw["sort"]),  # dicts keep insertion order, which is the sort order
            "skip": kw["skip"],
            "limit": kw["limit"],
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            coll, pipeline = build_employee_monthly(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            coll, pipeline = build_department_summary(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            coll, pipeline = build_late_leaderboard(month, limit, department)
        else:  # department_trend
            require(department=department, **{"from": from_, "to": to})
            validate_trend_range(from_, to)
            coll, pipeline = build_department_trend(department, from_, to)
        command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # The raw explain contains BSON types (datetime, ObjectId, Int64, Decimal128) that FastAPI's
    # JSON encoder can't handle. json_util turns them into plain JSON, e.g. {"$date": ...}.
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}
