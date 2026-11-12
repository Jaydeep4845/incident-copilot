# Service down or unreachable

## Symptoms
ServiceDown alert; callers log "connection refused" or name-resolution errors for that service.

## What to check
1. up metric per service.
2. Callers' error logs: which dependency do they fail to reach?
3. Restarts and crash logs; recent deploys of the service.

## Likely causes
Crash, OOM kill, failed deploy, service stopped.

## Safe actions
Restart the service (approval). If it crashes again, roll back its last deploy.
