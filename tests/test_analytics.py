"""Analytics tests. A small hand-made dataset (March 2031, chosen so it never collides with
other data) is inserted straight into MongoDB, then every endpoint is compared with
 (a) numbers worked out by hand (the HAND_* constants), and
 (b) a deliberately naive pure-Python reference (loops, Decimal), so a mistake in the
     pipelines can't hide behind a mistake in the expectation.
Needs a running API (API_URL, default http://localhost:8000); skipped otherwise.

Calendar facts used below (2031-03): the 1st is a Saturday, so Mon-Fri days are
3-7, 10-14, 17-21, 24-28 and 31 = 21 working days; from the 17th on there are 11.
"""
import os
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.environ.get("API_URL", "http://localhost:8000")
MONTH = "2031-03"
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


@pytest.fixture(scope="module")
def api():
    c = httpx.Client(base_url=BASE, timeout=30)
    try:
        c.get("/health").raise_for_status()
    except Exception:
        pytest.skip(f"API not running at {BASE}")
    return c


@pytest.fixture(scope="module")
def ds():
    """Insert the dataset, yield it, remove it again."""
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
    tag = random.randint(100, 999)
    D, D2, D3, D4 = f"T{tag}-main", f"T{tag}-quiet", f"T{tag}-later", f"T{tag}-tie"
    names = ["A1", "A2", "A3", "J", "N", "Z", "G", "F", "GHOST", "T1", "T2", "T3"]
    codes = sorted(random.sample(range(700000, 799999), len(names)))
    c = {name: f"EMP{n}" for name, n in zip(names, codes)}
    # sorted codes: A1 < A2 < A3 < J < N ..., which fixes the tie order (A1 before A2)
    emp = lambda k, dept, joined, ss="09:30", se="18:30": {
        "emp_code": c[k], "name": f"Name {k}", "email": f"{k}@example.com".lower(), "department": dept,
        "shift_start": ss, "shift_end": se, "joined_on": joined,
        "created_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    employees = [
        emp("A1", D, "2030-01-01"), emp("A2", D, "2030-01-01"), emp("A3", D, "2030-01-01"),
        emp("J", D, "2031-03-17"),                                  # mid-month joiner
        emp("N", D, "2030-01-01", "22:00", "06:00"),                # overnight shift
        emp("Z", D, "2030-01-01"),                                  # no logs at all
        emp("G", D2, "2031-03-31"),                                 # joins on the last day, no logs
        emp("F", D3, "2031-04-05"),                                 # joins after the month
        emp("T1", D4, "2030-01-01"), emp("T2", D4, "2030-01-01"), emp("T3", D4, "2030-01-01"),  # see test_trend_moving_average_*
    ]

    def log(k, day, status="PRESENT", late=0, ot=0, wh=None, half=False, legacy=False):
        d = {"emp_code": c[k], "date": f"{MONTH}-{day:02d}", "status": status,
             "punch_in": None, "punch_out": None, "work_hours": wh,
             "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
        if status in PRESENCE:
            d["punch_in"] = datetime(2031, 3, day, 4, 0, tzinfo=timezone.utc)
            if wh is not None:
                d["punch_out"] = d["punch_in"] + timedelta(hours=wh)
        if legacy:
            del d["half_day"], d["history"]  # old documents simply don't have these fields
        return d

    logs = [
        log("A1", 3, late=20, wh=9.0, legacy=True),
        log("A1", 4, wh=4.0, half=True),
        log("A1", 5, "LEAVE"),
        log("A1", 6, "ABSENT"),
        log("A1", 8, late=15, ot=40, wh=8.0),                       # Saturday: counts for late/ot, not present
        log("A2", 3, "WFH", late=25, wh=8.0),
        log("A2", 4, "ON_DUTY", late=10, wh=8.5),
        log("A2", 10, ot=35, wh=9.01),
        log("A3", 3, late=35, wh=9.0),
        log("A3", 4, late=30, wh=9.0),
        log("A3", 5, wh=8.25),
        log("A3", 6),                                               # open record: no work_hours yet
        log("J", 17, late=12, wh=9.0),
        log("J", 18, wh=4.49, half=True),
        log("J", 19, wh=9.0),
        log("N", 10, late=11, ot=40, wh=8.0),
        log("N", 11, wh=8.0),
        log("T1", 4, wh=8.0), log("T2", 4, wh=8.0),                 # Tue 03-04: 2 of 3 present; Mon 03-03: nobody
        log("GHOST", 3, late=100, wh=9.0),                          # employee does not exist -> ignored
    ]
    db.employees.insert_many(employees)
    db.attendance_logs.insert_many(logs)
    yield {"c": c, "D": D, "D2": D2, "D3": D3, "D4": D4, "employees": employees, "logs": logs}
    codes_all = list(c.values())
    db.attendance_logs.delete_many({"emp_code": {"$in": codes_all}})
    db.employees.delete_many({"emp_code": {"$in": codes_all}})


# ---- helpers: Decimal half-up + the brute-force reference ------------------------------------ #
def hu(x, places):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def is_weekday(day):
    return date.fromisoformat(day).weekday() < 5


def present_value(r):
    return (Decimal("0.5") if r.get("half_day", False) else Decimal(1)) if r["status"] in PRESENCE else Decimal(0)


def ref_monthly(ds, code, month):
    e = next(e for e in ds["employees"] if e["emp_code"] == code)
    recs = [r for r in ds["logs"] if r["emp_code"] == code and r["date"].startswith(month)]
    first = date.fromisoformat(month + "-01")
    d, working = max(first, date.fromisoformat(e["joined_on"])), 0
    while d.isoformat()[:7] == month:  # naive: walk every day
        working += d.weekday() < 5
        d += timedelta(days=1)
    present = sum((present_value(r) for r in recs if is_weekday(r["date"])), Decimal(0))
    return {
        "emp_code": code, "month": month, "working_days": working, "present_days": hu(present, 2),
        "leave_days": sum(r["status"] == "LEAVE" for r in recs),
        "late_count": sum(r["late_minutes"] > 0 for r in recs),
        "total_late_minutes": sum(r["late_minutes"] for r in recs),
        "total_overtime_minutes": sum(r["overtime_minutes"] for r in recs),
        "attendance_pct": hu(present / working * 100, 2) if working else None,
    }


def ref_summary(ds, month, dept=None):
    last = date(int(month[:4]), int(month[5:]), 1)
    last = (last.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    out = {}
    for e in ds["employees"]:
        if e["joined_on"] <= last.isoformat() and (dept is None or e["department"] == dept):
            out.setdefault(e["department"], []).append(e["emp_code"])
    items = []
    for name in sorted(out):
        recs = [r for r in ds["logs"] if r["emp_code"] in out[name] and r["date"].startswith(month)]
        hrs = [Decimal(str(r["work_hours"])) for r in recs if r["status"] in PRESENCE and r["work_hours"] is not None]
        items.append({
            "department": name, "headcount": len(out[name]),
            "present_days": hu(sum((present_value(r) for r in recs if is_weekday(r["date"])), Decimal(0)), 2),
            "avg_work_hours": hu(sum(hrs) / len(hrs), 2) if hrs else None,
            "late_count": sum(r["late_minutes"] > 0 for r in recs),
            "total_late_minutes": sum(r["late_minutes"] for r in recs),
            "leave_count": sum(r["status"] == "LEAVE" for r in recs),
            "on_duty_count": sum(r["status"] == "ON_DUTY" for r in recs),
        })
    return items


def ref_leaderboard(ds, month, limit, dept=None):
    by_code = {e["emp_code"]: e for e in ds["employees"]}
    totals = {}
    for r in ds["logs"]:
        e = by_code.get(r["emp_code"])
        if e and r["date"].startswith(month) and r["late_minutes"] > 0 and (dept is None or e["department"] == dept):
            t = totals.setdefault(r["emp_code"], [0, 0])
            t[0] += r["late_minutes"]
            t[1] += 1
    rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = []
    for code, (total, n) in rows:
        rank = 1 + sum(1 for _, (t, _) in rows if t > total)  # = 1 + how many are strictly ahead
        if rank <= limit:
            e = by_code[code]
            out.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                        "total_late_minutes": total, "late_count": n})
    return out


