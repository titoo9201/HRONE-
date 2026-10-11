"""/admin/explain on a realistic 100,000-record dataset.

The fixture builds a SCRATCH database (its own name, dropped afterwards), starts a second copy of the
API on another port pointed at it, and calls GET /admin/explain/{endpoint} with many parameter
combinations. For every response it asserts that the whole explain JSON, as one string (so
rejectedPlans and $lookup stages are included), contains "IXSCAN" and never "COLLSCAN".
Run with `pytest -s tests/test_explain.py` to see the totalDocsExamined report.
"""
import json
import os
import random
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
SCRATCH_DB = "attendance_explain_scratch"
PORT = 8765
N_EMPLOYEES, N_DAYS = 2000, 50  # 2000 x 50 = 100,000 attendance records
FIRST_DAY = date(2026, 6, 15)   # covers 2026-06-15 .. 2026-08-03, so July 2026 is complete
DEPARTMENTS = [f"Dept{i}" for i in range(10)]

REPORT = []  # (endpoint, params, docs_examined, keys_examined, ms)


@pytest.fixture(scope="module")
def server():
    uri = os.environ["MONGO_URI"]
    client = MongoClient(uri, tz_aware=True)
    client.drop_database(SCRATCH_DB)
    db = client[SCRATCH_DB]
    rnd = random.Random(7)
    utc = timezone.utc
    employees = [{
        "emp_code": f"EMP{100000 + i}", "name": f"Employee {i}", "email": f"e{i}@example.com",
        "department": DEPARTMENTS[i % 10],
        "shift_start": "22:00" if i % 40 == 0 else "09:30", "shift_end": "06:00" if i % 40 == 0 else "18:30",
        "joined_on": "2026-07-20" if i % 50 == 0 else "2026-01-01",  # some mid-period joiners
        "created_at": datetime(2026, 1, 1, tzinfo=utc)} for i in range(N_EMPLOYEES)]
    db.employees.insert_many(employees)
    logs = []
    for e in employees:
        for k in range(N_DAYS):
            day = FIRST_DAY + timedelta(days=k)
            status = rnd.choice(["PRESENT"] * 7 + ["WFH", "ON_DUTY", "LEAVE", "ABSENT"])
            present = status in ("PRESENT", "WFH", "ON_DUTY")
            wh = round(rnd.uniform(3, 10), 2) if present and rnd.random() > 0.05 else None
            p_in = datetime(day.year, day.month, day.day, 4, 0, tzinfo=utc) if present else None
            logs.append({
                "emp_code": e["emp_code"], "date": day.isoformat(), "status": status, "punch_in": p_in,
                "punch_out": p_in + timedelta(hours=wh) if wh else None, "work_hours": wh,
                "late_minutes": rnd.choice([0, 0, 0, 12, 25, 40]) if present else 0,
                "overtime_minutes": rnd.choice([0, 0, 35]) if wh else 0,
                "half_day": bool(wh and wh < 4.5), "history": []})
    db.attendance_logs.insert_many(logs, ordered=False)

    env = {**os.environ, "MONGO_DB": SCRATCH_DB}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    api = httpx.Client(base_url=f"http://localhost:{PORT}", timeout=120)
    try:
        for _ in range(60):
            try:
                if api.get("/health").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("scratch API did not start")
        yield api
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        client.drop_database(SCRATCH_DB)
        client.close()


def walk(node, visit):
    if isinstance(node, dict):
        for k, v in node.items():
            visit(k, v)
            walk(v, visit)
    elif isinstance(node, list):
        for v in node:
            walk(v, visit)


def docs_examined(explain: dict):
    """(docs, keys) examined: the find's executionStats, or the aggregation's cursor stage plus every $lookup."""
    docs = keys = 0
    if "stages" in explain:  # aggregation: the first stage is $cursor, $lookup stages report their own totals
        for stage in explain["stages"]:
            name = next(iter(stage))
            body = stage[name] if name == "$cursor" else stage
            es = body.get("executionStats", body)
            docs += es.get("totalDocsExamined", 0)
            keys += es.get("totalKeysExamined", 0)
    else:
        es = explain["executionStats"]
        docs, keys = es["totalDocsExamined"], es["totalKeysExamined"]
    return docs, keys


