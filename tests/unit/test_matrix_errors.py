"""Phase 5 regressions: compare-matrix must surface per-pair errors in
machine-readable output and exit nonzero — errors take precedence over
fail-on-any-diff. A matrix containing a failing pair must never report
success just because no diff was found."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import env_compare  # loaded by conftest.py


class _MatrixCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
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

        self.ident_a = self.tmp_path / "ident-a"
        self.ident_b = self.tmp_path / "ident-b"
        for d in (self.ident_a, self.ident_b):
            d.mkdir()
            (d / "same.txt").write_text("identical")

        self.diff_a = self.tmp_path / "diff-a"
        self.diff_b = self.tmp_path / "diff-b"
        for d, text in ((self.diff_a, "one"), (self.diff_b, "two")):
            d.mkdir()
            (d / "f.txt").write_text(text)

        self.missing = self.tmp_path / "does-not-exist"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(env_compare, name, value)
        self.tmp.cleanup()

    def _run_json(self, pairs, fail_on_any_diff=False, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = env_compare.run_compare_matrix(
                pairs, as_json=True, fail_on_any_diff=fail_on_any_diff, **kwargs
            )
        return rc, json.loads(buf.getvalue())

    def test_failing_pair_exits_2_and_reports_error(self):
        rc, payload = self._run_json(
            [f"{self.ident_a}:{self.ident_b}", f"{self.missing}:{self.ident_b}"]
        )
        self.assertEqual(rc, 2)
        self.assertTrue(payload["has_errors"])
        ok_row, err_row = payload["pairs"]
        self.assertIsNone(ok_row["error"])
        self.assertIsNotNone(err_row["error"])

    def test_error_takes_precedence_over_fail_on_any_diff(self):
        rc, _ = self._run_json(
            [f"{self.diff_a}:{self.diff_b}", f"{self.missing}:{self.ident_b}"],
            fail_on_any_diff=True,
        )
        self.assertEqual(rc, 2)

    def test_error_without_diff_is_not_success(self):
        rc, payload = self._run_json(
            [f"{self.missing}:{self.ident_b}"], fail_on_any_diff=True
        )
        self.assertEqual(rc, 2)
        self.assertFalse(payload["any_diff"])
        self.assertTrue(payload["has_errors"])

    def test_clean_diff_with_fail_flag_still_exits_1(self):
        rc, payload = self._run_json(
            [f"{self.diff_a}:{self.diff_b}"], fail_on_any_diff=True
        )
        self.assertEqual(rc, 1)
        self.assertFalse(payload["has_errors"])

    def test_identical_pairs_exit_0(self):
        rc, payload = self._run_json([f"{self.ident_a}:{self.ident_b}"])
        self.assertEqual(rc, 0)
        self.assertFalse(payload["has_errors"])
        self.assertFalse(payload["any_diff"])

    def test_text_output_error_also_exits_2(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = env_compare.run_compare_matrix(
                [f"{self.missing}:{self.ident_b}"],
                as_json=False,
                fail_on_any_diff=False,
            )
        self.assertEqual(rc, 2)
        self.assertIn("ERROR", buf.getvalue())

    def test_malformed_pair_is_error_not_early_exit(self):
        """R8: a pair without ':' must become an error row (exit 2, valid
        JSON) instead of aborting the whole run with exit 1 and no output."""
        rc, payload = self._run_json(["not-a-pair"])
        self.assertEqual(rc, 2)
        self.assertTrue(payload["has_errors"])
        self.assertIsNotNone(payload["pairs"][0]["error"])

    def test_malformed_pair_does_not_skip_valid_pairs(self):
        """Malformed input mixed with valid pairs: the valid pair still
        produces a real result row."""
        rc, payload = self._run_json(
            ["not-a-pair", f"{self.ident_a}:{self.ident_b}"]
        )
        self.assertEqual(rc, 2)
        self.assertTrue(payload["has_errors"])
        bad, good = payload["pairs"]
        self.assertIsNotNone(bad["error"])
        self.assertIsNone(good["error"])
        self.assertFalse(good["has_diff"])

    def test_drive_letter_pair_parses_as_windows_paths(self):
        """R8: C:\\left:C:\\right must split at the pair separator, not the
        drive letter. On any platform both sides must be treated as paths."""
        from mct.comparison import _split_pair

        self.assertEqual(
            _split_pair("C:\\proj\\left:C:\\proj\\right"),
            ("C:\\proj\\left", "C:\\proj\\right"),
        )
        self.assertEqual(
            _split_pair("C:/proj/left:C:/proj/right"),
            ("C:/proj/left", "C:/proj/right"),
        )
        # POSIX paths still split at the first colon.
        self.assertEqual(_split_pair("/a/b:/c/d"), ("/a/b", "/c/d"))
        self.assertEqual(_split_pair("snap-a:snap-b"), ("snap-a", "snap-b"))
        # No separator → malformed.
        self.assertIsNone(_split_pair("not-a-pair"))
        self.assertIsNone(_split_pair("C:\\proj\\onlyleft"))
