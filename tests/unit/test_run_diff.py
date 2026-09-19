"""
Unit tests for run_diff() in scripts/env-compare.py.
Uses real temp dirs with real files.
"""
import json
import tempfile
import unittest
from pathlib import Path

import env_compare  # loaded by conftest.py


class TestRunDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        # Two directories with matching files
        self.left = self.tmp_path / "left"
        self.right = self.tmp_path / "right"
        self.left.mkdir()
        self.right.mkdir()
        # Set STATE_DIR etc. like other tests do
        self._orig_state_dir = env_compare.STATE_DIR
        self._orig_index_path = env_compare.INDEX_PATH
        self._orig_comparison_index_path = env_compare.COMPARISON_INDEX_PATH
        env_compare.STATE_DIR = self.tmp_path / ".mct"
        env_compare.INDEX_PATH = env_compare.STATE_DIR / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = env_compare.STATE_DIR / "comparisons.json"
        env_compare.STATE_DIR.mkdir()
        # Point STORAGE_ROOT and PROJECT_ROOT at the tmp dir so
        # resolve_snapshot_or_path can resolve the absolute left/right paths.
        self._orig_storage_root = env_compare.STORAGE_ROOT
        self._orig_project_root = env_compare.PROJECT_ROOT
        env_compare.STORAGE_ROOT = self.tmp_path
        env_compare.PROJECT_ROOT = self.tmp_path

    def tearDown(self):
        env_compare.STATE_DIR = self._orig_state_dir
        env_compare.INDEX_PATH = self._orig_index_path
        env_compare.COMPARISON_INDEX_PATH = self._orig_comparison_index_path
        env_compare.STORAGE_ROOT = self._orig_storage_root
        env_compare.PROJECT_ROOT = self._orig_project_root
        self.tmp.cleanup()

    def _write(self, directory, filename, content):
        (directory / filename).write_text(content)

    def test_returns_0_on_identical(self):
        """Returns 0 when left and right are identical."""
        self._write(self.left, "a.txt", "hello")
        self._write(self.right, "a.txt", "hello")
        result = env_compare.run_diff(str(self.left), str(self.right), as_json=False, fail_on_diff=False)
        self.assertEqual(result, 0)

    def test_returns_0_without_fail_on_diff_even_when_different(self):
        """Returns 0 without --fail-on-diff even when files differ."""
        self._write(self.left, "a.txt", "hello")
        self._write(self.right, "a.txt", "world")
        result = env_compare.run_diff(str(self.left), str(self.right), as_json=False, fail_on_diff=False)
        self.assertEqual(result, 0)

    def test_returns_1_with_fail_on_diff_when_different(self):
        """Returns 1 with --fail-on-diff when files differ."""
        self._write(self.left, "a.txt", "hello")
        self._write(self.right, "a.txt", "world")
        result = env_compare.run_diff(str(self.left), str(self.right), as_json=False, fail_on_diff=True)
        self.assertEqual(result, 1)

    def test_returns_0_with_fail_on_diff_when_identical(self):
        """Returns 0 with --fail-on-diff when files are identical."""
        self._write(self.left, "a.txt", "hello")
        self._write(self.right, "a.txt", "hello")
        result = env_compare.run_diff(str(self.left), str(self.right), as_json=True, fail_on_diff=True)
        self.assertEqual(result, 0)

    def test_json_output_parseable(self):
        """JSON output contains expected keys."""
        self._write(self.left, "a.txt", "hello")
        self._write(self.right, "a.txt", "world")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            env_compare.run_diff(str(self.left), str(self.right), as_json=True, fail_on_diff=False)
        output = json.loads(buf.getvalue())
        self.assertIn("has_diff", output)
        self.assertIn("different_count", output)
        self.assertTrue(output["has_diff"])

    def test_human_output_has_diff_summary(self):
        """Human output contains 'Diff summary:' string."""
        self._write(self.left, "a.txt", "hello")
        self._write(self.right, "a.txt", "world")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            env_compare.run_diff(str(self.left), str(self.right), as_json=False, fail_on_diff=False)
        self.assertIn("Diff summary:", buf.getvalue())

    def test_records_to_history(self):
        """run_diff records comparison to comparisons.json."""
        self._write(self.left, "a.txt", "hello")
        self._write(self.right, "a.txt", "hello")
        env_compare.run_diff(str(self.left), str(self.right), as_json=False, fail_on_diff=False)
        data = env_compare.load_comparison_index()
        self.assertEqual(len(data["comparisons"]), 1)


if __name__ == "__main__":
    unittest.main()


class TestNormalizationCapSurfaced(unittest.TestCase):
    """XML files above the normalization size cap are compared raw; the CLI
    JSON must flag them (the UI already shows a badge)."""

    def setUp(self):
        import mct.xml_normalizer as xn
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.left = self.tmp_path / "left"
        self.right = self.tmp_path / "right"
        (self.left / "objects").mkdir(parents=True)
        (self.right / "objects").mkdir(parents=True)
        self._saved = {
            name: getattr(env_compare, name)
            for name in ("STATE_DIR", "INDEX_PATH", "COMPARISON_INDEX_PATH",
                         "STORAGE_ROOT", "PROJECT_ROOT")
        }
        env_compare.STORAGE_ROOT = self.tmp_path
        env_compare.PROJECT_ROOT = self.tmp_path
        env_compare.STATE_DIR = self.tmp_path / ".mct"
        env_compare.INDEX_PATH = env_compare.STATE_DIR / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = env_compare.STATE_DIR / "comparisons.json"
        env_compare.STATE_DIR.mkdir()
        self._orig_cap = xn._MAX_XML_BYTES
        xn._MAX_XML_BYTES = 16
        self._xn = xn

    def tearDown(self):
        self._xn._MAX_XML_BYTES = self._orig_cap
        for name, value in self._saved.items():
            setattr(env_compare, name, value)
        self.tmp.cleanup()

    def test_capped_files_listed_in_json(self):
        body = '<CustomObject xmlns="x"><a>1</a><b>2</b></CustomObject>'
        flipped = '<CustomObject xmlns="x"><b>2</b><a>1</a></CustomObject>'
        (self.left / "objects" / "Big.xml").write_text(body)
        (self.right / "objects" / "Big.xml").write_text(flipped)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            env_compare.run_diff(str(self.left), str(self.right),
                                 as_json=True, fail_on_diff=False)
        payload = json.loads(buf.getvalue())
        assert payload["normalization_capped_files"] == ["objects/Big.xml"]
        assert "objects/Big.xml" in payload["different_files"]
