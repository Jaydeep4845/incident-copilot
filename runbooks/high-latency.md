# High latency (HighLatencyP95)

## Symptoms
p95 latency above 1 second. Users see slow checkouts or slow order history pages.

## What to check
1. Latency by route: is one endpoint slow (for example order history) or all of them?
2. Slow traces: which span dominates the request time? A database span, a cache call, or a call to another
   service?
3. Dependency latency metrics: postgres, redis, inventory, payments, payment-provider.
4. Saturation: CPU usage, DB connection pool in use and waiting, memory.

## Likely causes
Slow queries (missing index, lock contention), an unresponsive cache or dependency, an exhausted connection
pool, CPU starvation.

## Safe actions
Depends on the cause; see the specific runbooks. Do not restart services blindly: restarts hide evidence.
