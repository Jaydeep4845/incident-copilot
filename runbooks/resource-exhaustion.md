# CPU or memory exhaustion

## Symptoms
CPU: cpu_cores of a service near its limit (orders is limited to 1 core) with latency rising on every route
of that service. Memory: memory_rss_mb growing steadily, then the container restarts (OOM kill) and
ServiceDown fires briefly.

## What to check
1. cpu_cores and memory_rss_mb per service over time.
2. Background jobs started in the logs (for example "scheduled report export job started").
3. Recent deploys that add in-memory state (caches, audit trails, buffers).
4. Restarts: up metric dropping to 0 and "started" log lines.

## Likely causes
Runaway background job, memory leak in a new release, traffic spike.

## Safe actions
Restart the service to recover (approval), then roll back the release or stop the job.
