# DECISIONS.md

1. **Indexes.**
   - `employees`: `emp_code` (unique, ensures duplicate-safe employee creation); `(department, joined_on)` and `(joined_on)` (enable indexed lookups for department summary and trend headcount).
   - `attendance_logs`: `(emp_code, date)` (unique, guarantees at most one record per employee/day and serves monthly summary); `(date, -1, emp_code, 1)` (serves attendance listing sort); `(date, 1, late_minutes, 1, emp_code, 1)` (enables IXSCAN for late leaderboard); `(emp_code, punch_in, -1)` (supports punch-out lookup).
   - Rejected: indexing `(emp_code, date, work_hours, late_minutes)` — rejected as unnecessarily wide; compound `(emp_code, date)` is already highly selective and minimizes write amplification.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` arbitrates concurrency at the storage engine. The first insert succeeds and returns 201. The second throws a `DuplicateKeyError`, which our handler intercepts and returns as HTTP 409.

3. **Ties.**
   We rank using `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4), then filter `rank <= limit`. Tied employees share the same rank; if tied at the cutoff, both satisfy `rank <= limit`, returning $> \text{limit}$ rows, ordered by `total_late_minutes` desc, then `emp_code` asc.

4. **Headcount.**
   The department aggregation originates on `employees` (filtering `joined_on <= period_end`), not `attendance_logs`. We `$lookup` attendance records per employee. Employees with zero logs yield an empty array, but their employee document still contributes 1 to `{ $sum: 1 }` in `$group`.

5. **One thing to change for 100x data.**
   Materialize daily and monthly attendance rollups via change streams or worker queues upon punch-out/regularization, replacing real-time cross-collection aggregation over tens of millions of documents with point reads.
