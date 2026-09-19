"""Sixth review (release remediation at c5f1ccf): six confirmed findings.

1. Metadata links must never expose files outside the compared tree —
   file/dir/dangling links rejected at index, at read/export time, and in
   git-archive extraction on every supported Python version.
2. Baseline fingerprints hash raw bytes — distinct binary contents can no
   longer collide after lossy decoding; read failures are explicit.
3. Retrieval incompleteness (retrieve warnings, skipped org types, API
   version mismatch) is visible in summaries and reports.
4. validate-deploy fails closed on unvalidated deletion scope.
5. --include-type/--exclude-type scope works end-to-end through the
   orchestrator launch to the diff server.
6. Offline reports list every active changed file — binary or otherwise.

Synthetic fixtures only. No Salesforce credentials or org access.
"""
from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import mct.config as config
from mct import baseline, comparison, snapshot, validate
from mct.retrieved_folder_compare import (
    LinkedMetadataError,
    apply_type_scope,
    compare_trees,
    index_tree,
)

spec = importlib.util.spec_from_file_location(
    "sixth_review_diff_ui", REPO / "scripts/serve-diff-ui.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)

spec_orch = importlib.util.spec_from_file_location(
    "sixth_review_orch_ui", REPO / "scripts/serve-orchestrator-ui.py"
)
orch = importlib.util.module_from_spec(spec_orch)
spec_orch.loader.exec_module(orch)

from mct.ui_server_base import UIHTTPServer


def _env_compare_parse(argv):
    spec_ec = importlib.util.spec_from_file_location(
        "sixth_review_env_compare", REPO / "scripts/env-compare.py"
    )
    mod = importlib.util.module_from_spec(spec_ec)
    # env-compare.py swaps its own sys.modules entry at import time — the
    # module must be registered before exec_module runs.
    sys.modules[spec_ec.name] = mod
    spec_ec.loader.exec_module(mod)
    old = sys.argv
    try:
        sys.argv = argv
        return mod.parse_args()
    finally:
        sys.argv = old


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated config + storage roots for every test in this file."""
    monkeypatch.setenv("MCT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "STORAGE_ROOT", tmp_path / "store")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "INDEX_PATH", tmp_path / "store/snapshots.json")
    monkeypatch.setattr(config, "COMPARISON_INDEX_PATH", tmp_path / "store/comparisons.json")
    monkeypatch.setattr(ui, "BASELINE_FILE", None)
    monkeypatch.setattr(ui, "PAIR_KEY", None)
    monkeypatch.setattr(ui, "INCLUDE_TYPES", None)
    monkeypatch.setattr(ui, "EXCLUDE_TYPES", None)
    ui.get_comparison.cache_clear()
    yield tmp_path
    ui.get_comparison.cache_clear()


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    left, right = tmp_path / "left", tmp_path / "right"
    (left / "classes").mkdir(parents=True)
    (right / "classes").mkdir(parents=True)
    _write(left / "classes/A.cls", "public class A { Integer v = 1; }\n")
    _write(right / "classes/A.cls", "public class A { Integer v = 2; }\n")
    return left, right


def _tar_bytes(entries) -> bytes:
    """entries: list of (name, kind, payload) — kind in
    'file'|'dir'|'symlink'|'hardlink'|'chardev'; payload = bytes for files,
    link target for links, None otherwise."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind, payload in entries:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = payload
                tf.addfile(ti)
            elif kind == "hardlink":
                ti.type = tarfile.LNKTYPE
                ti.linkname = payload
                tf.addfile(ti)
            elif kind == "chardev":
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 1
                ti.devminor = 3
                tf.addfile(ti)
            else:
                data = payload if isinstance(payload, bytes) else (payload or b"")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _archive_run(tar_bytes):
    """Fake _cfg.run returning the crafted archive for `git archive` calls."""

    def fake(cmd, **kwargs):
        class R:
            stdout = tar_bytes
            returncode = 0
            stderr = ""

        return R()

    return fake


def _handler(tmp_path, monkeypatch, left, right, left_info=None, right_info=None):
    handler = object.__new__(
        ui.make_handler(left, right, "left", "right",
                        left_info=left_info, right_info=right_info)
    )
    captured = {}
    handler._serve_json = lambda body, status=200: captured.update(
        json_body=body, status=status
    )
    handler._serve_bytes = lambda body, status=200, headers=None: captured.update(
        bytes_body=body, status=status
    )
    handler._read_body = lambda: {}
    return handler, captured


# ===========================================================================
# Finding 1 — linked metadata must never escape the compared tree
# ===========================================================================

MARKER = "EXTERNAL-SECRET-MARKER"


def test_index_tree_rejects_external_file_link(tmp_path):
    outside = _write(tmp_path / "outside.txt", MARKER)
    tree = tmp_path / "tree"
    (tree / "classes").mkdir(parents=True)
    _write(tree / "classes/A.cls", "public class A {}")
    (tree / "classes/Link.cls").symlink_to(outside)
    with pytest.raises(LinkedMetadataError, match="Link.cls"):
        index_tree(tree)


def test_index_tree_rejects_external_dir_link(tmp_path):
    outside = tmp_path / "outside"
    (outside / "classes").mkdir(parents=True)
    _write(outside / "classes/Secret.cls", MARKER)
    tree = tmp_path / "tree"
    (tree / "classes").mkdir(parents=True)
    _write(tree / "classes/A.cls", "public class A {}")
    (tree / "more").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LinkedMetadataError):
        index_tree(tree)