def ref_trend(ds, dept, d_from, d_to):
    emps = [e for e in ds["employees"] if e["department"] == dept]
    codes = {e["emp_code"] for e in emps}
    rows, rates = [], []
    day = date.fromisoformat(d_from)
    while day <= date.fromisoformat(d_to):
        s = day.isoformat()
        recs = [r for r in ds["logs"] if r["emp_code"] in codes and r["date"] == s]
        present = sum((present_value(r) for r in recs), Decimal(0))
        head = sum(e["joined_on"] <= s for e in emps)
        working = day.weekday() < 5
        # attendance_rate is the 4-dp half-up value, and the moving average is the mean of THOSE values
        rate = (present / head).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if working and head > 0 else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        rows.append({
            "date": s, "is_working_day": working, "headcount": head, "present_count": hu(present, 2),
            "late_count": sum(r["late_minutes"] > 0 for r in recs),
            "attendance_rate": None if rate is None else float(rate),
            "moving_avg_7d": hu(sum(window) / len(window), 4) if window else None,
        })
        day += timedelta(days=1)
    return rows


# ---- employee monthly --------------------------------------------------------------------- #
HAND_MONTHLY = {  # worked out by hand from the dataset above
    "A1": dict(working_days=21, present_days=1.5, leave_days=1, late_count=2, total_late_minutes=35,
               total_overtime_minutes=40, attendance_pct=7.14),       # 1.5/21 = 7.1428..
    "A2": dict(working_days=21, present_days=3.0, leave_days=0, late_count=2, total_late_minutes=35,
               total_overtime_minutes=35, attendance_pct=14.29),      # 3/21 = 14.2857..
    "A3": dict(working_days=21, present_days=4.0, leave_days=0, late_count=2, total_late_minutes=65,
               total_overtime_minutes=0, attendance_pct=19.05),       # 4/21 = 19.0476..
    "J": dict(working_days=11, present_days=2.5, leave_days=0, late_count=1, total_late_minutes=12,
              total_overtime_minutes=0, attendance_pct=22.73),        # 2.5/11 = 22.7272..
    "N": dict(working_days=21, present_days=2.0, leave_days=0, late_count=1, total_late_minutes=11,
              total_overtime_minutes=40, attendance_pct=9.52),        # 2/21 = 9.5238..
    "Z": dict(working_days=21, present_days=0.0, leave_days=0, late_count=0, total_late_minutes=0,
              total_overtime_minutes=0, attendance_pct=0.0),
    "F": dict(working_days=0, present_days=0.0, leave_days=0, late_count=0, total_late_minutes=0,
              total_overtime_minutes=0, attendance_pct=None),         # joins after the month
}


