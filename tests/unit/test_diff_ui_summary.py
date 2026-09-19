"""Unit tests for serve-diff-ui.py build_summary flags."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_spec = importlib.util.spec_from_file_location(
    "serve_diff_ui_summary", _SCRIPTS_DIR / "serve-diff-ui.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestNormalizationSkippedFlag:
    def test_oversized_xml_differ_is_flagged(self, tmp_path, monkeypatch):
        # Shrink the cap so the test doesn't need real 5 MB files.
        monkeypatch.setattr(_mod, "_MAX_XML_BYTES", 50)
        left = tmp_path / "left"
        right = tmp_path / "right"
        big_a = f'<Profile xmlns="{SF_NS}">' + "<userPermissions><name>A</name></userPermissions>" * 3 + "</Profile>"
        big_b = f'<Profile xmlns="{SF_NS}">' + "<userPermissions><name>B</name></userPermissions>" * 3 + "</Profile>"
        _write(left / "profiles" / "Admin.profile-meta.xml", big_a)
        _write(right / "profiles" / "Admin.profile-meta.xml", big_b)

        _mod.get_comparison.cache_clear()
        summary = _mod.build_summary(left, right, "l", "r")

        assert summary["different_count"] == 1
        assert summary["differ"][0]["normalization_skipped"] is True

    def test_small_xml_differ_not_flagged(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        _write(left / "objects" / "A.object-meta.xml", f'<CustomObject xmlns="{SF_NS}"><label>X</label></CustomObject>')
        _write(right / "objects" / "A.object-meta.xml", f'<CustomObject xmlns="{SF_NS}"><label>Y</label></CustomObject>')

        _mod.get_comparison.cache_clear()
        summary = _mod.build_summary(left, right, "l", "r")

        assert summary["different_count"] == 1
        assert "normalization_skipped" not in summary["differ"][0]


class TestSummaryBaselineClassification:
    def _trees(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        _write(left / "classes" / "A.cls", "v1")
        _write(right / "classes" / "A.cls", "v2")
        _write(left / "reports" / "R.report-meta.xml", "r1")
        _write(right / "reports" / "R.report-meta.xml", "r2")
        return left, right

    def _with_baseline(self, monkeypatch, tmp_path):
        bl_file = tmp_path / "baseline.json"
        monkeypatch.setattr(_mod, "BASELINE_FILE", bl_file)
        _mod.get_comparison.cache_clear()
        return bl_file

    def test_no_baseline_all_active(self, tmp_path, monkeypatch):
        left, right = self._trees(tmp_path)
        monkeypatch.setattr(_mod, "BASELINE_FILE", None)
        _mod.get_comparison.cache_clear()
        summary = _mod.build_summary(left, right, "l", "r")
        assert summary["different_count"] == 2
        assert summary["baseline_enabled"] is False

    def test_ignore_rule_moves_to_ignored(self, tmp_path, monkeypatch):
        import mct.baseline as bl

        left, right = self._trees(tmp_path)
        bl_file = self._with_baseline(monkeypatch, tmp_path)
        data = bl.default_baseline()
        data["ignore"]["types"] = ["reports"]
        bl.save_baseline(data, bl_file)

        summary = _mod.build_summary(left, right, "l", "r")

        assert summary["different_count"] == 1
        assert summary["ignored_count"] == 1
        assert summary["ignored"][0]["path"] == "reports/R.report-meta.xml"
        assert summary["ignored"][0]["rule"] == "type:reports"
        assert summary["baseline_enabled"] is True

    def test_accept_then_stale_resurfaces(self, tmp_path, monkeypatch):
        import mct.baseline as bl

        left, right = self._trees(tmp_path)
        bl_file = self._with_baseline(monkeypatch, tmp_path)
        bl.accept_diff("classes/A.cls", left / "classes" / "A.cls",
                       right / "classes" / "A.cls", baseline_file=bl_file)

        summary = _mod.build_summary(left, right, "l", "r")
        assert summary["accepted_count"] == 1
        accepted_paths = [f["path"] for f in summary["accepted"]]
        assert "classes/A.cls" in accepted_paths
        differ_paths = [f["path"] for f in summary["differ"]]
        assert "classes/A.cls" not in differ_paths

        # Content moves on → resurfaces as active with the stale flag
        (right / "classes" / "A.cls").write_text("v3", encoding="utf-8")
        _mod.get_comparison.cache_clear()
        summary = _mod.build_summary(left, right, "l", "r")
        entry = next(f for f in summary["differ"] if f["path"] == "classes/A.cls")
        assert entry["accepted_stale"] is True
        assert summary["accepted_count"] == 0


class TestAcceptEndpoint:
    def test_accept_writes_baseline_and_unaccept_removes(self, tmp_path, monkeypatch):
        import mct.baseline as bl

        left = tmp_path / "left"
        right = tmp_path / "right"
        _write(left / "classes" / "A.cls", "v1")
        _write(right / "classes" / "A.cls", "v2")
        bl_file = tmp_path / "baseline.json"
        monkeypatch.setattr(_mod, "BASELINE_FILE", bl_file)
        _mod.get_comparison.cache_clear()

        captured = {}

        class _Fake:
            left_root = left
            right_root = right
            def _read_body(self):
                return {"path": "classes/A.cls"}
            def _serve_json(self, data, status=200):
                captured.clear()
                captured.update(data)
                captured["_status"] = status

        _mod.DiffUIHandler._handle_accept_diff(_Fake(), accept=True)
        assert captured["ok"] is True
        assert "classes/A.cls" in bl.load_baseline(bl_file)["accepted"]

        _mod.DiffUIHandler._handle_accept_diff(_Fake(), accept=False)
        assert captured["ok"] is True
        assert bl.load_baseline(bl_file)["accepted"] == {}

    def test_accept_without_baseline_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "BASELINE_FILE", None)
        captured = {}

        class _Fake:
            left_root = tmp_path
            right_root = tmp_path
            def _read_body(self):
                return {"path": "x"}
            def _serve_json(self, data, status=200):
                captured.update(data)
                captured["_status"] = status

        _mod.DiffUIHandler._handle_accept_diff(_Fake(), accept=True)
        assert captured["ok"] is False
        assert captured["_status"] == 400
