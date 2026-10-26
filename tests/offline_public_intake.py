"""Public BTS intake offline contract, registry isolation and provenance.

No B2 access, API startup, production database or network. Self-contained SQLite.
"""
import json
import math
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bts_ingest import naming,public_intake,registry

class PublicIntakeTest(unittest.TestCase):
    def setUp(self):
        self.env_budget=mock.patch.dict("os.environ",{"BTS_PUBLIC_BYTE_BUDGET":str(2*1024**4)})
        self.env_budget.start()
        self.addCleanup(self.env_budget.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.db=str(pathlib.Path(self.tmp.name)/"test.sqlite3")
        self.reg=registry.Registry(self.db)
        self.p1,self.t1=self.reg.create_public_session()
        self.p2,self.t2=self.reg.create_public_session("  Tāmara  ")

    def tearDown(self):
        self.tmp.cleanup()

    def invite(self,token):
        return self.reg.invitation_for_token(token)

    def reserve(self,token,name="file.mov",size=1234,last_modified=10):
        inv=self.invite(token)
        return self.reg.reserve_asset(inv,name=name,relative_path="camera/"+name,
            size=size,content_type="video/quicktime",last_modified=last_modified,
            bucket="example-bts-objects",
            key_fn=lambda uid:naming.original_key(inv["production"],inv["shoot_date"],
                 inv["shoot_slug"],inv["contributor_slug"],uid,name))

    def test_public_session_rejects_nonfinite_or_invalid_ttls_without_writes(self):
        invalid = (-1, 0, -0.1, float("inf"), float("-inf"),
                   float("nan"), True, False, None, "3600", 10 ** 400, 1e-300)
        with self.reg.tx() as c:
            original = tuple(c.execute(
                "SELECT (SELECT COUNT(*) FROM invitations), "
                "(SELECT COUNT(*) FROM public_sessions), "
                "(SELECT COUNT(*) FROM events)"
            ).fetchone())
        for value in invalid:
            with self.subTest(ttl=repr(value)):
                with mock.patch.object(registry.secrets, "token_urlsafe",
                                       side_effect=AssertionError("token generated")):
                    with self.assertRaisesRegex(ValueError, "ttl_s"):
                        self.reg.create_public_session(ttl_s=value)
                with self.reg.tx() as c:
                    after = tuple(c.execute(
                        "SELECT (SELECT COUNT(*) FROM invitations), "
                        "(SELECT COUNT(*) FROM public_sessions), "
                        "(SELECT COUNT(*) FROM events)"
                    ).fetchone())
                self.assertEqual(after, original)

    def test_admin_invitation_rejects_nonfinite_ttl_and_grace(self):
        settings = dict(production="ExampleProduction", shoot_date="2026-10-27",
                        shoot_slug="unit", contributor_slug="crew",
                        contributor_label="Synthetic", ttl_s=3600,
                        max_total_bytes=1000, max_files=2, finish_grace_s=60)
        invalid_duration = (-60, float("inf"), float("-inf"), float("nan"),
                            True, None, "60", 10 ** 400)
        with self.reg.tx() as c:
            before = c.execute("SELECT COUNT(*) FROM invitations").fetchone()[0]
        for field in ("ttl_s", "finish_grace_s"):
            for value in invalid_duration:
                with self.subTest(field=field, invalid=repr(value)):
                    args = dict(settings, **{field: value})
                    with self.assertRaises(ValueError):
                        self.reg.create_invitation(**args)
        for ttl, grace in ((1e308, 1e308), (1e-300, 0)):
            with self.subTest(ttl=ttl, grace=grace):
                with self.assertRaises(ValueError):
                    self.reg.create_invitation(**dict(settings, ttl_s=ttl,
                                                      finish_grace_s=grace))
        with self.reg.tx() as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM invitations").fetchone()[0], before
            )

    def test_both_capability_creators_validate_integer_quota_fields(self):
        bad = (-1, True, False, 12.0, "12", None, 1 << 63, 10 ** 400)
        for method in ("public", "invitation"):
            for field in ("max_total_bytes", "max_files"):
                for value in bad:
                    with self.subTest(method=method, field=field, value=repr(value)):
                        args = {field: value}
                        if method == "public":
                            call = lambda: self.reg.create_public_session(**args)
                        else:
                            settings = dict(
                                production="Example", shoot_date="2026-10-27",
                                shoot_slug="unit", contributor_slug="crew",
                                contributor_label="Crew", ttl_s=3600,
                                max_total_bytes=1000, max_files=5
                            )
                            settings.update(args)
                            call = lambda: self.reg.create_invitation(**settings)
                        with self.assertRaisesRegex(ValueError, field):
                            call()

    def test_valid_fractional_ttl_zero_grace_and_disabled_quotas(self):
        iid, token = self.reg.create_invitation(
            "Example", "2026-10-27", "unit", "crew", "Crew",
            ttl_s=1800.25, max_total_bytes=0, max_files=0,
            finish_grace_s=0
        )
        record = self.reg.invitation_for_token(token)
        self.assertEqual(record["id"], iid)
        self.assertEqual(record["max_total_bytes"], 0)
        self.assertEqual(record["max_files"], 0)
        self.assertEqual(record["finish_grace_s"], 0)
        self.assertAlmostEqual(record["expires_at"] - record["created_at"], 1800.25, places=4)
        _, pub_token = self.reg.create_public_session(ttl_s=3600.5,
                                                      max_total_bytes=0, max_files=0)
        pub = self.reg.invitation_for_token(pub_token)
        self.assertIsNotNone(pub)
        self.assertEqual(pub["max_total_bytes"], 0)
        self.assertEqual(pub["max_files"], 0)
        self.assertTrue(math.isfinite(pub["expires_at"]))

    def test_capabilities_unique_and_hashed_at_rest(self):
        self.assertNotEqual(self.t1,self.t2)
        con=sqlite3.connect(self.db)
        hashes=[x[0] for x in con.execute("SELECT token_sha256 FROM invitations")]
        con.close()
        self.assertIn(registry.sha256_hex(self.t1),hashes)
        self.assertNotIn(self.t1,hashes)
        self.assertEqual(len(self.t1)>32,True)

    def test_name_optional_and_normalized(self):
        self.assertEqual(self.invite(self.t1)["contributor_label"],"")
        self.assertEqual(self.reg.public_session(self.p1)["uploader_name_claimed"],None)
        self.assertEqual(self.reg.public_session(self.p2)["uploader_name_claimed"],"Tāmara")

    def test_missing_name_no_fake_creator(self):
        p={**dict(self.invite(self.t1)),"is_public_intake":True}
        out=public_intake.provenance(p)
        self.assertEqual(out["shoot_date"],None)
        self.assertEqual(out["creator_identity"],None)
        self.assertEqual(out["uploader_name_claimed"],None)
        self.assertEqual(out["intake_kind"],"public_unclassified")

    def test_legacy_invitation_false_text_cannot_erase_shoot_provenance(self):
        _, token = self.reg.create_invitation(
            "LifeR", "2026-10-19", "test-camera", "contributor",
            "Synthetic Uploader", ttl_s=3600,
            max_total_bytes=1024, max_files=1,
        )
        inv = dict(self.invite(token))
        self.assertEqual(public_intake.provenance(inv)["shoot_date"], "2026-10-19")
        for malformed in ("False", "false", "0", "True", "true", "1"):
            with self.subTest(flag=malformed):
                record = {**inv, "is_public_intake": malformed}
                # The old bool(value) silently classified the string
                # "False" as public intake and erased assigned shoot data.
                with self.assertRaisesRegex(ValueError, "is_public_intake"):
                    public_intake.provenance(record)
                with self.assertRaisesRegex(ValueError, "is_public_intake"):
                    public_intake.actual_shoot_date(record)

    def test_public_flag_accepts_real_boolean_and_sqlite_integer(self):
        inv = dict(self.invite(self.t1))
        for flag in (True, 1):
            with self.subTest(flag=flag):
                self.assertTrue(public_intake.is_public({**inv, "is_public_intake": flag}))
                self.assertIsNone(public_intake.provenance(
                    {**inv, "is_public_intake": flag})["shoot_date"])
        for flag in (False, 0):
            with self.subTest(flag=flag):
                self.assertFalse(public_intake.is_public({**inv, "is_public_intake": flag}))
                self.assertEqual(public_intake.provenance(
                    {**inv, "is_public_intake": flag})["shoot_date"], "0000-00-00")

    def test_malformed_explicit_flags_fail_instead_of_guessing_origin(self):
        inv = dict(self.invite(self.t1))
        for malformed in (None, "", "no", 2, -1, 0.0, [], {}):
            with self.subTest(flag=repr(malformed)):
                with self.assertRaisesRegex(ValueError, "is_public_intake"):
                    public_intake.is_public({**inv, "is_public_intake": malformed})
        self.assertFalse(public_intake.is_public(inv))

    def test_session_partition_not_fake_shoot(self):
        inv=self.invite(self.t1)
        self.assertEqual(inv["shoot_date"],"0000-00-00")
        self.assertEqual(inv["production"],"Unclassified")
        self.assertEqual(inv["note"],public_intake.PUBLIC_NOTE)
        self.assertIsNone(public_intake.actual_shoot_date({**dict(inv),"is_public_intake":True}))

    def test_isolated_same_name_file_reservations(self):
        a,s1=self.reserve(self.t1)
        b,s2=self.reserve(self.t2)
        self.assertEqual((s1,s2),("new","new"))
        self.assertNotEqual(a["asset_id"],b["asset_id"])
        self.assertNotEqual(a["b2_key"],b["b2_key"])
        self.assertIsNone(self.reg.asset_by_key(self.p1,b["b2_key"]))
        self.assertIsNone(self.reg.asset_by_key(self.p2,a["b2_key"]))
        self.assertEqual(self.reg.receipt(self.p1)[0]["asset_id"],a["asset_id"])
        self.assertEqual(self.reg.receipt(self.p2)[0]["asset_id"],b["asset_id"])

    def test_touch_refuses_terminal_or_arbitrary_status_without_mutation(self):
        asset, status = self.reserve(self.t1, name="touch-test.mov")
        self.assertEqual(status, "new")
        with self.reg.tx() as c:
            before = c.execute(
                "SELECT status, updated_at, last_activity_at FROM assets WHERE asset_id=?",
                (asset["asset_id"],),
            ).fetchone()
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM jobs WHERE asset_id=?", (asset["asset_id"],)
            ).fetchone()[0], 0)

        for invalid in ("completed", "aborted", "failed", "quarantined", "garbage", "", True):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaisesRegex(ValueError, "touch status"):
                    self.reg.touch(asset["asset_id"], status=invalid)
                with self.reg.tx() as c:
                    current = c.execute(
                        "SELECT status, updated_at, last_activity_at FROM assets WHERE asset_id=?",
                        (asset["asset_id"],),
                    ).fetchone()
                    self.assertEqual(tuple(current), tuple(before))
                    self.assertEqual(c.execute(
                        "SELECT COUNT(*) FROM jobs WHERE asset_id=?", (asset["asset_id"],)
                    ).fetchone()[0], 0)

    def test_touch_retains_open_state_activity_and_completion_contract(self):
        asset, status = self.reserve(self.t1, name="open-activity.mov")
        self.assertEqual(status, "new")
        uid = asset["asset_id"]
        self.assertTrue(self.reg.touch(uid))
        self.assertTrue(self.reg.touch(uid, status="reserved"))
        self.assertTrue(self.reg.touch(uid, status="uploading"))
        self.assertEqual(self.reg.get_asset(uid)["status"], "uploading")
        self.assertTrue(self.reg.mark_completed(uid, "version-123", "etag-123", "test"))
        self.assertFalse(self.reg.touch(uid))
        self.assertFalse(self.reg.touch(uid, status="uploading"))
        with self.reg.tx() as c:
            jobs = c.execute(
                "SELECT kind, status FROM jobs WHERE asset_id=?", (uid,)
            ).fetchall()
        self.assertEqual([tuple(row) for row in jobs], [("process", "queued")])
        self.assertEqual(self.reg.get_asset(uid)["status"], "completed")

    def test_completion_rejects_invalid_version_without_terminal_side_effects(self):
        asset, kind = self.reserve(self.t1, name="missing-version.mov")
        self.assertEqual(kind, "new")
        uid = asset["asset_id"]
        before = self.reg.get_asset(uid)
        with self.reg.tx() as c:
            events_before = c.execute(
                "SELECT COUNT(*) FROM events WHERE asset_id=?", (uid,)
            ).fetchone()[0]
        invalid_versions = (
            None, "", " ", "\nversion", "ver\x00sion", "vérsion",
            True, 123, b"version", "v" * 4097,
        )
        for invalid in invalid_versions:
            with self.subTest(version=repr(invalid)):
                with self.assertRaisesRegex(ValueError, "version_id"):
                    self.reg.mark_completed(uid, invalid, "valid-etag", "worker")
                self.assertEqual(self.reg.get_asset(uid), before)
                with self.reg.tx() as c:
                    self.assertEqual(
                        c.execute("SELECT COUNT(*) FROM jobs WHERE asset_id=?",
                                  (uid,)).fetchone()[0], 0
                    )
                    self.assertEqual(
                        c.execute("SELECT COUNT(*) FROM events WHERE asset_id=?",
                                  (uid,)).fetchone()[0], events_before
                    )

    def test_completion_rejects_invalid_etag_without_terminal_side_effects(self):
        asset, _ = self.reserve(self.t1, name="missing-etag.mov")
        uid = asset["asset_id"]
        before = self.reg.get_asset(uid)
        with self.reg.tx() as c:
            events_before = c.execute(
                "SELECT COUNT(*) FROM events WHERE asset_id=?", (uid,)
            ).fetchone()[0]
        invalid_etags = (
            None, "", "\t", 'etag\rwith-control', "étag",
            False, 42, [], "e" * 4097,
        )
        for invalid in invalid_etags:
            with self.subTest(etag=repr(invalid)):
                with self.assertRaisesRegex(ValueError, "etag"):
                    self.reg.mark_completed(uid, "valid-version", invalid, "worker")
                self.assertEqual(self.reg.get_asset(uid), before)
                with self.reg.tx() as c:
                    self.assertEqual(
                        c.execute("SELECT COUNT(*) FROM jobs WHERE asset_id=?",
                                  (uid,)).fetchone()[0], 0
                    )
                    self.assertEqual(
                        c.execute("SELECT COUNT(*) FROM events WHERE asset_id=?",
                                  (uid,)).fetchone()[0], events_before
                    )

    def test_completion_keeps_valid_b2_multipart_etag_and_version_contract(self):
        asset, _ = self.reserve(self.t1, name="complete-with-identity.mov")
        uid = asset["asset_id"]
        etag = '"0123456789abcdef-12"'
        version = "4_z1234567890abcde_example"
        self.assertTrue(self.reg.mark_completed(uid, version, etag, "worker"))
        self.assertFalse(self.reg.mark_completed(uid, version, etag, "worker"))
        saved = self.reg.get_asset(uid)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["b2_version_id"], version)
        self.assertEqual(saved["b2_etag"], etag)
        with self.reg.tx() as c:
            jobs = c.execute(
                "SELECT kind,status FROM jobs WHERE asset_id=?", (uid,)
            ).fetchall()
            events = c.execute(
                "SELECT kind FROM events WHERE asset_id=? AND kind='asset.completed'",
                (uid,),
            ).fetchall()
        self.assertEqual([tuple(row) for row in jobs], [("process", "queued")])
        self.assertEqual(len(events), 1)

    def test_upload_activity_state_is_monotonic(self):
        asset, kind = self.reserve(self.t1, name="monotonic.mov")
        self.assertEqual(kind, "new")
        uid = asset["asset_id"]

        # Duplicate and forward transitions are valid. Upload completion
        # remains a separate operation, not an activity state.
        self.assertTrue(self.reg.touch(uid, status="reserved"))
        self.assertTrue(self.reg.touch(uid, status="uploading"))
        self.assertTrue(self.reg.touch(uid, status="uploading"))
        self.assertTrue(self.reg.touch(uid))
        with self.reg.tx() as c:
            before = c.execute(
                "SELECT status, updated_at, last_activity_at FROM assets WHERE asset_id=?",
                (uid,),
            ).fetchone()
        self.assertEqual(before["status"], "uploading")

        # A late request from the earlier 'reserved' phase cannot regress
        # either state or timestamps. No process job may appear prematurely.
        self.assertFalse(self.reg.touch(uid, status="reserved"))
        with self.reg.tx() as c:
            after = c.execute(
                "SELECT status, updated_at, last_activity_at FROM assets WHERE asset_id=?",
                (uid,),
            ).fetchone()
            self.assertEqual(tuple(before), tuple(after))
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM jobs WHERE asset_id=?", (uid,)
            ).fetchone()[0], 0)

        self.assertTrue(self.reg.mark_completed(uid, "version-456", "etag-456", "test"))
        self.assertFalse(self.reg.touch(uid, status="reserved"))
        self.assertFalse(self.reg.touch(uid, status="uploading"))
        self.assertFalse(self.reg.touch(uid))
        self.assertEqual(self.reg.get_asset(uid)["status"], "completed")
        with self.reg.tx() as c:
            self.assertEqual(c.execute(
                "SELECT kind, status FROM jobs WHERE asset_id=?", (uid,)
            ).fetchall()[0]["status"], "queued")

    def test_activity_rejects_reversal_after_bind_upload_id(self):
        asset, _ = self.reserve(self.t1, name="multipart.mov")
        uid = asset["asset_id"]
        self.assertTrue(self.reg.bind_upload_id(uid, "synthetic-multipart-id"))
        self.assertEqual(self.reg.get_asset(uid)["status"], "uploading")
        self.assertFalse(self.reg.touch(uid, status="reserved"))
        self.assertEqual(self.reg.get_asset(uid)["s3_upload_id"], "synthetic-multipart-id")
        self.assertEqual(self.reg.get_asset(uid)["status"], "uploading")
        self.assertTrue(self.reg.touch(uid, status="uploading"))
        self.assertTrue(self.reg.touch(uid))

    def test_invalid_multipart_upload_id_cannot_start_upload(self):
        asset, outcome = self.reserve(self.t1, name="invalid-multipart.mov")
        self.assertEqual(outcome, "new")
        uid = asset["asset_id"]
        with self.reg.tx() as c:
            original = c.execute(
                "SELECT status, s3_upload_id, updated_at, last_activity_at "
                "FROM assets WHERE asset_id=?", (uid,)
            ).fetchone()
            events = c.execute(
                "SELECT COUNT(*) FROM events WHERE asset_id=?", (uid,)
            ).fetchone()[0]

        invalid = (None, "", " ", "abc def", "abc\n", "\t", "\x7f",
                   "abc\x00def", b"abc", True, 7, "é", "a" * 4097)
        for value in invalid:
            with self.subTest(upload_id=repr(value)):
                with self.assertRaisesRegex(ValueError, "upload_id"):
                    self.reg.bind_upload_id(uid, value)
                with self.reg.tx() as c:
                    after = c.execute(
                        "SELECT status, s3_upload_id, updated_at, last_activity_at "
                        "FROM assets WHERE asset_id=?", (uid,)
                    ).fetchone()
                    count = c.execute(
                        "SELECT COUNT(*) FROM events WHERE asset_id=?", (uid,)
                    ).fetchone()[0]
                self.assertEqual(tuple(after), tuple(original))
                self.assertEqual(count, events)
        self.assertEqual(self.reg.get_asset(uid)["status"], "reserved")

    def test_multipart_binding_refreshes_activity_and_remains_idempotent(self):
        asset, outcome = self.reserve(self.t1, name="stale-before-bind.mov")
        self.assertEqual(outcome, "new")
        uid = asset["asset_id"]
        old = time.time() - 86400
        with self.reg.tx() as c:
            c.execute("UPDATE assets SET last_activity_at=?, updated_at=? "
                      "WHERE asset_id=?", (old, old, uid))
        self.assertIn(uid, [a["asset_id"] for a in self.reg.open_assets(3600)])

        # S3-style punctuation is permitted. Binding a multipart ID counts as
        # upload activity; a housekeeping job should no longer see it as stale.
        multipart_id = "ExampleUpload+part/2=_-"
        started = time.time()
        self.assertTrue(self.reg.bind_upload_id(uid, multipart_id))
        after = self.reg.get_asset(uid)
        self.assertEqual(after["status"], "uploading")
        self.assertEqual(after["s3_upload_id"], multipart_id)
        self.assertGreaterEqual(after["last_activity_at"], started)
        self.assertGreaterEqual(after["updated_at"], started)
        self.assertNotIn(uid, [a["asset_id"] for a in self.reg.open_assets(3600)])

        self.assertTrue(self.reg.bind_upload_id(uid, multipart_id))
        self.assertFalse(self.reg.bind_upload_id(uid, "other-upload"))
        with self.reg.tx() as c:
            bound = c.execute(
                "SELECT COUNT(*) FROM events WHERE asset_id=? AND kind='asset.multipart_bound'",
                (uid,)
            ).fetchone()[0]
        self.assertEqual(bound, 1)
        self.assertTrue(self.reg.mark_completed(uid, "synthetic-version", "synthetic-etag", "test"))
        self.assertFalse(self.reg.bind_upload_id(uid, multipart_id))
        self.assertEqual(self.reg.get_asset(uid)["status"], "completed")

    def test_resume_stays_inside_capability(self):
        a,s1=self.reserve(self.t1)
        b,s2=self.reserve(self.t1)
        self.assertEqual((s1,s2),("new","ambiguous"))
        self.assertEqual(a["asset_id"],b["asset_id"])
        self.assertTrue(self.reg.bind_upload_id(a["asset_id"],"test-upload-1"))
        self.assertFalse(self.reg.bind_upload_id(a["asset_id"],"test-upload-2"))
        inv=self.invite(self.t1)
        c,s3=self.reg.reserve_asset(inv,name="file.mov",relative_path="camera/file.mov",
            size=1234,content_type="video/quicktime",last_modified=10,
            bucket="example-bts-objects",
            key_fn=lambda uid:f"fixtures/{uid}",
            resume_asset_id=a["asset_id"])
        self.assertEqual(s3,"resume")
        self.assertEqual(c["s3_upload_id"],"test-upload-1")
        with self.assertRaises(ValueError):
            other=self.invite(self.t2)
            self.reg.reserve_asset(other,name="file.mov",relative_path="camera/file.mov",
                size=1234,content_type="video/quicktime",last_modified=10,
                bucket="example-bts-objects",key_fn=lambda uid:f"fixtures/{uid}",
                resume_asset_id=a["asset_id"])

    def test_public_completed_metadata_match_creates_distinct_original(self):
        a,s1=self.reserve(self.t1,name="same.mov")
        self.assertEqual(s1,"new")
        self.assertTrue(self.reg.mark_completed(a["asset_id"],"test-v1","etag","fixture"))
        b,s2=self.reserve(self.t1,name="same.mov")
        self.assertEqual(s2,"new")
        self.assertNotEqual(a["asset_id"],b["asset_id"])
        self.assertNotEqual(a["b2_key"],b["b2_key"])
        self.assertEqual(len(self.reg.receipt(self.p1)),2)

    def test_legacy_completed_metadata_match_retains_old_duplicate_behaviour(self):
        _,token=self.reg.create_invitation(
            "ExampleProduction","2026-10-20","unit","crew","Crew",3600,20000,10)
        a,s1=self.reserve(token,name="legacy.mov")
        self.assertEqual(s1,"new")
        self.assertTrue(self.reg.mark_completed(a["asset_id"],"test-v1","etag","fixture"))
        b,s2=self.reserve(token,name="legacy.mov")
        self.assertEqual((s2,b["asset_id"]),("duplicate",a["asset_id"]))

    def test_name_update_only_from_same_token(self):
        self.assertTrue(self.reg.update_public_name(self.t1," New name "))
        self.assertEqual(self.reg.public_session(self.p1)["uploader_name_claimed"],"New name")
        self.assertEqual(self.invite(self.t1)["contributor_label"],"New name")
        self.assertIsNone(self.reg.public_session(self.p2)["uploader_name_claimed"] if False else None)
        self.assertEqual(self.reg.public_session(self.p2)["uploader_name_claimed"],"Tāmara")

    def test_name_update_rejects_invalid_token_and_legacy(self):
        self.assertFalse(self.reg.update_public_name("made-up", "Eve"))
        lid,legacy=self.reg.create_invitation("ExampleProduction","2026-10-20","unit","crew",
                                             "ExampleContributor",3000,20000,10)
        self.assertFalse(self.reg.update_public_name(legacy,"Bogus"))
        self.assertIsNone(self.reg.public_session(lid))
        self.assertEqual(self.reg.invitation_for_token(legacy)["contributor_label"],"ExampleContributor")

    def test_expired_or_revoked_public_cannot_change_name(self):
        self.reg.revoke_invitation(self.p1)
        self.assertFalse(self.reg.update_public_name(self.t1,"later"))

    def test_empty_and_malicious_name(self):
        self.assertTrue(self.reg.update_public_name(self.t1,"  "))
        self.assertIsNone(self.reg.public_session(self.p1)["uploader_name_claimed"])
        with self.assertRaises(ValueError):
            self.reg.update_public_name(self.t1,"z"*121)
        with self.assertRaises(ValueError):
            self.reg.update_public_name(self.t1,"imposter\nhijack")
        with self.assertRaises(ValueError):
            self.reg.update_public_name(self.t1,{"name":"structured payload"})

    def test_per_session_quota_isolated(self):
        x,tk=self.reg.create_public_session(max_total_bytes=200,max_files=1)
        inv=self.invite(tk)
        a,_=self.reg.reserve_asset(inv,name="a.png",relative_path=None,size=100,
                                 content_type="image/png",last_modified=1,bucket="example-bts-objects",
                                 key_fn=lambda uid:f"test/{uid}/a.png")
        self.assertEqual(a["size_bytes"],100)
        with self.assertRaises(registry.QuotaError):
            self.reg.reserve_asset(inv,name="b.png",relative_path=None,size=101,
                                 content_type="image/png",last_modified=2,bucket="example-bts-objects",
                                 key_fn=lambda uid:f"test/{uid}/b.png")

    def test_cached_public_invitation_revocation_prevents_reservation(self):
        # A previous successful bearer-token lookup is not a future grant.
        cached = self.invite(self.t1)
        self.reg.revoke_invitation(self.p1)
        self.assertIsNone(self.invite(self.t1))
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.reg.reserve_asset(
                cached, name="stale.mov", relative_path=None, size=99,
                content_type="video/quicktime", last_modified=None,
                bucket="synthetic", key_fn=lambda uid: self.fail("allocated asset after revocation"),
            )
        self.assertEqual(self.reg.receipt(self.p1), [])

    def test_public_expiry_rejects_new_reservations_in_finish_grace(self):
        cached = self.invite(self.t1)
        with self.reg.tx() as c:
            c.execute(
                "UPDATE invitations SET expires_at=?, finish_grace_s=3600 WHERE id=?",
                (time.time() - 10, self.p1),
            )
        self.assertIsNone(self.invite(self.t1))
        self.assertIsNotNone(self.reg.invitation_for_token(self.t1, for_existing_asset=True))
        with self.assertRaisesRegex(ValueError, "Expired public invitation"):
            self.reg.reserve_asset(
                cached, name="new.mov", relative_path=None, size=99,
                content_type="video/quicktime", last_modified=None,
                bucket="synthetic", key_fn=lambda uid: self.fail("allocated a new expired asset"),
            )
        self.assertEqual(self.reg.receipt(self.p1), [])

    def test_public_expired_grace_allows_only_scoped_existing_resume(self):
        original, status = self.reserve(self.t1, name="resume.mov")
        self.assertEqual(status, "new")
        with self.reg.tx() as c:
            c.execute(
                "UPDATE invitations SET expires_at=?, finish_grace_s=3600 WHERE id=?",
                (time.time() - 10, self.p1),
            )
        stale = self.reg.invitation_for_token(self.t1, for_existing_asset=True)
        self.assertIsNotNone(stale)
        existing, kind = self.reg.reserve_asset(
            stale, name="resume.mov", relative_path="camera/resume.mov",
            size=1234, content_type="video/quicktime", last_modified=10,
            bucket="synthetic", key_fn=lambda uid: self.fail("resume allocated a new key"),
            resume_asset_id=original["asset_id"],
        )
        self.assertEqual((existing["asset_id"], kind), (original["asset_id"], "resume"))
        self.assertEqual(len(self.reg.receipt(self.p1)), 1)
        with self.assertRaisesRegex(ValueError, "Expired public invitation"):
            self.reg.reserve_asset(
                stale, name="fresh.mov", relative_path="camera/fresh.mov",
                size=1, content_type=None, last_modified=None, bucket="synthetic",
                key_fn=lambda uid: self.fail("new asset during grace"),
                create_distinct=True,
            )

    def test_public_revocation_disables_resume_in_finish_grace(self):
        original, _ = self.reserve(self.t1, name="resume.mov")
        cached = self.invite(self.t1)
        self.reg.revoke_invitation(self.p1)
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.reg.reserve_asset(
                cached, name="resume.mov", relative_path="camera/resume.mov",
                size=1234, content_type=None, last_modified=10,
                bucket="synthetic", key_fn=lambda uid: self.fail("revoked resume"),
                resume_asset_id=original["asset_id"],
            )

    def test_expired_past_grace_disallows_even_existing_resume(self):
        original, _ = self.reserve(self.t1, name="resume.mov")
        cached = self.invite(self.t1)
        with self.reg.tx() as c:
            c.execute(
                "UPDATE invitations SET expires_at=?, finish_grace_s=30 WHERE id=?",
                (time.time() - 60, self.p1),
            )
        with self.assertRaisesRegex(ValueError, "beyond upload finish grace"):
            self.reg.reserve_asset(
                cached, name="resume.mov", relative_path="camera/resume.mov",
                size=1234, content_type=None, last_modified=10,
                bucket="synthetic", key_fn=lambda uid: self.fail("late resume"),
                resume_asset_id=original["asset_id"],
            )

    def test_legacy_expired_grace_retains_open_resume_but_not_new_asset(self):
        iid, token = self.reg.create_invitation(
            "ExampleProduction", "2026-10-20", "unit", "crew", "Crew",
            3600, 10000, 5,
        )
        inv = self.invite(token)
        existing, _ = self.reserve(token, name="legacy.mov")
        with self.reg.tx() as c:
            c.execute(
                "UPDATE invitations SET expires_at=?, finish_grace_s=3600 WHERE id=?",
                (time.time() - 10, iid),
            )
        reused, kind = self.reg.reserve_asset(
            inv, name="legacy.mov", relative_path="camera/legacy.mov",
            size=1234, content_type=None, last_modified=10,
            bucket="synthetic", key_fn=lambda uid: self.fail("legacy resume key"),
        )
        self.assertEqual((reused["asset_id"], kind), (existing["asset_id"], "resume"))
        with self.assertRaisesRegex(ValueError, "Expired invitation"):
            self.reg.reserve_asset(
                inv, name="legacy-new.mov", relative_path=None, size=1,
                content_type=None, last_modified=None, bucket="synthetic",
                key_fn=lambda uid: self.fail("expired legacy new key"),
            )

    def test_cached_capability_cannot_increase_live_quota(self):
        _, token = self.reg.create_public_session(max_total_bytes=100, max_files=1)
        inv = dict(self.invite(token))
        # Simulates stale/untrusted cached quota fields. DB wins.
        inv["max_total_bytes"] = 1000000
        inv["max_files"] = 1000000
        with self.assertRaises(registry.QuotaError):
            self.reg.reserve_asset(
                inv, name="oversized.mov", relative_path=None, size=101,
                content_type=None, last_modified=None, bucket="synthetic",
                key_fn=lambda uid: self.fail("quota overrun"),
            )
        first, status = self.reg.reserve_asset(
            inv, name="first.mov", relative_path=None, size=50,
            content_type=None, last_modified=None, bucket="synthetic",
            key_fn=lambda uid: f"synthetic/{uid}",
        )
        self.assertEqual((first["size_bytes"], status), (50, "new"))
        with self.assertRaises(registry.QuotaError):
            self.reg.reserve_asset(
                inv, name="second.mov", relative_path=None, size=1,
                content_type=None, last_modified=None, bucket="synthetic",
                key_fn=lambda uid: self.fail("file count overrun"),
            )

    def test_negative_reservation_cannot_credit_public_quota(self):
        # Without input validation, -1000 bytes reduced recorded usage and
        # permitted a 1050-byte upload against a 100-byte session quota.
        _, token = self.reg.create_public_session(max_total_bytes=100, max_files=3)
        inv = self.invite(token)
        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000"}):
            with self.assertRaisesRegex(ValueError, "nonnegative integer"):
                self.reg.reserve_asset(
                    inv, name="negative.bin", relative_path=None, size=-1000,
                    content_type="application/octet-stream", last_modified=None,
                    bucket="synthetic", key_fn=lambda uid: f"synthetic/{uid}",
                )
            with self.assertRaises(registry.QuotaError):
                self.reg.reserve_asset(
                    inv, name="oversized.bin", relative_path=None, size=1050,
                    content_type="application/octet-stream", last_modified=None,
                    bucket="synthetic", key_fn=lambda uid: f"synthetic/{uid}",
                )
        self.assertEqual(self.reg.receipt(inv["id"]), [])

    def test_invalid_reservation_size_types_do_not_mutate_registry(self):
        inv = self.invite(self.t1)
        for bad in (-1, True, False, 1.5, "10", None, 1 << 63):
            with self.subTest(size=repr(bad)):
                # Fail before allocating an asset ID or calling key_fn.
                def forbidden_key(_uid):
                    self.fail("key_fn called on invalid reservation")
                with self.assertRaisesRegex(ValueError, "size must be"):
                    self.reg.reserve_asset(
                        inv, name="invalid.bin", relative_path=None, size=bad,
                        content_type=None, last_modified=None,
                        bucket="synthetic", key_fn=forbidden_key,
                    )
        self.assertEqual(self.reg.receipt(inv["id"]), [])

    def test_zero_byte_asset_remains_valid_but_ordinary_quota_holds(self):
        _, token = self.reg.create_public_session(max_total_bytes=100, max_files=3)
        inv = self.invite(token)
        with mock.patch.dict("os.environ", {"BTS_PUBLIC_BYTE_BUDGET": "1000"}):
            empty, status = self.reg.reserve_asset(
                inv, name="empty.dat", relative_path=None, size=0,
                content_type="application/octet-stream", last_modified=1,
                bucket="synthetic", key_fn=lambda uid: f"synthetic/{uid}",
            )
            self.assertEqual((empty["size_bytes"], status), (0, "new"))
            exact, status = self.reg.reserve_asset(
                inv, name="fits.dat", relative_path=None, size=100,
                content_type="application/octet-stream", last_modified=2,
                bucket="synthetic", key_fn=lambda uid: f"synthetic/{uid}",
            )
            self.assertEqual((exact["size_bytes"], status), (100, "new"))
            with self.assertRaises(registry.QuotaError):
                self.reg.reserve_asset(
                    inv, name="extra.dat", relative_path=None, size=1,
                    content_type=None, last_modified=3, bucket="synthetic",
                    key_fn=lambda uid: f"synthetic/{uid}",
                )
        self.assertEqual(len(self.reg.receipt(inv["id"])), 2)

    def test_lease_public_context_flag_and_old_invitation_stable(self):
        a,_=self.reserve(self.t1)
        self.reg.mark_completed(a["asset_id"],"v-1","etag","test")
        job=self.reg.lease_job("worker",60)
        self.assertTrue(job["invitation"]["is_public_intake"])
        self.assertIsNone(public_intake.actual_shoot_date(job["invitation"]))
        self.assertEqual(job["invitation"]["contributor_label"],"")
        self.assertTrue(self.reg.finish_job(job["job_id"],"worker",True,
                                {"sha256":"a"*64,"sha1":"b"*40}))
        lid,oldtk=self.reg.create_invitation("ExampleProduction","2026-10-19","studio","crew","Someone",
                                           3600,99999,3)
        inv=self.reg.invitation_for_token(oldtk)
        self.assertEqual(inv["shoot_date"],"2026-10-19")
        a,_=self.reserve(oldtk,name="another.mov")
        self.reg.mark_completed(a["asset_id"],"v-old","etag","test")
        job=self.reg.lease_job("worker2",60)
        self.assertFalse(job["invitation"]["is_public_intake"])
        self.assertEqual(public_intake.actual_shoot_date(job["invitation"]),"2026-10-19")

    def test_single_session_does_not_query_other_receipts(self):
        a,_=self.reserve(self.t1,name="alice.mov")
        b,_=self.reserve(self.t2,name="bob.mov")
        q1=json.dumps(self.reg.receipt(self.p1))
        self.assertIn(a["asset_id"],q1)
        self.assertNotIn(b["asset_id"],q1)
        self.assertNotIn("bob.mov",q1)

    def test_legacy_invitation_accepts_real_shoot_date(self):
        iid,tok=self.reg.create_invitation("ExampleProduction","2026-10-19","shot-one","crew","Crew",
                                           3600,10000,10)
        inv=self.reg.invitation_for_token(tok)
        self.assertEqual(public_intake.actual_shoot_date(inv),"2026-10-19")
        self.assertEqual(public_intake.provenance(inv)["intake_kind"],"invitation")
        self.assertEqual(public_intake.provenance(inv)["creator_identity"],None)

if __name__=="__main__":
    unittest.main()
