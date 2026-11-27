from datetime import datetime, timezone
import json
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
import httpx

from controlplane.zone_lease import ZoneFencingLeaseManager, ZoneLeaseConflict
from dataplane.dns_failover import SplitHorizonFailoverEngine, StaleTokenError, verify_dns_request
from storage.wal_chain import HashChainedWal

app = FastAPI(title="global-dns-split-horizon-controlplane", version="0.1.0")

WAL = HashChainedWal()
LEASE_MGR = ZoneFencingLeaseManager(wal=WAL, ttl_sec=20.0)


def _eval_opa(opa_url: str, opa_input: dict[str, Any]) -> tuple[bool, str]:
    base = opa_url.rstrip("/")
    with httpx.Client(timeout=3.0) as client:
        sh_resp = client.post(f"{base}/v1/data/dns/splithorizon", json={"input": opa_input})
        sh_resp.raise_for_status()
        sh_data = sh_resp.json().get("result") or {}
        if not sh_data.get("allow", False):
            return False, sh_data.get("reason", "denied by split-horizon policy")

        fo_resp = client.post(f"{base}/v1/data/dns/failover", json={"input": opa_input})
        fo_resp.raise_for_status()
        fo_data = fo_resp.json().get("result") or {}
        if not fo_data.get("allow", False):
            return False, fo_data.get("reason", "denied by failover window policy")

        return True, fo_data.get("reason", "allowed")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    ok, msg = WAL.verify()
    return {
        "status": "ok" if ok else "degraded",
        "wal_status": msg,
        "utc_time": datetime.now(timezone.utc).isoformat(),
        "state": LEASE_MGR.snapshot(),
    }


@app.post("/v1/dns/failover")
async def apply_dns_failover(
    request: Request,
    x_signature_256: str | None = Header(default=None, alias="X-Signature-256"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
) -> dict[str, Any]:
    raw_body = await request.body()
    secret = os.getenv("WEBHOOK_HMAC_SECRET", "local-dev-dns-secret")
    valid_sig, reason = verify_dns_request(secret, x_signature_256, x_timestamp, raw_body)
    if not valid_sig:
        raise HTTPException(status_code=401, detail={"error": reason})

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "malformed json payload"})

    fqdn = str(payload.get("fqdn", "edge.global.example.internal"))
    horizon = str(payload.get("horizon", "external"))
    record_type = str(payload.get("record_type", "A"))
    owner_id = str(payload.get("owner_id", "reconciler-1"))
    role = str(payload.get("actor_role", "dns-reconciler"))
    primary_ip = str(payload.get("primary_ip", "198.51.100.10"))
    fallback_ip = str(payload.get("fallback_ip", "203.0.113.20"))
    primary_healthy = bool(payload.get("primary_healthy", False))
    grace_lock = payload.get("grace_lock") or {}
    grace_mins = int(grace_lock.get("extra_minutes", 0)) if grace_lock.get("active") else 0
    now_utc = datetime.now(timezone.utc)

    opa_url = os.getenv("OPA_URL", "http://localhost:8181")
    try:
        allowed, policy_reason = _eval_opa(
            opa_url,
            {
                "fqdn": fqdn,
                "horizon": horizon,
                "record_type": record_type,
                "actor_role": role,
                "utc_hour": now_utc.hour,
                "grace_lock": grace_lock,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"opa evaluation failed: {exc}"})

    if not allowed:
        raise HTTPException(status_code=403, detail={"error": policy_reason})

    zone_key = f"{horizon}:{fqdn}"
    try:
        lease = LEASE_MGR.acquire(
            zone_key=zone_key,
            owner_id=owner_id,
            now_utc=now_utc.timestamp(),
            grace_extension_sec=grace_mins * 60.0,
        )
    except ZoneLeaseConflict as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)})

    engine = SplitHorizonFailoverEngine(
        lease_manager=LEASE_MGR,
        region=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"),
        bucket=os.getenv("DNS_STATE_BUCKET", "global-dns-failover-state"),
    )
    try:
        outcome = engine.resolve_and_failover(
            fqdn=fqdn,
            horizon=horizon,
            primary_ip=primary_ip,
            fallback_ip=fallback_ip,
            primary_healthy=primary_healthy,
            fencing_token=lease.fencing_token,
            now_utc=now_utc.timestamp(),
        )
    except StaleTokenError as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)})
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"dns failover failed: {exc}"})

    chain_ok, _ = WAL.verify()
    return {
        "status": "applied",
        "policy_reason": policy_reason,
        "fencing_token": lease.fencing_token,
        "wal_valid": chain_ok,
        "record": {
            "fqdn": outcome.fqdn,
            "horizon": outcome.horizon,
            "target_ip": outcome.target_ip,
            "s3_bucket": outcome.s3_bucket,
            "s3_key": outcome.s3_key,
            "wal_hash": outcome.wal_hash,
        },
    }