def test_index_tree_rejects_dangling_link(tmp_path):
    tree = tmp_path / "tree"
    (tree / "classes").mkdir(parents=True)
    _write(tree / "classes/A.cls", "public class A {}")
    (tree / "classes/Dangling.cls").symlink_to(tmp_path / "does-not-exist.cls")
    with pytest.raises(LinkedMetadataError, match="Dangling"):
        index_tree(tree)


def test_compare_trees_refuses_linked_tree(tmp_path):
    left, right = _pair(tmp_path)
    outside = _write(tmp_path / "outside.txt", MARKER)
    (left / "classes/Link.cls").symlink_to(outside)
    with pytest.raises(LinkedMetadataError):
        compare_trees(left, right)


def test_diff_api_refuses_link_swapped_in_after_cache(store):
    """A file replaced by an external link after the comparison was cached
    must produce an error, never the link target's content."""
    left, right = _pair(store)
    cmp = ui.get_comparison(left, right)
    assert cmp.differ_pairs  # cached before the swap
    (left / "classes/A.cls").unlink()
    (left / "classes/A.cls").symlink_to(store / "secret.txt")
    _write(store / "secret.txt", MARKER)

    result = ui.file_diff(left / "classes/A.cls", right / "classes/A.cls")
    assert result["error"]
    assert "link" in result["error"].lower()
    assert MARKER not in result["diff"]


def test_serve_file_content_refuses_link(store):
    left, right = _pair(store)
    handler, captured = _handler(store, None, left, right)
    outside = _write(store / "outside.txt", MARKER)
    link = left / "classes/OnlyLeft.cls"
    link.symlink_to(outside)
    handler._serve_file_content(link, "left")
    assert captured["json_body"]["error"]
    assert MARKER not in json.dumps(captured["json_body"])


def test_bundle_export_refuses_linked_source(store, monkeypatch):
    left, right = _pair(store)
    _write(left / "classes/New.cls", "public class New {}")
    handler, captured = _handler(store, monkeypatch, left, right)
    handler._resolve_components_via_sf = lambda paths: (
        {"ApexClass": {"New", "A"}}, None
    )
    # Swap an in-scope source file for a link after the comparison ran.
    ui.get_comparison(left, right)
    outside = _write(store / "outside.txt", MARKER)
    (left / "classes/New.cls").unlink()
    (left / "classes/New.cls").symlink_to(outside)

    handler._handle_export_bundle()
    # Fails closed (the delta-collection funnel refuses links before any
    # manifest resolution or zip write) — the marker is never read.
    assert captured["status"] >= 400
    assert "link" in captured["json_body"]["error"].lower()
    if "bytes_body" in captured:
        assert MARKER.encode() not in captured["bytes_body"]


def test_validate_temp_project_refuses_link(store):
    left, right = _pair(store)
    outside = _write(store / "outside.txt", MARKER)
    link = left / "classes/Evil.cls"
    link.symlink_to(outside)
    with pytest.raises(RuntimeError, match="symbolic link"):
        validate._write_temp_project([(link, "classes/Evil.cls")], "60.0", False)


def test_run_ui_never_launches_over_linked_tree(store, monkeypatch):
    left, right = _pair(store)
    outside = _write(store / "outside.txt", MARKER)
    (left / "classes/Link.cls").symlink_to(outside)
    launched = []
    monkeypatch.setattr(config, "run", lambda cmd, **kw: launched.append(cmd))
    with pytest.raises(LinkedMetadataError):
        comparison.run_ui(str(left), str(right), 0, True)
    assert not launched


_extract_seq = 0


def _extract(store, monkeypatch, entries):
    global _extract_seq
    _extract_seq += 1
    out_dir = store / f"out-{_extract_seq}"
    out_dir.mkdir()
    monkeypatch.setattr(config, "run", _archive_run(_tar_bytes(entries)))
    snapshot._extract_archive_to("ref", "sub", out_dir)
    return out_dir


def test_archive_extract_rejects_symlink_member(store, monkeypatch):
    with pytest.raises(RuntimeError, match="link"):
        _extract(store, monkeypatch, [
            ("sub/classes/A.cls", "file", b"class A {}"),
            ("sub/classes/Link.cls", "symlink", "/etc/passwd"),
        ])


def test_archive_extract_rejects_hardlink_member(store, monkeypatch):
    with pytest.raises(RuntimeError, match="link"):
        _extract(store, monkeypatch, [
            ("sub/classes/Hard.cls", "hardlink", "/etc/passwd"),
        ])


def test_archive_extract_rejects_special_member(store, monkeypatch):
    with pytest.raises(RuntimeError, match="file or directory"):
        _extract(store, monkeypatch, [
            ("sub/classes/Dev.cls", "chardev", None),
        ])


def test_archive_extract_rejects_traversal_and_sibling_prefix(store, monkeypatch):
    """`../` escapes, absolute names, and sibling-prefix landings (out_evil is
    NOT inside out) all fail component containment — not string prefixes."""
    for bad_name in ("../evil.txt", "/abs/evil.txt", "sub/../../evil.txt"):
        with pytest.raises(RuntimeError, match="escapes"):
            _extract(store, monkeypatch, [(bad_name, "file", b"x")])


def test_archive_extract_sibling_prefix_bypass_is_closed(store, monkeypatch):
    """A member resolving to a sibling directory whose name merely shares a
    string prefix (``out_evil``) must be rejected — the old startswith check
    allowed it."""
    out_dir = store / "out"
    out_dir.mkdir()
    sibling = store / "out_evil"
    sibling.mkdir()
    monkeypatch.setattr(
        config, "run",
        _archive_run(_tar_bytes([("../out_evil/evil.txt", "file", MARKER.encode())])),
    )
    with pytest.raises(RuntimeError, match="escapes"):
        snapshot._extract_archive_to("ref", "sub", out_dir)
    assert not (sibling / "evil.txt").exists()


