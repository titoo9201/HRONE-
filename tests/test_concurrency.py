"""Concurrency guarantees against a running API (API_URL, default http://localhost:8000).

Every request in a group waits on a barrier first, so they are released at the same instant.
  * 50 parallel punch-ins for one employee and date  -> exactly one 201, the rest 409
  * 50 parallel punch-outs for one open record      -> exactly one 200, the rest 409
  * 20 parallel PATCHes of one record               -> history length == number of 200s, the rest 409
  * 20 parallel creates of one emp_code              -> exactly one 201, the rest 409, one document in Mongo
"""
import concurrent.futures as cf
import os
import random
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.environ.get("API_URL", "http://localhost:8000")
IST = timezone(timedelta(hours=5, minutes=30))
CREATED = []


def ms(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


@pytest.fixture(scope="module")
def api():
    c = httpx.Client(base_url=BASE, timeout=60, limits=httpx.Limits(max_connections=100))
    try:
        c.get("/health").raise_for_status()
    except Exception:
        pytest.skip(f"API not running at {BASE}")
    return c


@pytest.fixture(scope="module")
def db():
    d = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
    yield d
    d.attendance_logs.delete_many({"emp_code": {"$in": CREATED}})
    d.employees.delete_many({"emp_code": {"$in": CREATED}})


def employee(api):
    code = f"EMP{random.randint(100000, 999999)}"
    CREATED.append(code)
    r = api.post("/employees", json={"emp_code": code, "name": "C", "email": "c@example.com", "department": "Conc", "joined_on": "2030-01-01"})
    assert r.status_code == 201, r.text
    return code


def fire_together(n, fn):
    """Run fn(i) in n threads released at the same moment; returns the list of results."""
    barrier = threading.Barrier(n)

    def task(i):
        barrier.wait()
        return fn(i)

    with cf.ThreadPoolExecutor(n) as ex:
        return list(ex.map(task, range(n)))


def test_50_parallel_punch_ins_one_winner(api, db):
    code = employee(api)
    body = {"emp_code": code, "punched_at": ms(2031, 3, 3, 9, 30)}
    codes = [r.status_code for r in fire_together(50, lambda i: api.post("/attendance/punch-in", json=body))]
    assert Counter(codes) == {201: 1, 409: 49}, Counter(codes)
    assert db.attendance_logs.count_documents({"emp_code": code}) == 1


def test_50_parallel_punch_outs_one_winner(api, db):
    code = employee(api)
    assert api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2031, 3, 3, 9, 30)}).status_code == 201
    out = {"emp_code": code, "punched_at": ms(2031, 3, 3, 18, 30)}
    results = fire_together(50, lambda i: api.post("/attendance/punch-out", json=out))
    codes = [r.status_code for r in results]
    assert Counter(codes) == {200: 1, 409: 49}, Counter(codes)
    stored = db.attendance_logs.find_one({"emp_code": code})
    assert stored["work_hours"] == 9.0 and stored["punch_out"] is not None


def test_20_parallel_patches_never_lose_history(api, db):
    code = employee(api)
    assert api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2031, 3, 3, 10, 5)}).status_code == 201
    assert api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(2031, 3, 3, 19, 10)}).status_code == 200

    def patch(i):  # every request asks for a different punch_in, so each one is a real change
        return api.patch(f"/attendance/{code}/2031-03-03", json={"punch_in": ms(2031, 3, 3, 9, i), "reason": f"fix number {i}", "regularized_by": f"hr{i}"})

    results = fire_together(20, patch)
    codes = Counter(r.status_code for r in results)
    assert set(codes) <= {200, 409} and codes[200] >= 1, codes
    assert codes[200] + codes[409] == 20
    history = db.attendance_logs.find_one({"emp_code": code})["history"]
    assert len(history) == codes[200], f"{len(history)} history entries for {codes[200]} successful PATCHes"
    # every winner's reason is in the history exactly once, and the entries form an unbroken chain
    winners = sorted(r.json()["history"][-1]["reason"] for r in results if r.status_code == 200)
    assert sorted(h["reason"] for h in history) == winners
    for a, b in zip(history, history[1:], strict=False):
        assert a["changes"]["punch_in"]["to"] == b["changes"]["punch_in"]["from"]
    print(f"\n20 parallel PATCH -> {dict(codes)}; history entries stored: {len(history)}")


def test_20_parallel_creates_of_one_emp_code(api, db):
    code = f"EMP{random.randint(100000, 999999)}"
    CREATED.append(code)
    body = {"emp_code": code, "name": "Same", "email": "s@example.com", "department": "Conc", "joined_on": "2030-01-01"}
    codes = [r.status_code for r in fire_together(20, lambda i: api.post("/employees", json=body))]
    assert Counter(codes) == {201: 1, 409: 19}, Counter(codes)
    assert db.employees.count_documents({"emp_code": code}) == 1
