# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity + race-safe creates), `(department, joined_on)` for
   department-scoped queries needing the R9 cutoff. `attendance_logs`: unique `(emp_code, date)` - the natural key,
   so it also makes punch-in race-safe; `(date desc, emp_code asc)` for the default listing sort and date-only
   filters; `(status, date desc, emp_code)` for status-filtered listing. Rejected a standalone `status` index - every
   status query also sorts by `date`, so one compound index serves both.

2. **Punch-in race.** Both requests look up the employee, build an identical document, and call `insert_one` -
   neither checks first. The unique `(emp_code, date)` index decides: MongoDB allows the first insert and raises
   `DuplicateKeyError` on the second. The loser catches that and returns 409. No check-then-insert window exists.

3. **Ties.** `$setWindowFields`/`$rank`, sorted only by `total_late_minutes`, gives equal totals the same rank and
   skips the next one automatically. `limit` is applied as `rank <= limit` after ranking, so every employee tied at
   the cutoff is returned even if that's more rows than `limit`.

4. **Headcount.** The pipeline starts from `employees` (filtered by `joined_on <= period end`) and left-joins each
   employee's logs via `$lookup` + `$unwind(preserveNullAndEmptyArray: true)`. An employee with no logs still
   produces one row with a null `logs`, so `$addToSet` on `emp_code` counts them while contributing 0 to every sum.

5. **100x the data.** I'd stop recomputing `department_trend`'s per-day headcount with a fresh `$lookup` on every
   densified day (today it scales with range x employees) and precompute a small daily-headcount series per
   department instead, updated as employees are created.