@pytest.mark.parametrize("k", list(HAND_MONTHLY))
def test_employee_monthly(api, ds, k):
    code = ds["c"][k]
    r = api.get(f"/analytics/employees/{code}/monthly", params={"month": MONTH})
    assert r.status_code == 200, r.text
    assert r.json() == {"emp_code": code, "month": MONTH, **HAND_MONTHLY[k]}
    assert r.json() == ref_monthly(ds, code, MONTH)


def test_employee_monthly_errors(api, ds):
    code = ds["c"]["A1"]
    assert api.get("/analytics/employees/EMP000001/monthly", params={"month": MONTH}).status_code == 404
    assert api.get("/analytics/employees/not-a-code/monthly", params={"month": MONTH}).status_code == 404
    for bad in ["2031-13", "2031-3", "2031-00", "abc", "0000-01", "2031-03-01"]:
        assert api.get(f"/analytics/employees/{code}/monthly", params={"month": bad}).status_code == 422, bad
    assert api.get(f"/analytics/employees/{code}/monthly").status_code == 422


def test_employee_monthly_month_without_records(api, ds):
    r = api.get(f"/analytics/employees/{ds['c']['A1']}/monthly", params={"month": "2031-04"})
    assert r.json()["present_days"] == 0.0 and r.json()["working_days"] == 22  # April 2031: 22 weekdays


# ---- department summary --------------------------------------------------------------------- #
def test_department_summary_hand_values(api, ds):
    D = ds["D"]
    r = api.get("/analytics/departments/summary", params={"month": MONTH, "department": D})
    assert r.status_code == 200
    assert r.json() == {"month": MONTH, "items": [{
        "department": D,
        "headcount": 6,            # A1 A2 A3 J N Z. F joins later; Z has no logs but counts
        "present_days": 13.0,      # 1.5 + 3 + 4 + 2.5 + 0 + 2
        "avg_work_hours": 7.95,    # 111.25 h over 14 records = 7.946..; open record + LEAVE/ABSENT excluded
        "late_count": 8, "total_late_minutes": 158, "leave_count": 1, "on_duty_count": 1}]}


