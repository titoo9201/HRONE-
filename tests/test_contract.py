"""Contract conformance: every response is validated against the schemas in openapi.yaml.

For each call the test finds the operation by (method, path template) and checks:
  * the status code is one the contract lists for that operation (an undocumented code is a failure)
  * the body matches the response schema: required fields, types, nullable, EpochMillis range, enums, formats
  * NO extra fields: every object schema that lists `properties` is treated as closed, so a leaked
    `_id` / `id` or any other additional field fails
  * no `_id` / `id` key anywhere in a body, and history[].changes from/to are epoch milliseconds
Needs a running API (API_URL, default http://localhost:8000).
"""
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from dotenv import load_dotenv
from jsonschema import Draft7Validator, FormatChecker
from pymongo import MongoClient

load_dotenv()
BASE = os.environ.get("API_URL", "http://localhost:8000")
SPEC = yaml.safe_load((Path(__file__).resolve().parent.parent / "openapi.yaml").read_text(encoding="utf-8"))
COMPONENTS = SPEC["components"]
IST = timezone(timedelta(hours=5, minutes=30))
MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000


# ---- OpenAPI 3.0 schema -> JSON Schema (draft 7) ---------------------------------------------- #
def convert(node):
    """Inline $refs, turn `nullable` into a null type, and close object schemas that list `properties`."""
    if isinstance(node, list):
        return [convert(n) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        kind, name = node["$ref"].split("/")[-2:]
        return convert(COMPONENTS[kind][name])
    out = {k: convert(v) for k, v in node.items() if k not in ("nullable", "readOnly", "example", "description")}
    if node.get("nullable") and "type" in out:
        out["type"] = [out["type"], "null"]
    if "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def operation(method, template):
    return SPEC["paths"][template][method.lower()]


def response_schema(method, template, status):
    responses = operation(method, template)["responses"]
    assert str(status) in responses, f"{method} {template}: status {status} is not in the contract {sorted(responses)}"
    resp = responses[str(status)]
    if "$ref" in resp:
        resp = COMPONENTS["responses"][resp["$ref"].split("/")[-1]]
    return convert(resp["content"]["application/json"]["schema"]) if "content" in resp else None


def keys_named(node, names, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in names:
                yield f"{path}/{k}"
            yield from keys_named(v, names, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from keys_named(v, names, f"{path}[{i}]")


def history_entries(body):
    items = body["items"] if isinstance(body, dict) and "items" in body else [body]
    for rec in items:
        if isinstance(rec, dict) and "history" in rec:
            yield from rec["history"]


def check(resp, method, template, status):
    """Validate one response against the contract; returns the parsed body."""
    assert resp.status_code == status, f"{method} {template}: expected {status}, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    schema = response_schema(method, template, status)
    errors = sorted(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, f"{method} {template} {status}: " + "; ".join(
        f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message[:120]}" for e in errors[:6])
    if not template.startswith("/admin/explain"):  # a raw explain legitimately mentions "_id" (index names)
        assert not list(keys_named(body, {"_id", "id"})), f"{method} {template}: an id key leaked: {list(keys_named(body, {'_id', 'id'}))}"
    for entry in history_entries(body):
        assert MS_MIN <= entry["at"] <= MS_MAX
        for field, change in entry["changes"].items():
            assert set(change) == {"from", "to"}, f"history change {field} must have exactly from/to"
            if field in ("punch_in", "punch_out"):
                for side in ("from", "to"):
                    v = change[side]
                    assert v is None or (type(v) is int and MS_MIN <= v <= MS_MAX), f"history {field}.{side} is not epoch ms: {v!r}"
    return body


# ---- fixtures ------------------------------------------------------------------------------------- #
def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


@pytest.fixture(scope="module")
def api():
    c = httpx.Client(base_url=BASE, timeout=30)
    try:
        c.get("/health").raise_for_status()
    except Exception:
        pytest.skip(f"API not running at {BASE}")
    return c


@pytest.fixture(scope="module")
def world(api):
    """Employees plus hand-inserted documents covering the shapes the API must convert."""
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
    tag = random.randint(1000, 9999)
    dept = f"Contract{tag}"
    codes = random.sample(range(800000, 899999), 6)
    w = {k: f"EMP{n}" for k, n in zip(["main", "legacy", "hist", "absent", "open", "night"], codes)}
    base = {"name": "C", "email": "c@example.com", "department": dept, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2030-01-01", "created_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    db.employees.insert_many([{**base, "emp_code": c} for c in w.values()])
    u = lambda d, h, m: datetime(2031, 3, d, h, m, tzinfo=timezone.utc)
    db.attendance_logs.insert_many([
        # legacy document: no half_day, no history
        {"emp_code": w["legacy"], "date": "2031-03-04", "status": "PRESENT", "punch_in": u(4, 4, 2), "punch_out": u(4, 13, 1),
         "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0},
        # corrected record with history; from/to are BSON dates
        {"emp_code": w["hist"], "date": "2031-03-04", "status": "PRESENT", "punch_in": u(4, 3, 58), "punch_out": u(4, 13, 0),
         "work_hours": 9.03, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
         "history": [{"at": u(5, 5, 0), "by": "hr.admin", "reason": "biometric glitch",
                      "changes": {"punch_in": {"from": u(4, 4, 35), "to": u(4, 3, 58)}, "late_minutes": {"from": 35, "to": 0},
                                  "work_hours": {"from": 8.42, "to": 9.03}}}]},
        {"emp_code": w["absent"], "date": "2031-03-04", "status": "ABSENT", "punch_in": None, "punch_out": None,
         "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": w["open"], "date": "2031-03-04", "status": "WFH", "punch_in": u(4, 4, 0), "punch_out": None,
         "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
    ])
    yield {"dept": dept, **w}
    db.attendance_logs.delete_many({"emp_code": {"$in": list(w.values())}})
    db.employees.delete_many({"emp_code": {"$in": list(w.values())}})


# ---- system / employees ------------------------------------------------------------------------- #
def test_health(api):
    assert check(api.get("/health"), "GET", "/health", 200) == {"status": "ok"}


def test_employees(api, world):
    code = f"EMP{random.randint(900000, 999999)}"
    body = {"emp_code": code, "name": "New Person", "email": "n@example.com", "department": world["dept"], "joined_on": "2031-01-01"}
    try:
        created = check(api.post("/employees", json=body), "POST", "/employees", 201)
        assert (created["shift_start"], created["shift_end"]) == ("09:30", "18:30")  # defaults are applied and returned
        check(api.post("/employees", json=body), "POST", "/employees", 409)
        check(api.post("/employees", json={**body, "emp_code": "bad"}), "POST", "/employees", 422)
        check(api.post("/employees", json={}), "POST", "/employees", 422)
        listing = check(api.get("/employees", params={"department": world["dept"]}), "GET", "/employees", 200)
        assert listing["total"] == 7 and listing["items"][0]["emp_code"] < listing["items"][-1]["emp_code"]
        check(api.get("/employees", params={"page": 0}), "GET", "/employees", 422)
    finally:
        MongoClient(os.environ["MONGO_URI"])[os.environ["MONGO_DB"]].employees.delete_one({"emp_code": code})


# ---- attendance ---------------------------------------------------------------------------------- #
def test_punch_in_and_out(api, world):
    code = world["main"]
    rec = check(api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2031, 3, 3, 9, 50, 30)}),
                "POST", "/attendance/punch-in", 201)
    assert rec["punch_out"] is None and rec["work_hours"] is None and rec["history"] == [] and rec["late_minutes"] == 20
    check(api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2031, 3, 3, 9, 50)}), "POST", "/attendance/punch-in", 409)
    check(api.post("/attendance/punch-in", json={"emp_code": "EMP000001"}), "POST", "/attendance/punch-in", 404)
    check(api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": 12345}), "POST", "/attendance/punch-in", 422)
    check(api.post("/attendance/punch-in", json={"emp_code": code, "status": "ABSENT"}), "POST", "/attendance/punch-in", 422)

    out = check(api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(2031, 3, 3, 19, 10)}),
                "POST", "/attendance/punch-out", 200)
    assert (out["work_hours"], out["overtime_minutes"], out["half_day"]) == (9.33, 40, False)
    check(api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(2031, 3, 3, 20, 0)}), "POST", "/attendance/punch-out", 409)
    check(api.post("/attendance/punch-out", json={"emp_code": "EMP000001"}), "POST", "/attendance/punch-out", 404)
    check(api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": "x"}), "POST", "/attendance/punch-out", 422)


def test_list_attendance_with_awkward_documents(api, world):
    for key in ("legacy", "hist", "absent", "open"):
        page = check(api.get("/attendance", params={"emp_code": world[key]}), "GET", "/attendance", 200)
        assert page["total"] == 1 and len(page["items"]) == 1
    legacy = api.get("/attendance", params={"emp_code": world["legacy"]}).json()["items"][0]
    assert legacy["half_day"] is False and legacy["history"] == []          # missing fields get their defaults
    hist = api.get("/attendance", params={"emp_code": world["hist"]}).json()["items"][0]
    assert hist["history"][0]["changes"]["punch_in"] == {"from": ms(2031, 3, 4, 10, 5), "to": ms(2031, 3, 4, 9, 28)}
    absent = api.get("/attendance", params={"emp_code": world["absent"]}).json()["items"][0]
    assert absent["punch_in"] is None and absent["punch_out"] is None and absent["work_hours"] is None
    check(api.get("/attendance", params={"date_from": "2031-03-05", "date_to": "2031-03-01"}), "GET", "/attendance", 422)
    check(api.get("/attendance", params={"status": "NOPE"}), "GET", "/attendance", 422)


def test_patch(api, world):
    code = world["hist"]
    body = {"status": "WFH", "reason": "working from home", "regularized_by": "hr.admin"}
    updated = check(api.patch(f"/attendance/{code}/2031-03-04", json=body), "PATCH", "/attendance/{emp_code}/{date}", 200)
    assert len(updated["history"]) == 2 and updated["history"][0]["by"] == "hr.admin"
    check(api.patch(f"/attendance/{code}/2031-03-04", json=body), "PATCH", "/attendance/{emp_code}/{date}", 422)  # changes nothing
    check(api.patch(f"/attendance/{code}/2031-03-09", json=body), "PATCH", "/attendance/{emp_code}/{date}", 404)
    check(api.patch("/attendance/EMP000001/2031-03-04", json=body), "PATCH", "/attendance/{emp_code}/{date}", 404)
    check(api.patch(f"/attendance/{code}/2031-03-04", json={**body, "reason": "x"}), "PATCH", "/attendance/{emp_code}/{date}", 422)
    check(api.patch(f"/attendance/{world['absent']}/2031-03-04", json={"status": "PRESENT", "reason": "was in office", "regularized_by": "hr",
                                                                        "punch_in": ms(2031, 3, 4, 9, 40)}),
          "PATCH", "/attendance/{emp_code}/{date}", 200)


# ---- analytics ---------------------------------------------------------------------------------- #
def test_analytics(api, world):
    code, dept = world["main"], world["dept"]
    m = check(api.get(f"/analytics/employees/{code}/monthly", params={"month": "2031-03"}), "GET", "/analytics/employees/{emp_code}/monthly", 200)
    assert m["attendance_pct"] is not None
    nulls = check(api.get(f"/analytics/employees/{code}/monthly", params={"month": "2020-01"}), "GET", "/analytics/employees/{emp_code}/monthly", 200)
    assert nulls["working_days"] == 0 and nulls["attendance_pct"] is None      # nullable field really is null
    check(api.get("/analytics/employees/EMP000001/monthly", params={"month": "2031-03"}), "GET", "/analytics/employees/{emp_code}/monthly", 404)
    check(api.get(f"/analytics/employees/{code}/monthly", params={"month": "2031-13"}), "GET", "/analytics/employees/{emp_code}/monthly", 422)

    s = check(api.get("/analytics/departments/summary", params={"month": "2031-03", "department": dept}), "GET", "/analytics/departments/summary", 200)
    assert s["items"][0]["headcount"] == 6
    empty = check(api.get("/analytics/departments/summary", params={"month": "2031-03", "department": "nope"}), "GET", "/analytics/departments/summary", 200)
    assert empty["items"] == []
    quiet = check(api.get("/analytics/departments/summary", params={"month": "2020-01", "department": dept}), "GET", "/analytics/departments/summary", 200)
    assert quiet["items"] == []                                                 # nobody had joined yet
    check(api.get("/analytics/departments/summary"), "GET", "/analytics/departments/summary", 422)

    check(api.get("/analytics/leaderboard/late", params={"month": "2031-03", "department": dept, "limit": 5}), "GET", "/analytics/leaderboard/late", 200)
    check(api.get("/analytics/leaderboard/late", params={"month": "2031-03", "limit": 0}), "GET", "/analytics/leaderboard/late", 422)

    t = check(api.get(f"/analytics/departments/{dept}/trend", params={"from": "2031-03-01", "to": "2031-03-10"}),
              "GET", "/analytics/departments/{department}/trend", 200)
    assert len(t["items"]) == 10 and t["items"][0]["attendance_rate"] is None and t["items"][2]["attendance_rate"] is not None
    check(api.get("/analytics/departments/nope/trend", params={"from": "2031-03-01", "to": "2031-03-10"}), "GET", "/analytics/departments/{department}/trend", 404)
    check(api.get(f"/analytics/departments/{dept}/trend", params={"from": "2031-03-10", "to": "2031-03-01"}), "GET", "/analytics/departments/{department}/trend", 422)


# ---- admin -------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("employee_monthly", {"emp_code": "EMP000001", "month": "2031-03"}),
    ("department_summary", {"month": "2031-03"}),
    ("late_leaderboard", {"month": "2031-03"}),
    ("department_trend", {"department": "x", "from": "2031-03-01", "to": "2031-03-05"}),
])
def test_explain_shape(api, endpoint, params):
    body = check(api.get(f"/admin/explain/{endpoint}", params=params), "GET", "/admin/explain/{endpoint}", 200)
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs") and body["explain"]


def test_explain_errors(api):
    check(api.get("/admin/explain/department_summary"), "GET", "/admin/explain/{endpoint}", 422)
    check(api.get("/admin/explain/nonsense"), "GET", "/admin/explain/{endpoint}", 422)


# ---- the checker itself must be able to fail ------------------------------------------------------- #
def test_checker_catches_extra_and_missing_fields():
    schema = response_schema("POST", "/attendance/punch-in", 201)
    good = {"emp_code": "EMP1", "date": "2031-03-04", "status": "PRESENT", "punch_in": ms(2031, 3, 4, 9, 0), "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    v = Draft7Validator(schema, format_checker=FormatChecker())
    assert not list(v.iter_errors(good))
    assert list(v.iter_errors({**good, "id": "abc"}))                      # leaked id
    assert list(v.iter_errors({k: x for k, x in good.items() if k != "half_day"}))  # missing field
    assert list(v.iter_errors({**good, "punch_in": 1783312500}))           # seconds, not ms
    assert list(v.iter_errors({**good, "punch_in": "2031-03-04T09:00:00Z"}))  # string instead of int
    assert list(v.iter_errors({**good, "status": "HOLIDAY"}))
    assert list(v.iter_errors({**good, "date": "2031-02-30"}))
