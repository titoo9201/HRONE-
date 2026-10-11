"""Integration tests for punch-in/punch-out/PATCH. They talk to a RUNNING server
(default http://localhost:8000, override with API_URL) and are skipped when it is not up.
Every test creates its own employee with a random emp_code, so reruns never collide."""
import concurrent.futures as cf
import os
import random
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.environ.get("API_URL", "http://localhost:8000")
IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def api():
    c = httpx.Client(base_url=BASE, timeout=15)
    try:
        c.get("/health").raise_for_status()
    except Exception:
        pytest.skip(f"API not running at {BASE}")
    return c


@pytest.fixture(scope="module")
def logs():
    return MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]].attendance_logs


def ms(y, mo, d, h, mi, s=0, us=0):
    """IST wall-clock -> epoch milliseconds."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + us // 1000  # integer math: no float rounding


CREATED = []  # every employee a test made, removed again at the end of the module


@pytest.fixture(scope="module", autouse=True)
def cleanup(logs):
    yield
    logs.delete_many({"emp_code": {"$in": CREATED}})
    logs.database.employees.delete_many({"emp_code": {"$in": CREATED}})


def new_employee(api, shift_start="09:30", shift_end="18:30"):
    code = f"EMP{random.randint(100000, 999999)}"
    CREATED.append(code)
    r = api.post("/employees", json={
        "emp_code": code, "name": "T", "email": "t@example.com", "department": "QA",
        "shift_start": shift_start, "shift_end": shift_end, "joined_on": "2026-01-01"})
    assert r.status_code == 201, r.text
    return code


def punch_in(api, code, at, status="PRESENT"):
    r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": at, "status": status})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- punch-out ----
def test_punch_out_sample_values(api):
    code = new_employee(api)
    punch_in(api, code, ms(2026, 7, 6, 10, 5))
    r = api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (9.08, 35, 40, False)
    assert b["punch_out"] == ms(2026, 7, 6, 19, 10) and b["history"] == []


def test_punch_out_half_day_and_second_truncation(api):
    code = new_employee(api)
    punch_in(api, code, ms(2026, 7, 7, 9, 30, 0, 900_000))  # .9 s must be dropped
    r = api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(2026, 7, 7, 13, 0, 0, 999_000)})
    b = r.json()
    assert b["punch_in"] == ms(2026, 7, 7, 9, 30) and b["punch_out"] == ms(2026, 7, 7, 13, 0)
    assert (b["work_hours"], b["half_day"]) == (3.5, True)


def test_punch_out_overnight_crosses_midnight(api):
    code = new_employee(api, "22:00", "06:00")
    rec = punch_in(api, code, ms(2026, 7, 6, 21, 55))
    assert rec["date"] == "2026-07-06"
    r = api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["date"], b["work_hours"], b["overtime_minutes"]) == ("2026-07-06", 8.75, 40)


def test_punch_out_errors(api):
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP000001"}).status_code == 404  # unknown employee
    code = new_employee(api)
    assert api.post("/attendance/punch-out", json={"emp_code": code}).status_code == 404  # no punch-in at all
    punch_in(api, code, ms(2026, 7, 6, 9, 30))
    p = lambda at: api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": at})
    assert p(ms(2026, 7, 6, 9, 0)).status_code == 404            # before the only punch-in: nothing at-or-before
    assert p(ms(2026, 7, 6, 9, 30)).status_code == 422           # equal to punch_in
    assert p(ms(2026, 7, 7, 9, 31)).status_code == 422           # more than 24 h later
    assert p(1783312500).status_code == 422                      # seconds-looking
    assert p(ms(2026, 7, 6, 18, 30)).status_code == 200
    assert p(ms(2026, 7, 6, 19, 30)).status_code == 409          # already closed


def test_parallel_punch_out_single_winner(api):
    code = new_employee(api)
    punch_in(api, code, ms(2026, 7, 6, 9, 30))
    with cf.ThreadPoolExecutor(10) as ex:
        futs = [ex.submit(api.post, "/attendance/punch-out",
                          json={"emp_code": code, "punched_at": ms(2026, 7, 6, 18, 30)}) for _ in range(10)]
        codes = sorted(f.result().status_code for f in futs)
    assert codes == [200] + [409] * 9


# -------------------------------------------------------------------- PATCH ----
def patch(api, code, day, **body):
    body.setdefault("reason", "fixing the record")
    body.setdefault("regularized_by", "hr.admin")
    return api.patch(f"/attendance/{code}/{day}", json=body)


def closed_record(api, code, day=(2026, 7, 7), pin=(10, 5), pout=(19, 10)):
    y, mo, d = day
    punch_in(api, code, ms(y, mo, d, *pin))
    r = api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(y, mo, d, *pout)})
    assert r.status_code == 200, r.text


def test_patch_recomputes_and_records_only_changed_fields(api):
    code = new_employee(api)
    closed_record(api, code)  # in 10:05 (late 35), out 19:10 (ot 40, 9.08 h)
    r = patch(api, code, "2026-07-07", punch_in=ms(2026, 7, 7, 9, 28), work_hours=1, late_minutes=99)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["late_minutes"], b["work_hours"], b["overtime_minutes"]) == (0, 9.7, 40)
    (h,) = b["history"]
    assert h["by"] == "hr.admin" and h["reason"] == "fixing the record"
    assert set(h["changes"]) == {"punch_in", "late_minutes", "work_hours"}  # overtime/half_day/status unchanged
    assert h["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert h["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert h["changes"]["work_hours"] == {"from": 9.08, "to": 9.7}
    # A second correction appends; the first entry is untouched.
    r2 = patch(api, code, "2026-07-07", status="WFH", reason="was working from home")
    assert [list(e["changes"]) for e in r2.json()["history"]] == [list(h["changes"]), ["status"]]
    assert r2.json()["history"][0] == h


def test_patch_absent_clears_punches_then_presence_needs_punch_in(api):
    code = new_employee(api)
    closed_record(api, code)
    assert patch(api, code, "2026-07-07", status="ABSENT", punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422
    r = patch(api, code, "2026-07-07", status="LEAVE")
    b = r.json()
    assert r.status_code == 200
    assert (b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"], b["overtime_minutes"]) == (None,) * 3 + (0, 0)
    assert b["history"][0]["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": None}
    assert patch(api, code, "2026-07-07", status="PRESENT").status_code == 422        # no punch_in anywhere
    assert patch(api, code, "2026-07-07", punch_in=ms(2026, 7, 7, 9, 0)).status_code == 422  # LEAVE + punch time
    r = patch(api, code, "2026-07-07", status="PRESENT", punch_in=ms(2026, 7, 7, 9, 0))
    assert r.status_code == 200 and r.json()["late_minutes"] == 0 and len(r.json()["history"]) == 2


def test_patch_validation_and_not_found(api):
    code = new_employee(api)
    closed_record(api, code)
    day = "2026-07-07"
    assert patch(api, "EMP000001", day, status="WFH").status_code == 404
    assert patch(api, code, "2026-07-08", status="WFH").status_code == 404
    assert patch(api, "weird-code", day, status="WFH").status_code == 404          # no regex on path params
    assert patch(api, code, day).status_code == 422                                # changes nothing
    assert patch(api, code, day, status="PRESENT").status_code == 422              # same value = nothing
    assert patch(api, code, day, punch_in=ms(2026, 7, 6, 10, 0)).status_code == 422  # other attendance date
    assert patch(api, code, day, punch_in=ms(2026, 7, 7, 19, 10)).status_code == 422  # not before punch_out
    assert patch(api, code, day, punch_out=ms(2026, 7, 8, 10, 6)).status_code == 422  # > 24 h
    assert patch(api, code, day, punch_out=ms(2026, 7, 7, 10, 5)).status_code == 422  # == punch_in
    assert patch(api, code, day, status=None).status_code == 422                   # null not allowed
    assert patch(api, code, day, punch_in=1783312500).status_code == 422          # seconds-looking
    assert patch(api, code, day, punch_in=1.7e12).status_code == 422               # float
    assert patch(api, code, day, status="WFH", reason="abc").status_code == 422    # reason too short
    assert patch(api, code, day, status="WFH", regularized_by="").status_code == 422
    assert patch(api, code, day, status="HOLIDAY").status_code == 422
    assert api.patch(f"/attendance/{code}/2026-02-30", json={"status": "WFH", "reason": "xxxxx", "regularized_by": "a"}).status_code == 422
    assert api.get("/attendance", params={"emp_code": code}).json()["items"][0]["history"] == []  # nothing leaked through


def test_patch_open_record_and_legacy_record(api, logs):
    code = new_employee(api)
    punch_in(api, code, ms(2026, 7, 7, 10, 5))  # still open
    r = patch(api, code, "2026-07-07", punch_out=ms(2026, 7, 7, 19, 10))
    assert r.status_code == 200 and r.json()["work_hours"] == 9.08 and r.json()["overtime_minutes"] == 40
    # Legacy document: no history and no half_day fields at all.
    legacy = new_employee(api)
    logs.insert_one({"emp_code": legacy, "date": "2026-07-14", "status": "PRESENT",
                     "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                     "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
                     "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    r = patch(api, legacy, "2026-07-14", status="ON_DUTY")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["half_day"] is False and len(b["history"]) == 1 and list(b["history"][0]["changes"]) == ["status"]


def test_parallel_patch_never_loses_history(api):
    code = new_employee(api)
    closed_record(api, code)
    n = 10
    with cf.ThreadPoolExecutor(n) as ex:
        futs = [ex.submit(patch, api, code, "2026-07-07", punch_in=ms(2026, 7, 7, 9, i)) for i in range(n)]
        results = [f.result() for f in futs]
    codes = [r.status_code for r in results]
    assert set(codes) <= {200, 409}, codes
    final = api.get("/attendance", params={"emp_code": code}).json()["items"][0]
    assert len(final["history"]) == codes.count(200) >= 1
    # The history is a chain: each entry's "from" is the previous entry's "to".
    ins = [e["changes"]["punch_in"] for e in final["history"]]
    assert all(a["to"] == b["from"] for a, b in zip(ins, ins[1:]))


def test_patch_noop_is_judged_on_requested_fields_not_stale_derived_values(api, logs):
    code = new_employee(api)
    p_in = datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc)   # 09:30 IST
    p_out = datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc)  # 18:30 IST -> real work_hours is 9.0
    logs.insert_one({"emp_code": code, "date": "2026-07-07", "status": "PRESENT", "punch_in": p_in,
                     "punch_out": p_out, "work_hours": 1.23, "late_minutes": 77,  # stale, wrong values
                     "overtime_minutes": 0, "half_day": False, "history": []})
    same_in = int(p_in.timestamp()) * 1000
    # Same punch_in again: nothing the caller can set would change -> 422, even though a recompute would differ.
    assert patch(api, code, "2026-07-07", punch_in=same_in).status_code == 422
    assert patch(api, code, "2026-07-07", status="PRESENT", punch_in=same_in).status_code == 422
    stored = logs.find_one({"emp_code": code})
    assert (stored["work_hours"], stored["late_minutes"], stored["history"]) == (1.23, 77, [])  # untouched
    # A real change goes through; the stale derived values are corrected and show up in history too.
    r = patch(api, code, "2026-07-07", status="WFH")
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["late_minutes"]) == (9.0, 0)
    assert set(b["history"][0]["changes"]) == {"status", "work_hours", "late_minutes"}
    assert b["history"][0]["changes"]["work_hours"] == {"from": 1.23, "to": 9.0}
