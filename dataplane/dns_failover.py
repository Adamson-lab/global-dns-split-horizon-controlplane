from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from controlplane.zone_lease import ZoneFencingLeaseManager

logger = logging.getLogger("global_dns.dataplane")


class StaleTokenError(Exception):
    pass


def sign_dns_request(secret: str, ts_unix: int, body: bytes) -> str:
    msg = f"{ts_unix}.".encode("utf-8") + body
    mac = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify_dns_request(
    secret: str,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    max_skew_sec: int = 300,
    now_unix: int | None = None,
) -> tuple[bool, str]:
    if not signature or not timestamp:
        return False, "missing X-Signature-256 or X-Timestamp header"
    try:
        ts = int(timestamp.strip())
    except ValueError:
        return False, "invalid X-Timestamp header"

    now = int(time.time()) if now_unix is None else now_unix
    if abs(now - ts) > max_skew_sec:
        return False, "timestamp outside replay window"

    expected = sign_dns_request(secret, ts, body)
    if not hmac.compare_digest(expected, signature.strip()):
        return False, "invalid hmac signature"
    return True, "ok"


def _is_retryable(exc: Any) -> bool:
    resp = getattr(exc, "response", {}) or {}
    code = resp.get("Error", {}).get("Code", "")
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code in {
        "Throttling",
        "ThrottlingException",
        "PriorRequestNotComplete",
        "TooManyRequestsException",
    } or status in {429, 502, 503, 504}


def _retry_aws(op: str, fn: Any, exc_cls: Any, attempts: int = 4) -> Any:
    backoff = 0.1
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exc_cls as exc:
            if not _is_retryable(exc) or attempt == attempts:
                raise
            # Cap per-retry pause at 2.0s so Route53/S3 throttling does not exhaust request deadlines.
            pause = min(backoff, 2.0)
            logger.warning("retrying aws op=%s attempt=%d pause_s=%.2f err=%s", op, attempt, pause, exc)
            time.sleep(pause)
            backoff *= 2.0


@dataclass
class FailoverOutcome:
    fqdn: str
    horizon: str
    target_ip: str
    fencing_token: int
    s3_bucket: str
    s3_key: str
    wal_hash: str


class SplitHorizonFailoverEngine:
    def __init__(
        self,
        lease_manager: ZoneFencingLeaseManager,
        region: str = "us-east-1",
        endpoint_url: str | None = "http://localhost:4566",
        bucket: str = "global-dns-failover-state",
        route53_client: Any | None = None,
        s3_client: Any | None = None,
        client_error_cls: Any | None = None,
    ) -> None:
        self.leases = lease_manager
        self.region = region
        self.bucket = bucket

        if client_error_cls is not None:
            self._client_error = client_error_cls
            boto3_mod = None
        else:
            try:
                import boto3
                from botocore.exceptions import ClientError

                boto3_mod = boto3
                self._client_error = ClientError
            except ImportError:
                boto3_mod = None

                class _FallbackError(Exception):
                    response: dict[str, Any] = {}

                self._client_error = _FallbackError

        client_kwargs: dict[str, Any] = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["aws_access_key_id"] = "test"
            client_kwargs["aws_secret_access_key"] = "test"

        if route53_client is not None:
            self.route53 = route53_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when route53_client is not supplied")
            self.route53 = boto3_mod.client("route53", **client_kwargs)

        if s3_client is not None:
            self.s3 = s3_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when s3_client is not supplied")
            self.s3 = boto3_mod.client("s3", **client_kwargs)

    def _ensure_bucket(self) -> None:
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except self._client_error:
            try:
                self.s3.create_bucket(Bucket=self.bucket)
            except self._client_error as exc:
                code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code", "")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise

    def resolve_and_failover(
        self,
        fqdn: str,
        horizon: str,
        primary_ip: str,
        fallback_ip: str,
        primary_healthy: bool,
        fencing_token: int,
        now_utc: float | None = None,
    ) -> FailoverOutcome:
        zone_key = f"{horizon}:{fqdn}"
        # Block stale or expired tokens prior to querying Route53 or writing failover state.
        if not self.leases.check_token(zone_key=zone_key, fencing_token=fencing_token, now_utc=now_utc):
            raise StaleTokenError(f"fencing token {fencing_token} rejected for {zone_key}")

        _retry_aws(
            "route53:list_hosted_zones",
            lambda: self.route53.list_hosted_zones(),
            self._client_error,
        )
        self._ensure_bucket()

        active_ip = primary_ip if primary_healthy else fallback_ip
        ts = time.time() if now_utc is None else now_utc
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        entry = self.leases.wal().append(
            op_type="dns_failover_applied",
            fencing_token=fencing_token,
            payload={
                "fqdn": fqdn,
                "horizon": horizon,
                "active_ip": active_ip,
                "primary_healthy": primary_healthy,
                "applied_at_utc": dt_utc.isoformat(),
            },
        )
        key = f"failovers/{dt_utc.strftime('%Y-%m-%d')}/{horizon}-{fqdn}-tok{fencing_token}.json"
        doc = {
            "fqdn": fqdn,
            "horizon": horizon,
            "active_ip": active_ip,
            "primary_healthy": primary_healthy,
            "fencing_token": fencing_token,
            "wal_hash": entry.entry_hash,
        }
        _retry_aws(
            "s3:put_object",
            lambda: self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(doc, indent=2).encode("utf-8"),
                ContentType="application/json",
            ),
            self._client_error,
        )
        return FailoverOutcome(
            fqdn=fqdn,
            horizon=horizon,
            target_ip=active_ip,
            fencing_token=fencing_token,
            s3_bucket=self.bucket,
            s3_key=key,
            wal_hash=entry.entry_hash,
        )
