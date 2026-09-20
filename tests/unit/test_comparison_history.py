"""
Unit tests for comparison history functions in scripts/env-compare.py:
  - load_comparison_index / save_comparison_index
  - _snapshot_id_for_path
  - record_comparison
"""
import tempfile
import unittest
from pathlib import Path

import env_compare  # loaded by conftest.py
from retrieved_folder_compare import TreeCompareResult


# ---------------------------------------------------------------------------
# TestLoadSaveComparisonIndex
# ---------------------------------------------------------------------------

class TestLoadSaveComparisonIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_state_dir = env_compare.STATE_DIR
        self._orig_index_path = env_compare.INDEX_PATH
        self._orig_comparison_index_path = env_compare.COMPARISON_INDEX_PATH
        env_compare.STATE_DIR = self.tmp_path / ".mct"
        env_compare.INDEX_PATH = env_compare.STATE_DIR / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = env_compare.STATE_DIR / "comparisons.json"
        env_compare.STATE_DIR.mkdir()

    def tearDown(self):
        env_compare.STATE_DIR = self._orig_state_dir
        env_compare.INDEX_PATH = self._orig_index_path
        env_compare.COMPARISON_INDEX_PATH = self._orig_comparison_index_path
        self.tmp.cleanup()

    def test_empty_default(self):
        """load_comparison_index returns default dict when file missing."""
        data = env_compare.load_comparison_index()
        self.assertEqual(data, {"version": 1, "comparisons": []})

    def test_roundtrip(self):
        """save then load roundtrips data correctly."""
        original = {"version": 1, "comparisons": [{"id": "cmp-test", "left_path": "a", "right_path": "b"}]}
        env_compare.save_comparison_index(original)
        loaded = env_compare.load_comparison_index()
        self.assertEqual(loaded, original)

    def test_file_created(self):
        """save_comparison_index creates the file."""
        env_compare.save_comparison_index({"version": 1, "comparisons": []})
        self.assertTrue(env_compare.COMPARISON_INDEX_PATH.exists())


# ---------------------------------------------------------------------------
# TestSnapshotIdForPath
# ---------------------------------------------------------------------------

class TestSnapshotIdForPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_state_dir = env_compare.STATE_DIR
        self._orig_index_path = env_compare.INDEX_PATH
        self._orig_comparison_index_path = env_compare.COMPARISON_INDEX_PATH
        env_compare.STATE_DIR = self.tmp_path / ".mct"
        env_compare.INDEX_PATH = env_compare.STATE_DIR / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = env_compare.STATE_DIR / "comparisons.json"
        env_compare.STATE_DIR.mkdir()

    def tearDown(self):
        env_compare.STATE_DIR = self._orig_state_dir
        env_compare.INDEX_PATH = self._orig_index_path
        env_compare.COMPARISON_INDEX_PATH = self._orig_comparison_index_path
        self.tmp.cleanup()

    def test_returns_none_when_empty(self):
        """Returns None when no snapshots exist."""
        result = env_compare._snapshot_id_for_path("some/path")
        self.assertIsNone(result)

    def test_finds_matching_snapshot(self):
        """Returns snapshot id when path matches."""
        env_compare.save_index({"version": 1, "snapshots": [
            {"id": "snap-001", "path": "force-app/main/default", "label": "test",
             "type": "branch", "created_at": "20260101-000000+0000", "files": 0,
             "branch_or_org": "main", "packages": []}
        ]})
        result = env_compare._snapshot_id_for_path("force-app/main/default")
        self.assertEqual(result, "snap-001")

    def test_returns_none_on_miss(self):
        """Returns None when path does not match any snapshot."""
        env_compare.save_index({"version": 1, "snapshots": [
            {"id": "snap-001", "path": "other/path", "label": "test",
             "type": "branch", "created_at": "20260101-000000+0000", "files": 0,
             "branch_or_org": "main", "packages": []}
        ]})
        result = env_compare._snapshot_id_for_path("force-app/main/default")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# TestRecordComparison
# ---------------------------------------------------------------------------

