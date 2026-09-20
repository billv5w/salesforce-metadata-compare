"""Managed-package classification: installed-package namespaces mark
one-sided components and profile grants as installation drift, not source
drift — classified and visible, never silently dropped."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import mct.config as _cfg
from mct.baseline import default_baseline
from mct.comparison import (
    _classify_result,
    managed_namespace_of,
    managed_namespaces_for,
    managed_namespaces_for_org,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point all config globals at a temp data root."""
    monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
    monkeypatch.setattr(_cfg, "STATE_DIR", tmp_path / "store")
    monkeypatch.setattr(_cfg, "INDEX_PATH", tmp_path / "store" / "snapshots.json")
    monkeypatch.setattr(
        _cfg, "COMPARISON_INDEX_PATH", tmp_path / "store" / "comparisons.json"
    )
    (tmp_path / "store").mkdir(parents=True)
    return tmp_path


def _write_packages_snapshot(
    store: Path, snap_id: str, org: str, rows: list[dict[str, Any]],
    created_at: str = "20260920-120000+1200",
) -> None:
    rel = f"{snap_id}.json"
    (_cfg.STORAGE_ROOT / rel).write_text(
        json.dumps({"result": rows}), encoding="utf-8"
    )
    index = {"snapshots": []}
    ip = _cfg.INDEX_PATH
    if ip.is_file():
        index = json.loads(ip.read_text(encoding="utf-8"))
    index["snapshots"].append({
        "id": snap_id, "type": "installed_packages", "org_alias": org,
        "path": rel, "created_at": created_at,
    })
    ip.write_text(json.dumps(index), encoding="utf-8")


class TestManagedNamespacesForOrg:
    def test_reads_namespaces_casefolded(self, store):
        _write_packages_snapshot(store, "pkgs-1", "fsc", [
            {"SubscriberPackageNamespace": "LLC_BI", "SubscriberPackageName": "nCino"},
            {"SubscriberPackageNamespace": "nc_sfass", "SubscriberPackageName": "FSC"},
        ])
        assert managed_namespaces_for_org("fsc") == frozenset({"llc_bi", "nc_sfass"})

    def test_newest_snapshot_wins(self, store):
        _write_packages_snapshot(
            store, "pkgs-old", "org1", [{"SubscriberPackageNamespace": "oldns"}],
            created_at="20260101-000000+0000",
        )
        _write_packages_snapshot(
            store, "pkgs-new", "org1", [{"SubscriberPackageNamespace": "newns"}],
            created_at="20260920-000000+1200",
        )
        assert managed_namespaces_for_org("org1") == frozenset({"newns"})

    def test_no_snapshot_returns_empty(self, store):
        assert managed_namespaces_for_org("unknown-org") == frozenset()

    def test_malformed_packages_file_returns_empty(self, store):
        (_cfg.STORAGE_ROOT / "bad.json").write_text("{not json", encoding="utf-8")
        _cfg.INDEX_PATH.write_text(json.dumps({"snapshots": [{
            "id": "bad", "type": "installed_packages", "org_alias": "o",
            "path": "bad.json", "created_at": "20260101-000000+0000",
        }]}), encoding="utf-8")
        assert managed_namespaces_for_org("o") == frozenset()

    def test_branch_rows_ignored(self, store):
        _cfg.INDEX_PATH.write_text(json.dumps({"snapshots": [{
            "id": "b1", "type": "branch", "org_alias": "o",
            "path": "b1", "created_at": "20260101-000000+0000",
        }]}), encoding="utf-8")
        assert managed_namespaces_for_org("o") == frozenset()

    def test_managed_namespaces_for_unions_org_sides(self, store):
        _write_packages_snapshot(
            store, "pkgs-r", "org-r", [{"SubscriberPackageNamespace": "nsr"}])
        left = {"type": "branch", "branch": "main"}
        right = {"type": "org_retrieve", "org_alias": "org-r"}
        assert managed_namespaces_for(left, right) == frozenset({"nsr"})
        assert managed_namespaces_for(left, None) == frozenset()


