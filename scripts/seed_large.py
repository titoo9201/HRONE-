"""Seeds a large, deliberately awkward dataset (about 100,000 attendance records) for load and
correctness checks.

    MONGO_DB=attendance_scratch python scripts/seed_large.py [--drop-indexes] [--force]

MONGO_URI / MONGO_DB come from the environment (or .env). The script DELETES both collections in that
database first, so it refuses names that don't contain "scratch" or "test" unless you pass --force.

What is in the data (everything is deterministic, seed 2026):
  * 2,300 employees in 10 departments, 2026-06-15 .. 2026-08-25 (72 days), so July 2026 is complete
  * ties: two groups of employees with identical total late minutes (and one with all-zero)
  * weekend records (for some employees), missing weekdays, ABSENT / LEAVE / WFH / ON_DUTY records
  * mid-month joiners (logs only from their joined_on), employees who join after the range
  * overnight-shift employees (22:00-06:00); some punch in after midnight, so the record date is the day before
  * zero-log employees; open punches (no punch_out); records with a history entry
  * legacy documents without `history` / `half_day`
  * logs whose employee does not exist ("ghost" employees), which analytics must ignore
Derived fields are computed with the same helpers as the API, so stored values follow R1-R5.
"""
import argparse
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from pymongo import MongoClient  # noqa: E402

load_dotenv()
from app.main import IST, attendance_date, derived_fields  # noqa: E402  (needs MONGO_* in the environment)

UTC = timezone.utc
FIRST_DAY = date(2026, 6, 15)
N_DAYS = 72
N_EMPLOYEES = 2300
DEPARTMENTS = [f"Dept{i}" for i in range(10)]
TIE_DATES_A = ("2026-07-06", "2026-07-07")  # tie group A: 15 late minutes on each of these two days
TIE_DATE_B = "2026-07-08"                   # tie group B: 30 late minutes on this day


def at_ist(day: date, hhmm: str, minutes: float) -> datetime:
    h, m = map(int, hhmm.split(":"))
    base = datetime(day.year, day.month, day.day, h, m, tzinfo=IST)
    return (base + timedelta(minutes=minutes)).astimezone(UTC).replace(microsecond=0)