CASES = [
    # ---- attendance_list ------------------------------------------------------------------------
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP100007"}),
    ("attendance_list", {"status": "WFH"}),
    ("attendance_list", {"status": "ABSENT", "page": 3, "page_size": 100}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"date_from": "2026-07-01"}),
    ("attendance_list", {"date_to": "2026-06-30"}),
    ("attendance_list", {"date_from": "2026-07-10", "date_to": "2026-07-10"}),
    ("attendance_list", {"emp_code": "EMP100007", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT"}),
    ("attendance_list", {"emp_code": "EMP100007", "status": "LEAVE"}),
    ("attendance_list", {"status": "PRESENT", "date_from": "2026-07-01", "date_to": "2026-07-15"}),
    ("attendance_list", {"page": 2}),
    ("attendance_list", {"page": 500, "page_size": 100}),
    ("attendance_list", {"emp_code": "EMP100007", "page": 2, "page_size": 10}),
    ("attendance_list", {"emp_code": "NOT-AN-EMPLOYEE"}),
    # ---- employee_monthly -----------------------------------------------------------------------
    ("employee_monthly", {"emp_code": "EMP100007", "month": "2026-07"}),
    ("employee_monthly", {"emp_code": "EMP100000", "month": "2026-06"}),
    ("employee_monthly", {"emp_code": "EMP100000", "month": "2031-01"}),  # month with no data
    # ---- department_summary ---------------------------------------------------------------------
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Dept3"}),
    ("department_summary", {"month": "2026-06"}),
    ("department_summary", {"month": "2026-08", "department": "Dept9"}),
    # ---- late_leaderboard -----------------------------------------------------------------------
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 3}),
    ("late_leaderboard", {"month": "2026-07", "department": "Dept3"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Dept3", "limit": 50}),
    ("late_leaderboard", {"month": "2026-06"}),
    # ---- department_trend -----------------------------------------------------------------------
    ("department_trend", {"department": "Dept3", "from": "2026-07-01", "to": "2026-07-31"}),
    ("department_trend", {"department": "Dept3", "from": "2026-06-15", "to": "2026-09-14"}),  # 92 days
    ("department_trend", {"department": "Dept0", "from": "2026-07-10", "to": "2026-07-10"}),
    ("department_trend", {"department": "no-such-department", "from": "2026-07-01", "to": "2026-07-05"}),
]


@pytest.mark.parametrize("endpoint,params", CASES, ids=[f"{e}-{'-'.join(map(str, p.values()))}" for e, p in CASES])
def test_plan_uses_index_and_never_scans_the_collection(server, endpoint, params):
    t = time.time()
    r = server.get(f"/admin/explain/{endpoint}", params=params)
    ms = (time.time() - t) * 1000
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint
    assert body["collection"] == ("attendance_logs" if endpoint in ("attendance_list", "employee_monthly", "late_leaderboard") else "employees")
    text = json.dumps(body["explain"])  # the whole document: winningPlan, rejectedPlans, $lookup stages
    assert "IXSCAN" in text, "no index scan in the plan"
    assert "COLLSCAN" not in text, "a collection scan is present somewhere in the explain output"
    assert "executionStats" in text
    docs, keys = docs_examined(body["explain"])
    REPORT.append((endpoint, params, docs, keys, ms))


def test_real_endpoints_agree_with_what_was_explained(server):
    """Sanity: the scratch data really has ~100k rows and the endpoints the explain mirrors work on it."""
    assert server.get("/attendance", params={"page_size": 1}).json()["total"] == N_EMPLOYEES * N_DAYS
    assert server.get("/analytics/departments/summary", params={"month": "2026-07"}).status_code == 200


# ---- validation: same rules as the real endpoints --------------------------------------------- #
@pytest.mark.parametrize("path,params", [
    ("/admin/explain/not_an_endpoint", {}),
    ("/admin/explain/employee_monthly", {}),
    ("/admin/explain/employee_monthly", {"emp_code": "EMP100007"}),
    ("/admin/explain/employee_monthly", {"month": "2026-07"}),
    ("/admin/explain/employee_monthly", {"emp_code": "EMP100007", "month": "2026-13"}),
    ("/admin/explain/department_summary", {}),
    ("/admin/explain/department_summary", {"month": "july"}),
    ("/admin/explain/late_leaderboard", {}),
    ("/admin/explain/late_leaderboard", {"month": "2026-07", "limit": 0}),
    ("/admin/explain/late_leaderboard", {"month": "2026-07", "limit": 51}),
    ("/admin/explain/department_trend", {}),
    ("/admin/explain/department_trend", {"department": "Dept3", "from": "2026-07-01"}),
    ("/admin/explain/department_trend", {"from": "2026-07-01", "to": "2026-07-05"}),
    ("/admin/explain/department_trend", {"department": "Dept3", "from": "2026-07-05", "to": "2026-07-01"}),
    ("/admin/explain/department_trend", {"department": "Dept3", "from": "2026-06-15", "to": "2026-09-15"}),  # 93 days
    ("/admin/explain/department_trend", {"department": "Dept3", "from": "2026-02-30", "to": "2026-03-05"}),
    ("/admin/explain/attendance_list", {"status": "HOLIDAY"}),
    ("/admin/explain/attendance_list", {"date_from": "2026-07-09", "date_to": "2026-07-01"}),
    ("/admin/explain/attendance_list", {"date_from": "2026-13-01"}),
    ("/admin/explain/attendance_list", {"page": 0}),
    ("/admin/explain/attendance_list", {"page_size": 101}),
])
def test_validation_gives_422(server, path, params):
    r = server.get(path, params=params)
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert isinstance(r.json()["detail"], list)


def test_zz_report(server):
    """Printed last (run with -s): how much each explained query touched."""
    print("\n\nexplain on 100,000 attendance records / 2,000 employees")
    print(f"{'endpoint':20s} {'docs examined':>13s} {'keys examined':>13s} {'ms':>6s}  params")
    for endpoint, params, docs, keys, ms in REPORT:
        print(f"{endpoint:20s} {docs:13d} {keys:13d} {ms:6.0f}  {params}")
    assert REPORT
