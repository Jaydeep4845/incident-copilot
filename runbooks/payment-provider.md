# Payment provider degraded

## Symptoms
payments logs: "payment provider declined request: upstream 503". payment-provider dependency latency or
errors rising. Checkout errors or slow checkouts; orders may show pool exhaustion because it waits on
payments while holding a database connection.

## What to check
1. payment-provider latency and error rate in payments metrics.
2. Did payments deploy? If not, and provider metrics changed, the cause is likely external.
3. Are retries amplifying the problem (gateway retry logs, orders request rate)?

## Likely causes
External provider outage or slowdown.

## Safe actions
External cause: open a ticket with the provider and degrade gracefully. Do not roll back unrelated deploys.
