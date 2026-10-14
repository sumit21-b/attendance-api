"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Layout of this single file:
  1. config / client          5. request models
  2. time + rule helpers      6. serializers
  3. validation helpers       7. query / pipeline builders (shared by endpoints AND explain)
  4. indexes (startup)        8. endpoints
"""
import calendar
import json
import os
import re
import time as _time
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config / client
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables take priority

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# tz_aware=True: PyMongo returns aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
       serverSelectionTimeoutMS=20000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
# strict=True: floats, strings and bools are rejected (422); range rejects "seconds" values.
EpochMs = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]


# --------------------------------------------------------------------------- #
# 4. Indexes (created idempotently at startup)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # employees
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in range(5):  # tolerate a database that is a few seconds late
        try:
            ensure_indexes()
            break
        except PyMongoError:
            if attempt == 4:
                break
            _time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time + business-rule helpers  (R1 - R5)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    d = as_utc(dt) - EPOCH
    return d.days * 86_400_000 + d.seconds * 1000 + d.microseconds // 1000


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of the punch-in; overnight shifts: an early-morning punch-in belongs to the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time().replace(microsecond=0) < time(*hm(shift_end)):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    d = date.fromisoformat(date_str)
    h, m = hm(shift_start)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    d = date.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)
    h, m = hm(shift_end)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; minutes counted from shift start (floor)."""
    secs = int((punch_in - shift_start_dt(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if >= 30."""
    secs = int((punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    mins = secs // 60
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, not float round())."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5: rounded work_hours < 4.50."""
    return Decimal(str(work_hours)) < Decimal("4.50")


def derived_fields(emp: dict, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All four stored derived values for a record (used by punch-in, punch-out and PATCH)."""
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, date_str, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"])
        out["half_day"] = compute_half_day(wh)
    return out


# --------------------------------------------------------------------------- #
# 3. Validation helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
HM_RE = r"^([01]\d|2[0-3]):[0-5]\d$"


def fail(loc: list, msg: str):
    """Raise a FastAPI-default 422 body."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_date(value: str, loc: list) -> date:
    if not DATE_RE.match(value):
        fail(loc, "must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(loc, "not a valid calendar date")


def month_bounds(month: str, loc=("query", "month")):
    """-> (first_day_str, last_day_str, first_day_date, last_day_date)."""
    if not MONTH_RE.match(month) or int(month[:4]) < 1:
        fail(list(loc), "must be YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    return first.isoformat(), last.isoformat(), first, last


def working_days(first: date, last: date, joined_on: str) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last."""
    start = max(first, date.fromisoformat(joined_on))
    n, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 5. Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HM_RE)
    shift_end: str = Field(default="18:30", pattern=HM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
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
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields sent by a client are simply ignored (pydantic drops unknown keys)


# --------------------------------------------------------------------------- #
# 6. Serializers (BSON -> API)
# --------------------------------------------------------------------------- #
def ser_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d["name"], "email": d["email"], "department": d["department"],
        "shift_start": d["shift_start"], "shift_end": d["shift_end"], "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


def _ser_change(field: str, v):
    return dt_to_ms(v) if field in ("punch_in", "punch_out") else v


def ser_attendance(d: dict) -> dict:
    history = []
    for h in d.get("history") or []:
        history.append({
            "at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
            "changes": {
                k: {"from": _ser_change(k, v.get("from")), "to": _ser_change(k, v.get("to"))}
                for k, v in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")), "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 7. Query / pipeline builders - used by the real endpoints AND /admin/explain
# --------------------------------------------------------------------------- #
ATT_SORT = {"date": -1, "emp_code": 1}


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size):
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    f = parse_date(date_from, ["query", "date_from"]) if date_from else None
    t = parse_date(date_to, ["query", "date_to"]) if date_to else None
    if f and t and f > t:
        fail(["query", "date_from"], "date_from must not be after date_to")
    if f or t:
        q["date"] = {}
        if f:
            q["date"]["$gte"] = f.isoformat()
        if t:
            q["date"]["$lte"] = t.isoformat()
    if status:
        q["status"] = status
    return q, (page - 1) * page_size, page_size


# shared aggregation expressions
IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None}}}, [2, 3, 4, 5, 6]]}
LATE = {"$ifNull": ["$late_minutes", 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
# present value DOUBLED (full day 2, half day 1) so sums stay exact integers
PRESENT2_ANYDAY = {"$cond": [IS_PRESENT, {"$cond": [IS_HALF, 1, 2]}, 0]}
PRESENT2_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, {"$cond": [IS_HALF, 1, 2]}, 0]}
ONE_IF = lambda cond: {"$cond": [cond, 1, 0]}  # noqa: E731


def build_monthly_pipeline(emp_code: str, month: str):
    first, last, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "p2": {"$sum": PRESENT2_WEEKDAY},
            "leave_days": {"$sum": ONE_IF({"$eq": ["$status", "LEAVE"]})},
            "late_count": {"$sum": ONE_IF({"$gt": [LATE, 0]})},
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": OVERTIME},
        }},
    ]


def build_summary_pipeline(month: str, department: Optional[str]):
    first, last, _, _ = month_bounds(month)
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "p2": {"$sum": PRESENT2_WEEKDAY},
            # work_hours in whole hundredths so sums/means are exact (no float drift)
            "hours_h": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "hours_n": {"$sum": ONE_IF(has_hours)},
            "late_count": {"$sum": ONE_IF({"$gt": [LATE, 0]})},
            "late_minutes": {"$sum": LATE},
            "leave_count": {"$sum": ONE_IF({"$eq": ["$status", "LEAVE"]})},
            "on_duty_count": {"$sum": ONE_IF({"$eq": ["$status", "ON_DUTY"]})},
        }},
    ]
    emp_match: dict = {"joined_on": {"$lte": last}}  # R9
    if department:
        emp_match["department"] = department
    s = lambda f: {"$sum": {"$ifNull": [f"$s.{f}", 0]}}  # noqa: E731
    return [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "s"}},
        # keep employees that have no logs at all
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1}, "p2": s("p2"), "hours_h": s("hours_h"),
            "hours_n": s("hours_n"), "late_count": s("late_count"), "late_minutes": s("late_minutes"),
            "leave_count": s("leave_count"), "on_duty_count": s("on_duty_count"),
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$divide": ["$p2", 2]},
            # mean of hundredths, rounded half-up with integer arithmetic: floor((2S+n)/(2n))
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_n", 0]}, {"$divide": [
                {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$hours_h"]}, "$hours_n"]},
                                        {"$multiply": [2, "$hours_n"]}]}}, 100]}, None]},
            "late_count": 1, "total_late_minutes": "$late_minutes",
            "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]):
    first, last, _, _ = month_bounds(month)
    pipe = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department:
        pipe.append({"$match": {"e.department": department}})
    pipe += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def build_trend_pipeline(department: str, frm: date, to: date):
    n = (to - frm).days + 1
    f, t = frm.isoformat(), to.isoformat()
    from_dt = datetime(frm.year, frm.month, frm.day, tzinfo=UTC)
    daily = [
        {"$match": {"date": {"$gte": f, "$lte": t}, "$expr": {"$in": ["$emp_code", "$$codes"]}}},
        {"$group": {"_id": "$date", "p2": {"$sum": PRESENT2_ANYDAY}, "late": {"$sum": ONE_IF({"$gt": [LATE, 0]})}}},
    ]
    window = {"documents": [-6, 0]}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joins": {"$push": "$joined_on"}, "codes": {"$push": "$emp_code"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"codes": "$codes"}, "pipeline": daily, "as": "daily"}},
        # one row per calendar day, generated inside the database (gap filling)
        {"$project": {"_id": 0, "joins": 1, "daily": 1, "days": {"$map": {
            "input": {"$range": [0, n]}, "as": "i",
            "in": {"$dateToString": {"format": "%Y-%m-%d",
                                     "date": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$i"}}}}}}}},
        {"$unwind": "$days"},
        {"$project": {
            "date": "$days",
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$days"}}},
            "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$days"]}}}},  # R9
            "d": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$days"]}}}, 0]},
        }},
        {"$set": {
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "p2": {"$ifNull": ["$d.p2", 0]}, "late_count": {"$ifNull": ["$d.late", 0]},
        }},
        # rate in 1/10000 units, half-up with integers: floor((10000*p2 + H) / (2H))
        {"$set": {"rate_k": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                        {"$floor": {"$divide": [{"$add": [{"$multiply": [10000, "$p2"]}, "$headcount"]},
                                                                {"$multiply": [2, "$headcount"]}]}}, None]}}},
        {"$set": {"nn": {"$cond": [{"$eq": ["$rate_k", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "ks": {"$sum": "$rate_k", "window": window}, "kn": {"$sum": "$nn", "window": window}}}},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]}, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_k", None]}, None, {"$divide": ["$rate_k", 10000]}]},
            # mean of the (rounded) daily rates, half-up: floor((2*sum + n) / (2n))
            "moving_avg_7d": {"$cond": [{"$gt": ["$kn", 0]}, {"$divide": [
                {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$ks"]}, "$kn"]}, {"$multiply": [2, "$kn"]}]}},
                10000]}, None]},
        }},
    ]


def parse_trend_range(frm: str, to: str):
    f = parse_date(frm, ["query", "from"])
    t = parse_date(to, ["query", "to"])
    if t < f:
        fail(["query", "to"], "to must not be before from")
    if (t - f).days + 1 > 92:
        fail(["query", "to"], "range must not exceed 92 days")
    return f, t


# --------------------------------------------------------------------------- #
# 8. Endpoints
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
        employees.insert_one(doc)  # uniqueness is enforced by the unique index, not by a prior read
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    cur = employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [ser_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": d, "status": body.status,
        "punch_in": ts, "punch_out": None, "history": [],
        **derived_fields(emp, d, ts, None),
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = as_utc(rec["punch_in"])
    if ts <= pin:
        fail(["body", "punched_at"], "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        fail(["body", "punched_at"], "punched_at is more than 24 hours after punch_in")
    fields = derived_fields(emp, rec["date"], pin, ts)
    fields["punch_out"] = ts
    # atomic compare-and-set: only succeeds while the record is still open
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return ser_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q, skip, limit = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q)
    cur = logs.find(q, {"_id": 0}).sort(list(ATT_SORT.items())).skip(skip).limit(limit)
    return {"items": [ser_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, ["path", "date"])
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            fail(["body"], "punch_in/punch_out cannot be supplied with status ABSENT or LEAVE")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            fail(["body", "punch_in"], "a presence status requires a punch_in")
        new_in = as_utc(new_in)
        new_out = as_utc(new_out) if new_out is not None else None
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            fail(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            fail(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

    # "changes nothing" is judged on the fields the client can change
    if (new_status == old_status and dt_to_ms(new_in) == dt_to_ms(old_in) and dt_to_ms(new_out) == dt_to_ms(old_out)):
        fail(["body"], "request changes nothing")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(emp, date, new_in, new_out)}
    old = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
           "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {}
    for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        a, b = old[k], new[k]
        if k in ("punch_in", "punch_out"):
            if dt_to_ms(a) != dt_to_ms(b):
                changes[k] = {"from": a, "to": b}
        elif a != b:
            changes[k] = {"from": a, "to": b}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    n_hist = len(rec.get("history") or [])
    # optimistic concurrency: only apply if nobody changed history / times since we read the record
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_in": old_in, "punch_out": old_out, "status": old_status,
         "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]}},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return ser_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first, last, first_d, last_d = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rows = list(logs.aggregate(build_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    p2 = int(r.get("p2", 0))
    wd = working_days(first_d, last_d, emp["joined_on"])
    pct = None
    if wd > 0:  # present/wd*100 half-up to 2 decimals using exact integers
        pct = ((10000 * p2 + wd) // (2 * wd)) / 100
    return {
        "emp_code": emp_code, "month": month, "working_days": wd, "present_days": p2 / 2,
        "leave_days": int(r.get("leave_days", 0)), "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    month_bounds(month)
    items = list(employees.aggregate(build_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_bounds(month)
    items = list(logs.aggregate(build_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    f, t = parse_trend_range(from_, to)
    rows = list(employees.aggregate(build_trend_pipeline(department, f, t)))
    if not rows:  # the pipeline starts from the department's employees: none -> unknown department
        raise HTTPException(404, "department not found")
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(value, name):
        if value is None or value == "":
            fail(["query", name], "field required")

    if endpoint == "attendance_list":
        q, skip, lim = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        inner = {"find": coll, "filter": q, "sort": ATT_SORT, "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            need(emp_code, "emp_code"); need(month, "month")
            coll, pipe = "attendance_logs", build_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            need(month, "month")
            coll, pipe = "employees", build_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            need(month, "month")
            coll, pipe = "attendance_logs", build_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            need(department, "department"); need(from_, "from"); need(to, "to")
            f, t = parse_trend_range(from_, to)
            coll, pipe = "employees", build_trend_pipeline(department, f, t)
        inner = {"aggregate": coll, "pipeline": pipe, "cursor": {}}

    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(result))}
