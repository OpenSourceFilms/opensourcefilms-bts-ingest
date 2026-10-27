"""Read-side accepted-generation pointer contract in disposable SQLite only."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bts_ingest import registry,derivative_generation,accepted_derivatives


class ReaderTests(unittest.TestCase):
    def setUp(self):
        td=tempfile.TemporaryDirectory(prefix="bts_accepted_read_")
        self.addCleanup(td.cleanup)
        self.reg=registry.Registry(Path(td.name)/"isolated.sqlite3")
        with mock.patch.dict("os.environ",{"BTS_PUBLIC_BYTE_BUDGET":"1000000"}):
            _,secret=self.reg.create_public_session(max_total_bytes=1000,max_files=5)
            inv=self.reg.invitation_for_token(secret)
            asset,_=self.reg.reserve_asset(
                inv,name="x.mov",relative_path="x.mov",size=10,content_type=None,
                last_modified=123,bucket="disposable",
                key_fn=lambda uid:"osf/0000/0000-00-00_unknown/crew/"+uid+"/x.mov")
            self.reg.mark_completed(asset["asset_id"],"v","etag","test")
        self.asset=asset
        with self.reg.tx() as c:
            self.job=c.execute("SELECT id FROM jobs").fetchone()[0]

    def manifest(self, token, data=b"synthetic"):
        key=derivative_generation.generation_key(self.asset["b2_key"],token,"thumb.jpg")
        return {"publication_protocol":derivative_generation.PROTOCOL,
                "derivatives":[{"b2_key":key,"version_id":"b2-synthetic-v1",
                                "sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)}]}

    def test_only_v2_done_is_readable(self):
        self.assertIsNone(accepted_derivatives.accepted_generation(self.reg,self.job))
        l=self.reg.lease_job_fenced("worker",60)
        self.assertIsNone(accepted_derivatives.accepted_generation(self.reg,self.job))
        manifest=self.manifest(l["lease_token"])
        self.assertTrue(self.reg.finish_job(self.job,"worker",True,result=manifest,
                                            lease_token=l["lease_token"]))
        ptr=accepted_derivatives.accepted_generation(self.reg,self.job)
        self.assertEqual(ptr["derivatives"][0]["b2_key"],manifest["derivatives"][0]["b2_key"])
        self.assertEqual(ptr["derivatives"][0]["version_id"],"b2-synthetic-v1")
        self.assertNotIn(l["lease_token"],json.dumps(ptr))
        with self.reg.tx() as c:
            stored=c.execute("SELECT accepted_generation_prefix,result_json FROM jobs").fetchone()
        self.assertEqual(stored["accepted_generation_prefix"],ptr["generation_prefix"])

    def test_legacy_done_cannot_forge_accepted_v2_pointer(self):
        l=self.reg.lease_job("legacy",60)
        fake=self.manifest("validfakecredential")
        fake["generation_prefix"]=derivative_generation.generation_prefix(
            self.asset["b2_key"],"validfakecredential")
        self.assertTrue(self.reg.finish_job(self.job,"legacy",True,result=fake))
        self.assertIsNone(accepted_derivatives.accepted_generation(self.reg,self.job))
        with self.reg.tx() as c:
            self.assertIsNone(c.execute("SELECT accepted_generation_prefix FROM jobs").fetchone()[0])

    def test_corrupt_record_must_fail_closed(self):
        l=self.reg.lease_job_fenced("worker",60)
        m=self.manifest(l["lease_token"])
        self.assertTrue(self.reg.finish_job(self.job,"worker",True,result=m,lease_token=l["lease_token"]))
        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET result_json=? WHERE id=?",
                      (json.dumps({**m,"generation_prefix":"forged"}),self.job))
        with self.assertRaises(ValueError):
            accepted_derivatives.accepted_generation(self.reg,self.job)

    def test_wrong_prefix_version_and_key_are_rejected(self):
        l=self.reg.lease_job_fenced("worker",60)
        m=self.manifest(l["lease_token"])
        self.assertTrue(self.reg.finish_job(self.job,"worker",True,result=m,lease_token=l["lease_token"]))
        def stored():
            with self.reg.tx() as c:
                return json.loads(c.execute("SELECT result_json FROM jobs").fetchone()[0])
        valid=stored()
        for variant in ("version","latest","duplicate","prefix","bytes"):
            with self.subTest(variant=variant):
                forged=json.loads(json.dumps(valid))
                if variant=="version":
                    forged["derivatives"][0]["version_id"]=None
                elif variant=="latest":
                    forged["derivatives"][0]["b2_key"]="canonical/latest/thumb.jpg"
                elif variant=="duplicate":
                    forged["derivatives"]*=2
                elif variant=="prefix":
                    forged["generation_prefix"]="other-prefix"
                else:
                    forged["derivatives"][0]["bytes"]=-1
                with self.reg.tx() as c:
                    c.execute("UPDATE jobs SET result_json=? WHERE id=?",
                              (json.dumps(forged),self.job))
                with self.assertRaises(ValueError):
                    accepted_derivatives.accepted_generation(self.reg,self.job)
        with self.reg.tx() as c:
            c.execute("UPDATE jobs SET result_json=? WHERE id=?",(json.dumps(valid),self.job))
        self.assertIsNotNone(accepted_derivatives.accepted_generation(self.reg,self.job))

    def test_empty_manifest_is_valid_and_no_synthetic_reader_fallback(self):
        l=self.reg.lease_job_fenced("worker",60)
        m={"publication_protocol":derivative_generation.PROTOCOL,"derivatives":[]}
        self.assertTrue(self.reg.finish_job(self.job,"worker",True,result=m,lease_token=l["lease_token"]))
        ptr=accepted_derivatives.accepted_generation(self.reg,self.job)
        self.assertEqual(ptr["derivatives"],[])
        with self.assertRaises(ValueError):
            accepted_derivatives.accepted_generation(self.reg,"1")


if __name__=="__main__":
    unittest.main()
