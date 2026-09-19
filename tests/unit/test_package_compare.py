"""compare-packages must support the same CI affordances as diff."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import env_compare  # loaded by conftest.py


def _pkg(name, version_id):
    return {"SubscriberPackageName": name,
            "SubscriberPackageNamespace": name.lower(),
            "SubscriberPackageVersionId": version_id}


class TestPackageCompareCI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tp = Path(self.tmp.name)
        self._orig = {n: getattr(env_compare, n)
                      for n in ("STORAGE_ROOT", "PROJECT_ROOT", "STATE_DIR", "INDEX_PATH")}
        env_compare.STORAGE_ROOT = tp
        env_compare.PROJECT_ROOT = tp
        env_compare.STATE_DIR = tp
        env_compare.INDEX_PATH = tp / "snapshots.json"
        self.left = tp / "packages-left.json"
        self.right = tp / "packages-right.json"
        self.left.write_text(json.dumps(
            {"result": [_pkg("CPQ", "04t1"), _pkg("Old", "04t2")]}))
        self.right.write_text(json.dumps(
            {"result": [_pkg("CPQ", "04t9"), _pkg("New", "04t3")]}))

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(env_compare, n, v)
        self.tmp.cleanup()

    def test_json_and_fail_on_diff(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = env_compare.compare_installed_packages(
                str(self.left), str(self.right), out=None,
                as_json=True, fail_on_diff=True)
        payload = json.loads(buf.getvalue())
        assert payload["has_diff"] is True
        assert [c["package"] for c in payload["changed"]] == ["cpq::CPQ"]
        assert payload["removed"] == ["old::Old"]
        assert payload["added"] == ["new::New"]
        assert rc == 1

    def test_identical_exits_zero(self):
        self.right.write_text(self.left.read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            rc = env_compare.compare_installed_packages(
                str(self.left), str(self.right), out=None,
                as_json=True, fail_on_diff=True)
        assert rc == 0

    def test_json_mode_skips_markdown_report(self):
        with contextlib.redirect_stdout(io.StringIO()):
            env_compare.compare_installed_packages(
                str(self.left), str(self.right), out=None, as_json=True)
        docs = Path(self.tmp.name) / "docs"
        assert not docs.exists() or not list(docs.glob("packages-*.md"))
