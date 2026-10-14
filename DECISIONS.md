# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (no duplicates, fast lookup), `department + emp_code` (filtered list), `joined_on` (headcount). `attendance_logs`: unique `emp_code + date` (one record per day, stops double punch-in, monthly queries), `date desc + emp_code` (list sorting, month ranges), `status + date desc + emp_code` (status filter). I rejected an index on `late_minutes`: the date range already narrows to one month, and extra indexes slow writes.

2. **Punch-in race.** Both requests find the employee and call `insert_one`. The unique index on (`emp_code`, `date`) lets only one insert succeed. The other gets `DuplicateKeyError` and I return 409. I never check first and then insert.

3. **Ties.** I rank first with `$rank`, then keep `rank <= limit`. Everyone tied at the cutoff is returned, so the list can be longer than `limit`.

4. **Headcount.** The summary starts from `employees`, not the logs. `$lookup` joins the logs and `$unwind` keeps employees with no logs, so they still count.

5. **100x data.** I would stop reading raw logs on every analytics call. I would keep a daily summary per employee, updated on each punch and correction.
