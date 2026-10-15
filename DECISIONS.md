# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** We created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to guarantee uniqueness and prevent race conditions. For queries, we added compound indexes on `attendance_logs(date, emp_code)` for sorted pagination, `attendance_logs(date, late_minutes)` for leaderboard filtering, and `employees(department, joined_on)` for department lookups. We considered and rejected a standalone index on `status` because its low cardinality provides poor selectivity compared to compound date-status filtering.

2. **Punch-in race.** Both requests resolve the timestamp to the same IST attendance date. When both execute an atomic `insert_one`, MongoDB's unique index on `(emp_code, date)` allows exactly one document to insert (returning 201). The second request immediately raises a `DuplicateKeyError`, which our route catches and cleanly maps to HTTP 409 Conflict without checking the database twice.

3. **Ties.** We use MongoDB's `$rank` window function for standard competition ranking (e.g. 1, 2, 2, 4) sorted by late minutes descending and emp_code ascending. Because `limit` is applied after ranking via `rank <= limit`, tied employees sharing a qualifying rank are both included, correctly returning more than `limit` rows when a tie spans the cutoff.

4. **Headcount.** The aggregation pipeline begins on `employees` (filtering `joined_on <= month_end`) rather than logs. It performs a `$lookup` into `attendance_logs` and unwinds with `preserveNullAndEmptyArrays: true`. Grouping by department counts employees via `$addToSet: "$emp_code"`, guaranteeing employees with zero logs are accurately counted in headcount.

5. **One thing you would change for 100x data.** At 100x scale, I would incrementally maintain daily and monthly summary rollups using MongoDB change streams or write-time hooks rather than running full cross-collection aggregations on raw punch logs on demand.

