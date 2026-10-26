"""Offline BTS object-key path-boundary acceptance.

No B2 calls, filesystem writes, real production identifiers or network.
Normal and unknown-date layouts retain the published six-part key grammar.
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bts_ingest import naming

ASSET_ID = "8c22d72b-6599-44d0-8d65-a9f12f39b74e"


class ObjectKeySafetyTest(unittest.TestCase):
    def make_key(self, shoot_date="2026-10-27", asset_id=ASSET_ID):
        return naming.original_key(
            "LifeR", shoot_date, "BTS Camera 2", "Example Contributor",
            asset_id, "camera/original take.mov"
        )

    def test_normal_key_and_derived_prefix_remain_stable(self):
        key = self.make_key()
        self.assertEqual(
            key,
            "LifeR/2026/2026-10-27_BTS-Camera-2/"
            "Example-Contributor/" + ASSET_ID + "/original-take.mov",
        )
        self.assertEqual(
            naming.derived_prefix(key),
            "LifeR/2026/2026-10-27_BTS-Camera-2/" + ASSET_ID,
        )

    def test_unclassified_public_date_sentinel_is_preserved(self):
        key = naming.original_key(
            "Unclassified", "0000-00-00", "intake-pending",
            "session-example", ASSET_ID, "Mobile/clip_0001.mp4",
        )
        self.assertEqual(
            key, "Unclassified/0000/0000-00-00_intake-pending/"
                 "session-example/" + ASSET_ID + "/clip_0001.mp4"
        )
        self.assertEqual(
            naming.derived_prefix(key),
            "Unclassified/0000/0000-00-00_intake-pending/" + ASSET_ID,
        )

    def test_date_and_path_injection_rejected_before_key_creation(self):
        invalid_dates = (
            "2026/../../private", "../2026-10-27", "/2026-10-27",
            "2026-10-27/../../private", "2026-10-27\\private",
            "2026-02-30", "2026-13-01", "2026-1-1",
            "2026-10-27\nmore", "0000-01-01", None, 20261027, "",
        )
        for value in invalid_dates:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                self.make_key(shoot_date=value)

    def test_asset_identifier_cannot_inject_another_directory(self):
        invalid_ids = (
            "../other", "../../outside", "/absolute", "a/b", "a\\b",
            ".", "..", "a\nb", "a:b", "", "x" * 129, None, 17,
        )
        for value in invalid_ids:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                self.make_key(asset_id=value)

    def test_derived_prefix_rejects_malformed_and_traversal_keys(self):
        valid = self.make_key()
        mutated = (
            valid + "/extra",
            valid.rsplit("/", 1)[0],
            valid.replace("/2026/", "/../../", 1),
            valid.replace("/" + ASSET_ID + "/", "/../", 1),
            valid.replace("/2026/", "/2025/", 1),
            valid.replace("2026-10-27", "2026-02-30"),
            valid.replace("Example-Contributor", "bad\\folder"),
            valid.replace("original-take.mov", "a\nb.mov"),
            valid.replace("original-take.mov", ".."),
            valid.replace("original-take.mov", "files/inside.mov"),
            "",
            None,
            3,
        )
        for value in mutated:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                naming.derived_prefix(value)

    def test_original_key_continues_sanitizing_filename_and_labels(self):
        key = naming.original_key(
            "LifeR/../../../outside", "2026-10-27",
            "unit/../second", "contributor\\home", ASSET_ID,
            "..\\..\\private\\clip.mov",
        )
        self.assertEqual(len(key.split("/")), 6)
        self.assertNotIn("/../", key)
        self.assertTrue(key.endswith("/" + ASSET_ID + "/clip.mov"))
        self.assertEqual(len(naming.derived_prefix(key).split("/")), 4)


if __name__ == "__main__":
    unittest.main()
