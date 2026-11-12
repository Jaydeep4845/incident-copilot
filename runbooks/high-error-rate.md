# High error rate (HighErrorRate)

## Symptoms
More than 5% of a service's requests return 5xx. The alerting service is often not the origin:
gateway errors usually reflect errors in orders, and orders errors often come from its dependencies.

## What to check
1. Which service has the highest error ratio, and when did it start? Compare with the service that paged.
2. Error logs of that service, grouped by message. Look for a dependency name (postgres, redis, inventory,
   payments, payment-provider) and an error class (Timeout, PoolTimeout, KeyError, connection refused).
3. Deploy history: did any service deploy shortly before the start? Check whether the deployed service is
   on the failing path; a deploy close in time is not proof.
4. Traces with errors: which span carries the error status?

## Likely causes
Bad deploy (code or config), a failing dependency, database problems, resource exhaustion.

## Safe actions
Roll back a deploy that is clearly correlated and on the failing path. Otherwise escalate to the owner of
the failing dependency.
