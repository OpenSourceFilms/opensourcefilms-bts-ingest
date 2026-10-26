"""Offline BTS worker-deadline validation, with disposable SQLite only.

This is not an API-consumer compatibility or per-attempt lease-fencing test.
No network, B2, worker process or production service is contacted.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bts_ingest import registry


INVALID_LEASES = (
    -1, 0, -0.5, float("nan"), float("inf"), float("-inf"),
    True, False, None, "60", 10 ** 400, 1e-300,
)
INVALID_OWNERS = (
    None, "", " ", "  worker", "worker ", "worker\nname", "worker\tname",
    "worker\x00name", 123, True, b"worker", "w" * 257, "wørker",
)

INVALID_DELAYS = (
    -1, -0.5, float("nan"), float("inf"), float("-inf"),
    True, False, None, "60", 10 ** 400,
)


class WorkerDeadlineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bts_worker_timing_")
        self.addCleanup(directory.cleanup)
        self.reg = registry.Registry(Path(directory.name) / "isolated.sqlite3")
        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000000"}):
            iid, token = self.reg.create_public_session(max_total_bytes=1000, max_files=5)
            inv = self.reg.invitation_for_token(token)
            asset, outcome = self.reg.reserve_asset(
                inv, name="synthetic.mov", relative_path="camera/synthetic.mov",
                size=123, content_type="video/quicktime", last_modified=100,
                bucket="disposable", key_fn=lambda uid: "synthetic/" + uid,
            )
            self.assertEqual(outcome, "new")
            self.assertEqual(asset["invitation_id"], iid)
            self.assertTrue(self.reg.mark_completed(asset["asset_id"], "v", "etag", "test"))
        self.asset_id = asset["asset_id"]
        with self.reg.tx() as c:
            self.job_id = c.execute(
                "SELECT id FROM jobs WHERE asset_id=?", (self.asset_id,)
            ).fetchone()["id"]

    def snapshot(self):
        with self.reg.tx() as c:
            row = c.execute(
                "SELECT status, attempts, lease_owner, lease_expires, "
                "available_at, updated_at, result_json, error FROM jobs WHERE id=?",
                (self.job_id,)
            ).fetchone()
            events = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return tuple(row), events

    def test_invalid_owner_cannot_consume_attempt_or_strand_queued_job(self):
        initial = self.snapshot()
        for owner in INVALID_OWNERS:
            with self.subTest(owner=repr(owner)):
                with self.assertRaisesRegex(ValueError, "worker owner"):
                    self.reg.lease_job(owner, 60)
                self.assertEqual(self.snapshot(), initial)
        # SQL NULL ownership was the original trap: lease_owner=? does not
        # match NULL, so such a lease is unreachable until expiry.
        with self.reg.tx() as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM jobs WHERE lease_owner IS NULL "
                          "AND status='leased'").fetchone()[0], 0,
            )
        job = self.reg.lease_job("worker:corrected-01", 60)
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(self.snapshot()[0][2], "worker:corrected-01")
        self.assertTrue(self.reg.heartbeat(self.job_id, "worker:corrected-01", 60))
        self.assertTrue(self.reg.finish_job(self.job_id, "worker:corrected-01", True))
        self.assertEqual(self.snapshot()[0][0], "done")

    def test_bad_owner_cannot_modify_an_existing_valid_lease(self):
        self.reg.lease_job("valid-owner", 60)
        baseline = self.snapshot()
        for owner in INVALID_OWNERS:
            with self.subTest(owner=repr(owner)):
                with self.assertRaisesRegex(ValueError, "worker owner"):
                    self.reg.heartbeat(self.job_id, owner, 60)
                with self.assertRaisesRegex(ValueError, "worker owner"):
                    self.reg.finish_job(self.job_id, owner, True)
                with self.assertRaisesRegex(ValueError, "worker owner"):
                    self.reg.defer_job(self.job_id, owner, 0, "bad worker")
                self.assertEqual(self.snapshot(), baseline)
        self.assertTrue(self.reg.defer_job(self.job_id, "valid-owner", 0, "retry"))
        self.assertEqual(self.snapshot()[0][0], "queued")

    def test_simple_printable_worker_id_preserves_normal_lease_lifecycle(self):
        owner = "pod:gpu-01 / group:A"
        leased = self.reg.lease_job(owner, 60)
        self.assertEqual(leased["attempt"], 1)
        self.assertTrue(self.reg.heartbeat(self.job_id, owner, 60))
        self.assertTrue(self.reg.finish_job(self.job_id, owner, True))
        self.assertEqual(self.snapshot()[0][0], "done")

    def test_malformed_success_hashes_fail_before_job_or_asset_changes(self):
        self.reg.lease_job("worker", 60)
        baseline = self.snapshot()
        asset_before = self.reg.get_asset(self.asset_id)
        invalid = (
            {"sha256": ""},
            {"sha256": "a" * 63},
            {"sha256": "g" * 64},
            {"sha256": None},
            {"sha256": ["a" * 64]},
            {"sha256": 123},
            {"sha1": "b" * 39, "sha256": "a" * 64},
            {"sha1": "z" * 40, "sha256": "a" * 64},
            {"sha1": ["b" * 40], "sha256": "a" * 64},
            ["not a result mapping"],
        )
        for data in invalid:
            with self.subTest(result=repr(data)):
                with self.assertRaisesRegex(ValueError, "worker|result"):
                    self.reg.finish_job(self.job_id, "worker", True, result=data)
                self.assertEqual(self.snapshot(), baseline)
                self.assertEqual(self.reg.get_asset(self.asset_id), asset_before)
        self.assertTrue(self.reg.heartbeat(self.job_id, "worker", 60))

    def test_success_hashes_are_normalized_before_duplicate_comparison(self):
        job = self.reg.lease_job("worker", 60)
        sha256 = "ABCDEF" * 10 + "ABCD"
        sha1 = "FEDCBA" * 6 + "FEDC"
        self.assertEqual(len(sha256), 64)
        self.assertEqual(len(sha1), 40)
        self.assertTrue(self.reg.finish_job(job["job_id"], "worker", True,
                        result={"sha256": sha256, "sha1": sha1, "frames": 24}))
        first = self.reg.get_asset(self.asset_id)
        self.assertEqual(first["sha256"], sha256.lower())
        self.assertEqual(first["sha1"], sha1.lower())
        with self.reg.tx() as c:
            result = c.execute("SELECT result_json FROM jobs WHERE id=?",
                               (self.job_id,)).fetchone()
        self.assertEqual(json.loads(result["result_json"])["sha256"], sha256.lower())

        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000000"}):
            _, token = self.reg.create_public_session(max_total_bytes=1000, max_files=5)
            inv = self.reg.invitation_for_token(token)
            second, outcome = self.reg.reserve_asset(
                inv, name="second.mov", relative_path="another-camera/second.mov",
                size=41, content_type="video/quicktime", last_modified=101,
                bucket="disposable", key_fn=lambda uid: "synthetic/" + uid,
            )
            self.assertEqual(outcome, "new")
            self.assertTrue(self.reg.mark_completed(second["asset_id"], "version", "etag", "test"))
        job = self.reg.lease_job("worker-two", 60)
        self.assertEqual(job["asset"]["asset_id"], second["asset_id"])
        self.assertTrue(self.reg.finish_job(job["job_id"], "worker-two", True,
                        result={"sha256": sha256.lower(), "sha1": sha1.lower()}))
        duplicate = self.reg.get_asset(second["asset_id"])
        self.assertEqual(duplicate["sha256"], sha256.lower())
        self.assertEqual(duplicate["duplicate_of"], self.asset_id)

    def test_preexisting_mixed_case_hash_is_still_detected_after_reopen(self):
        digest = "A1b2" * 16  # valid 64-character hexadecimal value
        first_job = self.reg.lease_job("prior-worker", 60)
        self.assertTrue(self.reg.finish_job(first_job["job_id"], "prior-worker",
                                            True, result={"sha256": digest}))
        with self.reg.tx() as c:
            # Model a DB written by an older deployed registry revision.
            c.execute("UPDATE assets SET sha256=? WHERE asset_id=?",
                      (digest, self.asset_id))
        self.reg = registry.Registry(self.reg.path)  # in-place schema upgrade
        with self.reg.tx() as c:
            self.assertEqual(
                c.execute("SELECT sha256 FROM assets WHERE asset_id=?",
                          (self.asset_id,)).fetchone()["sha256"], digest
            )
            self.assertIsNotNone(c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='assets_sha256_nocase'"
            ).fetchone())

        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000000"}):
            _, token = self.reg.create_public_session(max_total_bytes=1000, max_files=5)
            inv = self.reg.invitation_for_token(token)
            second, outcome = self.reg.reserve_asset(
                inv, name="second-legacy-hash.mov", relative_path="second.mov",
                size=41, content_type="video/quicktime", last_modified=101,
                bucket="disposable", key_fn=lambda uid: "synthetic/" + uid,
            )
            self.assertEqual(outcome, "new")
            self.assertTrue(self.reg.mark_completed(second["asset_id"],
                                                    "version", "etag", "test"))
        second_job = self.reg.lease_job("current-worker", 60)
        self.assertEqual(second_job["asset"]["asset_id"], second["asset_id"])
        self.assertTrue(self.reg.finish_job(second_job["job_id"], "current-worker",
                                            True, result={"sha256": digest.lower()}))
        match = self.reg.get_asset(second["asset_id"])
        self.assertEqual(match["sha256"], digest.lower())
        self.assertEqual(match["duplicate_of"], self.asset_id)
        # Historical metadata is retained byte-for-byte, not silently migrated.
        self.assertEqual(self.reg.get_asset(self.asset_id)["sha256"], digest)

    def test_success_without_content_hash_still_completes_compatibly(self):
        self.reg.lease_job("worker", 60)
        self.assertTrue(self.reg.finish_job(self.job_id, "worker", True,
                                            result={"processed": True}))
        asset = self.reg.get_asset(self.asset_id)
        self.assertIsNone(asset["sha256"])
        self.assertIsNone(asset["duplicate_of"])
        self.assertEqual(self.snapshot()[0][0], "done")

    def test_invalid_initial_lease_cannot_consume_attempt_or_requeue_jobs(self):
        initial = self.snapshot()
        for bad in INVALID_LEASES:
            with self.subTest(lease_s=repr(bad)):
                with self.assertRaises(ValueError):
                    self.reg.lease_job("worker", bad)
                self.assertEqual(self.snapshot(), initial)
        job = self.reg.lease_job("worker", 60.5)
        self.assertEqual(job["job_id"], self.job_id)
        self.assertEqual(job["attempt"], 1)
        self.assertEqual(self.snapshot()[0][1], 1)

    def test_invalid_heartbeat_leaves_current_lease_unchanged(self):
        self.reg.lease_job("worker", 60)
        before = self.snapshot()
        for bad in INVALID_LEASES:
            with self.subTest(lease_s=repr(bad)):
                with self.assertRaises(ValueError):
                    self.reg.heartbeat(self.job_id, "worker", bad)
                self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.reg.heartbeat(self.job_id, "worker", 60.25))
        after = self.snapshot()
        self.assertEqual(after[0][0], "leased")
        self.assertGreater(after[0][3], time.time())
        self.assertTrue(self.reg.finish_job(self.job_id, "worker", True,
                                            result={"sha256": "a" * 64}))
        self.assertEqual(self.snapshot()[0][0], "done")

    def test_invalid_deferrals_never_erase_lease_or_set_retry_infinity(self):
        self.reg.lease_job("worker", 60)
        before = self.snapshot()
        for bad in INVALID_DELAYS:
            with self.subTest(delay_s=repr(bad)):
                with self.assertRaises(ValueError):
                    self.reg.defer_job(self.job_id, "worker", bad, "synthetic capacity")
                self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.reg.defer_job(self.job_id, "worker", 0, "zero-delay retry"))
        state, _events = self.snapshot()
        self.assertEqual(state[0], "queued")
        self.assertEqual(state[1], 0)
        self.assertIsNone(state[2])
        self.assertIsNone(state[3])
        self.assertTrue(math.isfinite(state[4]))
        self.assertLessEqual(state[4], time.time())
        next_attempt = self.reg.lease_job("worker", 60)
        self.assertEqual(next_attempt["attempt"], 1)

    def test_positive_fractional_defer_and_expired_lease_rejection(self):
        first = self.reg.lease_job("worker", 60)
        self.assertEqual(first["attempt"], 1)
        before = time.time()
        self.assertTrue(self.reg.defer_job(self.job_id, "worker", 30.25, "short retry"))
        with self.reg.tx() as c:
            queued = c.execute(
                "SELECT available_at, status FROM jobs WHERE id=?", (self.job_id,)
            ).fetchone()
        self.assertEqual(queued["status"], "queued")
        self.assertGreaterEqual(queued["available_at"], before + 30.20)
        self.assertIsNone(self.reg.lease_job("another-worker", 30))

        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET available_at=0 WHERE id=?", (self.job_id,))
        self.reg.lease_job("worker", 60)
        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET lease_expires=? WHERE id=?",
                      (time.time() - 10, self.job_id))
        before_expired = self.snapshot()
        self.assertFalse(self.reg.heartbeat(self.job_id, "worker", 60))
        self.assertFalse(self.reg.defer_job(self.job_id, "worker", 0, "too late"))
        self.assertEqual(self.snapshot(), before_expired)


if __name__ == "__main__":
    unittest.main()
