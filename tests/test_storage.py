import os
import tempfile
import time
import unittest

from otop import storage


class DirectorySizeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        nested = os.path.join(self.directory, "a", "b")
        os.makedirs(nested)
        for path, size in ((os.path.join(self.directory, "one"), 100),
                           (os.path.join(nested, "two"), 250)):
            with open(path, "wb") as handle:
                handle.write(b"x" * size)

    def tearDown(self):
        for root, dirs, files in os.walk(self.directory, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.directory)

    def wait_for(self, cache, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = cache.snapshot()
            if not snapshot["scanning"] and snapshot["age"] is not None:
                return snapshot
            time.sleep(0.02)
        self.fail("directory walk did not finish")
        return None

    def test_walk_counts_bytes_and_files_recursively(self):
        cache = storage.DirectorySize(self.directory, interval=300)
        cache.refresh()
        snapshot = self.wait_for(cache)
        self.assertEqual(snapshot["bytes"], 350)
        self.assertEqual(snapshot["files"], 2)
        self.assertIsNone(snapshot["error"])

    def test_result_is_cached_until_the_interval_elapses(self):
        cache = storage.DirectorySize(self.directory, interval=3600)
        cache.refresh()
        first = self.wait_for(cache)
        with open(os.path.join(self.directory, "three"), "wb") as handle:
            handle.write(b"y" * 1000)
        cache.refresh()                                 # too soon: must not rescan
        self.assertEqual(cache.snapshot()["bytes"], first["bytes"])
        cache.refresh(force=True)                       # 'r' in the UI
        self.assertEqual(self.wait_for(cache)["bytes"], 1350)

    def test_missing_path_reports_an_error_instead_of_raising(self):
        cache = storage.DirectorySize("/nope/not/here", interval=1)
        cache.refresh()
        snapshot = self.wait_for(cache)
        self.assertIsNone(snapshot["bytes"])
        self.assertIn("not found", snapshot["error"])

    def test_no_path_configured(self):
        cache = storage.DirectorySize(None, interval=1)
        cache.refresh()
        snapshot = self.wait_for(cache)
        self.assertIn("no filestore path", snapshot["error"])


class DiskUsageTest(unittest.TestCase):
    def test_root_filesystem(self):
        usage = storage.disk_usage("/")
        self.assertIsNone(usage["error"])
        self.assertGreater(usage["total"], 0)
        self.assertLessEqual(usage["used"], usage["total"])
        self.assertTrue(0 <= usage["percent"] <= 100)

    def test_missing_path_is_reported(self):
        usage = storage.disk_usage("/nope/not/here")
        self.assertIsNotNone(usage["error"])
        self.assertIsNone(usage["total"])


class ComposeTest(unittest.TestCase):
    disk = {"total": 1000, "used": 600, "free": 400, "percent": 60.0, "device": 42}

    def test_other_is_the_remainder(self):
        result = storage.compose(self.disk, [
            {"label": "LIVE database", "kind": "database", "bytes": 200, "path": None},
            {"label": "LIVE filestore", "kind": "filestore", "bytes": 300, "path": None},
        ])
        self.assertEqual(result["accounted"], 500)
        self.assertEqual(result["other"], 100)

    def test_other_never_goes_negative(self):
        result = storage.compose(self.disk, [
            {"label": "db", "kind": "database", "bytes": 900, "path": None}])
        self.assertEqual(result["other"], 0)

    def test_unknown_sizes_are_skipped(self):
        result = storage.compose(self.disk, [
            {"label": "db", "kind": "database", "bytes": None, "path": None}])
        self.assertEqual(result["accounted"], 0)
        self.assertEqual(result["other"], 600)

    def test_entry_on_another_filesystem_is_excluded_from_other(self):
        result = storage.compose(self.disk, [
            {"label": "filestore", "kind": "filestore", "bytes": 300, "path": "/"},
        ])
        row = result["rows"][0]
        self.assertFalse(row["same_fs"])                # device 42 is not the real one
        self.assertEqual(result["other"], 600)          # not subtracted

    def test_unknown_disk_usage_gives_unknown_other(self):
        result = storage.compose({"total": None, "used": None, "device": None}, [])
        self.assertIsNone(result["other"])


if __name__ == "__main__":
    unittest.main()
