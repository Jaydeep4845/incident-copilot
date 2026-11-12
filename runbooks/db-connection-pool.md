# Database connection pool exhausted

## Symptoms
Logs: "couldn't get a database connection after 5.0 sec (pool exhausted)". Metrics: pool connections in use
equal to the pool maximum, requests waiting above zero. Requests fail with 503 after about 5 seconds.

## What to check
1. Did the pool size change? Compare db_pool_connections_max before and after; check recent config deploys.
2. Are connections held longer than usual? orders holds a connection for the whole checkout transaction,
   including calls to inventory and payments, so any slow downstream call keeps connections busy.
3. Did the request rate increase (for example because of retries)?

## Likely causes
Pool size reduced by a config change; a slow dependency holding connections; a traffic or retry spike.
Pool exhaustion is often a symptom, not the root cause.

## Safe actions
Roll back a config change that reduced the pool. If a dependency is slow, address the dependency.