class TestRecordComparison(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_state_dir = env_compare.STATE_DIR
        self._orig_index_path = env_compare.INDEX_PATH
        self._orig_comparison_index_path = env_compare.COMPARISON_INDEX_PATH
        env_compare.STATE_DIR = self.tmp_path / ".mct"
        env_compare.INDEX_PATH = env_compare.STATE_DIR / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = env_compare.STATE_DIR / "comparisons.json"
        env_compare.STATE_DIR.mkdir()

    def tearDown(self):
        env_compare.STATE_DIR = self._orig_state_dir
        env_compare.INDEX_PATH = self._orig_index_path
        env_compare.COMPARISON_INDEX_PATH = self._orig_comparison_index_path
        self.tmp.cleanup()

    def _make_result(self, differ=0, only_left=0, only_right=0, identical=5):
        """Build a minimal TreeCompareResult for testing."""
        left_ix = {}
        right_ix = {}
        differ_pairs = []
        only_left_keys = []
        only_right_keys = []
        for i in range(identical):
            key = f"same_{i}"
            p = Path(f"/tmp/left/file_{i}.txt")
            left_ix[key] = (p, f"file_{i}.txt")
            right_ix[key] = (p, f"file_{i}.txt")
        for i in range(differ):
            key = f"diff_{i}"
            lp = Path(f"/tmp/left/diff_{i}.txt")
            rp = Path(f"/tmp/right/diff_{i}.txt")
            left_ix[key] = (lp, f"diff_{i}.txt")
            right_ix[key] = (rp, f"diff_{i}.txt")
            differ_pairs.append((lp, rp, f"diff_{i}.txt", f"diff_{i}.txt"))
        for i in range(only_left):
            key = f"onlyleft_{i}"
            p = Path(f"/tmp/left/only_{i}.txt")
            left_ix[key] = (p, f"only_{i}.txt")
            only_left_keys.append(key)
        for i in range(only_right):
            key = f"onlyright_{i}"
            p = Path(f"/tmp/right/only_{i}.txt")
            right_ix[key] = (p, f"only_{i}.txt")
            only_right_keys.append(key)
        return TreeCompareResult(
            left_ix=left_ix, right_ix=right_ix,
            only_left_keys=tuple(only_left_keys),
            only_right_keys=tuple(only_right_keys),
            identical_count=identical,
            differ_pairs=tuple(differ_pairs),
        )

    def test_creates_entry(self):
        """record_comparison creates an entry in comparisons.json."""
        result = self._make_result()
        rec = env_compare.record_comparison("left/path", "right/path", result)
        self.assertIsNotNone(rec)
        data = env_compare.load_comparison_index()
        self.assertEqual(len(data["comparisons"]), 1)

    def test_appends_multiple(self):
        """record_comparison appends to existing entries."""
        result = self._make_result()
        env_compare.record_comparison("left/a", "right/a", result)
        env_compare.record_comparison("left/b", "right/b", result)
        data = env_compare.load_comparison_index()
        self.assertEqual(len(data["comparisons"]), 2)

    def test_keeps_newest_max_records(self):
        """record_comparison trims history to the newest COMPARISON_HISTORY_MAX_RECORDS."""
        from mct.index import COMPARISON_HISTORY_MAX_RECORDS

        result = self._make_result()
        old_rows = []
        for i in range(COMPARISON_HISTORY_MAX_RECORDS):
            old_rows.append(
                {
                    "id": f"cmp-old-{i}",
                    "created_at": f"20260101-{i:06d}+0000",
                    "left_path": f"left/old/{i}",
                    "right_path": f"right/old/{i}",
                    "left_snapshot_id": None,
                    "right_snapshot_id": None,
                    "different_count": 0,
                    "only_left_count": 0,
                    "only_right_count": 0,
                    "identical_count": 1,
                    "total_left": 1,
                    "total_right": 1,
                    "different_files": [],
                    "only_left_files": [],
                    "only_right_files": [],
                }
            )
        env_compare.save_comparison_index({"version": 1, "comparisons": old_rows})

        rec = env_compare.record_comparison("left/new", "right/new", result)
        self.assertIsNotNone(rec)

        data = env_compare.load_comparison_index()
        self.assertEqual(len(data["comparisons"]), COMPARISON_HISTORY_MAX_RECORDS)
        ids = [row.get("id") for row in data["comparisons"]]
        self.assertIn(rec.comparison_id, ids)
        self.assertNotIn("cmp-old-0", ids)

    def test_counts_match_result(self):
        """record_comparison stores counts matching the TreeCompareResult."""
        result = self._make_result(differ=2, only_left=1, only_right=3, identical=10)
        rec = env_compare.record_comparison("left/path", "right/path", result)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.different_count, 2)
        self.assertEqual(rec.only_left_count, 1)
        self.assertEqual(rec.only_right_count, 3)
        self.assertEqual(rec.identical_count, 10)

    def test_does_not_raise_on_ioerror(self):
        """record_comparison never raises even when the index path is unwritable."""
        # /nonexistent is only guaranteed unwritable on POSIX — on Windows CI
        # it resolves to C:\nonexistent which admin runners can create. A path
        # under a regular file is unwritable on every platform.
        blocker = self.tmp_path / "blocker"
        blocker.write_text("not a directory")
        env_compare.COMPARISON_INDEX_PATH = blocker / "comparisons.json"
        result = self._make_result()
        # Should not raise
        rec = env_compare.record_comparison("left/path", "right/path", result)
        # Returns None on failure
        self.assertIsNone(rec)


if __name__ == "__main__":
    unittest.main()
