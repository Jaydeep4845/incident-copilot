# Redis cache unavailable or slow

## Symptoms
inventory logs: "redis unavailable, falling back to database". cache lookups with result="error" rising.
Stock lookups slow by about 1 second (the cache timeout) before falling back to Postgres.

## What to check
1. Cache lookup results over time: hit, miss, error.
2. Redis dependency latency. Timeouts suggest a hung server; connection refused suggests it is down.
3. Did inventory deploy recently? A client upgrade can break cache access, but check whether errors started
   exactly at the deploy time or later.

## Likely causes
Redis down or hung, network issues, client misconfiguration after a deploy.

## Safe actions
Restore Redis (restart the cache, with approval). The database fallback keeps the shop working, but slowly.
