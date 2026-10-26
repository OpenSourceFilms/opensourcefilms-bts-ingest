# BTS Ingest: offline registry and provenance core

**Initial public core release, not a live upload service or the complete BTS ingest pipeline.**

This extracted Python-standard-library package includes the SQLite invitation/asset/worker-job registry, server-assigned safe object naming, and provenance rules that separate a contributor's optional name claim from verified creator identity. It was developed by Open Source Films for behind-the-scenes production media ingestion.

## Components

- **registry**: local SQLite invitation and public-session capabilities, asset reservations, upload state, quotas, jobs, leases and an event journal. Supply an explicit disposable database path with Registry(path) or BTS_REGISTRY_DB. No production-specific database path is compiled into this release.
- **naming**: safe object filename and storage-key construction.
- **public_intake**: keeps unknown shoot date and uploader identity as unknown, avoiding invented metadata.

## Verify offline

Run Python 3.8+ with no additional packages:

```bash
python3 -B tests/offline_public_intake.py -q
```

Expected result: **Ran 28 tests, OK**. All tests use temporary SQLite databases, synthetic file metadata, synthetic object storage names and no B2 or public network connections.

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
`mark_status()`. Both remain unchanged. Synthetic SQLite regression
tests verify an invalid touch leaves the asset and job rows unchanged.

## Security and publication boundaries

**This is not a deployable HTTP upload application. Do not expose these modules directly to untrusted traffic.** Browser authentication, upload signing, storage access controls, network rate limiting, asset verification and deployment hardening are deliberately not included or asserted as tested. Neither Backblaze credentials, contributor records, footage, URLs nor production endpoints are distributed.

See ATTRIBUTION.md for technology provenance. OSF's MIT licence applies only to original files in this repository.
