"""
Employee Attendance & Analytics API (fixed version)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
Requires MongoDB 5.0+ (leaderboard uses $setWindowFields).

Rules used everywhere (single source of truth, see helpers below):
  * Late     : punch-in up to 10 min after shift start = 0.
               After 10 min = FULL minutes from shift start (10:11 -> 11).
  * Overtime : every minute worked after shift end (02:10 vs 02:00 -> 10).
               Overnight shifts (shift_end <= shift_start) end next day.
  * Early out: minutes punched out BEFORE shift end (shortfall), else 0.
  * Work hrs : (punch_out - punch_in) in hours, ROUND_HALF_UP to 2 decimals.
  * Half day : work_hours < 4.5
  * All times stored in UTC, calendar date / shift logic in IST.
"""
import os
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

IST = timezone(timedelta(hours=5, minutes=30))
VALID_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
WORKING_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
GRACE_SECONDS = 600          # 10 minutes
OVERTIME_THRESHOLD_MIN = 1   # 1 = every minute after shift end counts (raise it for a minimum rule)
HALF_DAY_HOURS = 4.5

# MongoDB configuration from environment variables
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

if not MONGO_URI or not MONGO_DB:
    raise RuntimeError(
        "MONGO_URI and MONGO_DB must be set in the environment"
    )

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.1.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)],
                              name="ix_employees_department_emp_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True,
                                    name="uq_attendance_emp_code_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)],
                                    name="ix_attendance_date_emp_code")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str):
    try:
        h, m = map(int, value.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59) or len(value) != 5:
            raise ValueError
        return h, m
    except Exception:
        raise ValueError("time must be HH:MM")


def parse_date(value: str) -> date_cls:
    d = datetime.strptime(value, "%Y-%m-%d").date()
    if d.isoformat() != value:
        raise ValueError("bad date")
    return d


