# BTS Ingest: offline registry and provenance core

**Initial public core release, not a live upload service or the complete BTS ingest pipeline.**

This extracted Python-standard-library package includes the SQLite invitation/asset/worker-job registry, server-assigned safe object naming, and provenance rules that separate a contributor's optional name claim from verified creator identity. It was developed by Open Source Films for behind-the-scenes production media ingestion.

## Components

- **registry**: local SQLite invitation and public-session capabilities, asset reservations, upload state, quotas, jobs, leases and an event journal. Supply an explicit disposable database path with Registry(path) or BTS_REGISTRY_DB. No production-specific database path is compiled into this release.
- **naming**: safe object filename and storage-key construction. Date and server asset-ID segments are validated
  before composing the six-part object key; malformed keys cannot be reused
  to construct derived-storage prefixes.
- **public_intake**: keeps unknown shoot date and uploader identity as unknown, avoiding invented metadata.

## Verify offline

Run Python 3.8+ with no additional packages:

```bash
python3 -B tests/offline_public_intake.py -q
python3 -B tests/offline_object_key_safety.py -q
python3 -B tests/offline_worker_timing.py -q
```

Expected result: **36 registry tests, 6 object-key tests, and 4 worker-timing tests, all OK**. All tests use temporary SQLite databases, synthetic file metadata, synthetic object storage names and no B2 or public network connections.

### Minimal local registry example

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from bts_ingest.registry import Registry

with TemporaryDirectory() as directory:
    registry = Registry(Path(directory) / "example.sqlite3")
    session_id, secret_token = registry.create_public_session()
    invitation = registry.invitation_for_token(secret_token)
    assert invitation is not None
    assert invitation["id"] == session_id
