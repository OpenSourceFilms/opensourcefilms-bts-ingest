"""Offline v2 lease fencing contract; disposable SQLite and synthetic assets only.

This is NOT deployed HTTP worker/API acceptance. V1 owner-only leases remain
supported for old callers and remain unsafe against same-owner stale workers.
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bts_ingest import registry


class FencedLeaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bts_fenced_")
        self.addCleanup(directory.cleanup)
        self.reg = registry.Registry(Path(directory.name) / "isolated.sqlite3")
        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000000"}):
            _, token = self.reg.create_public_session(max_total_bytes=1000, max_files=5)
            inv = self.reg.invitation_for_token(token)
            asset, outcome = self.reg.reserve_asset(
                inv, name="synthetic.mov", relative_path="camera/synthetic.mov",
                size=123, content_type="video/quicktime", last_modified=100,
                bucket="disposable", key_fn=lambda uid: "synthetic/" + uid,
            )
            self.assertEqual(outcome, "new")
            self.assertTrue(self.reg.mark_completed(asset["asset_id"], "v", "etag", "test"))
        with self.reg.tx() as c:
            self.job_id = c.execute("SELECT id FROM jobs WHERE asset_id=?",
                                    (asset["asset_id"],)).fetchone()["id"]

    def row(self):
        with self.reg.tx() as c:
            return dict(c.execute("SELECT * FROM jobs WHERE id=?", (self.job_id,)).fetchone())

    def expire(self):
        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET lease_expires=? WHERE id=?",
                      (time.time() - 5, self.job_id))

    def test_v2_token_is_hash_only_and_legacy_calls_cannot_mutate_v2(self):
        lease = self.reg.lease_job_fenced("worker", 60)
        token = lease["lease_token"]
        row = self.row()
        self.assertNotEqual(row["lease_token_sha256"], token)
        self.assertEqual(row["lease_token_sha256"], registry.sha256_hex(token))
        self.assertEqual(len(token) >= 40, True)
        self.assertFalse(self.reg.heartbeat(self.job_id, "worker", 60))
        self.assertFalse(self.reg.defer_job(self.job_id, "worker", 0, "legacy request"))
        self.assertFalse(self.reg.finish_job(self.job_id, "worker", True))
        self.assertEqual(self.row(), row)
        self.assertTrue(self.reg.heartbeat(self.job_id, "worker", 60, lease_token=token))
        self.assertTrue(self.reg.finish_job(self.job_id, "worker", True,
                                            result={"sha256": "A" * 64},
                                            lease_token=token))
        self.assertEqual(self.row()["status"], "done")
        self.assertIsNone(self.row()["lease_token_sha256"])
        self.assertFalse(self.reg.finish_job(self.job_id, "worker", True,
                                             lease_token=token))

    def test_same_owner_expired_attempt_cannot_mutate_successor(self):
        first = self.reg.lease_job_fenced("same-worker", 60)
        self.expire()
        second = self.reg.lease_job_fenced("same-worker", 60)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual((first["attempt"], second["attempt"]), (1, 2))
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        before = self.row()
        self.assertFalse(self.reg.heartbeat(self.job_id, "same-worker", 60,
                                            lease_token=first["lease_token"]))
        self.assertFalse(self.reg.defer_job(self.job_id, "same-worker", 0, "stale",
                                            lease_token=first["lease_token"]))
        self.assertFalse(self.reg.finish_job(self.job_id, "same-worker", True,
                                             lease_token=first["lease_token"]))
        self.assertEqual(self.row(), before)
        self.assertTrue(self.reg.heartbeat(self.job_id, "same-worker", 60,
                                           lease_token=second["lease_token"]))
        self.assertTrue(self.reg.finish_job(self.job_id, "same-worker", True,
                                            lease_token=second["lease_token"]))

    def test_other_owner_and_wrong_token_are_rejected(self):
        lease = self.reg.lease_job_fenced("worker-A", 60)
        self.assertFalse(self.reg.heartbeat(self.job_id, "worker-B", 60,
                                            lease_token=lease["lease_token"]))
        self.assertFalse(self.reg.finish_job(self.job_id, "worker-B", True,
                                             lease_token=lease["lease_token"]))
        self.assertFalse(self.reg.defer_job(self.job_id, "worker-B", 0, "no",
                                            lease_token=lease["lease_token"]))
        self.assertFalse(self.reg.finish_job(self.job_id, "worker-A", True,
                                             lease_token="another-valid-token"))
        for token in ("", "\n", "bad/character", 7):
            with self.subTest(token=repr(token)):
                with self.assertRaisesRegex(ValueError, "lease token"):
                    self.reg.heartbeat(self.job_id, "worker-A", 60, lease_token=token)
        self.assertTrue(self.reg.finish_job(self.job_id, "worker-A", True,
                                            lease_token=lease["lease_token"]))

    def test_defer_reacquire_even_when_attempt_number_repeats(self):
        first = self.reg.lease_job_fenced("worker", 60)
        self.assertTrue(self.reg.defer_job(self.job_id, "worker", 0, "capacity",
                                           lease_token=first["lease_token"]))
        self.assertEqual(self.row()["attempts"], 0)
        self.assertIsNone(self.row()["lease_token_sha256"])
        second = self.reg.lease_job_fenced("worker", 60)
        self.assertEqual(second["attempt"], first["attempt"])
        self.assertNotEqual(second["lease_token"], first["lease_token"])
        before = self.row()
        self.assertFalse(self.reg.finish_job(self.job_id, "worker", True,
                                             lease_token=first["lease_token"]))
        self.assertFalse(self.reg.defer_job(self.job_id, "worker", 0, "stale",
                                            lease_token=first["lease_token"]))
        self.assertEqual(self.row(), before)
        self.assertTrue(self.reg.finish_job(self.job_id, "worker", True,
                                            lease_token=second["lease_token"]))

    def test_expiration_is_invalid_even_before_new_lease(self):
        first = self.reg.lease_job_fenced("worker", 60)
        self.expire()
        before = self.row()
        self.assertFalse(self.reg.heartbeat(self.job_id, "worker", 60,
                                            lease_token=first["lease_token"]))
        self.assertFalse(self.reg.finish_job(self.job_id, "worker", True,
                                             lease_token=first["lease_token"]))
        self.assertFalse(self.reg.defer_job(self.job_id, "worker", 0, "stale",
                                            lease_token=first["lease_token"]))
        self.assertEqual(self.row(), before)

    def test_attempt_budget_quarantines_expired_v2_without_token(self):
        for attempt in range(1, registry.MAX_ATTEMPTS + 1):
            current = self.reg.lease_job_fenced("worker", 60)
            self.assertEqual(current["attempt"], attempt)
            self.expire()
        self.assertIsNone(self.reg.lease_job_fenced("worker", 60))
        row = self.row()
        self.assertEqual(row["status"], "quarantined")
        self.assertEqual(row["attempts"], registry.MAX_ATTEMPTS)
        self.assertIsNone(row["lease_token_sha256"])
        self.assertFalse(self.reg.finish_job(self.job_id, "worker", True,
                                             lease_token=current["lease_token"]))

    def test_legacy_lease_still_supports_original_method_signatures(self):
        old = self.reg.lease_job("old-client", 60)
        self.assertNotIn("lease_token", old)
        self.assertIsNone(self.row()["lease_token_sha256"])
        self.assertTrue(self.reg.heartbeat(self.job_id, "old-client", 60))
        self.assertTrue(self.reg.finish_job(self.job_id, "old-client", True))

    def test_migrate_legacy_database_in_place_without_rewriting_jobs(self):
        with tempfile.TemporaryDirectory(prefix="bts_v1_db_") as directory:
            path = Path(directory) / "old.sqlite3"
            old_schema = registry.SCHEMA.replace(
                "  lease_token_sha256 TEXT,    -- optional v2 fencing; legacy leases use NULL\n", "")
            with sqlite3.connect(path) as c:
                c.executescript(old_schema)
                c.execute("INSERT INTO jobs(asset_id,kind,status,attempts,"
                          "lease_owner,lease_expires,created_at,updated_at)"
                          " VALUES(?,?,?,?,?,?,?,?)",
                          ("old-asset", "process", "leased", 1,
                           "old-worker", time.time() + 600, time.time(), time.time()))
            upgraded = registry.Registry(path)
            with upgraded.tx() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
                self.assertIn("lease_token_sha256", cols)
                row = c.execute("SELECT attempts,lease_owner,lease_token_sha256 FROM jobs").fetchone()
                self.assertEqual((row["attempts"], row["lease_owner"],
                                  row["lease_token_sha256"]), (1, "old-worker", None))
            upgraded = registry.Registry(path)
            with upgraded.tx() as c:
                self.assertEqual(c.execute(
                    "SELECT COUNT(*) FROM pragma_table_info('jobs') "
                    "WHERE name='lease_token_sha256'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
