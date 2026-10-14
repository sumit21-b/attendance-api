# Employee Attendance & Analytics API

A backend API built with **FastAPI** and **MongoDB** (6.0 or newer). All the code is in `app/main.py`.

## What it does
- Create and list employees.
- Punch in and punch out (late minutes, overtime, work hours and half day are calculated by the API).
- Correct a day's record. Every correction is saved in the record's `history`.
- Analytics: employee monthly summary, department summary, late leaderboard and department trend.
- `/admin/explain/{endpoint}` shows the query plan, so you can check the indexes are used.

## How to run
```bash
python -m venv .venv
.venv\Scripts\activate          # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```
Set the database in a `.env` file or in environment variables:
```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
Then start the server:
```bash
uvicorn app.main:app --port 8000
```
Open `http://localhost:8000/docs` to try the endpoints. Indexes are created automatically at startup.

## Notes
- `.env` is not in the repository. Use your own MongoDB (local or Atlas).
- See `REVIEW.md` for the bugs fixed in the starter code and `DECISIONS.md` for my design choices.
