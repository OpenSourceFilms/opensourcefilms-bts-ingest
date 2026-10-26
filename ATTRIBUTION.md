# Upstream foundations and ownership boundaries

BTS Ingest applies existing media-ingestion and storage techniques. OSF does not claim to have invented SQL databases, object storage, content hashing, multipart upload or capability-token security.

- SQLite (https://www.sqlite.org/): transactional local database used by the extracted registry.
- Python (https://www.python.org/): runtime, standard library and sqlite3 bindings.
- Backblaze B2 (https://www.backblaze.com/cloud-storage): third-party object storage used by OSF's separate production system; not accessed by this extracted package.
- AWS S3-compatible upload designs: independent protocols and implementations used only in the separately operated service.

The original OSF-authored source and documentation in this extract are released under MIT. No upstream libraries, applications, credentials, media or service implementations are distributed or relicensed.