def test_archive_extract_accepts_plain_metadata(store, monkeypatch):
    out_dir = _extract(store, monkeypatch, [
        ("sub/classes/", "dir", None),
        ("sub/classes/A.cls", "file", b"class A {}"),
    ])
    assert (out_dir / "sub/classes/A.cls").is_file()


def test_archive_extract_member_validation_on_legacy_python(store, monkeypatch):
    """The pre-3.12 fallback path (no tarfile filter=) applies the same
    member validation — links, specials and escapes are rejected there too."""
    monkeypatch.setattr(sys, "version_info", (3, 10))
    with pytest.raises(RuntimeError, match="link"):
        _extract(store, monkeypatch, [
            ("sub/classes/Link.cls", "symlink", "/etc/passwd"),
        ])
    with pytest.raises(RuntimeError, match="escapes"):
        _extract(store, monkeypatch, [("../evil.txt", "file", b"x")])


def _merge(store, monkeypatch, entries):
    monkeypatch.setattr(config, "run", _archive_run(_tar_bytes(entries)))
    out_dir = store / "merged"
    out_dir.mkdir()
    return snapshot._merge_package_dirs("ref", ["pkg"], out_dir)


def test_merge_package_dirs_rejects_link_member(store, monkeypatch):
    with pytest.raises(RuntimeError, match="link"):
        _merge(store, monkeypatch, [
            ("pkg/main/default/classes/Link.cls", "symlink", "/etc/passwd"),
        ])


def test_merge_package_dirs_rejects_special_member(store, monkeypatch):
    with pytest.raises(RuntimeError, match="file or directory"):
        _merge(store, monkeypatch, [
            ("pkg/main/default/classes/Dev.cls", "chardev", None),
        ])


def test_merge_package_dirs_rejects_traversal_member(store, monkeypatch):
    with pytest.raises(RuntimeError, match="escapes"):
        _merge(store, monkeypatch, [
            ("pkg/main/default/../evil.txt", "file", MARKER.encode()),
        ])


def test_merge_package_dirs_accepts_plain_files(store, monkeypatch):
    merged, collisions = _merge(store, monkeypatch, [
        ("pkg/main/default/classes/", "dir", None),
        ("pkg/main/default/classes/A.cls", "file", b"class A {}"),
    ])
    assert (merged / "classes/A.cls").is_file()
    assert collisions == []


def test_report_shows_linked_entry_without_reading_target(store):
    """A link swapped in after indexing renders as an explanatory entry —
    the marker must never reach the report."""
    left, right = _pair(store)
    handler, _captured = _handler(store, None, left, right)
    ui.get_comparison(left, right)
    outside = _write(store / "secret.txt", MARKER)
    (left / "classes/A.cls").unlink()
    (left / "classes/A.cls").symlink_to(outside)
    html = ui._build_standalone_report(handler)
    assert "classes/A.cls" in html
    assert "symbolic link" in html.lower()
    assert MARKER not in html


# ===========================================================================
# Finding 2 — lossless baseline fingerprints
# ===========================================================================

def test_binary_fingerprint_is_lossless(store):
    """v1 collided these two after UTF-8 replacement decoding; v2 must not."""
    l = _write(store / "l/x.resource", b"\x00\x82")
    r = _write(store / "r/x.resource", b"\x00\x83")
    fp = baseline.compute_fingerprint(l, r)
    _write(store / "r/x.resource", b"\x00\x84")
    assert baseline.compute_fingerprint(l, r) != fp
    # identical bytes (at another location) → identical fingerprint
    _write(store / "r2/x.resource", b"\x00\x84")
    assert baseline.compute_fingerprint(l, store / "r2/x.resource") == \
        baseline.compute_fingerprint(l, r)


def test_fingerprint_missing_vs_empty_vs_swapped(store):
    empty = _write(store / "empty.cls", b"")
    other = _write(store / "o.cls", b"x")
    f_missing_l = baseline.compute_fingerprint(None, other)
    f_empty_l = baseline.compute_fingerprint(empty, other)
    f_missing_r = baseline.compute_fingerprint(other, None)
    f_empty_r = baseline.compute_fingerprint(other, empty)
    assert len({f_missing_l, f_empty_l, f_missing_r, f_empty_r}) == 4
    assert f_missing_l != f_missing_r  # swapped sides differ


def test_fingerprint_read_failure_is_explicit(store):
    not_a_file = store / "adir"
    not_a_file.mkdir()
    with pytest.raises(baseline.FingerprintReadError):
        baseline.compute_fingerprint(not_a_file, None)


def test_accept_diff_on_unreadable_file_fails(store):
    not_a_file = store / "classes"
    not_a_file.mkdir()
    with pytest.raises(baseline.FingerprintReadError):
        baseline.accept_diff(
            "classes/X.cls", not_a_file, None,
            baseline_file=store / "baseline.json",
        )


def test_unreadable_accepted_file_resurfaces_stale(store):
    lp = _write(store / "l/classes/A.cls", "a")
    rp = _write(store / "r/classes/A.cls", "b")
    bl_file = store / "baseline.json"
    baseline.accept_diff("classes/A.cls", lp, rp, baseline_file=bl_file)
    bl = baseline.load_baseline(bl_file)
    assert baseline.classify_entry("classes/A.cls", lp, rp, bl)[0] == "accepted"
    # File becomes unreadable (replaced by a directory).
    lp.unlink()
    lp.mkdir()
    status, _ = baseline.classify_entry("classes/A.cls", lp, rp, bl)
    assert status == "accepted_stale"


