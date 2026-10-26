"""Durable intake registry (SQLite). Single writer: the upload service process.

Workers on other pods never open this file; they use the service's /api/worker endpoints.
"""
import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid

from . import public_intake

DEFAULT_DB = os.environ.get("BTS_REGISTRY_DB")

SCHEMA = """
CREATE TABLE IF NOT EXISTS invitations (
  id INTEGER PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,
  production TEXT NOT NULL,
  shoot_date TEXT NOT NULL,
  shoot_slug TEXT NOT NULL,
  contributor_slug TEXT NOT NULL,
  contributor_label TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  finish_grace_s REAL NOT NULL,
  max_total_bytes INTEGER NOT NULL,
  max_files INTEGER NOT NULL,
  revoked_at REAL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  invitation_id INTEGER NOT NULL REFERENCES invitations(id),
  original_name TEXT NOT NULL,
  relative_path TEXT,
  size_bytes INTEGER NOT NULL,
  content_type TEXT,
  client_last_modified INTEGER,
  fingerprint TEXT NOT NULL,
  b2_bucket TEXT NOT NULL,
  b2_key TEXT NOT NULL UNIQUE,
  s3_upload_id TEXT,
  status TEXT NOT NULL,           -- reserved|uploading|completed|aborted|failed
  b2_version_id TEXT,
  b2_etag TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_activity_at REAL NOT NULL,
  completed_at REAL,
  sha256 TEXT,                    -- whole-object content hash computed by the worker
  sha1 TEXT,
  duplicate_of TEXT               -- earliest other asset with identical sha256
);
CREATE INDEX IF NOT EXISTS assets_inv ON assets(invitation_id, fingerprint);
CREATE INDEX IF NOT EXISTS assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS assets_sha256 ON assets(sha256);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL,           -- queued|leased|done|failed|quarantined
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires REAL,
  result_json TEXT,
  error TEXT,
  available_at REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(asset_id, kind)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  asset_id TEXT,
  detail_json TEXT
);
CREATE TABLE IF NOT EXISTS public_sessions (
  invitation_id INTEGER PRIMARY KEY REFERENCES invitations(id),
  uploader_name_claimed TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""

MAX_ATTEMPTS = 3
MAX_SQLITE_SIZE_BYTES = (1 << 63) - 1


def sha256_hex(s):
    return hashlib.sha256(s.encode()).hexdigest()


class Registry:
    def __init__(self, path=DEFAULT_DB):
        if path is None:
            raise ValueError("Supply an isolated SQLite registry path or BTS_REGISTRY_DB")
        self.path = os.path.abspath(os.fspath(path))
        path = self.path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c = sqlite3.connect(path, timeout=30)
        try:
            c.executescript(SCHEMA)  # executescript manages its own transaction
        finally:
            c.close()

    @contextlib.contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=DELETE")  # network volume: no WAL shared memory
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def event(self, c, actor, kind, asset_id=None, **detail):
        c.execute("INSERT INTO events(ts,actor,kind,asset_id,detail_json) VALUES(?,?,?,?,?)",
                  (time.time(), actor, kind, asset_id, json.dumps(detail, sort_keys=True)))

    # -- invitations -------------------------------------------------------------------------
    def create_invitation(self, production, shoot_date, shoot_slug, contributor_slug,
                          contributor_label, ttl_s, max_total_bytes, max_files,
                          finish_grace_s=72 * 3600, note=None):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO invitations(token_sha256,production,shoot_date,shoot_slug,contributor_slug,"
                "contributor_label,created_at,expires_at,finish_grace_s,max_total_bytes,max_files,note)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sha256_hex(token), production, shoot_date, shoot_slug, contributor_slug,
                 contributor_label, now, now + ttl_s, finish_grace_s, max_total_bytes, max_files, note))
            self.event(c, "admin", "invitation.created", invitation_id=cur.lastrowid,
                       production=production, shoot=f"{shoot_date}_{shoot_slug}",
                       contributor=contributor_slug, expires_at=now + ttl_s)
        return cur.lastrowid, token

    def invitation_for_token(self, token, *, for_existing_asset=False):
        """Return the invitation row, or None if unknown/revoked/expired.

        Already-reserved assets may keep uploading for finish_grace_s after expiry so a
        long upload started just before the deadline is not stranded half-way.
        """
        if not token or len(token) > 200:
            return None
        with self.tx() as c:
            r = c.execute("SELECT * FROM invitations WHERE token_sha256=?", (sha256_hex(token),)).fetchone()
        if r is None or r["revoked_at"] is not None:
            return None
        deadline = r["expires_at"] + (r["finish_grace_s"] if for_existing_asset else 0)
        return r if time.time() < deadline else None

    def revoke_invitation(self, invitation_id):
        with self.tx() as c:
            c.execute("UPDATE invitations SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                      (time.time(), invitation_id))
            self.event(c, "admin", "invitation.revoked", invitation_id=invitation_id)

    # -- automatic public intake sessions ------------------------------------------------------
    def create_public_session(self, name=None, *, ttl_s=90 * 86400,
                              max_total_bytes=1 * 1024**4, max_files=2000):
        """Mint a private upload capability without inventing shoot metadata."""
        name = public_intake.clean_name(name)
        token = secrets.token_urlsafe(32)
        now = time.time()
        session_slug = "session-" + secrets.token_hex(12)
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO invitations(token_sha256,production,shoot_date,shoot_slug,"
                "contributor_slug,contributor_label,created_at,expires_at,finish_grace_s,"
                "max_total_bytes,max_files,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sha256_hex(token), public_intake.PUBLIC_PRODUCTION,
                 public_intake.PUBLIC_SHOOT_DATE, public_intake.PUBLIC_SHOOT_SLUG,
                 session_slug, name or "", now, now + ttl_s, 72*3600,
                 max_total_bytes, max_files, public_intake.PUBLIC_NOTE))
            iid = cur.lastrowid
            c.execute(
                "INSERT INTO public_sessions(invitation_id,uploader_name_claimed,created_at,updated_at)"
                " VALUES(?,?,?,?)", (iid, name, now, now))
            self.event(c, "public", "public_session.created", invitation_id=iid)
        return iid, token

    def public_session(self, invitation_id):
        with self.tx() as c:
            row = c.execute(
                "SELECT uploader_name_claimed,created_at,updated_at FROM public_sessions "
                "WHERE invitation_id=?", (invitation_id,)).fetchone()
        return dict(row) if row else None

    def update_public_name(self, token, name):
        """Modify only this capability's claimed uploader name, not creator identity."""
        name = public_intake.clean_name(name)
        now = time.time()
        with self.tx() as c:
            row = c.execute(
                "SELECT i.id FROM invitations i "
                "JOIN public_sessions p ON p.invitation_id=i.id "
                "WHERE i.token_sha256=? AND i.revoked_at IS NULL AND i.expires_at>?",
                (sha256_hex(token), now)).fetchone()
            if row is None:
                return False
            c.execute("UPDATE public_sessions SET uploader_name_claimed=?,updated_at=? "
                      "WHERE invitation_id=?", (name, now, row["id"]))
            c.execute("UPDATE invitations SET contributor_label=? WHERE id=?",
                      (name or "", row["id"]))
            self.event(c, "public", "public_session.name_updated", invitation_id=row["id"],
                       has_name=bool(name))
        return True

    # -- assets ------------------------------------------------------------------------------
    def reserve_asset(self, inv, *, name, relative_path, size, content_type, last_modified,
                      bucket, key_fn, resume_asset_id=None, create_distinct=False):
        """Reserve a source, never silently conflate public originals on metadata.

        Legacy invitation semantics remain unchanged. Public resumes require a
        user-confirmed, server-issued asset ID scoped to this private session.
        """
        # Validate before fingerprinting, quota calculations, creating a key or
        # opening the transaction. Negative reservations would otherwise
        # *credit* the quota and let later uploads exceed its capacity.
        # Bool is an int subclass but cannot represent a file byte length.
        if type(size) is not int or not 0 <= size <= MAX_SQLITE_SIZE_BYTES:
            raise ValueError("size must be a nonnegative integer within SQLite's signed 64-bit range")
        fp = sha256_hex(json.dumps([relative_path or name, size, last_modified]))
        with self.tx() as c:
            # Read time only after acquiring the SQLite write lock. A caller
            # may have been waiting for that lock while the link expired.
            now = time.time()
            # invitation_for_token() may have returned this row earlier.
            # Revocation/expiry can change before we acquire the writer lock.
            # Never rely on the caller's cached invitation for admission or
            # quota limits: this check shares the asset INSERT transaction.
            current = c.execute(
                "SELECT * FROM invitations WHERE id=?", (inv["id"],)
            ).fetchone()
            if (current is None or current["revoked_at"] is not None
                    or current["token_sha256"] != inv["token_sha256"]):
                raise ValueError("Reservation invitation missing, revoked or changed")
            expired = now >= current["expires_at"]
            if expired and now >= current["expires_at"] + current["finish_grace_s"]:
                raise ValueError("Reservation invitation expired beyond upload finish grace")
            is_public = c.execute(
                "SELECT 1 FROM public_sessions WHERE invitation_id=?", (current["id"],)
            ).fetchone() is not None
            if is_public:
                if resume_asset_id and create_distinct:
                    raise ValueError("Cannot request resume and new original together")
                if resume_asset_id:
                    existing = c.execute(
                        "SELECT * FROM assets WHERE asset_id=? AND invitation_id=? "
                        "AND fingerprint=? AND status IN ('reserved','uploading')",
                        (resume_asset_id, inv["id"], fp)).fetchone()
                    if existing is None:
                        raise ValueError("Resume asset unavailable for this upload session")
                    return dict(existing), "resume"
                if expired:
                    raise ValueError("Expired public invitation cannot reserve a new original")
                if not create_distinct:
                    prior_open = c.execute(
                        "SELECT * FROM assets WHERE invitation_id=? AND fingerprint=? "
                        "AND status IN ('reserved','uploading') ORDER BY created_at DESC",
                        (inv["id"],fp)).fetchone()
                    if prior_open is not None:
                        # This is only an *option*. It has NOT reserved or
                        # bound the newly selected file to the old original.
                        return dict(prior_open), "ambiguous"
            else:
                if resume_asset_id or create_distinct:
                    raise ValueError("Explicit-resume fields are for public sessions only")
                prev = c.execute(
                    "SELECT * FROM assets WHERE invitation_id=? AND fingerprint=? "
                    "AND status IN ('reserved','uploading','completed') ORDER BY created_at DESC",
                    (inv["id"], fp)).fetchone()
                if prev is not None:
                    if expired and prev["status"] == "completed":
                        raise ValueError("Expired invitation cannot access completed duplicate")
                    return dict(prev), ("duplicate" if prev["status"] == "completed" else "resume")
                if expired:
                    raise ValueError("Expired invitation cannot reserve a new original")
            used = c.execute("SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) b FROM assets "
                             "WHERE invitation_id=? AND status IN ('reserved','uploading','completed')",
                             (inv["id"],)).fetchone()
            if used["n"] + 1 > current["max_files"]:
                raise QuotaError("file count limit for this upload link reached")
            if used["b"] + int(size) > current["max_total_bytes"]:
                raise QuotaError("total size limit for this upload link reached")
            # A common public URL must not allow unlimited new private sessions
            # to bypass one another's budget. Account-level public quota is
            # deliberately zero unless the native owner explicitly sets it.
            if is_public:
                try:
                    budget = int(os.environ.get("BTS_PUBLIC_BYTE_BUDGET", "0"))
                except (ValueError, OverflowError):
                    budget = 0
                if budget <= 0:
                    raise QuotaError("Public upload capacity is not configured yet")
                committed = c.execute(
                    "SELECT COALESCE(SUM(a.size_bytes),0) "
                    "FROM assets a JOIN public_sessions p ON p.invitation_id=a.invitation_id "
                    "WHERE a.status IN ('reserved','uploading','completed')"
                ).fetchone()[0]
                if committed + int(size) > budget:
                    raise QuotaError("Public upload capacity reached; contact Open Source Films")
            asset_id = str(uuid.uuid4())
            key = key_fn(asset_id)
            c.execute("INSERT INTO assets(asset_id,invitation_id,original_name,relative_path,size_bytes,"
                      "content_type,client_last_modified,fingerprint,b2_bucket,b2_key,status,created_at,"
                      "updated_at,last_activity_at) VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?)",
                      (asset_id, inv["id"], name, relative_path, int(size), content_type, last_modified,
                       fp, bucket, key, now, now, now))
            self.event(c, f"invite:{inv['id']}", "asset.reserved", asset_id, name=name, size=int(size), key=key)
            row = c.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        return dict(row), "new"

    def asset_by_key(self, inv_id, key):
        with self.tx() as c:
            r = c.execute("SELECT * FROM assets WHERE invitation_id=? AND b2_key=?", (inv_id, key)).fetchone()
        return dict(r) if r else None

    def get_asset(self, asset_id):
        with self.tx() as c:
            r = c.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        return dict(r) if r else None

    def bind_upload_id(self, asset_id, upload_id):
        """Only open assets may bind/resume an uploadId. A terminal asset never reopens."""
        with self.tx() as c:
            r = c.execute("SELECT s3_upload_id, status FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
            if r is None or r["status"] not in ("reserved", "uploading"):
                return False
            if r["s3_upload_id"] not in (None, upload_id):
                return False
            if r["s3_upload_id"] is None:
                cur = c.execute(
                    "UPDATE assets SET s3_upload_id=?, status='uploading', updated_at=? "
                    "WHERE asset_id=? AND status IN ('reserved','uploading') AND s3_upload_id IS NULL",
                    (upload_id, time.time(), asset_id))
                if not cur.rowcount:
                    return False
                self.event(c, "signer", "asset.multipart_bound", asset_id, upload_id=upload_id)
        return True

    def touch(self, asset_id, status=None):
        """Update signing activity while open; never set a terminal state.

        Completion is exclusively mark_completed(), which records object
        provenance and queues its processing job. Aborts/failures are
        exclusively mark_status(). Signing activity cannot bypass either.
        """
        if status is not None and status not in ("reserved", "uploading"):
            raise ValueError("touch status must be 'reserved' or 'uploading'")
        now = time.time()
        with self.tx() as c:
            if status is not None:
                cur = c.execute(
                    "UPDATE assets SET last_activity_at=?, status=?, updated_at=? "
                    "WHERE asset_id=? AND status IN ('reserved','uploading')",
                    (now, status, now, asset_id))
            else:
                cur = c.execute(
                    "UPDATE assets SET last_activity_at=?, updated_at=? "
                    "WHERE asset_id=? AND status IN ('reserved','uploading')",
                    (now, now, asset_id))
            return bool(cur.rowcount)

    def mark_completed(self, asset_id, version_id, etag, actor):
        now = time.time()
        with self.tx() as c:
            cur = c.execute("UPDATE assets SET status='completed', b2_version_id=?, b2_etag=?, completed_at=?,"
                            " updated_at=? WHERE asset_id=? AND status IN ('reserved','uploading')",
                            (version_id, etag, now, now, asset_id))
            if cur.rowcount:
                self.event(c, actor, "asset.completed", asset_id, version_id=version_id, etag=etag)
                c.execute("INSERT OR IGNORE INTO jobs(asset_id,kind,status,created_at,updated_at)"
                          " VALUES(?,'process','queued',?,?)", (asset_id, now, now))
            return bool(cur.rowcount)

    def mark_status(self, asset_id, status, actor, **detail):
        """A late abort/failure report cannot revoke an already completed upload."""
        if status not in ("aborted", "failed"):
            raise ValueError("mark_status is only for terminal negative outcomes")
        with self.tx() as c:
            cur = c.execute("UPDATE assets SET status=?, updated_at=? "
                            "WHERE asset_id=? AND status IN ('reserved','uploading')",
                            (status, time.time(), asset_id))
            if cur.rowcount:
                self.event(c, actor, f"asset.{status}", asset_id, **detail)
            return bool(cur.rowcount)

    def receipt(self, inv_id):
        with self.tx() as c:
            rows = c.execute("SELECT a.asset_id, a.original_name, a.relative_path, a.size_bytes, a.status,"
                             " a.b2_key, a.b2_version_id, a.completed_at, j.status AS processing"
                             " FROM assets a LEFT JOIN jobs j ON j.asset_id=a.asset_id AND j.kind='process'"
                             " WHERE a.invitation_id=? ORDER BY a.created_at", (inv_id,)).fetchall()
        return [dict(r) for r in rows]

    def open_assets(self, older_than_s):
        with self.tx() as c:
            rows = c.execute("SELECT a.*, i.expires_at, i.finish_grace_s, i.revoked_at FROM assets a"
                             " JOIN invitations i ON i.id=a.invitation_id"
                             " WHERE a.status IN ('reserved','uploading') AND a.last_activity_at < ?",
                             (time.time() - older_than_s,)).fetchall()
        return [dict(r) for r in rows]

    # -- jobs (leased to workers through the service API) -------------------------------------
    def lease_job(self, owner, lease_s):
        with self.tx() as c:
            now = time.time()
            # Crashed workers spend attempts too; expiry must not bypass the budget.
            c.execute("UPDATE jobs SET status='quarantined', lease_owner=NULL, lease_expires=NULL,"
                      " updated_at=? WHERE status='leased' AND lease_expires <= ? AND attempts >= ?",
                      (now, now, MAX_ATTEMPTS))
            c.execute("UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires=NULL, updated_at=? "
                      "WHERE status='leased' AND lease_expires <= ?", (now, now))
            j = c.execute("SELECT * FROM jobs WHERE status='queued' AND available_at <= ? ORDER BY id LIMIT 1",
                          (now,)).fetchone()
            if j is None:
                return None
            c.execute("UPDATE jobs SET status='leased', lease_owner=?, lease_expires=?, attempts=attempts+1,"
                      " updated_at=? WHERE id=?", (owner, now + lease_s, now, j["id"]))
            self.event(c, owner, "job.leased", j["asset_id"], job_id=j["id"], attempt=j["attempts"] + 1)
            a = c.execute("SELECT * FROM assets WHERE asset_id=?", (j["asset_id"],)).fetchone()
            inv = c.execute("SELECT i.production, i.shoot_date, i.shoot_slug, i.contributor_slug,"
                            " i.contributor_label, p.invitation_id AS public_invitation_id"
                            " FROM invitations i LEFT JOIN public_sessions p ON p.invitation_id=i.id"
                            " WHERE i.id=?", (a["invitation_id"],)).fetchone()
        return {"job_id": j["id"], "attempt": j["attempts"] + 1, "asset": dict(a),
                "invitation": {**dict(inv), "is_public_intake": inv["public_invitation_id"] is not None}}

    def heartbeat(self, job_id, owner, lease_s):
        with self.tx() as c:
            now = time.time()
            cur = c.execute("UPDATE jobs SET lease_expires=?, updated_at=? WHERE id=? AND lease_owner=?"
                            " AND status='leased' AND lease_expires>?",
                            (now + lease_s, now, job_id, owner, now))
        return bool(cur.rowcount)

    def finish_job(self, job_id, owner, ok, result=None, error=None, quarantine=False):
        with self.tx() as c:
            now = time.time()
            j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            # An expired lease no longer owns the job, even if another
            # worker has not yet run lease_job() to requeue it.
            if (j is None or j["lease_owner"] != owner or j["status"] != "leased"
                    or j["lease_expires"] is None or j["lease_expires"] <= now):
                return False
            if ok:
                status = "done"
            elif quarantine or j["attempts"] >= MAX_ATTEMPTS:
                status = "quarantined"
            else:
                status = "queued"
            c.execute("UPDATE jobs SET status=?, lease_owner=NULL, lease_expires=NULL, result_json=?,"
                      " error=?, updated_at=? WHERE id=?",
                      (status, json.dumps(result) if result is not None else j["result_json"], error, now, job_id))
            self.event(c, owner, f"job.{status}", j["asset_id"], job_id=job_id, error=error)
            if ok and result and result.get("sha256"):
                dup = c.execute("SELECT asset_id FROM assets WHERE sha256=? AND asset_id<>? ORDER BY completed_at"
                                " LIMIT 1", (result["sha256"], j["asset_id"])).fetchone()
                c.execute("UPDATE assets SET sha256=?, sha1=?, duplicate_of=?, updated_at=? WHERE asset_id=?",
                          (result["sha256"], result.get("sha1"), dup["asset_id"] if dup else None, now, j["asset_id"]))
                if dup:
                    self.event(c, owner, "asset.content_duplicate", j["asset_id"], duplicate_of=dup["asset_id"])
        return True

    def defer_job(self, job_id, owner, delay_s, reason):
        """Give a job back without spending an attempt (e.g. no scratch space right now)."""
        with self.tx() as c:
            now = time.time()
            cur = c.execute("UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires=NULL,"
                            " attempts=MAX(attempts-1,0), available_at=?, error=?, updated_at=?"
                            " WHERE id=? AND lease_owner=? AND status='leased' AND lease_expires>?",
                            (now + delay_s, f"deferred: {reason}", now, job_id, owner, now))
            if cur.rowcount:
                j = c.execute("SELECT asset_id FROM jobs WHERE id=?", (job_id,)).fetchone()
                self.event(c, owner, "job.deferred", j["asset_id"], job_id=job_id, reason=reason, delay_s=delay_s)
        return bool(cur.rowcount)


class QuotaError(Exception):
    pass