def test_department_summary_all_departments(api, ds):
    r = api.get("/analytics/departments/summary", params={"month": MONTH})
    mine = [i for i in r.json()["items"] if i["department"] in (ds["D"], ds["D2"], ds["D3"], ds["D4"])]
    assert mine == ref_summary(ds, MONTH)
    assert [i["department"] for i in mine] == [ds["D"], ds["D2"], ds["D4"]]  # D3 has headcount 0 in March -> omitted
    quiet = next(i for i in mine if i["department"] == ds["D2"])
    assert quiet == {"department": ds["D2"], "headcount": 1, "present_days": 0.0, "avg_work_hours": None,
                     "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    names = [i["department"] for i in r.json()["items"]]
    assert names == sorted(names)
    # In April the later joiner counts, and the others all joined before.
    april = api.get("/analytics/departments/summary", params={"month": "2031-04", "department": ds["D3"]})
    assert april.json()["items"][0]["headcount"] == 1


def test_department_summary_errors(api, ds):
    assert api.get("/analytics/departments/summary").status_code == 422
    assert api.get("/analytics/departments/summary", params={"month": "2031-13"}).status_code == 422
    assert api.get("/analytics/departments/summary", params={"month": MONTH, "department": "nope"}).json()["items"] == []


# ---- leaderboard ----------------------------------------------------------------------------- #
def board(api, **params):
    r = api.get("/analytics/leaderboard/late", params={"month": MONTH, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_leaderboard_ties_and_limit_on_rank(api, ds):
    D, c = ds["D"], ds["c"]
    full = board(api, department=D, limit=10)
    # hand: A3 65 -> 1; A1 35 and A2 35 tie -> 2, 2; next is 4 (rank 3 is skipped); N 11 -> 5. Zero-late Z not listed.
    assert [(r["emp_code"], r["rank"], r["total_late_minutes"], r["late_count"]) for r in full] == [
        (c["A3"], 1, 65, 2), (c["A1"], 2, 35, 2), (c["A2"], 2, 35, 2), (c["J"], 4, 12, 1), (c["N"], 5, 11, 1)]
    assert full[0]["name"] == "Name A3" and full[0]["department"] == D
    assert len(board(api, department=D, limit=1)) == 1
    assert len(board(api, department=D, limit=2)) == 3   # tie at the cutoff: both rank-2 rows stay
    assert len(board(api, department=D, limit=3)) == 3   # rank 3 doesn't exist, rank 4 is beyond the limit
    assert len(board(api, department=D, limit=4)) == 4
    for limit in (1, 2, 3, 4, 5, 10):
        assert board(api, department=D, limit=limit) == ref_leaderboard(ds, MONTH, limit, D)


def test_leaderboard_without_department_ignores_ghost_employee(api, ds):
    assert board(api, limit=50) == ref_leaderboard(ds, MONTH, 50)
    assert ds["c"]["GHOST"] not in [r["emp_code"] for r in board(api, limit=50)]


def test_leaderboard_ranks_inside_department(api, ds):
    # ranking happens inside the department, not globally: asking for another department gives nothing here
    assert board(api, department=ds["D2"]) == []


def test_leaderboard_validation(api, ds):
    for bad in (0, 51, -1, "x"):
        assert api.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": bad}).status_code == 422
    assert api.get("/analytics/leaderboard/late").status_code == 422
    assert api.get("/analytics/leaderboard/late", params={"month": "2031-99"}).status_code == 422


# ---- trend ------------------------------------------------------------------------------------ #
HAND_TREND_D = [  # (date, working, present_count, late_count, rate, moving_avg); headcount is 5 on every day
    ("2031-03-01", False, 0, 0, None, None),
    ("2031-03-02", False, 0, 0, None, None),
    ("2031-03-03", True, 3, 3, 0.6, 0.6),
    ("2031-03-04", True, 2.5, 2, 0.5, 0.55),
    ("2031-03-05", True, 1, 0, 0.2, 0.4333),      # (0.6+0.5+0.2)/3
    ("2031-03-06", True, 1, 0, 0.2, 0.375),       # (0.6+0.5+0.2+0.2)/4
    ("2031-03-07", True, 0, 0, 0.0, 0.3),         # weekday with no record at all: rate 0.0, not null
    ("2031-03-08", False, 1, 1, None, 0.3),       # Saturday record: present/late counted, rate null
    ("2031-03-09", False, 0, 0, None, 0.3),
    ("2031-03-10", True, 2, 1, 0.4, 0.26),        # window = rows 4..10: (0.5+0.2+0.2+0+0.4)/5; the 0.6 fell out
]


def trend(api, dept, d_from, d_to):
    r = api.get(f"/analytics/departments/{dept}/trend", params={"from": d_from, "to": d_to})
    assert r.status_code == 200, r.text
    assert r.json()["department"] == dept
    return r.json()["items"]


def test_trend_hand_values(api, ds):
    rows = trend(api, ds["D"], "2031-03-01", "2031-03-10")
    assert rows == [
        {"date": d, "is_working_day": w, "headcount": 5, "present_count": p, "late_count": lc,
         "attendance_rate": rate, "moving_avg_7d": mv}
        for d, w, p, lc, rate, mv in HAND_TREND_D]
    assert rows == ref_trend(ds, ds["D"], "2031-03-01", "2031-03-10")


def test_trend_moving_average_is_over_rounded_rates(api, ds):
    # 3 employees: Mon 03-03 nobody present (rate 0.0), Tue 03-04 two of three (2/3 -> 0.6667).
    # Mean of the ROUNDED rates = (0 + 0.6667) / 2 = 0.33335 -> half-up 0.3334.
    # (Averaging the raw 2/3 would give 0.33333 -> 0.3333. This is the exact tie case.)
    rows = trend(api, ds["D4"], "2031-03-03", "2031-03-04")
    assert [(r["attendance_rate"], r["moving_avg_7d"]) for r in rows] == [(0.0, 0.0), (0.6667, 0.3334)]
    assert rows == ref_trend(ds, ds["D4"], "2031-03-03", "2031-03-04")
    # And a longer window over the same department: each row is the mean of its rounded predecessors.
    rows = trend(api, ds["D4"], "2031-03-03", "2031-03-12")
    assert rows == ref_trend(ds, ds["D4"], "2031-03-03", "2031-03-12")


def test_trend_department_without_any_logs_and_joiner_headcount(api, ds):
    rows = trend(api, ds["D2"], "2031-03-29", "2031-04-02")  # G joins on 2031-03-31; there are no logs at all
    assert [(r["date"], r["headcount"], r["attendance_rate"], r["moving_avg_7d"]) for r in rows] == [
        ("2031-03-29", 0, None, None), ("2031-03-30", 0, None, None),
        ("2031-03-31", 1, 0.0, 0.0), ("2031-04-01", 1, 0.0, 0.0), ("2031-04-02", 1, 0.0, 0.0)]
    assert rows == ref_trend(ds, ds["D2"], "2031-03-29", "2031-04-02")


def test_trend_midmonth_joiner_changes_headcount(api, ds):
    rows = trend(api, ds["D"], "2031-03-14", "2031-03-20")
    assert [r["headcount"] for r in rows] == [5, 5, 5, 6, 6, 6, 6]  # J (joined 03-17) appears from the 17th
    assert rows == ref_trend(ds, ds["D"], "2031-03-14", "2031-03-20")


def test_trend_long_range_matches_reference(api, ds):
    rows = trend(api, ds["D"], "2031-01-01", "2031-04-02")  # 92 days, window shorter than 7 at the start
    assert len(rows) == 92
    assert rows == ref_trend(ds, ds["D"], "2031-01-01", "2031-04-02")


def test_trend_errors(api, ds):
    D = ds["D"]
    get = lambda d, f, t: api.get(f"/analytics/departments/{d}/trend", params={"from": f, "to": t}).status_code
    assert get("no-such-dept", "2031-03-01", "2031-03-02") == 404
    assert get(D, "2031-03-05", "2031-03-04") == 422
    assert get(D, "2031-01-01", "2031-04-03") == 422        # 93 days
    assert get(D, "2031-03-01", "2031-03-01") == 200        # one day is fine
    assert get(D, "2031-02-30", "2031-03-01") == 422
    assert api.get(f"/analytics/departments/{D}/trend", params={"from": "2031-03-01"}).status_code == 422
