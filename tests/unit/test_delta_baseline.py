"""Phase 3 regressions: every action path (manifest/ZIP export, selected
export, reverse-sync, validate-deploy) must honor the same baseline
classification as the summary — including pair-scoped acceptances.

`sf project generate manifest` is mocked at the mct.safety.run boundary;
comparison and classification run for real on temporary trees.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_spec = importlib.util.spec_from_file_location(
    "serve_diff_ui", _SCRIPTS_DIR / "serve-diff-ui.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

DiffUIHandler = _mod.DiffUIHandler

import mct.baseline as _bl
import mct.config as _cfg
import mct.delta as _delta


PAIR = "branch:main↔org:prod"


def _trees(tmp_path: Path):
    """left/right trees: one changed class, one left-only, one right-only."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "classes").mkdir(parents=True, exist_ok=True)
    (left / "classes" / "Changed.cls").write_text("public class Changed { Integer v = 1; }\n")
    (right / "classes" / "Changed.cls").write_text("public class Changed { Integer v = 2; }\n")
    (left / "classes" / "LeftOnly.cls").write_text("public class LeftOnly {}\n")
    (right / "classes" / "RightOnly.cls").write_text("public class RightOnly {}\n")
    return left, right


class _FakeHandler:
    _collect_delta_files = DiffUIHandler._collect_delta_files


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """Handler with real left/right roots and a temp baseline file attached."""
    left, right = _trees(tmp_path)
    baseline_file = tmp_path / "baseline.json"
    monkeypatch.setattr(_mod, "BASELINE_FILE", baseline_file)
    monkeypatch.setattr(_mod, "PAIR_KEY", PAIR)
    _mod.get_comparison.cache_clear()
    h = _FakeHandler()
    h.left_root = left
    h.right_root = right
    h.baseline_file = baseline_file
    return h


def _displays(files):
    return sorted(d for _, d in files)


def _accept(handler, disp, pair_key=PAIR):
    """Accept *disp* against the real files, writing handler.baseline_file."""
    lp = handler.left_root / disp
    rp = handler.right_root / disp
    return _bl.accept_diff(
        disp,
        lp if lp.is_file() else None,
        rp if rp.is_file() else None,
        baseline_file=handler.baseline_file,
        pair_key=pair_key,
    )


class TestCollectDeltaBaseline:
    def test_accepted_changed_file_excluded_from_deploy(self, handler):
        """RED: accepting a changed file leaves it in the default export today."""
        _accept(handler, "classes/Changed.cls")
        deploy, destroy = handler._collect_delta_files()
        assert _displays(deploy) == ["classes/LeftOnly.cls"]
        assert _displays(destroy) == ["classes/RightOnly.cls"]

    def test_ignored_left_only_excluded_from_deploy(self, handler):
        data = _bl.default_baseline()
        data["ignore"]["paths"] = ["classes/LeftOnly.cls"]
        _bl.save_baseline(data, handler.baseline_file)
        deploy, _ = handler._collect_delta_files()
        assert "classes/LeftOnly.cls" not in _displays(deploy)
        assert "classes/Changed.cls" in _displays(deploy)

    def test_ignored_right_only_excluded_from_destroy(self, handler):
        data = _bl.default_baseline()
        data["ignore"]["paths"] = ["classes/RightOnly.cls"]
        _bl.save_baseline(data, handler.baseline_file)
        _, destroy = handler._collect_delta_files()
        assert destroy == []

    def test_accepted_right_only_excluded_from_destroy(self, handler):
        _accept(handler, "classes/RightOnly.cls")
        _, destroy = handler._collect_delta_files()
        assert destroy == []

    def test_stale_acceptance_returns_to_active(self, handler):
        _accept(handler, "classes/Changed.cls")
        # Content moved on since acceptance → resurfaces as active drift.
        (handler.left_root / "classes" / "Changed.cls").write_text(
            "public class Changed { Integer v = 99; }\n"
        )
        _mod.get_comparison.cache_clear()
        deploy, _ = handler._collect_delta_files()
        assert "classes/Changed.cls" in _displays(deploy)

    def test_selected_paths_cannot_bypass_baseline(self, handler):
        """A crafted request naming an accepted/ignored path must not smuggle
        it into the export; selection only narrows the active set."""
        _accept(handler, "classes/Changed.cls")
        deploy, _ = handler._collect_delta_files(
            selected_paths=["classes/Changed.cls", "classes/LeftOnly.cls"]
        )
        assert _displays(deploy) == ["classes/LeftOnly.cls"]

    def test_pair_scoped_acceptance_only_applies_to_same_pair(self, handler):
        """Accepted under a different pair stays active for this pair."""
        _accept(handler, "classes/Changed.cls", pair_key="branch:release↔org:uat")
        deploy, _ = handler._collect_delta_files()
        assert "classes/Changed.cls" in _displays(deploy)

    def test_legacy_pairless_acceptance_still_global(self, handler):
        _accept(handler, "classes/Changed.cls", pair_key=None)
        deploy, _ = handler._collect_delta_files()
        assert "classes/Changed.cls" not in _displays(deploy)

    def test_no_baseline_everything_active(self, tmp_path, monkeypatch):
        left, right = _trees(tmp_path)
        monkeypatch.setattr(_mod, "BASELINE_FILE", None)
        monkeypatch.setattr(_mod, "PAIR_KEY", None)
        _mod.get_comparison.cache_clear()
        h = _FakeHandler()
        h.left_root, h.right_root = left, right
        deploy, destroy = h._collect_delta_files()
        assert _displays(deploy) == ["classes/Changed.cls", "classes/LeftOnly.cls"]
        assert _displays(destroy) == ["classes/RightOnly.cls"]


