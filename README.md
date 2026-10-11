# Employee Attendance & Analytics API

FastAPI + MongoDB service for punching in/out, manual corrections and attendance analytics.
All application code is in `app/main.py`; the contract is `openapi.yaml`, the stored data is described in `DATA_MODEL.md`.
Needs **Python 3.11+** and **MongoDB 6.0+** (developed against `mongo:7`).

## Setup and run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # MONGO_URI and MONGO_DB are required; real env vars win over .env
docker run -d -p 27017:27017 mongo:7
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `python sample_seed.py` loads the nine sample documents.

## Tests
```bash
pip install -r requirements-dev.txt
pytest tests/test_helpers.py                           # pure unit tests, no server or database needed
pytest                                                 # everything: needs the API running on :8000 against your MONGO_DB
```
The integration tests clean up the data they create. `tests/test_explain.py` builds and drops its own 100k-record scratch database and starts its own API copy.
`scripts/seed_large.py` (100k awkward records), `scripts/bench.py` (timings) and `scripts/cold_start_check.py` (start-up time with no indexes) are optional helpers; they only touch databases named `*scratch*`/`*test*`.

## Known limitations
- `GET /analytics/departments/summary` without a `department` takes about 2 s on ~115k records. It starts from `employees` and runs a `$lookup` per employee, so that people with no logs are counted (R9). A logs-first pipeline got it to about 1 s but made the headcount logic harder to follow, so I did not use it.
- `/admin/explain/...` never returns 404: for an unknown employee or department it just explains a query that finds nothing.
- `GET /attendance` uses `skip`/`limit`, so very deep pages (page 1000 of 100) are slower (~0.2 s) than early ones.
- Where the spec was open I chose: a punch-out earlier than every punch-in gives 404 ("no punch-in found"), equal to the punch-in gives 422; `moving_avg_7d` averages the already rounded 4-decimal rates.
- Checked on Python 3.10 and 3.13 (compiles on 3.10, `vermin` reports a minimum of 3.9); I had no 3.11 interpreter to run it on.
- `REVIEW.md` and `DECISIONS.md` are written by the author separately.
