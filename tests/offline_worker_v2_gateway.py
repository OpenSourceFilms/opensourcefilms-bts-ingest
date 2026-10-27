"""Isolated gateway proof: exact B2 version verification and atomic lease finish.

All storage is in memory, all registry rows are in temporary SQLite.
No HTTP, real B2, production service, live media or credentials.
"""
import hashlib
import io
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bts_ingest import derivative_generation, registry, worker_v2_gateway


class FakeVersionB2:
    def __init__(self):
        self.versions = {}
        self.seq = 0
        self.calls = 0
        self.on_get = None
        self.forced_version = None

    def put(self, key, data):
        self.seq += 1
        version = f"v{self.seq}"
        self.versions[key, version] = data
        return version

    def get_object(self, *, Bucket, Key, VersionId):
        self.calls += 1
        if self.on_get:
            callback, self.on_get = self.on_get, None
            callback()
        data = self.versions[Key, VersionId]
        return {"VersionId": self.forced_version or VersionId,
                "Body": io.BytesIO(data)}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(prefix="bts_v2_gateway_")
        self.addCleanup(d.cleanup)
        self.reg = registry.Registry(Path(d.name) / "isolated.sqlite3")
        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000000"}):
            _, secret = self.reg.create_public_session(max_total_bytes=1000, max_files=5)
            inv = self.reg.invitation_for_token(secret)
            self.asset, state = self.reg.reserve_asset(
                inv, name="source.mov", relative_path="source.mov", size=123,
                content_type="video/quicktime", last_modified=42,
                bucket="disposable", key_fn=lambda uid: (
                    "osf/0000/0000-00-00_unknown/crew/" + uid + "/source.mov"),
            )
            self.assertEqual(state, "new")
            self.assertTrue(self.reg.mark_completed(self.asset["asset_id"], "v", "etag", "test"))
        self.client = FakeVersionB2()
        self.gate = worker_v2_gateway.FencedWorkerGateway(
            self.reg, self.client, "synthetic-derived", lease_s=60,
            max_verified_bytes=1 << 20
        )

    def get_job(self):
        with self.reg.tx() as c:
            return dict(c.execute("SELECT * FROM jobs").fetchone())

    def payload(self, token, data=b"accepted output"):
        key = derivative_generation.generation_key(self.asset["b2_key"], token, "thumb.jpg")
        version = self.client.put(key, data)
        return {"publication_protocol": derivative_generation.PROTOCOL,
                "derivatives": [{"b2_key":key,
                                 "sha256":hashlib.sha256(data).hexdigest(),
                                 "version_id":version, "bytes":len(data),
                                 "role":"thumbnail"}]}

    def test_verified_acceptance_and_no_v1_fallback(self):
        lease = self.gate.lease("worker")
        result = self.payload(lease["lease_token"])
        self.assertTrue(self.gate.heartbeat(lease["job_id"], "worker", lease["lease_token"]))
        self.assertFalse(self.reg.finish_job(lease["job_id"], "worker", True, result=result))
        self.assertTrue(self.gate.finish(lease["job_id"], "worker",
                                         lease["lease_token"], True, result))
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.get_job()["status"], "done")
        self.assertFalse(self.gate.finish(lease["job_id"], "worker",
                                          lease["lease_token"], True, result))
        self.assertEqual(self.client.calls, 1)

    def test_rejects_unverified_missing_or_wrong_remote_versions(self):
        for kind in ("missing", "wrong", "changed", "mismatched-size"):
            with self.subTest(kind=kind):
                d = tempfile.TemporaryDirectory(prefix="bts_gateway_bad_")
                try:
                    reg = registry.Registry(Path(d.name) / "db.sqlite3")
                    with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET":"1000000"}):
                        _, secret = reg.create_public_session(max_total_bytes=1000,max_files=5)
                        inv = reg.invitation_for_token(secret)
                        asset, _ = reg.reserve_asset(
                            inv, name="x.mov", relative_path="x.mov", size=10,
                            content_type=None, last_modified=1,bucket="synthetic",
                            key_fn=lambda uid: "osf/0000/0000-00-00_unknown/crew/"+uid+"/source.mov")
                        reg.mark_completed(asset["asset_id"], "v", "etag", "test")
                    client = FakeVersionB2()
                    gate = worker_v2_gateway.FencedWorkerGateway(reg,client,"synthetic",lease_s=60)
                    claim=gate.lease("worker")
                    key=derivative_generation.generation_key(asset["b2_key"],claim["lease_token"],"x.jpg")
                    ver=client.put(key,b"abc")
                    manifest={"publication_protocol":derivative_generation.PROTOCOL,
                              "derivatives":[{"b2_key":key,"sha256":hashlib.sha256(b"abc").hexdigest(),
                                              "version_id":ver,"bytes":3}]}
                    if kind=="missing":
                        client.versions.clear()
                    elif kind=="wrong":
                        client.forced_version="wrong-version"
                    elif kind=="changed":
                        client.versions[key,ver]=b"bad"
                    else:
                        manifest["derivatives"][0]["bytes"]=4
                    with self.assertRaises(worker_v2_gateway.DerivativeVerificationError):
                        gate.finish(claim["job_id"],"worker",claim["lease_token"],True,manifest)
                    with reg.tx() as c:
                        self.assertEqual(c.execute("SELECT status FROM jobs").fetchone()[0],"leased")
                finally:
                    d.cleanup()

    def test_lease_expiry_during_remote_get_cannot_commit(self):
        a = self.gate.lease("same-owner")
        manifest = self.payload(a["lease_token"])
        def reassign():
            with self.reg.tx() as c:
                c.execute("UPDATE jobs SET lease_expires=? WHERE id=?",
                          (time.time()-5,a["job_id"]))
            self.successor=self.gate.lease("same-owner")
        self.client.on_get=reassign
        self.assertFalse(self.gate.finish(a["job_id"],"same-owner",
                                          a["lease_token"],True,manifest))
        self.assertEqual(self.get_job()["status"],"leased")
        self.assertEqual(self.get_job()["lease_owner"],"same-owner")
        self.assertEqual(self.get_job()["attempts"],2)
        self.assertTrue(self.gate.finish(self.successor["job_id"],"same-owner",
                                         self.successor["lease_token"],True,
                                         self.payload(self.successor["lease_token"])))

    def test_synthetic_stale_generation_is_never_selected(self):
        a=self.gate.lease("worker")
        stale=self.payload(a["lease_token"],b"A")
        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET lease_expires=? WHERE id=?",
                      (time.time()-5,a["job_id"]))
        b=self.gate.lease("worker")
        fresh=self.payload(b["lease_token"],b"B")
        self.assertTrue(self.gate.finish(b["job_id"],"worker",b["lease_token"],True,fresh))
        self.assertFalse(self.gate.finish(a["job_id"],"worker",a["lease_token"],True,stale))
        with self.reg.tx() as c:
            import json
            accepted=json.loads(c.execute("SELECT result_json FROM jobs").fetchone()[0])
        self.assertEqual(accepted["derivatives"][0]["b2_key"],
                         fresh["derivatives"][0]["b2_key"])

    def test_bounded_stream_and_strict_v2_credentials(self):
        lease=self.gate.lease("worker")
        payload=self.payload(lease["lease_token"],b"oversize")
        payload["derivatives"][0]["bytes"]=1<<30
        with self.assertRaises(worker_v2_gateway.DerivativeVerificationError):
            self.gate.finish(lease["job_id"],"worker",lease["lease_token"],True,payload)
        self.assertEqual(self.client.calls,0)
        for missing in (None,"",42,"bad/secret"):
            with self.subTest(token=repr(missing)):
                with self.assertRaises(ValueError):
                    self.gate.heartbeat(lease["job_id"],"worker",missing)
        self.assertTrue(self.gate.defer(lease["job_id"],"worker",
                                        lease["lease_token"],0,"synthetic retry"))


if __name__ == "__main__":
    unittest.main()
