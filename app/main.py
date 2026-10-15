"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file:
  1. Setup                (Mongo client, indexes, app)
  2. Time & rounding      (epoch-ms <-> datetime, IST, half-up rounding)
  3. Business rules       (R1-R5: attendance date, late, overtime, work hours, half day)
  4. Request models       (validation)
  5. Serializers          (Mongo document -> API JSON)
  6. Endpoints            (employees, attendance)
  7. Analytics pipelines  (monthly, department summary, late leaderboard, trend)
  8. Explain endpoint
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, StrictInt, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the environment

# tz_aware=True: every datetime read from Mongo is a UTC-aware datetime (never naive).
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
log = logging.getLogger("attendance")


INDEXES = [
    # (collection, keys, unique, what it serves)
    ("employees", [("emp_code", ASCENDING)], True, "identity + duplicate guard"),
    ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], False, "dept filter + emp_code sort, trend"),
    ("employees", [("joined_on", ASCENDING)], False, "headcount: joined by month end"),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], True, "one record per day, employee queries"),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], False, "default list order, month scans"),
    ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], False, "status filter"),
]


def create_indexes():
    """Idempotent: creating an index that already exists is a no-op. One failure does not block the others."""
    for collection, keys, unique, purpose in INDEXES:
        try:
            db[collection].create_index(keys, unique=unique)
        except PyMongoError as exc:
            log.error("index on %s (%s) failed: %s", collection, purpose, exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        create_indexes()
    except PyMongoError as exc:  # keep the app up so /health can answer 503
        log.error("could not create indexes: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 2. Time & rounding helpers
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(value):
    """datetime -> epoch milliseconds. Anything else (None, numbers) is returned unchanged."""
    if isinstance(value, datetime):
        return (value - EPOCH) // timedelta(milliseconds=1)
    return value


def round_half_up(value, places: int = 2) -> float:
    """Half-up rounding (Python's round() and Mongo's $round both round halves to even)."""
    d = value if isinstance(value, Decimal) else Decimal(str(round(value, 9)))  # round(.., 9) hides float noise
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_range(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def count_weekdays(start: date, end: date) -> int:
    """Number of Mon-Fri days from start to end inclusive (R7). Pure date arithmetic, at most ~31 steps."""
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# 3. Business rules (R1 - R5)
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> time:
    return time(int(value[:2]), int(value[3:]))


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST date of the punch-in; for overnight shifts, an early-morning punch belongs to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(att_date: str, emp: dict) -> tuple[datetime, datetime]:
    """Scheduled shift start and end (IST) for an attendance date. Overnight shifts end on the next day."""
    day = date.fromisoformat(att_date)
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), tzinfo=IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), tzinfo=IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if strictly more than 600 raw seconds after shift start; then floor to whole minutes."""
    seconds = (punch_in - shift_start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, counted only if at least 30."""
    minutes = int((punch_out - shift_end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours between punches, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_derived(emp: dict, att_date: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four stored derived fields for a record (used by punch-in, punch-out and PATCH)."""
    derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:  # ABSENT / LEAVE
        return derived
    shift_start, shift_end = shift_bounds(att_date, emp)
    derived["late_minutes"] = compute_late_minutes(punch_in, shift_start)
    if punch_out is not None:
        derived["work_hours"] = compute_work_hours(punch_in, punch_out)
        derived["overtime_minutes"] = compute_overtime(punch_out, shift_end)
        derived["half_day"] = derived["work_hours"] < 4.5  # R5, on the rounded value
    return derived


def check_punch_out_time(punch_in: datetime, punch_out: datetime):
    """punch_out must be after punch_in and at most 24 hours later (422 otherwise)."""
    if punch_out <= punch_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


# --------------------------------------------------------------------------- #
# 4. Request models and parameter types
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

# Strict int: floats, strings and booleans are rejected; the range rejects seconds-looking values.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


def _check_calendar_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError("not a valid calendar date")
    return value


DateStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_check_calendar_date)]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# 5. Serializers (Mongo document -> API JSON; never expose _id)
# --------------------------------------------------------------------------- #
def employee_to_api(doc: dict) -> dict:
    doc = {k: v for k, v in doc.items() if k != "_id"}
    doc["created_at"] = to_ms(doc.get("created_at"))
    return doc


def attendance_to_api(doc: dict) -> dict:
    history = [
        {
            "at": to_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {
                field: {"from": to_ms(change["from"]), "to": to_ms(change["to"])}
                for field, change in entry["changes"].items()
            },
        }
        for entry in doc.get("history") or []
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, so races are safe
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(punched_at, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **compute_derived(emp, att_date, punched_at, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record that started at or before the punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}, sort=[("date", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_out_time(rec["punch_in"], punched_at)

    derived = compute_derived(emp, rec["date"], rec["punch_in"], punched_at)
    update = {"punch_out": punched_at, **derived}
    # Atomic: only succeeds while punch_out is still empty, so one of two concurrent requests gets 409.
    result = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": update})
    if result.modified_count == 0:
        raise HTTPException(409, "already punched out")
    return attendance_to_api({**rec, **update})


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and its explain."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort(list(ATTENDANCE_SORT.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


CHANGE_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
CHANGE_DEFAULTS = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}  # for legacy docs missing the field


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # 1. Work out the final status and punch times (omitted fields keep their stored value).
    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{status} cannot have punch times")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, f"{status} requires a punch_in")
        if attendance_date(new_in, emp) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            check_punch_out_time(new_in, new_out)

    # 2. Recompute derived fields and diff against what is stored.
    new_values = {"status": status, "punch_in": new_in, "punch_out": new_out, **compute_derived(emp, date, new_in, new_out)}
    changes = {}
    for field in CHANGE_FIELDS:
        old = rec.get(field, CHANGE_DEFAULTS.get(field))
        if old != new_values[field]:
            changes[field] = {"from": old, "to": new_values[field]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    # 3. Write only if the record is still exactly as we read it (status, punches, history length),
    #    so a concurrent correction can never be silently overwritten; the loser gets 409.
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_len = len(rec.get("history") or [])
    still_unchanged = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
    }
    result = db.attendance_logs.update_one(still_unchanged, {"$set": new_values, "$push": {"history": entry}})
    if result.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently, retry")
    return attendance_to_api({**rec, **new_values, "history": [*(rec.get("history") or []), entry]})


# --------------------------------------------------------------------------- #
# 7. Analytics pipelines
#    Each pipeline is built by a function so the explain endpoint can reuse it unchanged.
#    Analytics read the STORED derived values (late_minutes, half_day, ...), never recompute them.
# --------------------------------------------------------------------------- #
def half_up_expr(expr, places: int):
    """Mongo expression for half-up rounding ($round alone rounds halves to even)."""
    scale = 10 ** places
    noise_free = {"$round": [{"$multiply": [expr, scale]}, 6]}
    return {"$divide": [{"$floor": {"$add": [noise_free, 0.5]}}, scale]}


# Building blocks, evaluated against one attendance_logs document.
IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}  # 1=Sun..7=Sat
PRESENT_ANY_DAY = {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}  # half day = 0.5
PRESENT_WEEKDAY = {"$cond": [IS_WEEKDAY, PRESENT_ANY_DAY, 0]}  # R7: only Mon-Fri count
LATE_FLAG = {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(condition):
    return {"$sum": {"$cond": [condition, 1, 0]}}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": PRESENT_WEEKDAY},
                "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": {"$sum": LATE_FLAG},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = get_employee_or_404(emp_code)
    totals = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)), {})  # no logs -> no row

    # Working days: Mon-Fri from the later of month start and joined_on (R7).
    first, last = month_range(month)
    start = max(first, emp["joined_on"])
    working_days = count_weekdays(date.fromisoformat(start), date.fromisoformat(last)) if start <= last else 0

    present = totals.get("present_days", 0)
    pct = round_half_up(Decimal(str(present)) * 100 / working_days, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees (so people with zero logs still count), then joins each one's logs for the month."""
    first, last = month_range(month)
    employee_filter = {"joined_on": {"$lte": last}}  # R9
    if department is not None:
        employee_filter["department"] = department
    return [
        {"$match": employee_filter},
        {
            # Per-employee partial sums, computed inside the join (an employee with no logs gets m = []).
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": PRESENT_WEEKDAY},
                            "hours_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
                            "hours_n": count_if(HAS_HOURS),
                            "late_count": {"$sum": LATE_FLAG},
                            "late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                            "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                        }
                    },
                ],
                "as": "m",
            }
        },
        {"$set": {"m": {"$first": "$m"}}},  # [] -> missing, [x] -> x
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one per employee, logs or not
                "present_days": {"$sum": {"$ifNull": ["$m.present_days", 0]}},
                "hours_sum": {"$sum": {"$ifNull": ["$m.hours_sum", 0]}},
                "hours_n": {"$sum": {"$ifNull": ["$m.hours_n", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$m.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$m.late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$m.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$m.on_duty_count", 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                # average over records = total hours / number of records
                "avg_work_hours": {
                    "$cond": [{"$gt": ["$hours_n", 0]}, half_up_expr({"$divide": ["$hours_sum", "$hours_n"]}, 2), None]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": LATE_FLAG}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees have no match and are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank gives ties the same rank and skips the next ones (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, so ties at the cutoff all stay
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
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def trend_day_count(date_from: str, date_to: str) -> int:
    """Number of calendar days in the range; 422 if the range is backwards or longer than 92 days."""
    days = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    if days < 1:
        raise HTTPException(422, "'to' must not be before 'from'")
    if days > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return days


def department_trend_pipeline(department: str, date_from: str, date_to: str, days: int) -> list:
    return [
        {"$match": {"department": department, "joined_on": {"$lte": date_to}}},
        {
            "$facet": {
                # Branch 1: per-date totals from the department's logs (only dates that have records).
                "by_date": [
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [
                                {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                                {"$project": {"_id": 0, "date": 1, "present": PRESENT_ANY_DAY, "late": LATE_FLAG}},
                            ],
                            "as": "logs",
                        }
                    },
                    {"$unwind": "$logs"},
                    {"$group": {"_id": "$logs.date", "present": {"$sum": "$logs.present"}, "late": {"$sum": "$logs.late"}}},
                ],
                # Branch 2: join dates of everyone who could be in the headcount.
                "joined": [{"$group": {"_id": None, "dates": {"$push": "$joined_on"}}}],
            }
        },
        {"$set": {"join_dates": {"$ifNull": [{"$first": "$joined.dates"}, []]}}},
        # Gap filling: generate one row per calendar day with $range, so weekends / empty days exist.
        {
            "$set": {
                "rows": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {"$dateFromString": {"dateString": date_from}},
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$rows"},
        {
            "$project": {
                "date": "$rows",
                "stats": {"$first": {"$filter": {"input": "$by_date", "cond": {"$eq": ["$$this._id", "$rows"]}}}},
                "headcount": {"$size": {"$filter": {"input": "$join_dates", "cond": {"$lte": ["$$this", "$rows"]}}}},
                "is_working_day": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$rows"}}}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$stats.present", 0]},
                "late_count": {"$ifNull": ["$stats.late", 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        # 7-day moving average: current row + up to 6 earlier rows inside the range; $avg skips nulls.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$set": {"moving_avg_7d": half_up_expr("$moving_avg_7d", 4)}},  # null stays null
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: DateStr = Query(alias="from"), date_to: DateStr = Query(alias="to")):
    days = trend_day_count(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(department_trend_pipeline(department, date_from, date_to, days)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint
#    Builds each endpoint's main query with the same builder functions as the real endpoint.
# --------------------------------------------------------------------------- #
def require(**params):
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, f"missing query parameter(s): {', '.join(missing)}")


def explain_command(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))  # BSON types -> plain JSON


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    range_from: Optional[DateStr] = Query(None, alias="from"),
    range_to: Optional[DateStr] = Query(None, alias="to"),
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            require(department=department, **{"from": range_from, "to": range_to})
            days = trend_day_count(range_from, range_to)
            collection, pipeline = "employees", department_trend_pipeline(department, range_from, range_to, days)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": explain_command(command)}