def test_v1_fingerprint_becomes_stale_without_corrupting_baseline(store):
    """A fingerprint stored by the old scheme can never match v2 — the entry
    goes accepted_stale until re-accepted; note and ignore rules survive."""
    lp = _write(store / "l/classes/A.cls", "a")
    rp = _write(store / "r/classes/A.cls", "b")
    bl_file = store / "baseline.json"
    bl_file.write_text(json.dumps({
        "version": 1,
        "ignore": {"types": [], "paths": [], "xml_elements": []},
        "accepted": {
            "classes/A.cls": {
                # v1: bare sha256 of decoded text — format differs from v2:*.
                "fingerprint": "0" * 64,
                "accepted_at": "yesterday",
                "note": "keep me",
            }
        },
    }))
    bl = baseline.load_baseline(bl_file)
    status, detail = baseline.classify_entry("classes/A.cls", lp, rp, bl)
    assert status == "accepted_stale"
    # Re-accept → v2 fingerprint → accepted again; note replaced per accept.
    baseline.accept_diff("classes/A.cls", lp, rp, baseline_file=bl_file)
    bl2 = baseline.load_baseline(bl_file)
    assert bl2["accepted"]["classes/A.cls"]["fingerprint"].startswith("v2:")
    assert baseline.classify_entry("classes/A.cls", lp, rp, bl2)[0] == "accepted"


def test_changed_binary_acceptance_resurfaces_in_drift(store):
    """The plan's regression: accept a resource pair holding 00 82, change
    right side to 00 83 — the stale entry must count as active drift."""
    lp = _write(store / "l/x.resource", b"\x00\x82")
    rp = _write(store / "r/x.resource", b"\x00\x82x")
    bl_file = store / "baseline.json"
    baseline.accept_diff("x.resource", lp, rp, baseline_file=bl_file)
    rp.write_bytes(b"\x00\x83")
    bl = baseline.load_baseline(bl_file)
    left, right = store / "l", store / "r"
    result = compare_trees(left, right)
    deploy, _destroy = __import__("mct.delta", fromlist=["x"]).collect_deploy_files(
        result, bl
    )
    assert any(d == "x.resource" for _, d in deploy)


# ===========================================================================
# Finding 3 — incomplete retrieval must be visible
# ===========================================================================

def _warned_info(**kw):
    base = {
        "id": "snap-x", "type": "org_retrieve", "created_at": "2025-01-01",
        "manifest_kind": "union", "org_alias": "uat",
    }
    base.update(kw)
    return base


def test_summary_api_carries_provenance_warnings(store):
    left, right = _pair(store)
    li = _warned_info(api_version="60.0")
    ri = _warned_info(
        api_version="62.0",
        skipped_org_types=["Report"],
        retrieve_warnings=["classes/A.cls: entity is deleted"],
    )
    handler, captured = _handler(store, None, left, right, li, ri)
    handler.path = "/api/summary"
    handler._route_GET()
    data = captured["json_body"]
    warns = data["provenance_warnings"]
    assert any("API version mismatch" in w for w in warns)
    assert any("Report" in w and "skipped" in w for w in warns)
    assert any("retrieve warning" in w for w in warns)
    # The raw retrieve warnings also ride along in the provenance info.
    assert data["right_info"]["retrieve_warnings"] == ["classes/A.cls: entity is deleted"]


def test_report_includes_completeness_warnings(store):
    left, right = _pair(store)
    ri = _warned_info(
        skipped_org_types=["Report"],
        retrieve_warnings=["classes/A.cls: <b>entity is deleted</b>"],
    )
    handler, _c = _handler(store, None, left, right, None, ri)
    html = ui._build_standalone_report(handler)
    assert "Snapshot completeness warnings" in html
    assert "Report" in html
    assert "skipped" in html
    # Warning text is untrusted — rendered escaped, never as markup.
    assert "&lt;b&gt;entity is deleted&lt;/b&gt;" in html
    assert "<b>entity is deleted</b>" not in html


def test_report_clean_snapshots_have_no_warning_section(store):
    left, right = _pair(store)
    handler, _c = _handler(store, None, left, right,
                           _warned_info(), _warned_info(api_version="62.0"))
    # Matching api versions? left has none — set both the same.
    handler.left_info = _warned_info(api_version="62.0")
    html = ui._build_standalone_report(handler)
    assert "Snapshot completeness warnings" not in html


# ===========================================================================
# Finding 4 — validation fails closed on unvalidated deletions
# ===========================================================================

@pytest.fixture
def no_sf(store, monkeypatch):
    calls = []
    import mct.safety
    monkeypatch.setattr(mct.safety, "run", lambda *a, **kw: calls.append(a[0]))
    return calls


def test_validate_refuses_deletion_only_delta(store, no_sf):
    left, right = _pair(store)
    _write(right / "classes/TargetOnly.cls", "public class TargetOnly {}")
    rc = validate.run_validate_deploy(str(left), str(right), "org", "60.0", 60)
    assert rc == 1
    assert no_sf == []  # no deploy attempted at all
    reports = list((store / "store").glob("validation-report-*.json"))
    assert reports, "a validation outcome must still be persisted"
    report = json.loads(reports[-1].read_text())
    assert report["success"] is False
    assert report["error"] == "unsupported_delta_scope"
    assert report["unvalidated_deletions"] == ["classes/TargetOnly.cls"]


