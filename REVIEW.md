# REVIEW.md

Defects found in the starter `app/main.py`. Each was fixed in the new file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always returns 200, never touches MongoDB. | Stop MongoDB: `/health` still says ok (must be 503). | Ping MongoDB; 503 on failure. |
| 2 | `compute_late_minutes` | Builds shift start on the **UTC** date of a naive datetime; shift times are IST. Punch 09:28 IST (03:58 UTC) is compared with 09:30 UTC. | Any normal punch-in gets a wrong `late_minutes`. | Convert to IST (R1), use the attendance date's shift start. |
| 3 | `compute_late_minutes` | `int(seconds/60) > 10` is not "strictly more than 10:00". 09:40:01 gives minutes=10, so it returns 0 (must be 10). | Punch 09:40:01 on a 09:30 shift. | `secs > 600`, then `secs // 60` (R2). |
| 4 | `compute_late_minutes` | Ignores overnight shifts (start on previous day). | 22:00-06:00 shift punching at 02:00. | Shift start comes from the attendance date (R1). |
| 5 | `compute_work_hours` | `round()` is banker's/float rounding, not half-up; also no truncation to seconds. | 1h 0m 18s gives 1.0, must be 1.01. | `Decimal` with `ROUND_HALF_UP` on whole seconds (R4). |
| 6 | `compute_overtime` | No 30-minute minimum; end time built on `date_str` so overnight shifts end on the wrong day; `max(0, ...)` returns 5 minutes as 5. | Out at 18:35 gives 5 (must be 0); overnight gives about -16h. | Shift end is on the next day for overnight shifts; floor minutes, 0 unless >= 30 (R3). |
| 7 | `EmployeeIn` | No validation: bad `emp_code`, bad email, `"25:99"` shifts, `joined_on: "abc"`, equal shifts, empty name all accepted. | POST with `shift_start:"99:99"` returns 201. | Pydantic patterns, lengths, date check, `shift_start != shift_end`. |
| 8 | `create_employee` | Check-then-insert race, no unique index: parallel creates give several 201s and duplicate `emp_code`s. | Fire 10 parallel POSTs. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 9 | `create_employee` | `datetime.now()` is local naive time; returns a raw datetime (serialized as ISO string) instead of epoch ms. | `created_at` is a string. | UTC `now`, converted to epoch ms. |
| 10 | `list_employees` | `skip = page * page_size` skips the first page (should be `(page-1)`). | `page=1` returns employees 21-40. | `(page - 1) * page_size`. |
| 11 | `list_employees` | `total` is `count_documents({})`, ignores the department filter. | Filter by department, `total` is the whole collection. | Count with the same filter. |
| 12 | `list_employees` | No sort (must be by `emp_code`); `page`/`page_size` unvalidated (0, -1, 100000 accepted; negative skip raises, giving 500). | `page_size=100000`, `page=0`. | `Query(ge=1, le=100)`; sort by `emp_code`. |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` raises `TypeError` -> **500** (must be 404). | Punch in for `EMP9999`. | Explicit 404. |
| 14 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses server-local time (naive) and keeps milliseconds. The `date` is the server's date, not IST (R1); no overnight rule. | Server in UTC: punch at 00:30 IST is stored on the previous day. | `ms_to_dt` (UTC, truncated to seconds); `attendance_date` follows R1. |
| 15 | `punch_in` | Check-then-insert race: two simultaneous requests can both pass `find_one`, giving two 201s. | Parallel identical requests. | Unique index `(emp_code, date)`; `DuplicateKeyError` -> 409. |
| 16 | `punch_in` | `status` is a plain string: `ABSENT`, `LEAVE` or `"foo"` is accepted. `punched_at` accepts floats, strings, seconds. | `status:"ABSENT"` returns 201. | `Literal` presence statuses; strict ranged epoch-ms type (422). |
| 17 | `punch_in` | Leaks `id` (the `_id`) and returns raw datetimes instead of epoch ms. | Look at the response. | Serializer without `_id`; instants in epoch ms. |
| 18 | `list_attendance` | `list(find(q))` loads the whole collection into memory, then sorts and slices in Python. Slow at 100k. | 100k records. | `sort/skip/limit` in MongoDB, `count_documents`. |
| 19 | `list_attendance` | Sort is by `date` only (needs `emp_code` as tie-break); `status` and dates unvalidated; `date_from > date_to` not 422; leaks `id`; legacy docs without `half_day`/`history` lack those fields. | Same-day records come back in random order; `date_from=zzz`. | Sort `date desc, emp_code asc`, validation, serializer with defaults. |
| 20 | startup | No indexes at all: every query is a COLLSCAN; no uniqueness guarantees. | `explain` shows COLLSCAN. | `ensure_indexes()` at startup (idempotent). |
| 21 | client | `MongoClient` is not `tz_aware`, so datetimes come back naive (silent timezone bugs). No server selection timeout. | Compare/convert datetimes. | `tz_aware=True`, UTC; 3s timeout; Mongo errors become 503. |

**Looked at and decided not to be defects:** `load_dotenv()` (default `override=False`, so real environment variables already win); `doc.pop("_id")` in `create_employee` (needed because `insert_one` adds `_id`); building the client at import time (PyMongo connects lazily and is thread-safe); sync `def` endpoints (FastAPI runs them in a thread pool, fine with PyMongo); `IST` as a fixed offset (India has no DST).
