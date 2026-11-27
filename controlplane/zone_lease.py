from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Any

from storage.wal_chain import HashChainedWal


class ZoneLeaseConflict(Exception):
    pass


@dataclass
class ZoneLease:
    zone_key: str
    owner_id: str
    fencing_token: int
    expires_at_utc: float


class ZoneFencingLeaseManager:
    # Monotonic fencing tokens prevent a stalled reconciler from applying stale DNS failover state.
    def __init__(self, wal: HashChainedWal | None = None, ttl_sec: float = 20.0) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, ZoneLease] = {}
        self._token_seq: int = 0
        self._wal = wal or HashChainedWal()
        self._ttl_sec = ttl_sec

    def acquire(
        self,
        zone_key: str,
        owner_id: str,
        now_utc: float | None = None,
        grace_extension_sec: float = 0.0,
    ) -> ZoneLease:
        # Use UTC epoch timestamps so Daylight Saving Time transitions never shift lease expiry.
        now = time.time() if now_utc is None else now_utc
        clamped_grace = min(max(grace_extension_sec, 0.0), 900.0)
        with self._lock:
            current = self._leases.get(zone_key)
            if current is not None and current.expires_at_utc > now and current.owner_id != owner_id:
                raise ZoneLeaseConflict(f"zone {zone_key} actively leased by {current.owner_id}")

            self._token_seq += 1
            lease = ZoneLease(
                zone_key=zone_key,
                owner_id=owner_id,
                fencing_token=self._token_seq,
                expires_at_utc=now + self._ttl_sec + clamped_grace,
            )
            self._leases[zone_key] = lease
            self._wal.append(
                op_type="zone_lease_acquired",
                fencing_token=lease.fencing_token,
                payload={
                    "zone_key": zone_key,
                    "owner_id": owner_id,
                    "expires_at_utc": lease.expires_at_utc,
                    "grace_sec": clamped_grace,
                    "recorded_at_utc": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                },
            )
            return lease

    def check_token(self, zone_key: str, fencing_token: int, now_utc: float | None = None) -> bool:
        now = time.time() if now_utc is None else now_utc
        with self._lock:
            lease = self._leases.get(zone_key)
            if lease is None:
                return False
            if lease.expires_at_utc <= now:
                return False
            return fencing_token == lease.fencing_token

    def wal(self) -> HashChainedWal:
        return self._wal

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "leases": {
                    k: {
                        "owner_id": v.owner_id,
                        "fencing_token": v.fencing_token,
                        "expires_at_utc": v.expires_at_utc,
                    }
                    for k, v in self._leases.items()
                },
                "wal_length": len(self._wal.entries()),
            }