def test_validate_refuses_mixed_delta(store, no_sf):
    """Mixed deltas must not silently validate only additions/changes."""
    left, right = _pair(store)  # A.cls differs (deployable) ...
    _write(right / "classes/TargetOnly.cls", "public class T {}")  # + deletion
    rc = validate.run_validate_deploy(str(left), str(right), "org", "60.0", 60)
    assert rc == 1
    assert no_sf == []
    report = json.loads(
        sorted((store / "store").glob("validation-report-*.json"))[-1].read_text()
    )
    assert report["success"] is False
    assert report["unvalidated_deletions"] == ["classes/TargetOnly.cls"]
    assert report["deployable_files"] >= 1


def test_baseline_ignored_deletion_does_not_block(store, no_sf, monkeypatch):
    left, right = _pair(store)
    _write(right / "classes/TargetOnly.cls", "public class T {}")
    bl_file = store / "baseline.json"
    baseline.save_baseline(
        {**baseline.default_baseline(),
         "ignore": {"types": [], "paths": ["classes/TargetOnly.cls"], "xml_elements": []}},
        path=bl_file,
    )
    monkeypatch.setattr(baseline, "baseline_path", lambda: bl_file)

    class R:
        stdout = json.dumps({"result": {"success": True}})
        returncode = 0

    monkeypatch.setattr("mct.safety.run", lambda *a, **kw: R())
    rc = validate.run_validate_deploy(str(left), str(right), "org", "60.0", 60)
    assert rc == 0


def test_validate_no_drift_still_zero(store, no_sf):
    left, right = _pair(store)
    (right / "classes/A.cls").write_text(
        (left / "classes/A.cls").read_text()
    )
    rc = validate.run_validate_deploy(str(left), str(right), "org", "60.0", 60)
    assert rc == 0
    assert no_sf == []


# ===========================================================================
# Finding 5 — comparison filters end-to-end
# ===========================================================================

def test_ui_parser_accepts_scope_flags(store):
    args = _env_compare_parse([
        "env-compare.py", "ui", "--left", "l", "--right", "r",
        "--include-type", "classes", "--include-type", "reports",
        "--exclude-type", "profiles",
    ])
    assert args.include_type == ["classes", "reports"]
    assert args.exclude_type == ["profiles"]


def test_serve_diff_ui_parser_accepts_scope_flags(store):
    old = sys.argv
    try:
        sys.argv = [
            "serve-diff-ui.py", "--left", "l", "--right", "r",
            "--include-type", "classes", "--exclude-type", "reports",
        ]
        args = ui.parse_args()
    finally:
        sys.argv = old
    assert args.include_type == ["classes"]
    assert args.exclude_type == ["reports"]


def test_run_ui_forwards_scope_to_diff_server(store, monkeypatch):
    left, right = _pair(store)
    launched = []
    monkeypatch.setattr(config, "run", lambda cmd, **kw: launched.append(cmd))
    comparison.run_ui(
        str(left), str(right), 0, True,
        include_types=["classes"], exclude_types=["reports"],
    )
    assert launched, "run_ui must launch the diff server"
    cmd = launched[0]
    joined = " ".join(cmd)
    assert "--include-type classes" in joined
    assert "--exclude-type reports" in joined


def _typed_trees(store):
    left, right = _pair(store)  # classes/A.cls differs
    _write(left / "reports/R.report", "<Report><x>L</x></Report>")
    _write(right / "reports/R.report", "<Report><x>R</x></Report>")
    _write(left / "triggers/T.trigger", "trigger T on X (before insert) {}")
    _write(right / "triggers/T.trigger", "trigger T on X (after insert) {}")
    return left, right


def test_scoped_summary_filters_types(store, monkeypatch):
    left, right = _typed_trees(store)
    monkeypatch.setattr(ui, "INCLUDE_TYPES", frozenset({"classes"}))
    summary = ui.build_summary(left, right, "left", "right")
    paths = [d["path"] for d in summary["differ"]]
    assert paths == ["classes/A.cls"]
    assert summary["scope"]["include_types"] == ["classes"]
    assert summary["scope"]["exclude_types"] == []
    # Whole-tree counts stay as context; drift counts are scoped.
    assert summary["different_count"] == 1
    monkeypatch.setattr(ui, "INCLUDE_TYPES", None)
    monkeypatch.setattr(ui, "EXCLUDE_TYPES", frozenset({"reports"}))
    ui.get_comparison.cache_clear()
    summary = ui.build_summary(left, right, "left", "right")
    assert sorted(d["path"] for d in summary["differ"]) == [
        "classes/A.cls", "triggers/T.trigger",
    ]


def test_scope_is_case_insensitive_and_exclusion_wins(store, monkeypatch):
    left, right = _typed_trees(store)
    monkeypatch.setattr(ui, "INCLUDE_TYPES", frozenset({"CLASSES", "Reports"}))
    monkeypatch.setattr(ui, "EXCLUDE_TYPES", frozenset({"reports"}))
    summary = ui.build_summary(left, right, "left", "right")
    assert [d["path"] for d in summary["differ"]] == ["classes/A.cls"]


def test_scope_cache_cannot_leak(store):
    """Different scope keys must not share a cached comparison."""
    left, right = _typed_trees(store)
    scoped = ui.get_comparison(left, right, (frozenset({"classes"}), frozenset()))
    unscoped = ui.get_comparison(left, right, (frozenset(), frozenset()))
    assert len(scoped.differ_pairs) == 1
    assert len(unscoped.differ_pairs) == 3
    # Indexes stay full either way — companions/context still resolve.
    assert "reports/r.report" in scoped.left_ix


