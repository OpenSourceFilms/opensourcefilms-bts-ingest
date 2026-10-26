# BTS Ingest: offline registry and provenance core

**Initial public core release, not a live upload service or the complete BTS ingest pipeline.**

This extracted Python-standard-library package includes the SQLite invitation/asset/worker-job registry, server-assigned safe object naming, and provenance rules that separate a contributor's optional name claim from verified creator identity. It was developed by Open Source Films for behind-the-scenes production media ingestion.

## Components

- **registry**: local SQLite invitation and public-session capabilities, asset reservations, upload state, quotas, jobs, leases and an event journal. Supply an explicit disposable database path with Registry(path) or BTS_REGISTRY_DB. No production-specific database path is compiled into this release.
- **naming**: safe object filename and storage-key construction.
- **public_intake**: keeps unknown shoot date and uploader identity as unknown, avoiding invented metadata.

## Verify offline

Run Python 3.8+ with no additional packages: python3 -B tests/offline_public_intake.py -q

The 16 test cases use temporary SQLite databases, synthetic file metadata, synthetic object storage names and no B2 or public network connections.

## Security and publication boundaries

**This is not a deployable HTTP upload application. Do not expose these modules directly to untrusted traffic.** Browser authentication, upload signing, storage access controls, network rate limiting, asset verification and deployment hardening are deliberately not included or asserted as tested. Neither Backblaze credentials, contributor records, footage, URLs nor production endpoints are distributed.

See ATTRIBUTION.md for technology provenance. OSF's MIT licence applies only to original files in this repository.
