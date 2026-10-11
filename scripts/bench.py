"""Times every endpoint against a running API that holds the large dataset (scripts/seed_large.py).

    python scripts/bench.py [http://localhost:8000]

Each request is repeated and the minimum and median wall-clock time (HTTP round trip) are printed.
"""
import statistics
import sys
import time

import httpx

EMP, DEPT, MONTH = "EMP100007", "Dept3", "2026-07"

REQUESTS = [
    ("GET  /health", "GET", "/health", {}),
    ("GET  /employees", "GET", "/employees", {}),
    ("GET  /employees?department&page=3", "GET", "/employees", {"department": DEPT, "page": 3}),
    ("GET  /attendance (no filter)", "GET", "/attendance", {}),
    ("GET  /attendance page 2", "GET", "/attendance", {"page": 2}),
    ("GET  /attendance page 1000 (deep skip)", "GET", "/attendance", {"page": 1000, "page_size": 100}),
    ("GET  /attendance?emp_code", "GET", "/attendance", {"emp_code": EMP}),
    ("GET  /attendance?status", "GET", "/attendance", {"status": "WFH"}),
    ("GET  /attendance?date range (month)", "GET", "/attendance", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("GET  /attendance?emp+range+status", "GET", "/attendance",
     {"emp_code": EMP, "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT"}),
    ("GET  analytics employee monthly", "GET", f"/analytics/employees/{EMP}/monthly", {"month": MONTH}),
    ("GET  analytics dept summary (all depts)", "GET", "/analytics/departments/summary", {"month": MONTH}),
    ("GET  analytics dept summary (1 dept)", "GET", "/analytics/departments/summary", {"month": MONTH, "department": DEPT}),
    ("GET  analytics late leaderboard", "GET", "/analytics/leaderboard/late", {"month": MONTH}),
    ("GET  analytics late leaderboard (dept)", "GET", "/analytics/leaderboard/late", {"month": MONTH, "department": DEPT}),
    ("GET  analytics dept trend (month)", "GET", f"/analytics/departments/{DEPT}/trend", {"from": "2026-07-01", "to": "2026-07-31"}),
    ("GET  analytics dept trend (92 days)", "GET", f"/analytics/departments/{DEPT}/trend", {"from": "2026-06-15", "to": "2026-09-14"}),
    ("GET  explain attendance_list", "GET", "/admin/explain/attendance_list", {}),
    ("GET  explain employee_monthly", "GET", "/admin/explain/employee_monthly", {"emp_code": EMP, "month": MONTH}),
    ("GET  explain department_summary", "GET", "/admin/explain/department_summary", {"month": MONTH}),
    ("GET  explain late_leaderboard", "GET", "/admin/explain/late_leaderboard", {"month": MONTH}),
    ("GET  explain department_trend", "GET", "/admin/explain/department_trend", {"department": DEPT, "from": "2026-07-01", "to": "2026-07-31"}),
]


def run(base_url: str, rounds: int = 5) -> list:
    rows = []
    with httpx.Client(base_url=base_url, timeout=120) as c:
        for label, method, path, params in REQUESTS:
            times = []
            for _ in range(rounds):
                t = time.perf_counter()
                r = c.request(method, path, params=params)
                times.append((time.perf_counter() - t) * 1000)
                assert r.status_code == 200, (label, r.status_code, r.text[:200])
            rows.append((label, min(times), statistics.median(times)))
    return rows


def print_table(rows) -> None:
    print(f"{'endpoint':46s} {'min ms':>8s} {'median ms':>10s}")
    for label, lo, med in rows:
        flag = "   <-- slowest" if med == max(r[2] for r in rows) else ""
        print(f"{label:46s} {lo:8.0f} {med:10.0f}{flag}")


if __name__ == "__main__":
    print_table(run(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"))
