# Database lock contention

## Symptoms
Writes time out while reads work. Logs: "reservation query cancelled: statement timeout". Inventory
reservations return 503 after about 3 seconds.

## What to check
1. Which operation fails: reserve (UPDATE) or select_stock (SELECT)?
2. Are only some rows (SKUs) affected? A transaction locking a range of rows blocks only those rows.
3. Long-running transactions: maintenance jobs, manual sessions, stuck migrations.

## Likely causes
A long transaction holding row locks; a migration taking a table lock.

## Safe actions
Identify the blocking session (pg_stat_activity, pg_locks) and ask its owner to end it. Restarting
application services does not release the lock.
