# Slow database queries

## Symptoms
Logs: "slow query <name> took N ms". Latency on routes that read from Postgres; database CPU high.

## What to check
1. Which query is slow (select_user_orders, select_stock, insert_order, reserve)?
2. Did a migration run recently? Migrations that drop or rebuild indexes make queries fall back to full
   table scans. Indexes can also be dropped manually, outside any deploy.
3. Are queries cancelled by a statement timeout? That points to lock waits rather than scans.

## Likely causes
Missing index (full table scan on orders, 4M rows), lock contention, a heavy ad-hoc query.

## Safe actions
Recreate the missing index (CREATE INDEX CONCURRENTLY) with the database owner. Rolling back application
code does not restore a dropped index.