class TestReverseSyncPairScope:
    """retrieve-delta selection must apply the pair scope of the comparison."""

    def _result(self, tmp_path):
        from mct.retrieved_folder_compare import compare_trees

        left, right = _trees(tmp_path)
        return compare_trees(left, right)

    def test_pair_scoped_acceptance_excluded_from_reverse_sync(self, tmp_path):
        left, right = _trees(tmp_path)
        baseline_file = tmp_path / "baseline.json"
        _bl.accept_diff(
            "classes/Changed.cls",
            left / "classes" / "Changed.cls",
            right / "classes" / "Changed.cls",
            baseline_file=baseline_file,
            pair_key=PAIR,
        )
        result = self._result(tmp_path)
        files = _delta.collect_reverse_sync_files(
            result, _bl.load_baseline(baseline_file), pair_key=PAIR
        )
        assert _displays(files) == ["classes/RightOnly.cls"]

    def test_other_pair_acceptance_does_not_apply(self, tmp_path):
        left, right = _trees(tmp_path)
        baseline_file = tmp_path / "baseline.json"
        _bl.accept_diff(
            "classes/Changed.cls",
            left / "classes" / "Changed.cls",
            right / "classes" / "Changed.cls",
            baseline_file=baseline_file,
            pair_key="branch:release↔org:uat",
        )
        result = self._result(tmp_path)
        files = _delta.collect_reverse_sync_files(
            result, _bl.load_baseline(baseline_file), pair_key=PAIR
        )
        assert "classes/Changed.cls" in _displays(files)


