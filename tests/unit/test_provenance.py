"""Unit tests for snapshot provenance plumbing (run_ui → serve-diff-ui summary)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import mct.comparison as comparison
import mct.index as index_mod

_spec = importlib.util.spec_from_file_location(
    "serve_diff_ui_prov", _SCRIPTS_DIR / "serve-diff-ui.py"
)
_sdu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sdu)


class TestSnapshotProvenance:
    def test_returns_info_for_known_snapshot_id(self, monkeypatch):
        row = {
            "id": "org-uat-manifest-repo-x", "type": "org_retrieve",
            "created_at": "20260710-120000-0700", "manifest_kind": "repo-manifest",
            "branch": "main", "org_alias": "uat", "path": "retrieved-org-x/main/default",
            "api_version": "66.0",
        }
        monkeypatch.setattr(comparison, "load_snapshot_row_by_id", lambda sid: row)
        info = comparison._snapshot_provenance("org-uat-manifest-repo-x")
        assert info == {
            "id": "org-uat-manifest-repo-x", "type": "org_retrieve",
            "created_at": "20260710-120000-0700", "manifest_kind": "repo-manifest",
            "branch": "main", "org_alias": "uat", "api_version": "66.0",
            "skipped_org_types": None, "retrieve_warnings": None,
        }

    def test_returns_none_for_plain_path(self, monkeypatch):
        def raise_(sid):
            raise RuntimeError("Snapshot id not found")
        monkeypatch.setattr(comparison, "load_snapshot_row_by_id", raise_)
        assert comparison._snapshot_provenance("some/random/path") is None


class TestParseInfoArg:
    def test_valid_json_dict(self):
        assert _sdu._parse_info('{"type": "branch"}') == {"type": "branch"}

    def test_none_and_invalid_return_none(self):
        assert _sdu._parse_info(None) is None
        assert _sdu._parse_info("") is None
        assert _sdu._parse_info("not json") is None
        assert _sdu._parse_info('["list"]') is None


class TestSummaryIncludesInfo:
    def test_summary_endpoint_payload_carries_info(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        for root in (left, right):
            (root / "classes").mkdir(parents=True)
            (root / "classes" / "A.cls").write_text("public class A {}\n")

        handler_cls = _sdu.make_handler(
            left, right, "l", "r", "66.0",
            {"type": "branch", "branch": "main"},
            {"type": "org_retrieve", "manifest_kind": "union-manifest"},
        )
        assert handler_cls.left_info == {"type": "branch", "branch": "main"}
        assert handler_cls.right_info == {"type": "org_retrieve", "manifest_kind": "union-manifest"}


class TestProvenanceWarnings:
    def _info(self, **kw):
        base = {"id": "x", "type": "org_retrieve", "created_at": "20260806-000000+0000",
                "manifest_kind": "union-manifest", "branch": None, "org_alias": None,
                "api_version": None, "skipped_org_types": None, "retrieve_warnings": None}
        base.update(kw)
        return base

    def test_api_version_mismatch_warns(self):
        warns = comparison._provenance_warnings(
            self._info(api_version="65.0"), self._info(api_version="66.0"))
        assert any("65.0" in w and "66.0" in w for w in warns)

    def test_org_to_org_compare_warns_about_shared_baseline(self):
        warns = comparison._provenance_warnings(
            self._info(org_alias="uat"), self._info(org_alias="prod"))
        assert any("uat" in w and "prod" in w for w in warns)

    def test_skipped_types_and_retrieve_warnings_surface(self):
        warns = comparison._provenance_warnings(
            self._info(skipped_org_types=["Report"]),
            self._info(retrieve_warnings=["objects/X.object: not found"]))
        assert any("Report" in w for w in warns)
        assert any("may be incomplete" in w for w in warns)

    def test_no_warnings_for_clean_matching_sides(self):
        assert comparison._provenance_warnings(
            self._info(api_version="66.0", branch="main"),
            self._info(api_version="66.0", org_alias="uat")) == []
