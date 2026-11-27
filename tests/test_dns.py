import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from controlplane.zone_lease import ZoneFencingLeaseManager, ZoneLeaseConflict
from dataplane.dns_failover import SplitHorizonFailoverEngine, StaleTokenError, sign_dns_request, verify_dns_request
from storage.wal_chain import HashChainedWal


class TestGlobalDnsSplitHorizon(unittest.TestCase):
    def setUp(self) -> None:
        self.wal = HashChainedWal()
        self.leases = ZoneFencingLeaseManager(wal=self.wal, ttl_sec=20.0)
        self.fake_r53 = MagicMock()
        self.fake_r53.list_hosted_zones.return_value = {"HostedZones": []}
        self.fake_s3 = MagicMock()
        self.engine = SplitHorizonFailoverEngine(
            lease_manager=self.leases,
            region="us-east-1",
            endpoint_url=None,
            bucket="test-dns-bucket",
            route53_client=self.fake_r53,
            s3_client=self.fake_s3,
        )

    def test_split_horizon_failover_and_wal_verification(self) -> None:
        t0 = 1764267000.0
        lease = self.leases.acquire("external:edge.api.example.internal", "reconciler-a", now_utc=t0, grace_extension_sec=600.0)
        out = self.engine.resolve_and_failover(
            fqdn="edge.api.example.internal",
            horizon="external",
            primary_ip="198.51.100.10",
            fallback_ip="203.0.113.20",
            primary_healthy=False,
            fencing_token=lease.fencing_token,
            now_utc=t0 + 2,
        )
        self.assertEqual(out.target_ip, "203.0.113.20")
        ok, msg = self.wal.verify()
        self.assertTrue(ok, msg)
        self.fake_s3.put_object.assert_called_once()

    def test_active_lease_conflict(self) -> None:
        t0 = 1764267000.0
        self.leases.acquire("internal:edge.svc.example.internal", "reconciler-a", now_utc=t0)
        with self.assertRaises(ZoneLeaseConflict):
            self.leases.acquire("internal:edge.svc.example.internal", "reconciler-b", now_utc=t0 + 5)

    def test_chaos_stalled_reconciler_stale_token_rejected(self) -> None:
        t0 = 1764267000.0
        old_lease = self.leases.acquire("external:edge.app.example.internal", "reconciler-a", now_utc=t0)
        # Reconciler-a stalls past TTL and reconciler-b takes over after expiry.
        new_lease = self.leases.acquire("external:edge.app.example.internal", "reconciler-b", now_utc=t0 + 25)
        self.assertGreater(new_lease.fencing_token, old_lease.fencing_token)

        with self.assertRaises(StaleTokenError):
            self.engine.resolve_and_failover(
                fqdn="edge.app.example.internal",
                horizon="external",
                primary_ip="198.51.100.10",
                fallback_ip="203.0.113.20",
                primary_healthy=False,
                fencing_token=old_lease.fencing_token,
                now_utc=t0 + 26,
            )

    def test_hmac_and_skew_verification(self) -> None:
        secret = "dns-secret"
        body = b'{"fqdn":"edge.test.example.internal","horizon":"internal"}'
        now = 1764267000
        sig = sign_dns_request(secret, now, body)
        ok, _ = verify_dns_request(secret, sig, str(now), body, now_unix=now)
        self.assertTrue(ok)
        expired, _ = verify_dns_request(secret, sig, str(now - 301), body, now_unix=now)
        self.assertFalse(expired)

    def test_bench_failover_and_wal(self) -> None:
        wal = HashChainedWal()
        mgr = ZoneFencingLeaseManager(wal=wal, ttl_sec=60.0)
        base = 1764267000.0
        iters = 2000
        t0 = time.perf_counter()
        for i in range(iters):
            zk = f"external:edge-{i % 8}.example.internal"
            lease = mgr.acquire(zk, f"r-{i % 3}", now_utc=base + (i * 65.0))
            self.assertTrue(mgr.check_token(zk, lease.fencing_token, now_utc=base + (i * 65.0)))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ok, _ = wal.verify()
        self.assertTrue(ok)
        print(f"BENCH lease+check+wal ({iters} ops): {elapsed_ms:.2f} ms ({(elapsed_ms / iters) * 1000:.2f} us/op)")


if __name__ == "__main__":
    unittest.main()