class TestRetrieveDeltaPairScope(unittest.TestCase):
    """End-to-end: retrieve_delta must derive pair_key from snapshot provenance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig = {
            name: getattr(_cfg, name)
            for name in ("STORAGE_ROOT", "STATE_DIR", "INDEX_PATH",
                         "COMPARISON_INDEX_PATH", "PROJECT_ROOT", "MANIFEST_DIR")
        }
        _cfg.PROJECT_ROOT = self.tmp_path
        _cfg.STORAGE_ROOT = self.tmp_path / "store"
        _cfg.STATE_DIR = _cfg.STORAGE_ROOT
        _cfg.INDEX_PATH = _cfg.STORAGE_ROOT / "snapshots.json"
        _cfg.COMPARISON_INDEX_PATH = _cfg.STORAGE_ROOT / "comparisons.json"
        _cfg.MANIFEST_DIR = self.tmp_path / "manifest"
        _cfg.STORAGE_ROOT.mkdir(parents=True)

        # Snapshot trees under the store + index rows carrying provenance.
        self.left, self.right = _trees(_cfg.STORAGE_ROOT)
        index = {"version": 1, "snapshots": [
            {"id": "snap-branch", "type": "branch", "created_at": "1", "path": "left", "branch": "main"},
            {"id": "snap-org", "type": "org_retrieve", "created_at": "2", "path": "right", "org_alias": "prod"},
        ]}
        _cfg.INDEX_PATH.write_text(json.dumps(index))
        self.baseline_file = _cfg.STORAGE_ROOT / "baseline.json"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(_cfg, name, value)
        self.tmp.cleanup()

    def _run(self):
        import mct.snapshot as _snapshot

        return _snapshot.retrieve_delta(
            "prod", "snap-branch", "snap-org", timeout=5, api_version="60.0"
        )

    def test_pair_scoped_acceptance_excludes_from_delta(self):
        """Accepted for THIS pair → nothing left to retrieve; no subprocess runs."""
        _bl.accept_diff(
            "classes/Changed.cls",
            self.left / "classes" / "Changed.cls",
            self.right / "classes" / "Changed.cls",
            baseline_file=self.baseline_file,
            pair_key=PAIR,
        )
        # RightOnly.cls still drifts org-side, so block resolution to see what
        # the selection contains.
        captured = {}

        def fake_resolve(paths, batch_size=_delta.MANIFEST_BATCH_SIZE):
            captured["paths"] = [str(p) for p in paths]
            return {"ApexClass": {"RightOnly"}}, None

        import mct.snapshot as _snapshot
        import unittest.mock as mock

        retr_out = _cfg.STORAGE_ROOT / "retr-out"
        retr_out.mkdir()
        with mock.patch.object(_delta, "resolve_components_via_sf", fake_resolve), \
             mock.patch.object(_snapshot, "retrieve_with_manifest") as retr, \
             mock.patch.object(_snapshot, "register_snapshot", side_effect=lambda s: s):
            retr.side_effect = lambda manifest, org, name, timeout, api_version, collected=None: retr_out
            snap = self._run()
        assert not any("Changed.cls" in p for p in captured["paths"])
        assert snap is not None

    def test_no_drift_after_full_acceptance_returns_none(self):
        _bl.accept_diff(
            "classes/Changed.cls",
            self.left / "classes" / "Changed.cls",
            self.right / "classes" / "Changed.cls",
            baseline_file=self.baseline_file,
            pair_key=PAIR,
        )
        _bl.accept_diff(
            "classes/RightOnly.cls",
            None,
            self.right / "classes" / "RightOnly.cls",
            baseline_file=self.baseline_file,
            pair_key=PAIR,
        )
        _bl.accept_diff(
            "classes/LeftOnly.cls",
            self.left / "classes" / "LeftOnly.cls",
            None,
            baseline_file=self.baseline_file,
            pair_key=PAIR,
        )
        assert self._run() is None


class TestValidateDeployPairScope(unittest.TestCase):
    """validate-deploy selection must derive pair_key from snapshot provenance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig = {
            name: getattr(_cfg, name)
            for name in ("STORAGE_ROOT", "STATE_DIR", "INDEX_PATH",
                         "COMPARISON_INDEX_PATH", "PROJECT_ROOT", "MANIFEST_DIR")
        }
        _cfg.PROJECT_ROOT = self.tmp_path
        _cfg.STORAGE_ROOT = self.tmp_path / "store"
        _cfg.STATE_DIR = _cfg.STORAGE_ROOT
        _cfg.INDEX_PATH = _cfg.STORAGE_ROOT / "snapshots.json"
        _cfg.COMPARISON_INDEX_PATH = _cfg.STORAGE_ROOT / "comparisons.json"
        _cfg.MANIFEST_DIR = self.tmp_path / "manifest"
        _cfg.STORAGE_ROOT.mkdir(parents=True)

        self.left, self.right = _trees(_cfg.STORAGE_ROOT)
        index = {"version": 1, "snapshots": [
            {"id": "snap-branch", "type": "branch", "created_at": "1", "path": "left", "branch": "main"},
            {"id": "snap-org", "type": "org_retrieve", "created_at": "2", "path": "right", "org_alias": "prod"},
        ]}
        _cfg.INDEX_PATH.write_text(json.dumps(index))
        self.baseline_file = _cfg.STORAGE_ROOT / "baseline.json"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(_cfg, name, value)
        self.tmp.cleanup()

    def test_pair_scoped_acceptance_excluded_from_validation_set(self):
        import mct.validate as _validate

        _bl.accept_diff(
            "classes/Changed.cls",
            self.left / "classes" / "Changed.cls",
            self.right / "classes" / "Changed.cls",
            baseline_file=self.baseline_file,
            pair_key=PAIR,
        )
        entries, _destroy = _validate._collect_delta("snap-branch", "snap-org", True)
        displays = sorted(d for _, d in entries)
        assert "classes/Changed.cls" not in displays
        assert "classes/LeftOnly.cls" in displays

    def test_other_pair_acceptance_kept(self):
        import mct.validate as _validate

        _bl.accept_diff(
            "classes/Changed.cls",
            self.left / "classes" / "Changed.cls",
            self.right / "classes" / "Changed.cls",
            baseline_file=self.baseline_file,
            pair_key="branch:release↔org:uat",
        )
        entries, _destroy = _validate._collect_delta("snap-branch", "snap-org", True)
        assert "classes/Changed.cls" in sorted(d for _, d in entries)
