"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load .env without overriding OS-level environment variables
load_dotenv(override=False)

logger = logging.getLogger("uvicorn.error")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Lifespan / Startup: Idempotent Index Creation (No Swallowing Errors)
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently."""
    # employees indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.employees.create_index([("department", ASCENDING)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_indexes()
        logger.info("Database indexes created/verified idempotently.")
    except Exception as e:
        logger.error(f"Failed to create database indexes at startup: {e}")
        raise
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_epoch_millis(ms: int) -> datetime:
    # Truncate instant to whole seconds before calculation / storage (R1)
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=UTC)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def compute_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is earlier than shift_end belongs to the shift
    that started the previous day.
    """
    punch_time_str = punch_in_ist.strftime("%H:%M")
    if is_overnight_shift(shift_start, shift_end):
        if punch_time_str < shift_end:
            prev_day = (punch_in_ist.date() - timedelta(days=1)).isoformat()
            return prev_day
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (600 seconds) after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start,
    not since grace ended. Otherwise 0.
    """
    sh, sm = parse_hhmm(shift_start)
    d = datetime.fromisoformat(attendance_date).date()
    start_dt = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)
    diff_seconds = (punch_in_ist - start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    secs = int((punch_out - punch_in).total_seconds())
    val = (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(val)


def compute_overtime(
    punch_out_ist: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out,
    counted only if that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on next calendar day.
    """
    eh, em = parse_hhmm(shift_end)
    d = datetime.fromisoformat(attendance_date).date()
    if is_overnight_shift(shift_start, shift_end):
        d = d + timedelta(days=1)
    end_dt = datetime(d.year, d.month, d.day, eh, em, 0, tzinfo=IST)
    diff_seconds = (punch_out_ist - end_dt).total_seconds()
    if diff_seconds >= 1800:
        return int(diff_seconds // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not history:
        return []
    result = []
    for entry in history:
        e = {
            "at": to_epoch_millis(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": {},
        }
        changes = entry.get("changes", {})
        for field, change in changes.items():
            f_val = change.get("from")
            t_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(f_val, datetime):
                    f_val = to_epoch_millis(f_val)
                if isinstance(t_val, datetime):
                    t_val = to_epoch_millis(t_val)
            e["changes"][field] = {"from": f_val, "to": t_val}
        result.append(e)
    return result


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history")),
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_millis(doc.get("created_at")),
    }


def get_month_date_range(month_str: str) -> tuple[str, str, int, int, int]:
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    return f"{month_str}-01", f"{month_str}-{num_days:02d}", year, month, num_days


# --------------------------------------------------------------------------- #
# Pipeline Builders (Used by Analytics and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$lte": [
                        {
                            "$isoDayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        5,
                    ]
                },
                "is_presence": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
                "half_day": {"$ifNull": ["$half_day", False]},
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_presence"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    month_start: str, month_end: str, department: Optional[str] = None
) -> list:
    match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date",
                                                                "format": "%Y-%m-%d",
                                                            }
                                                        }
                                                    },
                                                    5,
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$gt": ["$$log.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "emp_leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {"$ne": ["$$log.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": "$$log.work_hours",
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$and": [
                                    {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$$log.work_hours", None]},
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_records": {"$sum": "$emp_work_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "total_work_hours": 1,
                "total_work_records": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list:
    pipeline: list = [
        # Stage 1: Match logs in target month with late_minutes > 0
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        # Stage 2: Group by emp_code and calculate totals
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Stage 3: Lookup employee name and department
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # Stage 4: Drop logs whose employee does not exist
        {"$unwind": "$emp"},
    ]

    # Stage 5: Optional department filter before ranking
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        # Stage 6: Project fields
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        # Stage 7: Standard competition ranking (1, 2, 2, 4) using $rank
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        # Stage 8: Filter rank <= limit (can return more than limit rows on ties)
        {"$match": {"rank": {"$lte": limit}}},
        # Stage 9: Order by total_late_minutes DESC, emp_code ASC
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        # Stage 10: Final projection
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(
    department: str, date_from: str, date_to: str
) -> list:
    from_dt = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
    to_dt = datetime.fromisoformat(date_to).replace(tzinfo=UTC)
    to_dt_next = to_dt + timedelta(days=1)

    return [
        # Stage 1: Match date range in attendance_logs
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to}
            }
        },
        # Stage 2: Join employees to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        # Stage 3: Group by date to calculate daily present_count and late_count
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        # Stage 4: Project to BSON date for densification
        {
            "$project": {
                "_id": 0,
                "date_dt": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Stage 5: Union a seed document from employees to ensure non-empty input for $densify
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": {"$literal": from_dt},
                            "present_count": {"$literal": None},
                            "late_count": {"$literal": None},
                        }
                    },
                ],
            }
        },
        # Stage 6: Densify to fill missing dates in the range
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_next],
                },
            }
        },
        # Stage 7: Group by date_dt to merge seed with actual attendance data
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        # Stage 8: Convert back to YYYY-MM-DD string, handle null defaults, compute is_working_day
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "date": "$_id",
                        "format": "%Y-%m-%d",
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": "$_id"},
                        5,
                    ]
                },
            }
        },
        # Stage 9: Lookup headcount (department employees joined_on <= that day)
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        # Stage 10: Extract headcount and compute attendance_rate
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # Stage 11: Moving average over preceding 6 days and current day (inside range)
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        # Stage 12: Sort and project final shape
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


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    # Concurrency safe via unique index on emp_code
    now = datetime.now(timezone.utc)
    # Truncate to whole seconds
    now = now.replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        dt_utc = from_epoch_millis(body.punched_at)
    else:
        # Default to current time, truncated to whole seconds
        now = datetime.now(timezone.utc).replace(microsecond=0)
        dt_utc = now

    dt_ist = to_ist(dt_utc)
    date_str = compute_attendance_date(dt_ist, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(dt_ist, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = from_epoch_millis(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_dt = record.get("punch_in")
    if punch_in_dt is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    # 422 if punched_at <= punch_in or more than 24 hours after it
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if punch_out_dt > punch_in_dt + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    # 409 if that record is already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    # Compute derived metrics
    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    punch_out_ist = to_ist(punch_out_dt)
    overtime_min = compute_overtime(punch_out_ist, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    # Atomic update: exactly one concurrent request gets 200, loser gets 409
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated_doc is None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return serialize_attendance_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    q = build_attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    # Must sort in database: date DESC, emp_code ASC
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    # Validation rules:
    # Setting status ABSENT or LEAVE clears both punch times; supplying punch times with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="Cannot supply punch times when setting ABSENT or LEAVE")

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = bool(record.get("half_day", False))

    if body.status in ("ABSENT", "LEAVE"):
        new_status = body.status
        new_punch_in = None
        new_punch_out = None
    else:
        new_status = body.status if body.status is not None else old_status
        new_punch_in = from_epoch_millis(body.punch_in) if body.punch_in is not None else old_punch_in
        new_punch_out = from_epoch_millis(body.punch_out) if body.punch_out is not None else old_punch_out

    # A presence status requires a punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and new_punch_in is None:
        raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

    # punch_in must stay on the record's attendance date (R1)
    if new_punch_in is not None:
        calc_date = compute_attendance_date(to_ist(new_punch_in), emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    # punch_out must be after punch_in and within 24 hours
    if new_punch_out is not None:
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        if new_punch_out <= new_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if new_punch_out > new_punch_in + timedelta(hours=24):
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(to_ist(new_punch_in), emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(to_ist(new_punch_out), emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Determine what actually changed
    changes: Dict[str, Any] = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    # A request that changes nothing is 422
    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    # Prepare audit trail history entry
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency safe: check history length matches current so concurrent edits do not lose history
    current_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(current_history)},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start, month_end, year, m, num_days = get_month_date_range(month)

    # Calculate working_days (R7)
    emp_joined = date.fromisoformat(emp["joined_on"])
    end_dt = date(year, m, num_days)
    if emp_joined > end_dt:
        working_days = 0
    else:
        effective_start = max(date(year, m, 1), emp_joined)
        working_days = sum(
            1
            for day_offset in range((end_dt - effective_start).days + 1)
            if (effective_start + timedelta(days=day_offset)).weekday() < 5
        )

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        # R8: 2 decimals half-up
        raw_pct = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end, _, _, _ = get_month_date_range(month)
    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in results:
        total_records = doc.get("total_work_records", 0)
        total_hours = doc.get("total_work_hours", 0.0)

        if total_records > 0:
            raw_avg = Decimal(str(total_hours)) / Decimal(total_records)
            avg_work_hours = float(raw_avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            avg_work_hours = None

        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_work_hours,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end, _, _, _ = get_month_date_range(month)
    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dep_count = db.employees.count_documents({"department": department})
    if dep_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must not be earlier than from")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="date range must not exceed 92 days")

    pipeline = build_department_trend_pipeline(department, date_from, date_to)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for doc in results:
        rate = doc.get("attendance_rate")
        m_avg = doc.get("moving_avg_7d")

        if rate is not None:
            rate = float(
                Decimal(str(rate)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            )
        if m_avg is not None:
            m_avg = float(
                Decimal(str(m_avg)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            )

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(doc.get("present_count", 0.0)),
            "late_count": int(doc.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs MongoDB's explain with executionStats verbosity for the main query or
    pipeline of the named endpoint, built exactly as that endpoint builds it.
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")

    month_regex = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
    date_regex = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    if endpoint == "attendance_list":
        if date_from and not date_regex.match(date_from):
            raise HTTPException(status_code=422, detail="Invalid date_from")
        if date_to and not date_regex.match(date_to):
            raise HTTPException(status_code=422, detail="Invalid date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month or not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Valid month is required (YYYY-MM)")

        month_start, month_end, _, _, _ = get_month_date_range(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month or not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Valid month is required (YYYY-MM)")

        month_start, month_end, _, _, _ = get_month_date_range(month)
        pipeline = build_department_summary_pipeline(month_start, month_end, department)
        cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month or not month_regex.match(month):
            raise HTTPException(status_code=422, detail="Valid month is required (YYYY-MM)")

        month_start, month_end, _, _, _ = get_month_date_range(month)
        pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        # May accept department via query or param
        trend_dep = department
        trend_from = from_date or date_from
        trend_to = to_date or date_to

        if not trend_dep:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not trend_from or not date_regex.match(trend_from):
            raise HTTPException(status_code=422, detail="Valid 'from' date is required (YYYY-MM-DD)")
        if not trend_to or not date_regex.match(trend_to):
            raise HTTPException(status_code=422, detail="Valid 'to' date is required (YYYY-MM-DD)")

        d_from = date.fromisoformat(trend_from)
        d_to = date.fromisoformat(trend_to)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to must not be earlier than from")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="date range must not exceed 92 days")

        pipeline = build_department_trend_pipeline(trend_dep, trend_from, trend_to)
        cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        explain_res = db.command(cmd)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }
