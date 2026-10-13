# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne Software Engineer Trainee assignment.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Create a local .env (not committed) with MONGO_URI / MONGO_DB, e.g.:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=attendance_db
# Local Mongo via Docker: docker run -p 27017:27017 mongo:7
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
Requires MongoDB 6.0+ (the grader uses 7.0; `$densify` and `$rank` need 5.1+/5.0+ respectively, both covered).
Atlas free tier (M0) also works. Interactive docs at `http://localhost:8000/docs` once it's running.

## What's implemented
- All endpoints in `openapi.yaml`, including the three "hard" analytics endpoints as MongoDB aggregation
  pipelines (no Python-side looping over records) and `GET /admin/explain/{endpoint}`.
- R1-R10 business rules: see the helper functions at the top of `app/main.py` (each docstring cites the rule
  number it implements) and `test_helpers.py` below.
- Concurrency: punch-in and employee creation rely on unique indexes + catching `DuplicateKeyError`, never
  check-then-insert. Punch-out uses an atomic `find_one_and_update` compare-and-set on `punch_out: None`.
  Regularization uses optimistic concurrency (the update filter pins every mutable field to the value that was
  read, so a concurrent edit can't silently overwrite it - the loser gets 409).
- Part A: see `REVIEW.md` for every defect found in the starter and how each was fixed.
- Part C: see `DECISIONS.md`.

## Testing notes (please read before the live walkthrough)
- `test_helpers.py` (not part of the submission contract, included for transparency) unit-tests the R1-R5 date/time/
  rounding helpers against every worked example in `PROBLEM_STATEMENT.docx` and `DATA_MODEL.md` - all pass.
- The CRUD, validation and concurrency-guard endpoints were smoke-tested end to end against `mongomock` (an
  in-memory MongoDB stand-in) - employee creation/validation/duplicates, punch-in/punch-out (including the
  sequential-vs-concurrent 404-vs-409 distinction), regularization and its history/no-op/invalid-combo rules, and
  pagination all pass.
- **The four analytics pipelines (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`)
  were not run against a live MongoDB** - `mongomock` doesn't implement `$dateFromString`, `$densify`,
  `$setWindowFields` or `$rank`, and no MongoDB server was available in the environment they were written in. They
  were written and reviewed carefully against MongoDB's documented aggregation semantics and cross-checked against
  every rule in section 6 and "The three hard endpoints" of `PROBLEM_STATEMENT.docx`, but **I have not personally
  watched them return correct numbers on real data yet**. Before submitting, run `sample_seed.py` against a real
  MongoDB 6.0+, hit each analytics endpoint by hand, and - per the assignment's own instructions - build a few of
  the awkward cases (a tie, a zero-log employee, a weekend record, a mid-month joiner, an overnight shift, a missing
  weekday) and check the numbers. I'd also open `/docs` and try each endpoint from there once with the Mongo
  connection live, since that's the fastest way to catch anything I got wrong.

## Known gaps
- None of the "You" endpoints were left unimplemented; everything in section 7 of `PROBLEM_STATEMENT.docx` is
  present.
