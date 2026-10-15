# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, duplicate guard), `(department, emp_code)` (filtered, sorted listing, trend), `joined_on` (headcount). `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race guard, employee queries), `(date desc, emp_code)` (default list order, month scans), `(status, date desc, emp_code)` (status filter). I rejected `(emp_code, punch_in)` for punch-out: the `(emp_code, date)` index already narrows to one employee's few records.
2. **Punch-in race.** Both requests validate and insert. The unique `(emp_code, date)` index lets MongoDB accept one insert; the other raises `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.
3. **Ties.** `$setWindowFields` with `$rank` gives ties the same rank (1, 2, 2, 4). I filter `rank <= limit`, so everyone tied at the cutoff is returned, even if that exceeds `limit`.
4. **Headcount.** The summary pipeline starts from `employees` (joined by month end) and `$lookup`s each person's logs, so someone with no logs still counts once.
5. **100x data.** Pre-aggregate monthly totals per employee, updated at write time, so analytics stop scanning raw logs.
