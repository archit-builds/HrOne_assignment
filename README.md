# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # or set MONGO_URI / MONGO_DB as real environment variables
uvicorn app.main:app --port 8000
```
- `MONGO_URI` (default `mongodb://localhost:27017`) and `MONGO_DB` (default `attendance_db`). Real environment variables win over `.env`.
- MongoDB 6.0+ is required. Indexes are created automatically at startup (idempotent).
- Optional sample data: `python sample_seed.py` (**it empties `employees` and `attendance_logs`** in the selected database first).

## Tests
```bash
pip install pytest httpx
pytest -q
```
The tests need a running MongoDB. They use their own database, `attendance_test`, which is emptied before each test.

## Status
All 12 endpoints in `openapi.yaml` are implemented, including punch-out, PATCH (with history), the four analytics
pipelines and `/admin/explain/{endpoint}`. Explain plans were checked on a 100k-record dataset: all five endpoints use
`IXSCAN` with no `COLLSCAN`.

Notes:
- Working days in the employee monthly summary are counted with simple date arithmetic (at most 31 days); everything that
  reads attendance data is a MongoDB aggregation.
- A trend range of more than 92 calendar days (rows) is rejected with 422.
- If MongoDB is unreachable at startup the app still starts (so `/health` can return 503), but then no indexes exist until restart.
