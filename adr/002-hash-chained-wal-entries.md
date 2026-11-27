# ADR 002: Hash-Chained WAL Entries for DNS Failover Events

## Context
Split-horizon DNS changes across internal and external views require an auditable sequence showing each lease and failover event.

## Decision
Record each mutation as a `WalEntry` in `storage/wal_chain.py` linking `entry_hash` to `prev_hash` via SHA-256.

## Consequences
Any alteration or omission breaks `HashChainedWal.verify()`. Entries are currently stored in memory and emitted per failover to S3.
