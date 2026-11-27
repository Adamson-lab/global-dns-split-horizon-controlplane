# Design Notes: Global DNS Architecture with Split-Horizon and Failover

This system separates zone lease management and hash-chained WAL recording (`controlplane/zone_lease.py`, `controlplane/server.py`, `storage/wal_chain.py`) from DNS failover execution (`dataplane/dns_failover.py`) over AWS Route53 and S3 with Open Policy Agent (OPA) checks.

## Trust-Boundary and Network Enclave Topology

```mermaid
flowchart TB
    subgraph ExternalIngress["External Untrusted Ingress"]
        Caller["Caller"]
    end

    subgraph ControlPlaneEnclave["Control Plane Enclave"]
        API["FastAPI Server :8080"]
        LeaseMgr["ZoneFencingLeaseManager"]
        WAL["HashChainedWal"]
        API --> LeaseMgr
        LeaseMgr --> WAL
    end

    subgraph PolicyAndCloud["Policy and Cloud Plane"]
        OPA["OPA :8181"]
        AWS["LocalStack Route53 & S3 :4566"]
    end

    subgraph DataPlane["Data Plane"]
        Engine["SplitHorizonFailoverEngine"]
    end

    Caller -->|"HTTP + HMAC-SHA256 :8080"| API
    API -->|"HTTP REST Rego Eval :8181"| OPA
    API --> Engine
    Engine -->|"check_token"| LeaseMgr
    Engine -->|"boto3 Route53 & S3 :4566"| AWS
```

## Distributed Concurrency and Failover Sequence

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant S as FastAPI Server :8080
    participant O as OPA :8181
    participant L as ZoneFencingLeaseManager
    participant E as SplitHorizonFailoverEngine

    C->>S: POST /v1/dns/failover
    S->>S: verify_dns_request
    alt Invalid signature or skew
        S-->>C: 401 Unauthorized
    else Valid signature
        par Policy evaluation
            S->>O: POST /v1/data/dns/splithorizon
            S->>O: POST /v1/data/dns/failover
        end
        alt Policy denied
            S-->>C: 403 Forbidden
        else Policy allowed
            critical Lease acquisition
                S->>L: acquire(zone_key, owner_id)
            option Active conflict
                S-->>C: 409 Conflict
            end
            S->>E: resolve_and_failover(fencing_token)
            alt Stale or expired token
                E-->>S: StaleTokenError
                S-->>C: 409 Conflict
            else Token valid
                E-->>S: FailoverOutcome
                S-->>C: 200 OK
            end
        end
    end
```

## Zone Lease Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Active: acquire() issues next fencing_token
    note right of Active: Monotonic token checked before Route53 and S3 calls
    Active --> Active: same owner re-acquires or extends grace
    Active --> Expired: expires_at_utc reached
    Expired --> Active: new owner acquires higher fencing_token
    Active --> [*]
```

## WAL and Failover Record Lineage

```mermaid
flowchart LR
    Lease["ZoneLease (fencing_token)"] --> Engine["SplitHorizonFailoverEngine"]
    Engine --> WAL["WalEntry (seq, prev_hash, entry_hash)"]
    Engine --> S3["S3 Snapshot JSON"]
```

## Trade-Offs and Limitations

Lease expirations and UTC change windows use UTC timestamps so Daylight Saving Time changes do not shift cutoffs. Off-window failovers allow up to a 15-minute grace lock when an incident ID of at least 4 characters is provided. Transient Route53 and S3 errors retry up to 4 times with per-attempt delay capped at `2.0s`.

Limitations: `ZoneFencingLeaseManager` and `HashChainedWal` reside in single-process memory rather than a distributed store, and `list_hosted_zones` reads a single page without pagination loops.