```

The example uses a disposable SQLite file and does not contact object storage. A real application must protect the returned bearer token, enforce authentication/quotas at the HTTP boundary, and independently implement and test upload, storage and access controls. **This extract is not a deployable upload service.**

### Object-key path safety

`original_key()` rejects malformed or path-like `shoot_date` and
`asset_id` values rather than inserting them directly into a B2 key.
A shoot date must be a genuine `YYYY-MM-DD` calendar date; the public
intake's explicit unknown-date sentinel `0000-00-00` remains supported.
An asset ID must be a bounded ASCII single path component. Ordinary
server-issued UUIDs and the published date/sentinel layouts are unchanged.

`derived_prefix()` now refuses malformed keys, extra path segments,
traversal components, invalid dates and year/date mismatches instead of
blindly splitting and reassembling input. These checks matter when another
stage materialises B2 object keys as directories on a RunPod volume;
the key validation **does not itself guarantee downstream filesystem
containment**. The new six synthetic test cases enforce the format without
accessing the real B2 bucket or production footage.

## Strict byte-count and quota contract

Asset reservations require a Python integer `size` between **0 and
9,223,372,036,854,775,807** (the SQLite signed 64-bit maximum). Zero is
permitted for an empty file. Negative sizes, floating-point values,
strings, booleans and out-of-range integers are rejected **before**
fingerprinting, allocating an asset ID, choosing a storage key, or
updating quota records.

This validation is important: a negative byte count would otherwise
reduce the recorded total and allow later reservations to exceed both
per-session and aggregate public budgets. The offline regression suite
verifies the attempted bypass is refused.

## Multipart upload ID binding and activity

`Registry.bind_upload_id()` requires a **nonempty, printable ASCII**
multipart upload ID between 1 and 4,096 characters. Null IDs, empty
strings, spaces, controls and non-string values cannot move an asset
from `reserved` to `uploading` or emit a binding event. Normal
S3-compatible punctuation remains accepted.

On the first successful binding the registry updates
`last_activity_at` together with `updated_at` and `s3_upload_id`
under the same SQLite transaction. The stale-upload scanner uses
`last_activity_at`, so initiating multipart upload now counts as
activity. Rebinding the same ID is idempotent, a different ID is refused,
and a completed asset cannot reopen. Subsequent upload signing/activity
still uses `touch()` to renew the inactivity clock.

Two temporary-SQLite regressions cover invalid identifiers, exact
non-mutation on rejection, stale-upload recovery, idempotency and
completion. This is a public library contract, **not evidence that the
deployed HTTP signing and B2 service use the updated implementation**.

## Invitation lifetime and quota validation

Both administrative invitations and public upload sessions validate their
time-limited capabilities before generating a bearer token or writing to
SQLite. `ttl_s` must be a **finite positive numeric duration**, and the
resulting expiry must be strictly in the future. An administrative
`finish_grace_s` must be finite and nonnegative; expiry plus grace must
also be finite. The existing public-session grace period remains 72 hours.

`max_total_bytes` and `max_files` must be nonnegative Python integers
within SQLite's signed 64-bit range. Zero remains supported for an
administratively disabled quota. NaN/infinity, booleans, negative counts,
oversized integers and nonnumeric lifetimes are rejected, instead of
creating unusable records or inadvertently non-expiring capabilities.

Four additional temporary-SQLite regression tests cover invalid admin
and public settings with zero database mutations, and preserve normal
fractional-duration and zero-grace operation. This tests only the
published registry, not the lifetime policy enforced by a deployed
HTTP service.

## Invitation revocation, expiry and upload-resume grace

When `reserve_asset()` is called with an invitation row retrieved earlier,
the registry **rechecks the current invitation** under the same SQLite write
transaction that reserves the asset. A revoked or missing invitation cannot
create or resume a reservation. New reservations cannot be made after
`expires_at`, even during the configured `finish_grace_s` interval.
An asset already reserved while an invitation was valid can still be
resumed within that interval using the existing per-session public asset ID
(or the legacy invitation's matching open-asset fingerprint). Neither
kind of resume is available after the grace deadline. Quota admission reads
`max_files` and `max_total_bytes` from the **live** invitation record, not
cached caller-supplied fields.

This closes a stale-invitation gap in the *published registry library*. It
is not proof that a deployed HTTP service passes the same checks or that a
bearer token has been authenticated. The upload boundary must independently
validate untrusted request fields, authorize access and protect signing and
object storage; this public code is not the deployed uploader.

## Upload activity cannot mark an asset completed

`Registry.touch()` is an activity/heartbeat operation for open assets.
An optional status argument may only be `reserved` or `uploading`.
It rejects `completed`, `aborted`, `failed` and arbitrary values
**before opening a SQLite transaction**. This prevents an activity call
from silently switching an asset into a terminal state and bypassing the
appropriate transition and event/journal behavior.

The supported completion path is `mark_completed()`, which also queues
a `process` job for worker ingestion; negative terminal outcomes use
`mark_status()`. Both remain unchanged.

Activity status transitions are **monotonic**:
`reserved → reserved/uploading`, `uploading → uploading`. A stale
`touch(status="reserved")` arriving after upload start is refused
without changing the existing status or timestamps. Timestamp-only
`touch()` remains supported on either open state. This applies equally
after an S3 multipart upload ID has been bound. Synthetic SQLite tests
verify these restrictions without touching object storage.

## Worker scheduling durations

The worker job registry now validates the time arguments passed to
`lease_job()`, `heartbeat()` and `defer_job()` **before modifying any
SQLite rows**. Lease and heartbeat durations must be finite and positive
and must actually produce a future deadline, not one that rounds down
to the current Unix timestamp. Deferred retry delays must be finite and
nonnegative; a zero delay remains a supported immediate retry. Booleans,
negative values, NaN/infinity, nonnumeric values and impossible
floating-point deadlines are rejected with `ValueError`.

The existing worker-method signatures and successful return values are
unchanged. Four disposable-SQLite regressions verify that invalid
durations cannot consume an attempt, alter an existing lease or strand
a queued job behind an infinite retry date. Successful heartbeat,
completion, zero-delay and fractional-delay deferral remain supported.

**Important:** This does *not* fix the separate same-owner, stale-worker
**lease fencing** race tracked in
[issue #2](https://github.com/OpenSourceFilms/opensourcefilms-bts-ingest/issues/2).
That requires per-attempt credentials coordinated with the deployed
worker/API consumers. The public repository does not contain those
running services, and the timing validation is not a production
acceptance test.

## Security and publication boundaries

**This is not a deployable HTTP upload application. Do not expose these modules directly to untrusted traffic.** Browser authentication, upload signing, storage access controls, network rate limiting, asset verification and deployment hardening are deliberately not included or asserted as tested. Neither Backblaze credentials, contributor records, footage, URLs nor production endpoints are distributed.

See ATTRIBUTION.md for technology provenance. OSF's MIT licence applies only to original files in this repository.