def test_scoped_export_excludes_out_of_scope(store, monkeypatch):
    left, right = _typed_trees(store)
    monkeypatch.setattr(ui, "EXCLUDE_TYPES", frozenset({"reports", "triggers"}))
    ui.get_comparison.cache_clear()
    handler, _c = _handler(store, monkeypatch, left, right)
    deploy, destroy = handler._collect_delta_files()
    displays = [d for _, d in deploy] + [d for _, d in destroy]
    assert displays == ["classes/A.cls"]


def test_companion_expansion_still_works_under_scope(store, monkeypatch):
    """Whole-component expansion uses the full left index even when scoped."""
    left, right = _pair(store)
    _write(left / "classes/A.cls-meta.xml", "<ApexClass><apiVersion>60</apiVersion></ApexClass>")
    _write(right / "classes/A.cls-meta.xml", "<ApexClass><apiVersion>60</apiVersion></ApexClass>")
    monkeypatch.setattr(ui, "INCLUDE_TYPES", frozenset({"classes"}))
    ui.get_comparison.cache_clear()
    handler, _c = _handler(store, monkeypatch, left, right)
    deploy, _destroy = handler._collect_delta_files()
    expanded = handler._bundle_source_entries(deploy)
    displays = {d for _, d in expanded}
    assert "classes/A.cls-meta.xml" in displays


def test_apply_type_scope_preserves_indexes(store):
    left, right = _typed_trees(store)
    result = compare_trees(left, right)
    scoped = apply_type_scope(result, ["classes"], None)
    assert len(scoped.left_ix) == len(result.left_ix)
    assert len(scoped.right_ix) == len(result.right_ix)
    assert len(scoped.differ_pairs) == 1
    # Omitted filters keep the original object.
    assert apply_type_scope(result, None, None) is result


