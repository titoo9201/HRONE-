"""Cold-start check on a large database, then a speed run.

    python scripts/cold_start_check.py [--keep]

1. seeds ~115k attendance records into a scratch database (name below) and DROPS every index except _id,
   like the grader's database
2. copies ONLY app/ to a temp folder (no .env anywhere near it) and starts uvicorn there with MONGO_URI /
   MONGO_DB given purely as environment variables
3. measures the time from process start until GET /health first returns 200
4. checks that startup created every index
5. runs scripts/bench.py against that server
Finally the scratch database is dropped (unless --keep).
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / ".env")
SCRATCH_DB = "attendance_cold_scratch"
PORT = 8766
EXPECTED_INDEXES = {
    "employees": {"_id_", "emp_code_unique", "dept_emp_code", "dept_joined_on", "joined_on"},
    "attendance_logs": {"_id_", "emp_code_date_unique", "date_desc_emp_code", "emp_code_punch_in_desc"},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not drop the scratch database at the end")
    args = ap.parse_args()
    uri = os.environ["MONGO_URI"]
    os.environ["MONGO_DB"] = SCRATCH_DB  # so that importing seed_large (and app.main) points at the scratch DB
    import bench
    import seed_large

    client = MongoClient(uri, tz_aware=True)
    db = client[SCRATCH_DB]
    stats = seed_large.seed(db, drop_indexes=True)
    print(f"seeded {stats['attendance_logs']:,} attendance records and {stats['employees']:,} employees into '{SCRATCH_DB}'")
    for coll in EXPECTED_INDEXES:
        names = {i["name"] for i in db[coll].list_indexes()}
        assert names == {"_id_"}, f"{coll} should only have _id before start, has {names}"
    print("indexes before start: only _id on both collections")

    workdir = Path(tempfile.mkdtemp(prefix="cold_start_"))
    shutil.copytree(ROOT / "app", workdir / "app", ignore=shutil.ignore_patterns("__pycache__"))
    assert not any(workdir.glob(".env*")), "a .env must not be present for this test"
    env = {k: v for k, v in os.environ.items() if k not in ("MONGO_URI", "MONGO_DB")}
    env.update(MONGO_URI=uri, MONGO_DB=SCRATCH_DB)
    proc = None
    try:
        t0 = time.perf_counter()
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
                                cwd=workdir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://localhost:{PORT}"
        first_answer = ready = None
        while time.perf_counter() - t0 < 60:
            try:
                r = httpx.get(base + "/health", timeout=5)
                first_answer = first_answer or time.perf_counter() - t0
                if r.status_code == 200:
                    ready = time.perf_counter() - t0
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.02)
        assert ready is not None, "/health never returned 200 within 60 s"
        print(f"uvicorn start -> first HTTP answer: {first_answer:.2f} s,  -> /health 200: {ready:.2f} s   (limit 20 s)")

        for coll, expected in EXPECTED_INDEXES.items():
            names = {i["name"] for i in db[coll].list_indexes()}
            print(f"indexes on {coll}: {sorted(names)}")
            assert names == expected, f"{coll}: missing {expected - names}, unexpected {names - expected}"
        print("all indexes were created by the app at startup")

        print()
        bench.print_table(bench.run(base))
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=20)
        shutil.rmtree(workdir, ignore_errors=True)
        if not args.keep:
            client.drop_database(SCRATCH_DB)
            print(f"\nscratch database '{SCRATCH_DB}' dropped")


if __name__ == "__main__":
    main()
