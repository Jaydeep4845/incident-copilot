# Suspected bad deploy

## Symptoms
Errors or latency start within a few minutes after a deploy of a service on the failing path.

## What to check
1. Deploy history: service, version, change summary, config keys changed.
2. Does the deployed service own the failing operation? Errors in a different service may be unrelated.
3. Code errors (KeyError, TypeError, exceptions in logs) point to code changes; timeouts, pool limits and
   credential errors point to configuration changes.
4. Compare the version label in traces and logs before and after.

## Likely causes
New code path with a bug; a config change (timeouts, pool sizes, retries, credentials).

## Safe actions
Roll back the deploy (requires approval). Keep the evidence for the post-mortem.