def test_orchestrator_launch_serves_filtered_summary(store, monkeypatch):
    """End-to-end: orchestrator /api/launch-diff-ui with include_types
    spawns env-compare ui → real diff server → filtered summary."""
    monkeypatch.setattr(orch, "WORKSPACES_DIR", store / "ws")
    monkeypatch.setattr(orch, "WORKSPACES_PATH", store / "ws/workspaces.json")

    project = store / "project"
    left, right = project / "left", project / "right"
    _write(left / "classes/A.cls", "public class A { Integer v = 1; }\n")
    _write(right / "classes/A.cls", "public class A { Integer v = 2; }\n")
    _write(left / "reports/R.report", "<Report><x>L</x></Report>")
    _write(right / "reports/R.report", "<Report><x>R</x></Report>")

    server = UIHTTPServer(("127.0.0.1", 0), orch.Handler)
    server.session_token = secrets.token_urlsafe(32)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    token = server.session_token
    spawned_pid = None
    diff_url = None
    # The orchestrator requires a concrete port — reserve then release an
    # ephemeral one for the child diff server.
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        diff_port = s.getsockname()[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request(
            "POST", "/api/launch-diff-ui",
            body=json.dumps({
                "repo_root": str(project),
                "left": str(left),
                "right": str(right),
                "port": diff_port,
                "include_types": ["classes"],
            }),
            headers={
                "Host": f"127.0.0.1:{port}",
                "X-MCT-Token": token,
                "Content-Type": "application/json",
            },
        )
        res = conn.getresponse()
        body = json.loads(res.read())
        conn.close()
        assert res.status == 200 and body.get("ok"), body
        spawned_pid = body.get("pid")
        diff_url = body["url"]

        # Bootstrap the diff server's session token from its index page.
        conn = http.client.HTTPConnection("127.0.0.1", int(diff_url.rsplit(":", 1)[1]), timeout=20)
        conn.request("GET", "/", headers={"Host": diff_url.split("//", 1)[1]})
        html = conn.getresponse().read().decode()
        conn.close()
        marker = html.split("__MCT_TOKEN__", 1)[1]
        diff_token = marker.split('"', 2)[1]

        conn = http.client.HTTPConnection("127.0.0.1", int(diff_url.rsplit(":", 1)[1]), timeout=20)
        conn.request(
            "GET", "/api/summary",
            headers={"Host": diff_url.split("//", 1)[1], "X-MCT-Token": diff_token},
        )
        res = conn.getresponse()
        summary = json.loads(res.read())
        conn.close()
        assert res.status == 200, summary
        assert [d["path"] for d in summary["differ"]] == ["classes/A.cls"]
        assert summary["scope"]["include_types"] == ["classes"]
        assert summary["different_count"] == 1
    finally:
        server.shutdown()
        if spawned_pid:
            try:
                os.kill(spawned_pid, 15)
            except OSError:
                pass
        # The spawned diff server is a grandchild — find it by port and stop
        # it so no stray server outlives the test.
        if diff_url:
            subprocess.run(
                ["sh", "-c", f"lsof -ti tcp:{diff_url.rsplit(':', 1)[1]} | xargs kill"],
                capture_output=True,
            )


# ===========================================================================
# Finding 6 — every active changed file reaches the offline report
# ===========================================================================

def test_binary_only_change_is_listed(store):
    left, right = _pair(store)
    _write(left / "staticresources/logo.png", b"\x89PNG\x00\x01")
    _write(right / "staticresources/logo.png", b"\x89PNG\x00\x02")
    handler, _c = _handler(store, None, left, right)
    html = ui._build_standalone_report(handler)
    assert "(no changed files)" not in html
    assert "staticresources/logo.png" in html
    assert "binary" in html.lower()
    assert "bytes" in html  # size/hash detail


def test_mixed_text_and_binary_report_lists_every_active_change(store):
    left, right = _pair(store)  # text change in classes/A.cls
    _write(left / "staticresources/logo.png", b"\x89PNG\x00\x01")
    _write(right / "staticresources/logo.png", b"\x89PNG\x00\x02")
    handler, _c = _handler(store, None, left, right)
    html = ui._build_standalone_report(handler)
    changed_section = html.split("Changed files (active)", 1)[1].split("<h2>", 1)[0]
    assert "classes/A.cls" in changed_section
    assert "staticresources/logo.png" in changed_section


def test_accepted_binary_stays_in_accepted_section(store, monkeypatch):
    left, right = _pair(store)
    lp = _write(left / "staticresources/logo.png", b"\x89PNG\x00\x01")
    rp = _write(right / "staticresources/logo.png", b"\x89PNG\x00\x02")
    bl_file = store / "baseline.json"
    baseline.accept_diff(
        "staticresources/logo.png", lp, rp, baseline_file=bl_file
    )
    monkeypatch.setattr(ui, "BASELINE_FILE", bl_file)
    ui.get_comparison.cache_clear()
    handler, _c = _handler(store, monkeypatch, left, right)
    html = ui._build_standalone_report(handler)
    accepted_section = html.split("Accepted diffs", 1)[1]
    assert "staticresources/logo.png" in accepted_section
    # The changed-files section ends at the next <h2> — the accepted binary
    # must appear only in the accepted list, never as an unlabeled active diff.
    changed_section = html.split("Changed files (active)", 1)[1].split("<h2>", 1)[0]
    assert "staticresources/logo.png" not in changed_section
    assert "classes/A.cls" in changed_section


def test_missing_side_renders_explanatory_entry(store):
    left, right = _pair(store)
    handler, _c = _handler(store, None, left, right)
    ui.get_comparison(left, right)
    (left / "classes/A.cls").unlink()  # vanished after the comparison ran
    html = ui._build_standalone_report(handler)
    assert "classes/A.cls" in html
    assert "missing or unreadable" in html.lower()


def test_summary_flags_binary_differ_entries(store):
    left, right = _pair(store)
    _write(left / "staticresources/logo.png", b"\x89PNG\x00\x01")
    _write(right / "staticresources/logo.png", b"\x89PNG\x00\x02")
    summary = ui.build_summary(left, right, "left", "right")
    by_path = {d["path"]: d for d in summary["differ"]}
    assert by_path["staticresources/logo.png"]["binary"] is True
    assert "binary" not in by_path["classes/A.cls"]


# ===========================================================================
# Follow-up — ancestor links, shared read policy, scoped metrics, cache key
# ===========================================================================

from mct.retrieved_folder_compare import (
    check_metadata_read,
    read_metadata_bytes,
)


def _swap_dir_for_link(root: Path, dirname: str, target: Path) -> None:
    """Replace <root>/<dirname> with a symlink to *target* (ancestor swap —
    the leaf inside keeps its spelling and is NOT itself a link)."""
    (root / dirname).rename(root / f"{dirname}.original")
    (root / dirname).symlink_to(target, target_is_directory=True)


def test_check_metadata_read_rejects_ancestor_link(tmp_path):
    root = tmp_path / "left"
    _write(root / "classes/A.cls", "local")
    outside = tmp_path / "outside"
    _write(outside / "A.cls", MARKER)
    _swap_dir_for_link(root, "classes", outside)
    with pytest.raises(LinkedMetadataError):
        check_metadata_read(root, root / "classes/A.cls")


def test_check_metadata_read_rejects_link_pointing_inside_root(tmp_path):
    """A link that resolves INSIDE the root still fails — containment alone
    is not sufficient; the link component itself is the violation."""
    root = tmp_path / "left"
    _write(root / "realdir/A.cls", "local")
    _write(root / "classes/A.cls", "local")
    (root / "classes").rename(root / "classes.original")
    (root / "classes").symlink_to(root / "realdir", target_is_directory=True)
    with pytest.raises(LinkedMetadataError):
        check_metadata_read(root, root / "classes/A.cls")


def test_check_metadata_read_rejects_traversal(tmp_path):
    root = tmp_path / "left"
    root.mkdir()
    outside = _write(tmp_path / "outside.txt", MARKER)
    with pytest.raises(LinkedMetadataError):
        check_metadata_read(root, root / ".." / "outside.txt")
    with pytest.raises(LinkedMetadataError):
        check_metadata_read(root, outside)


def test_check_metadata_read_accepts_root_alias_spelling(tmp_path):
    """Links at/above the root (system aliases) are not metadata policy:
    an unresolved spelling of the root still verifies cleanly."""
    root = tmp_path / "left"
    _write(root / "classes/A.cls", "local")
    resolved = root.resolve()
    p = resolved / "classes/A.cls"
    assert check_metadata_read(resolved, p) == p.resolve()
    # Relative spelling resolves under the root.
    assert check_metadata_read(
        resolved, Path("classes/A.cls")
    ) == p.resolve()


def test_read_metadata_bytes_roundtrip_and_refusal(tmp_path):
    root = tmp_path / "left"
    _write(root / "classes/A.cls", "local-bytes")
    assert read_metadata_bytes(root, root / "classes/A.cls") == b"local-bytes"
    outside = tmp_path / "outside"
    _write(outside / "A.cls", MARKER)
    _swap_dir_for_link(root, "classes", outside)
    with pytest.raises(LinkedMetadataError):
        read_metadata_bytes(root, root / "classes/A.cls")


def test_file_diff_refuses_ancestor_link(store):
    left, right = _pair(store)
    ui.get_comparison(left, right)  # warm the cache before the swap
    outside = store / "outside"
    _write(outside / "A.cls", MARKER)
    _swap_dir_for_link(left, "classes", outside)
    result = ui.file_diff(
        left / "classes/A.cls", right / "classes/A.cls",
        left_root=left, right_root=right,
    )
    assert result["error"]
    assert "link" in result["error"].lower()
    assert MARKER not in result["diff"]


def test_serve_file_content_refuses_ancestor_link(store):
    left, right = _pair(store)
    handler, captured = _handler(store, None, left, right)
    outside = store / "outside"
    _write(outside / "Only.cls", MARKER)
    _swap_dir_for_link(left, "classes", outside)
    handler._serve_file_content(left / "classes/Only.cls", "left")
    assert captured["json_body"]["error"]
    assert MARKER not in json.dumps(captured["json_body"])


def test_report_entry_for_ancestor_linked_file_is_explained(store):
    left, right = _pair(store)
    handler, _c = _handler(store, None, left, right)
    ui.get_comparison(left, right)
    outside = store / "outside"
    _write(outside / "A.cls", MARKER)
    _swap_dir_for_link(left, "classes", outside)
    html = ui._build_standalone_report(handler)
    assert "classes/A.cls" in html
    assert "symbolic link" in html.lower()
    assert MARKER not in html


def test_left_context_dirs_rejects_ancestor_link(store):
    left, right = _pair(store)
    handler, _c = _handler(store, None, left, right)
    outside = store / "outside"
    _write(outside / "A.cls", MARKER)
    _swap_dir_for_link(left, "classes", outside)
    with pytest.raises(LinkedMetadataError):
        handler._left_context_dirs(["classes/A.cls"])


def test_validate_temp_project_refuses_ancestor_link(store):
    left, right = _pair(store)
    outside = store / "outside"
    _write(outside / "A.cls", MARKER)
    _swap_dir_for_link(left, "classes", outside)
    with pytest.raises(LinkedMetadataError):
        validate._write_temp_project(
            [(left / "classes/A.cls", "classes/A.cls")],
            "60.0", False, trusted_root=left,
        )


def test_fingerprint_refuses_ancestor_link(store):
    left, right = _pair(store)
    outside = store / "outside"
    _write(outside / "A.cls", MARKER)
    _swap_dir_for_link(left, "classes", outside)
    with pytest.raises(baseline.FingerprintReadError):
        baseline.compute_fingerprint(
            left / "classes/A.cls", right / "classes/A.cls",
            left_root=left, right_root=right,
        )


def test_scoped_metrics_reconcile_with_scope(store):
    """Scoped totals/identical count reflect only in-scope files; whole-tree
    indexes remain complete for component expansion."""
    left, right = _typed_trees(store)  # classes+reports+triggers all differ
    _write(left / "layouts/L.layout", "<layout>x</layout>")
    _write(right / "layouts/L.layout", "<layout>x</layout>")  # identical
    result = compare_trees(left, right)
    scoped = apply_type_scope(result, ["classes"], None)
    # Whole-tree internals preserved
    assert len(scoped.left_ix) == len(result.left_ix)
    assert scoped.identical_count == result.identical_count
    # Scoped reporting values
    assert scoped.visible_total_left == 1
    assert scoped.visible_total_right == 1
    assert scoped.visible_identical_count == 0
    # Unscoped results report full-tree values through the same accessors
    assert result.visible_total_left == len(result.left_ix)
    assert result.visible_identical_count == result.identical_count


def test_scoped_identical_counts_normalized_pairs(store):
    """An in-scope file identical only after normalization counts as
    identical, not differ — same semantics as the unscoped summary."""
    left, right = _pair(store)
    _write(left / "classes/B.cls", "same\r\n")
    _write(right / "classes/B.cls", "same\n")  # line-ending-only diff
    result = apply_type_scope(compare_trees(left, right), ["classes"], None)
    assert result.visible_identical_count == 1  # B.cls normalized-identical
    assert len(result.differ_pairs) == 1        # A.cls still differs


def test_summary_reports_scope_and_whole_tree_separately(store, monkeypatch):
    left, right = _typed_trees(store)
    monkeypatch.setattr(ui, "INCLUDE_TYPES", frozenset({"classes"}))
    summary = ui.build_summary(left, right, "left", "right")
    assert summary["total_left"] == 1 and summary["total_right"] == 1
    assert summary["identical_count"] == 0
    assert summary["whole_tree"]["total_left"] == 3
    assert summary["whole_tree"]["identical_count"] == 0


def test_history_records_scoped_metrics(store, monkeypatch):
    """record_comparison must store the scoped view — a filtered compare
    must not write whole-tree denominators into trend history."""
    import mct.index as _idx

    left, right = _typed_trees(store)
    result = apply_type_scope(compare_trees(left, right), ["classes"], None)
    rec = _idx.record_comparison("left", "right", result)
    assert rec is not None
    assert rec.total_left == 1 and rec.total_right == 1
    assert rec.identical_count == 0
    assert rec.different_count == 1


def test_run_diff_json_payload_uses_scoped_metrics(store, capsys):
    left, right = _typed_trees(store)
    rc = comparison.run_diff(
        str(left), str(right), True, True, include_types=["classes"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["total_left"] == 1
    assert payload["identical_count"] == 0
    assert payload["different_count"] == 1
