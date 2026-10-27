"""Generation-scoped derivative-publication contract, isolated synthetic test.

Tests the public registry's accepted result pointer against a memory-only
versioned B2 substitute. Does NOT exercise real B2, worker, HTTP or local
filesystem promotion. Production output migration must be independently proven.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bts_ingest import derivative_generation, registry


class FakeVersionedB2:
    def __init__(self):
        self.latest = {}
        self.versions = {}
        self.number = 0

    def put(self, key, data):
        self.number += 1
        version = f"synthetic-v{self.number}"
        self.latest[key] = version
        self.versions[(key, version)] = bytes(data)
        return version

    def get(self, key, version=None):
        version = version or self.latest[key]
        return self.versions[key, version]


class DerivativeGenerationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bts_derivative_generation_")
        self.addCleanup(directory.cleanup)
        self.reg = registry.Registry(Path(directory.name) / "isolated.sqlite3")
        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000000"}):
            _, cap = self.reg.create_public_session(max_total_bytes=1000, max_files=5)
            inv = self.reg.invitation_for_token(cap)
            self.asset, outcome = self.reg.reserve_asset(
                inv, name="synthetic.mov", relative_path="camera/synthetic.mov",
                size=123, content_type="video/quicktime", last_modified=100,
                bucket="disposable", key_fn=lambda uid: registry.sha256_hex(uid)[:24],
            )
            self.assertEqual(outcome, "new")
            self.assertTrue(self.reg.mark_completed(self.asset["asset_id"], "v", "etag", "test"))
        # The synthetic reserve callback above is not a production object-key
        # generator. Set a structurally valid server-shaped key for this proof.
        self.original_key = "lifER/0000/0000-00-00_unknown/crew/" + self.asset["asset_id"] + "/source.mov"
        with self.reg.tx() as c:
            c.execute("UPDATE assets SET b2_key=? WHERE asset_id=?",
                      (self.original_key, self.asset["asset_id"]))
            self.job_id = c.execute("SELECT id FROM jobs WHERE asset_id=?",
                                    (self.asset["asset_id"],)).fetchone()["id"]
        self.fake = FakeVersionedB2()

    def manifest(self, token, data=b"synthetic", filename="thumbnail.jpg"):
        key = derivative_generation.generation_key(self.original_key, token, filename)
        version = self.fake.put(key, data)
        return {"publication_protocol": derivative_generation.PROTOCOL,
                "derivatives": [{
                    "role": "thumbnail", "b2_key": key,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "version_id": version, "bytes": len(data)}]}

    def stored(self):
        with self.reg.tx() as c:
            return c.execute("SELECT status,result_json,lease_token_sha256 FROM jobs "
                             "WHERE id=?", (self.job_id,)).fetchone()

    def test_generation_name_is_safe_and_domain_separated_from_secret(self):
        lease = self.reg.lease_job_fenced("worker", 60)
        token = lease["lease_token"]
        prefix = derivative_generation.generation_prefix(self.original_key, token)
        self.assertIn("/_generations/", prefix)
        self.assertNotIn(token, prefix)
        self.assertNotIn(registry.sha256_hex(token)[:32], prefix)
        for name in ("../x.jpg", "nested/x.jpg", "", ".", "..", "a\\b"):
            with self.subTest(filename=name):
                with self.assertRaises(ValueError):
                    derivative_generation.generation_key(self.original_key, token, name)

    def test_validation_refuses_shared_keys_and_prepublished_local_paths(self):
        lease = self.reg.lease_job_fenced("worker", 60)
        token = lease["lease_token"]
        good = self.manifest(token)
        wrong_token = "some-other-valid-token"
        invalid = [
            None,
            {},
            {"processed": True},
            {**good, "derivatives": [{**good["derivatives"][0],
                                     "b2_key": derivative_generation.generation_key(
                                         self.original_key, wrong_token, "thumbnail.jpg")}]},
            {**good, "derivatives": [{**good["derivatives"][0],
                                     "b2_key": "shared/latest/thumbnail.jpg"}]},
            {**good, "derivatives": [{**good["derivatives"][0],
                                     "version_id": None}]},
            {**good, "derivatives": [{**good["derivatives"][0],
                                     "sha256": "invalid"}]},
            {**good, "derivatives": [{**good["derivatives"][0],
                                     "bytes": -1}]},
            {**good, "derivatives": good["derivatives"] * 2},
            {**good, "published_path": "/canonical/visible"},
            {**good, "publish_state": "replaced"},
            {**good, "publication_protocol": "latest-key-wins"},
        ]
        for item in invalid:
            with self.subTest(item=item):
                before = tuple(self.stored())
                with self.assertRaises(ValueError):
                    self.reg.finish_job(self.job_id, "worker", True,
                                        result=item, lease_token=token)
                self.assertEqual(tuple(self.stored()), before)
        self.assertTrue(self.reg.finish_job(self.job_id, "worker", True,
                                            result=good, lease_token=token))

    def test_late_A_upload_cannot_change_accepted_B_version_pointer(self):
        a = self.reg.lease_job_fenced("same-owner", 60)
        data_a = b"stale A generation"
        pending_a = self.manifest(a["lease_token"], data_a)
        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET lease_expires=? WHERE id=?",
                      (time.time() - 4, self.job_id))
        b = self.reg.lease_job_fenced("same-owner", 60)
        self.assertNotEqual(a["lease_token"], b["lease_token"])
        data_b = b"accepted B generation"
        committed_b = self.manifest(b["lease_token"], data_b)
        self.assertTrue(self.reg.finish_job(self.job_id, "same-owner", True,
                                            result=committed_b, lease_token=b["lease_token"]))
        accepted_before = json.loads(self.stored()["result_json"])
        self.assertEqual(accepted_before["generation_prefix"],
                         derivative_generation.generation_prefix(self.original_key, b["lease_token"]))
        self.assertNotIn(b["lease_token"], json.dumps(accepted_before))
        # Delayed A continues uploading its *own* generation. Canonical
        # readers resolve B from the accepted manifest and pinned version.
        self.manifest(a["lease_token"], b"even later stale A")
        self.assertFalse(self.reg.finish_job(self.job_id, "same-owner", True,
                                             result=pending_a, lease_token=a["lease_token"]))
        accepted_after = json.loads(self.stored()["result_json"])
        self.assertEqual(accepted_before, accepted_after)
        ref = accepted_after["derivatives"][0]
        self.assertEqual(self.fake.get(ref["b2_key"], ref["version_id"]), data_b)
        self.assertNotEqual(ref["b2_key"], pending_a["derivatives"][0]["b2_key"])

    def test_stale_generation_finish_during_B_lease_is_not_accepted(self):
        a = self.reg.lease_job_fenced("same-owner", 60)
        pending_a = self.manifest(a["lease_token"])
        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET lease_expires=? WHERE id=?",
                      (time.time() - 4, self.job_id))
        b = self.reg.lease_job_fenced("same-owner", 60)
        self.assertFalse(self.reg.finish_job(self.job_id, "same-owner", True,
                                             result=pending_a, lease_token=a["lease_token"]))
        self.assertEqual(self.stored()["status"], "leased")
        good = self.manifest(b["lease_token"])
        self.assertTrue(self.reg.finish_job(self.job_id, "same-owner", True,
                                            result=good, lease_token=b["lease_token"]))

    def test_legacy_unfenced_worker_contract_remains_unchanged(self):
        lease = self.reg.lease_job("legacy-worker", 60)
        self.assertNotIn("lease_token", lease)
        self.assertTrue(self.reg.finish_job(self.job_id, "legacy-worker", True,
                                            result={"derivatives": [{"b2_key": "legacy"}]}))


if __name__ == "__main__":
    unittest.main()
