"""Baseline must be applied consistently: compare-matrix counts and the
comparison-history records must match what `diff` prints, not raw counts."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import env_compare  # loaded by conftest.py


class _BaselineTreesCase(unittest.TestCase):
    """Two trees differing in one file, with a baseline rule ignoring it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.left = self.tmp_path / "left"
        self.right = self.tmp_path / "right"
        self.left.mkdir()
        self.right.mkdir()
        (self.left / "a.txt").write_text("hello")
        (self.right / "a.txt").write_text("world")

        self._orig = {
            name: getattr(env_compare, name)
            for name in ("STATE_DIR", "INDEX_PATH", "COMPARISON_INDEX_PATH",
                         "STORAGE_ROOT", "PROJECT_ROOT")
        }
        env_compare.STORAGE_ROOT = self.tmp_path
        env_compare.PROJECT_ROOT = self.tmp_path
        env_compare.STATE_DIR = self.tmp_path
        env_compare.INDEX_PATH = self.tmp_path / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = self.tmp_path / "comparisons.json"

        baseline = {
            "version": 1,
            "ignore": {"types": [], "paths": ["a.txt"], "xml_elements": []},
            "options": {"strip_retrieve_defaults": False},
            "accepted": {},
        }
        (self.tmp_path / "baseline.json").write_text(json.dumps(baseline))

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(env_compare, name, value)
        self.tmp.cleanup()


class TestCompareMatrixBaseline(_BaselineTreesCase):
    def _run_matrix(self, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = env_compare.run_compare_matrix(
                [f"{self.left}:{self.right}"], as_json=True, **kwargs
            )
        return rc, json.loads(buf.getvalue())

    def test_baseline_ignored_diff_does_not_trip_fail_on_any_diff(self):
        rc, payload = self._run_matrix(fail_on_any_diff=True)
        self.assertEqual(payload["pairs"][0]["different"], 0)
        self.assertFalse(payload["pairs"][0]["has_diff"])
        self.assertEqual(rc, 0)

    def test_no_baseline_bypass_restores_raw_counts(self):
        rc, payload = self._run_matrix(fail_on_any_diff=True, use_baseline=False)
        self.assertEqual(payload["pairs"][0]["different"], 1)
        self.assertEqual(rc, 1)


class TestHistoryRecordsPostBaselineCounts(_BaselineTreesCase):
    def test_history_counts_match_printed_summary(self):
        with contextlib.redirect_stdout(io.StringIO()):
            env_compare.run_diff(
                str(self.left), str(self.right), as_json=False, fail_on_diff=False
            )
        data = json.loads(env_compare.COMPARISON_INDEX_PATH.read_text())
        rec = data["comparisons"][0]
        self.assertEqual(rec["different_count"], 0)
        self.assertEqual(rec["different_files"], [])
        self.assertTrue(rec["baseline_applied"])
