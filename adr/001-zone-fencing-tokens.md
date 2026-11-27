# ADR 001: Fencing Tokens for Split-Horizon Zone Failover

## Context
Concurrent DNS reconcilers can race during health-check flaps and overwrite records with stale failover targets.

## Decision
Issue a monotonically increasing `fencing_token` per lease acquisition in `ZoneFencingLeaseManager` and verify it in `SplitHorizonFailoverEngine.resolve_and_failover` before Route53/S3 mutations.

## Consequences
Delayed workers holding an older token fail with `StaleTokenError` rather than clobbering newer state. Lease state is kept in process memory in this implementation.