def valid_epoch_ms(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 100_000_000_000


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def to_ms(dt) -> Optional[int]:
    if dt is None:
        return None
    return int(as_utc(dt).timestamp() * 1000)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """Shift 17:00, punch-in 17:11 -> 11. Up to 17:10 -> 0."""
    h, m = parse_hhmm(shift_start)
    d = parse_date(date_str)
    shift_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    elapsed = (as_utc(punch_in) - shift_dt).total_seconds()
    if elapsed <= GRACE_SECONDS:
        return 0
    return int(elapsed // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((as_utc(punch_out) - as_utc(punch_in)).total_seconds()))
    return float((seconds / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def shift_end_dt(shift_start: str, shift_end: str, date_str: str) -> datetime:
    """Shift end as an IST datetime. Overnight shift (17:00 -> 02:00) ends next day."""
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    d = parse_date(date_str)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if (eh, em) <= (sh, sm):
        end += timedelta(days=1)
    return end


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """Shift ends 02:00, punch-out 02:10 -> 10 minutes overtime."""
    end = shift_end_dt(shift_start, shift_end, date_str)
    minutes = int((as_utc(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= max(OVERTIME_THRESHOLD_MIN, 1) else 0


def compute_early_leave(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """Shift ends 02:00, punch-out 01:40 -> 20 minutes short."""
    end = shift_end_dt(shift_start, shift_end, date_str)
    seconds = (end - as_utc(punch_out)).total_seconds()
    return int(seconds // 60) if seconds >= 60 else 0


def ist_text(dt) -> Optional[str]:
    """Human readable IST time, e.g. '2026-10-14 22:48:05'."""
    if dt is None:
        return None
    return as_utc(dt).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def minutes_text(total: int) -> str:
    """348 -> '5 ghante 48 minute'."""
    total = int(total or 0)
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} ghante {m} minute"
    if h:
        return f"{h} ghante"
    return f"{m} minute"


def serialize(doc: dict) -> dict:
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    # readable IST times + minute summaries (epoch ms fields below are kept for the API contract)
    if "punch_in" in doc:
        doc["punch_in_ist"] = ist_text(doc.get("punch_in"))
    if "punch_out" in doc:
        doc["punch_out_ist"] = ist_text(doc.get("punch_out"))
    doc["late_text"] = minutes_text(doc.get("late_minutes", 0))
    doc["overtime_text"] = minutes_text(doc.get("overtime_minutes", 0))
    doc["early_leave_text"] = minutes_text(doc.get("early_leave_minutes", 0))
    if doc.get("work_hours") is not None:
        total_minutes = round(doc["work_hours"] * 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60

        hour_label = "hour" if hours == 1 else "hours"
        doc["work_hours_text"] = f"{hours} {hour_label} {minutes} minutes"
    else:
        doc["work_hours_text"] = None
    for f in ("punch_in", "punch_out", "created_at"):
        if isinstance(doc.get(f), datetime):
            doc[f] = to_ms(doc[f])
    if doc.get("history"):
        doc["history"] = [
            {**h, "at": to_ms(h["at"]) if isinstance(h.get("at"), datetime) else h.get("at")}
            for h in doc["history"]
        ]
    return doc


def month_range(month: str):
    try:
        start = datetime.strptime(month, "%Y-%m").date()
        if start.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="Month must be YYYY-MM")
    nxt = date_cls(start.year + 1, 1, 1) if start.month == 12 else date_cls(start.year, start.month + 1, 1)
    return start, nxt


def present_value(log: dict) -> float:
    if log.get("status") in WORKING_STATUSES:
        return 0.5 if log.get("half_day") else 1.0
    return 0.0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def _t(cls, v):
        parse_hhmm(v)
        return v

    @field_validator("joined_on")
    @classmethod
    def _d(cls, v):
        parse_date(v)
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Health / Employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database connection failed")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0})
                 .sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    for it in items:
        if isinstance(it.get("created_at"), datetime):
            it["created_at"] = to_ms(it["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    if body.punched_at is not None:
        if not valid_epoch_ms(body.punched_at):
            raise HTTPException(status_code=422, detail="punched_at must be epoch milliseconds")
        ts = ms_to_utc(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    local_dt = ts.astimezone(IST)
    local_date = local_dt.date()

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    punch_time = local_dt.strftime("%H:%M")

    if shift_end <= shift_start and punch_time < shift_end:
        d = (local_date - timedelta(days=1)).isoformat()
    else:
        d = local_date.isoformat()

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    no_punch = body.status in ("ABSENT", "LEAVE")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": None if no_punch else ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0 if no_punch else compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "early_leave_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")
    doc["_id"] = res.inserted_id
    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(payload: PunchOutIn):
    emp = db.employees.find_one({"emp_code": payload.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if payload.punched_at is None:
        out_time = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        if not valid_epoch_ms(payload.punched_at):
            raise HTTPException(status_code=422, detail="punched_at must be epoch milliseconds")
        out_time = ms_to_utc(payload.punched_at)

    att = db.attendance_logs.find_one(
        {"emp_code": payload.emp_code, "punch_out": None,
         "punch_in": {"$ne": None, "$lte": out_time}},
        sort=[("punch_in", -1)],
    )
    if not att:
        raise HTTPException(status_code=404, detail="No open punch-in record found")

    in_time = as_utc(att["punch_in"])
    if out_time <= in_time:
        raise HTTPException(status_code=422, detail="Punch-out must be later than punch-in")
    if (out_time - in_time).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="Punch-out cannot be over 24 hours after punch-in")

    work_hours = compute_work_hours(in_time, out_time)
    overtime = compute_overtime(out_time, emp["shift_start"], emp["shift_end"], att["date"])
    early_leave = compute_early_leave(out_time, emp["shift_start"], emp["shift_end"], att["date"])
    half_day = work_hours < HALF_DAY_HOURS

    res = db.attendance_logs.update_one(
        {"_id": att["_id"], "punch_out": None},
        {"$set": {"punch_out": out_time, "work_hours": work_hours,
                  "overtime_minutes": overtime, "early_leave_minutes": early_leave,
                  "half_day": half_day}},
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=409, detail="Attendance record already updated")

    return {
        "emp_code": payload.emp_code,
        "date": att["date"],
        "status": att["status"],
        "punch_in": to_ms(in_time),
        "punch_out": to_ms(out_time),
        "punch_in_ist": ist_text(in_time),
        "punch_out_ist": ist_text(out_time),
        "work_hours": work_hours,
        "late_minutes": att.get("late_minutes", 0),
        "late_duration": minutes_text(att.get("late_minutes", 0)),
        "overtime_minutes": overtime,
        "overtime_text": minutes_text(overtime),
        "early_leave_minutes": early_leave,
        "early_leave_text": minutes_text(early_leave),
        "half_day": half_day,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    for v in (date_from, date_to):
        if v is not None:
            try:
                parse_date(v)
            except ValueError:
                raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)
    cursor = (db.attendance_logs.find(query)
              .sort([("date", -1), ("emp_code", 1)])
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, payload: RegularizeRequest):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        parse_date(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Date must use YYYY-MM-DD format")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    data = payload.model_dump(exclude_unset=True)
    data.pop("reason", None)
    data.pop("regularized_by", None)

    if "status" in data and data["status"] not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    changes, update = {}, {}
    for field in ("status", "punch_in", "punch_out"):
        if field not in data:
            continue
        value = data[field]
        if field != "status" and value is not None:
            if not valid_epoch_ms(value):
                raise HTTPException(status_code=422, detail=f"{field} must be epoch milliseconds")
            value = ms_to_utc(value)
        old = record.get(field)
        same = (to_ms(old) == to_ms(value)) if field != "status" else old == value
        if not same:
            changes[field] = {
                "from": old.isoformat() if isinstance(old, datetime) else old,
                "to": value.isoformat() if isinstance(value, datetime) else value,
            }
            update[field] = value

    if not changes:
        raise HTTPException(status_code=422, detail="No changes provided")

    status = update.get("status", record.get("status"))
    p_in = update.get("punch_in", record.get("punch_in"))
    p_out = update.get("punch_out", record.get("punch_out"))

    if status in ("ABSENT", "LEAVE"):
        if data.get("punch_in") is not None or data.get("punch_out") is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot have punch times")
        p_in = p_out = None
        update["punch_in"] = None
        update["punch_out"] = None
    elif p_in is None:
        raise HTTPException(status_code=422, detail="Punch-in is required")

    if p_in is not None and p_out is not None:
        p_in, p_out = as_utc(p_in), as_utc(p_out)
        if p_out <= p_in:
            raise HTTPException(status_code=422, detail="Punch-out must be later than punch-in")
        if (p_out - p_in).total_seconds() > 86400:
            raise HTTPException(status_code=422,
                                detail="Punch-out cannot be over 24 hours after punch-in")

    # Recalculate derived fields with the SAME helpers as punch-in / punch-out
    if p_in is None:
        update.update({"late_minutes": 0, "work_hours": None, "overtime_minutes": 0,
                       "early_leave_minutes": 0, "half_day": False})
    else:
        update["late_minutes"] = compute_late_minutes(p_in, employee["shift_start"], date)
        if p_out is not None:
            wh = compute_work_hours(p_in, p_out)
            update["work_hours"] = wh
            update["overtime_minutes"] = compute_overtime(
                p_out, employee["shift_start"], employee["shift_end"], date)
            update["early_leave_minutes"] = compute_early_leave(
                p_out, employee["shift_start"], employee["shift_end"], date)
            update["half_day"] = wh < HALF_DAY_HOURS
        else:
            update.update({"work_hours": None, "overtime_minutes": 0,
                           "early_leave_minutes": 0, "half_day": False})

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": payload.regularized_by,
        "reason": payload.reason,
        "changes": changes,
    }
    res = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": update, "$push": {"history": history_entry}},
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=409, detail="Record changed concurrently; retry")

    return serialize(db.attendance_logs.find_one({"_id": record["_id"]}))


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    month_start, nxt = month_range(month)
    month_end = nxt - timedelta(days=1)
    
    joined_on = parse_date(employee["joined_on"])
    day, working_days = max(month_start, joined_on), 0
    while day <= month_end:
        if day.weekday() < 5:
            working_days += 1
        day += timedelta(days=1)

    logs = db.attendance_logs.find({
        "emp_code": emp_code,
        "date": {"$gte": month_start.isoformat(), "$lt": nxt.isoformat()},
    })
    present, leave, late = 0.0, 0, 0
    for r in logs:
        if parse_date(r["date"]).weekday() < 5:
            present += present_value(r)
        if r.get("status") == "LEAVE":
            leave += 1
        if r.get("late_minutes", 0) > 0:
            late += 1

    return {
        "emp_code": emp_code, "name": employee["name"],
        "department": employee["department"], "month": month,
        "working_days": working_days, "present_days": present,
        "leave_days": leave, "late_count": late,
        "attendance_pct": round(present / working_days * 100, 4) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month_start, nxt = month_range(month)
    month_end = nxt - timedelta(days=1)

    ef = {"joined_on": {"$lte": month_end.isoformat()}}
    if department:
        ef["department"] = department
    employees = list(db.employees.find(ef, {"_id": 0}))
    dept_of = {e["emp_code"]: e["department"] for e in employees}

    result = {}
    for e in employees:
        result.setdefault(e["department"], {
            "department": e["department"], "headcount": 0, "present_days": 0.0,
            "hours": [], "late_count": 0, "total_late_minutes": 0,
            "leave_count": 0, "on_duty_count": 0})
        result[e["department"]]["headcount"] += 1

    logs = db.attendance_logs.find({
        "emp_code": {"$in": list(dept_of)},
        "date": {"$gte": month_start.isoformat(), "$lt": nxt.isoformat()},
    })
    for log in logs:
        r = result[dept_of[log["emp_code"]]]
        if parse_date(log["date"]).weekday() < 5:
            r["present_days"] += present_value(log)
        if log.get("status") in WORKING_STATUSES and log.get("work_hours") is not None:
            r["hours"].append(log["work_hours"])      # average over ALL logs, not avg-of-avgs
        late = log.get("late_minutes", 0)
        if late > 0:
            r["late_count"] += 1
            r["total_late_minutes"] += late
        if log.get("status") == "LEAVE":
            r["leave_count"] += 1
        if log.get("status") == "ON_DUTY":
            r["on_duty_count"] += 1

    out = []
    for dept in sorted(result):
        r = result[dept]
        out.append({
            "department": dept, "headcount": r["headcount"],
            "present_days": r["present_days"],
            "avg_work_hours": round(sum(r["hours"]) / len(r["hours"]), 2) if r["hours"] else None,
            "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"],
        })
    return out


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    month_start, nxt = month_range(month)
    if limit < 1:
        raise HTTPException(status_code=422, detail="Limit must be positive")

    pipeline = [
        {"$match": {"date": {"$gte": month_start.isoformat(), "$lt": nxt.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},   # ties share a rank
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department",
                      "total_late_minutes": 1, "rank": 1}},
        {"$sort": {"rank": 1, "emp_code": 1}},
    ])
    return list(db.attendance_logs.aggregate(pipeline))


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    try:
        start, end = parse_date(from_date), parse_date(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must use YYYY-MM-DD")
    if end < start:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
    if (end - start).days > 91:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    employees = list(db.employees.find({"department": department},
                                       {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(status_code=404, detail="Department not found")

    codes = [e["emp_code"] for e in employees]
    # attendance_logs has NO "department" field -> filter by emp_code instead
    logs_by_day = {}
    for log in db.attendance_logs.find({
        "emp_code": {"$in": codes},
        "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
    }):
        logs_by_day.setdefault(log["date"], []).append(log)

    rows = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        day = d.isoformat()
        headcount = sum(1 for e in employees if e["joined_on"] <= day)
        logs = logs_by_day.get(day, [])
        present = sum(present_value(l) for l in logs)
        late = sum(1 for l in logs if l.get("late_minutes", 0) > 0)
        working = d.weekday() < 5
        rows.append({
            "date": day, "is_working_day": working, "headcount": headcount,
            "present_count": present, "late_count": late,
            "attendance_rate": round(present / headcount * 100, 4)
            if working and headcount > 0 else None,
        })

    for i, row in enumerate(rows):
        window = [r["attendance_rate"] for r in rows[max(0, i - 6):i + 1]
                  if r["attendance_rate"] is not None]
        row["moving_avg_7d"] = round(sum(window) / len(window), 4) if window else None
    return rows


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, month: Optional[str] = None,
                  department: Optional[str] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None,
                  emp_code: Optional[str] = None,
                  page: int = 1, page_size: int = 20, limit: int = 10):
    allowed = {"attendance_list", "employee_monthly", "department_summary",
               "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(status_code=422, detail="Invalid endpoint")
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="Invalid pagination")

    def dept_codes(dep):
        return [e["emp_code"] for e in db.employees.find({"department": dep}, {"emp_code": 1})]

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="Invalid date range")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        elif department:
            q["emp_code"] = {"$in": dept_codes(department)}
        if date_from:
            q.setdefault("date", {})["$gte"] = date_from
        if date_to:
            q.setdefault("date", {})["$lte"] = date_to
        exp = (db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)])
               .skip((page - 1) * page_size).limit(page_size).explain())
        coll = "attendance_logs"

    elif endpoint in ("employee_monthly", "late_leaderboard"):
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        s, n = month_range(month)
        q = {"date": {"$gte": s.isoformat(), "$lt": n.isoformat()}}
        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(status_code=422, detail="emp_code is required")
            q["emp_code"] = emp_code
        else:
            q["late_minutes"] = {"$gt": 0}
        exp = db.attendance_logs.find(q).explain()
        coll = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        s, n = month_range(month)
        q = {"joined_on": {"$lte": (n - timedelta(days=1)).isoformat()}}
        if department:
            q["department"] = department
        exp = db.employees.find(q).explain()
        coll = "employees"

    else:  # department_trend
        if not department or not date_from or not date_to:
            raise HTTPException(status_code=422,
                                detail="department, date_from and date_to are required")
        exp = db.attendance_logs.find({
            "emp_code": {"$in": dept_codes(department)},
            "date": {"$gte": date_from, "$lte": date_to},
        }).explain()
        coll = "attendance_logs"

    return {"endpoint": endpoint, "collection": coll, "explain": exp}