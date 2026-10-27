"""Offline-qualifiable v2 worker service adapter, not a deployed HTTP route.

The API owner must authenticate callers, select a versioned route, instantiate
this gateway with a restricted B2 read client and a configured derived bucket,
and prevent the old v1 lease route from leasing jobs after cutover.

Verification is performed OUTSIDE SQLite's writer transaction, followed by
the registry's atomic per-lease finish predicate. Thus a lease expiring during
remote verification cannot commit a stale result.

This does not implement B2 generation writes or local promotion.
"""
from __future__ import annotations

import hashlib
import math
import time

from . import derivative_generation
from .registry import _lease_token_hash, _worker_owner


class DerivativeVerificationError(ValueError):
    """No worker-provided asset path, token or upstream exception in messages."""


def verify_pinned_versions(s3, bucket, manifest, *, max_total_bytes=100 * (1 << 30)):
    """Stream GET of each *exact pinned version* and verify byte count + SHA-256.

    No latest-key lookup, no write, no response caching, no unbounded read.
    Call validate_manifest() first to validate claim-specific keys and metadata.
    """
    if type(bucket) is not str or not bucket or any(c.isspace() for c in bucket):
        raise ValueError("a configured derived bucket is required")
    if type(max_total_bytes) is not int or max_total_bytes < 0:
        raise ValueError("max_total_bytes must be a nonnegative integer")
    if not isinstance(manifest, dict) or type(manifest.get("derivatives")) is not list:
        raise DerivativeVerificationError("missing derivative manifest")
    declared = 0
    for entry in manifest["derivatives"]:
        if not isinstance(entry, dict) or type(entry.get("bytes")) is not int:
            raise DerivativeVerificationError("invalid derivative byte count")
        n = entry["bytes"]
        if n < 0 or n > max_total_bytes - declared:
            raise DerivativeVerificationError("derivative verification byte limit exceeded")
        declared += n
    for entry in manifest["derivatives"]:
        stream = None
        try:
            want_version = entry["version_id"]
            response = s3.get_object(
                Bucket=bucket, Key=entry["b2_key"], VersionId=want_version
            )
            if type(response) is not dict or response.get("VersionId") != want_version:
                raise DerivativeVerificationError("remote derivative version mismatch")
            stream = response.get("Body")
            if stream is None or not callable(getattr(stream, "read", None)):
                raise DerivativeVerificationError("missing derivative object body")
            hasher = hashlib.sha256()
            received = 0
            while True:
                chunk = stream.read(1 << 20)
                if type(chunk) is not bytes:
                    raise DerivativeVerificationError("invalid derivative object stream")
                if not chunk:
                    break
                received += len(chunk)
                if received > entry["bytes"]:
                    raise DerivativeVerificationError("derivative object exceeds declared size")
                hasher.update(chunk)
            if received != entry["bytes"] or hasher.hexdigest() != entry["sha256"].lower():
                raise DerivativeVerificationError("remote derivative content mismatch")
        except DerivativeVerificationError:
            raise
        except Exception:
            # Never expose B2 exception details, paths, auth or request headers.
            raise DerivativeVerificationError("remote derivative verification failed") from None
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
    return True


class FencedWorkerGateway:
    """Versioned service handler core. HTTP auth, routing and B2 config are external."""

    def __init__(self, reg, s3_read_client, derived_bucket, *, lease_s=90,
                 max_verified_bytes=100 * (1 << 30)):
        if s3_read_client is None:
            raise ValueError("remote derivative verifier must not be disabled")
        if type(lease_s) not in (int, float) or not math.isfinite(lease_s) or lease_s <= 0:
            raise ValueError("lease_s must be positive and finite")
        self.reg, self.client, self.bucket = reg, s3_read_client, derived_bucket
        self.lease_s, self.max_verified_bytes = lease_s, max_verified_bytes

    def lease(self, owner):
        return self.reg.lease_job_fenced(owner, self.lease_s)

    def heartbeat(self, job_id, owner, lease_token):
        _lease_token_hash(lease_token)
        if lease_token is None:
            raise ValueError("v2 lease token is required")
        return self.reg.heartbeat(job_id, owner, self.lease_s, lease_token=lease_token)

    def defer(self, job_id, owner, lease_token, delay_s, reason):
        _lease_token_hash(lease_token)
        if lease_token is None:
            raise ValueError("v2 lease token is required")
        return self.reg.defer_job(job_id, owner, delay_s, reason,
                                  lease_token=lease_token)

    def finish(self, job_id, owner, lease_token, ok, result=None,
               error=None, quarantine=False):
        """Validate+verify remote success first, then CAS finish on same claim."""
        owner = _worker_owner(owner)
        token_hash = _lease_token_hash(lease_token)
        if token_hash is None:
            raise ValueError("v2 lease token is required")
        if type(ok) is not bool or type(quarantine) is not bool:
            raise ValueError("finish status flags must be booleans")
        if ok:
            # Read-only claim check before lengthy remote verification; the
            # registry makes the authoritative check again during finish.
            with self.reg.tx() as c:
                row = c.execute(
                    "SELECT a.b2_key FROM jobs j JOIN assets a ON a.asset_id=j.asset_id"
                    " WHERE j.id=? AND j.lease_owner=? AND j.lease_token_sha256=?"
                    " AND j.status='leased' AND j.lease_expires>?",
                    (job_id, owner, token_hash, time.time())
                ).fetchone()
            if row is None:
                return False
            derivative_generation.validate_manifest(row["b2_key"], lease_token, result)
            verify_pinned_versions(self.client, self.bucket, result,
                                   max_total_bytes=self.max_verified_bytes)
        return self.reg.finish_job(job_id, owner, ok, result=result,
                                   error=error, quarantine=quarantine,
                                   lease_token=lease_token)