def build(n_employees: int = N_EMPLOYEES):
    rnd = random.Random(2026)
    employees, logs, stats = [], [], {}

    def count(key):
        stats[key] = stats.get(key, 0) + 1

    for i in range(n_employees):
        overnight = i % 40 == 3
        zero_log = i % 77 == 5
        mid_joiner = i % 55 == 7
        future_joiner = i % 150 == 11
        if mid_joiner:
            joined = (date(2026, 7, 6) + timedelta(days=rnd.randint(0, 18))).isoformat()
        elif future_joiner:
            joined = "2026-08-20"
        else:
            joined = (date(2025, 1, 1) + timedelta(days=rnd.randint(0, 500))).isoformat()
        emp = {
            "emp_code": f"EMP{100000 + i}", "name": f"Employee {i}", "email": f"emp{i}@example.com",
            "department": DEPARTMENTS[i % 10],
            "shift_start": "22:00" if overnight else "09:30", "shift_end": "06:00" if overnight else "18:30",
            "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        employees.append(emp)
        for flag, name in [(overnight, "overnight"), (zero_log, "zero_log"), (mid_joiner, "mid_month_joiner"),
                           (future_joiner, "joins_after_range")]:
            if flag:
                count(f"employees_{name}")
        if zero_log or future_joiner:
            continue

        tie_a, tie_b = 1000 <= i < 1020, 1020 <= i < 1025
        weekend_worker = i % 9 == 0
        for k in range(N_DAYS):
            day = FIRST_DAY + timedelta(days=k)
            weekday = day.weekday() < 5
            if day.isoformat() < joined:
                continue
            if not weekday and not (weekend_worker and rnd.random() < 0.35):
                continue
            if weekday and rnd.random() < 0.03:
                count("missing_weekdays")
                continue

            if tie_a or tie_b:
                status = "PRESENT"  # the tie groups always work, so their only late minutes are the planted ones
            else:
                status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"], [80, 6, 3, 6, 5])[0]
            p_in = p_out = None
            if status in ("PRESENT", "WFH", "ON_DUTY"):
                late = (not (tie_a or tie_b)) and rnd.random() < 0.3
                offset = rnd.uniform(10.5, 150 if overnight else 90) if late else rnd.uniform(-25, 9.5)
                p_in = at_ist(day, emp["shift_start"], offset) + timedelta(seconds=rnd.randint(0, 59))
                if rnd.random() < 0.03:
                    count("open_punches")
                else:
                    span = rnd.uniform(6, 9.5) if overnight else rnd.uniform(3, 10.5)
                    p_out = p_in + timedelta(hours=span, seconds=rnd.randint(0, 59))
            day_str = attendance_date(p_in, emp["shift_start"], emp["shift_end"]) if p_in else day.isoformat()
            d = derived_fields(emp, day_str, status, p_in, p_out)
            doc = {"emp_code": emp["emp_code"], "date": day_str, "status": status, "punch_in": p_in,
                   "punch_out": p_out, **d, "history": []}
            if tie_a or tie_b:
                doc["late_minutes"] = 15 if (tie_a and day_str in TIE_DATES_A) else 30 if (tie_b and day_str == TIE_DATE_B) else 0
            if p_in and day_str != p_in.astimezone(IST).date().isoformat():
                count("overnight_after_midnight")
            if not weekday:
                count("weekend_records")
            r = rnd.random()
            if r < 0.05:
                del doc["half_day"], doc["history"]
                count("legacy_without_history_half_day")
            elif r < 0.06 and p_in:
                doc["history"] = [{"at": p_in + timedelta(days=1), "by": "hr.admin", "reason": "biometric glitch",
                                   "changes": {"punch_in": {"from": p_in + timedelta(minutes=30), "to": p_in},
                                               "late_minutes": {"from": 40, "to": doc["late_minutes"]}}}]
                count("with_history")
            count(f"status_{status}")
            logs.append(doc)

    # Logs of employees that do not exist; analytics must ignore them.
    for g in ("EMP999998", "EMP999999"):
        for k in range(N_DAYS):
            day = FIRST_DAY + timedelta(days=k)
            logs.append({"emp_code": g, "date": day.isoformat(), "status": "PRESENT",
                         "punch_in": at_ist(day, "09:30", 45), "punch_out": at_ist(day, "18:30", 60),
                         "work_hours": 9.25, "late_minutes": 45, "overtime_minutes": 60, "half_day": False, "history": []})
            count("ghost_logs")
    stats["employees"], stats["attendance_logs"] = len(employees), len(logs)
    return employees, logs, stats


def seed(db, drop_indexes: bool = False) -> dict:
    employees, logs, stats = build()
    db.employees.drop()
    db.attendance_logs.drop()
    db.employees.insert_many(employees)
    for i in range(0, len(logs), 20000):
        db.attendance_logs.insert_many(logs[i:i + 20000], ordered=False)
    if drop_indexes:  # leaves only the default _id index, like the grader's database
        db.employees.drop_indexes()
        db.attendance_logs.drop_indexes()
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--drop-indexes", action="store_true", help="leave only the _id index on both collections")
    ap.add_argument("--force", action="store_true", help="allow a database name without 'scratch' or 'test'")
    args = ap.parse_args()
    name = os.environ["MONGO_DB"]
    if not args.force and not any(w in name for w in ("scratch", "test")):
        sys.exit(f"refusing to wipe '{name}': the name has no 'scratch' or 'test' in it (use --force to override)")
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[name]
    stats = seed(db, args.drop_indexes)
    print(f"seeded database '{name}':")
    for k in sorted(stats):
        print(f"  {k:36s} {stats[k]:>8,}")


if __name__ == "__main__":
    main()
