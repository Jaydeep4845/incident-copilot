# Retry storms

## Symptoms
gateway logs: "retrying checkout (attempt N of M)". Request rate to orders rises well above user traffic;
downstream pools and queues saturate.

## What to check
1. Retry count and backoff configuration (recent gateway config deploys).
2. Ratio of orders requests to gateway checkouts.
3. What made the first attempts fail? A small, normal error rate can be amplified into an outage.

## Likely causes
Aggressive retries without backoff, combined with a partial downstream failure.

## Safe actions
Roll back the retry configuration; add exponential backoff and a retry budget.