class TestManagedNamespaceOf:
    M = frozenset({"llc_bi"})

    def test_namespaced_field_path(self):
        assert managed_namespace_of(
            "objects/Account/fields/LLC_BI__Collateral__c.field-meta.xml",
            self.M,
        ) == "llc_bi"

    def test_namespaced_class_path(self):
        assert managed_namespace_of("classes/LLC_BI__Helper.cls-meta.xml", self.M) == "llc_bi"

    def test_suffix_name_is_not_namespace(self):
        # Foo__c is <name>__c, not <foo>__C — never managed.
        assert managed_namespace_of(
            "objects/Account/fields/Foo__c.field-meta.xml", self.M
        ) is None

    def test_unknown_namespace_returns_none(self):
        assert managed_namespace_of(
            "classes/ZZZ__Thing.cls-meta.xml", self.M
        ) is None

    def test_unmanaged_name_returns_none(self):
        assert managed_namespace_of("classes/MyClass.cls-meta.xml", self.M) is None

    def test_empty_namespaces_returns_none(self):
        assert managed_namespace_of(
            "classes/LLC_BI__Helper.cls-meta.xml", frozenset()
        ) is None


def _result(
    tmp_path: Path,
    *,
    differ: list[str] | None = None,
    only_left: list[str] | None = None,
    only_right: list[str] | None = None,
) -> SimpleNamespace:
    """Minimal TreeCompareResult stand-in: indexes and pair lists."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    left_ix: dict[str, tuple[Path, str]] = {}
    right_ix: dict[str, tuple[Path, str]] = {}
    differ_pairs = []
    for disp in differ or []:
        lp, rp = left / disp, right / disp
        differ_pairs.append((lp, rp, disp, disp))
        left_ix[disp] = (lp, disp)
        right_ix[disp] = (rp, disp)
    for disp in only_left or []:
        left_ix[disp] = (left / disp, disp)
    for disp in only_right or []:
        right_ix[disp] = (right / disp, disp)
    return SimpleNamespace(
        differ_pairs=differ_pairs,
        only_left_keys=list(only_left or []),
        only_right_keys=list(only_right or []),
        left_ix=left_ix, right_ix=right_ix,
        left_root=left, right_root=right,
    )


class TestClassifyResultManaged:
    def test_managed_only_right_is_ignored_with_rule(self, tmp_path):
        result = _result(
            tmp_path,
            only_right=[
                "objects/Account/fields/LLC_BI__F__c.field-meta.xml",
                "classes/RealDrift.cls-meta.xml",
            ],
        )
        cls = _classify_result(
            result, default_baseline(),
            managed_right=frozenset({"llc_bi"}),
        )
        assert cls["only_right_files"] == ["classes/RealDrift.cls-meta.xml"]
        assert cls["ignored_files"] == [{
            "path": "objects/Account/fields/LLC_BI__F__c.field-meta.xml",
            "rule": "managed:llc_bi",
        }]

    def test_managed_only_left_is_ignored(self, tmp_path):
        result = _result(tmp_path, only_left=["classes/LLC_BI__Helper.cls-meta.xml"])
        cls = _classify_result(
            result, default_baseline(), managed_left=frozenset({"llc_bi"})
        )
        assert cls["only_left_files"] == []
        assert cls["ignored_files"][0]["rule"] == "managed:llc_bi"

    def test_wrong_side_namespaces_do_not_apply(self, tmp_path):
        # Namespaces are side-scoped: right-only file classified against the
        # RIGHT org's packages, not the left's.
        result = _result(tmp_path, only_right=["classes/LLC_BI__H.cls-meta.xml"])
        cls = _classify_result(
            result, default_baseline(), managed_left=frozenset({"llc_bi"})
        )
        assert cls["only_right_files"] == ["classes/LLC_BI__H.cls-meta.xml"]
        assert cls["ignored_files"] == []

    def test_no_namespaces_means_no_classification(self, tmp_path):
        result = _result(tmp_path, only_right=["classes/LLC_BI__H.cls-meta.xml"])
        cls = _classify_result(result, default_baseline())
        assert cls["only_right_files"] == ["classes/LLC_BI__H.cls-meta.xml"]

    def test_differ_pairs_never_managed_classified(self, tmp_path):
        # A managed component present on BOTH sides with different content is
        # real drift — only one-sided entries classify as installation drift.
        result = _result(tmp_path, differ=["classes/LLC_BI__H.cls-meta.xml"])
        cls = _classify_result(
            result, default_baseline(), managed_right=frozenset({"llc_bi"})
        )
        assert cls["different_files"] == ["classes/LLC_BI__H.cls-meta.xml"]
        assert cls["ignored_files"] == []


class TestDeltaManaged:
    def test_deploy_excludes_managed_only_sides(self, tmp_path):
        import mct.delta as _delta

        result = _result(
            tmp_path,
            only_left=[
                "classes/Deploy_Me.cls-meta.xml",
                "classes/LLC_BI__Skip.cls-meta.xml",
            ],
            only_right=[
                "classes/Destroy_Me.cls-meta.xml",
                "classes/LLC_BI__Skip2.cls-meta.xml",
            ],
        )
        deploy, destroy = _delta.collect_deploy_files(
            result, default_baseline(),
            managed_left=frozenset({"llc_bi"}),
            managed_right=frozenset({"llc_bi"}),
        )
        assert [d for _, d in deploy] == ["classes/Deploy_Me.cls-meta.xml"]
        assert [d for _, d in destroy] == ["classes/Destroy_Me.cls-meta.xml"]

    def test_reverse_sync_excludes_managed_right(self, tmp_path):
        import mct.delta as _delta

        result = _result(
            tmp_path,
            only_right=[
                "classes/Pull_Me.cls-meta.xml",
                "classes/LLC_BI__Skip.cls-meta.xml",
            ],
        )
        files = _delta.collect_reverse_sync_files(
            result, default_baseline(), managed_right=frozenset({"llc_bi"})
        )
        assert [d for _, d in files] == ["classes/Pull_Me.cls-meta.xml"]


class TestAcceptanceFingerprintManaged:
    def test_managed_grant_churn_does_not_stale_acceptance(self, tmp_path):
        """A profile accepted under managed-namespace normalization must stay
        accepted when only managed grants churn — the fingerprint covers the
        same normalized view the user reviewed."""
        import mct.baseline as _bl

        left = tmp_path / "l"
        right = tmp_path / "r"
        (left / "profiles").mkdir(parents=True)
        (right / "profiles").mkdir(parents=True)
        lp = left / "profiles" / "Admin.profile-meta.xml"
        rp = right / "profiles" / "Admin.profile-meta.xml"
        head = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Profile xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        )
        tail = (
            '    <userPermissions><name>ApiEnabled</name>'
            '<enabled>true</enabled></userPermissions>\n</Profile>\n'
        )
        managed_tab = (
            '    <tabVisibilities><tab>LLC_BI__Portal</tab>'
            '<visibility>Hidden</visibility></tabVisibilities>\n'
        )
        # Non-default verdict — only dropped under managed_namespaces, so the
        # test cannot pass via the always-on default-grant rule.
        managed_tab2 = (
            '    <tabVisibilities><tab>LLC_BI__Admin</tab>'
            '<visibility>Hidden</visibility></tabVisibilities>\n'
        )
        lp.write_text(head + tail, encoding="utf-8")
        rp.write_text(head + managed_tab + tail, encoding="utf-8")

        bl_file = tmp_path / "baseline.json"
        managed = frozenset({"llc_bi"})
        _bl.accept_diff(
            "profiles/Admin.profile-meta.xml", lp, rp,
            baseline_file=bl_file, managed_namespaces=managed,
        )
        # Package adds another managed tab grant — normalized view unchanged.
        rp.write_text(head + managed_tab + managed_tab2 + tail, encoding="utf-8")
        status, _ = _bl.classify_entry(
            "profiles/Admin.profile-meta.xml", lp, rp,
            _bl.load_baseline(bl_file), managed_namespaces=managed,
        )
        assert status == "accepted"

    def test_without_namespaces_fingerprint_is_sensitive(self, tmp_path):
        """Sanity: without the managed context the same churn IS a content
        change — classification must be opted into, not assumed."""
        import mct.baseline as _bl

        left = tmp_path / "l"
        right = tmp_path / "r"
        (left / "profiles").mkdir(parents=True)
        (right / "profiles").mkdir(parents=True)
        lp = left / "profiles" / "A.profile-meta.xml"
        rp = right / "profiles" / "A.profile-meta.xml"
        head = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Profile xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        )
        grant = (
            '    <tabVisibilities><tab>ZZZ__T</tab>'
            '<visibility>Hidden</visibility></tabVisibilities>\n'
        )
        grant2 = (
            '    <tabVisibilities><tab>ZZZ__U</tab>'
            '<visibility>Hidden</visibility></tabVisibilities>\n'
        )
        lp.write_text(head + "</Profile>\n", encoding="utf-8")
        rp.write_text(head + grant + "</Profile>\n", encoding="utf-8")
        bl_file = tmp_path / "baseline.json"
        _bl.accept_diff(
            "profiles/A.profile-meta.xml", lp, rp, baseline_file=bl_file
        )
        rp.write_text(head + grant + grant2 + "</Profile>\n", encoding="utf-8")
        status, _ = _bl.classify_entry(
            "profiles/A.profile-meta.xml", lp, rp, _bl.load_baseline(bl_file)
        )
        assert status == "accepted_stale"
