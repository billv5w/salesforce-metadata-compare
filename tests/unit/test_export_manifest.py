"""
Unit tests for serve-diff-ui.py's SDR-backed manifest export:
_resolve_components_via_sf, _serialize_manifest, _parse_manifest_members,
_left_context_dirs, and the destructive-overlap protection.

`sf project generate manifest` calls are mocked (via mct.safety.run) so these tests
don't depend on the Salesforce CLI being installed/authenticated in CI.
"""
from __future__ import annotations

import importlib.util
import sys
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

SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _package_xml(members_by_type: dict[str, list[str]], version: str = "60.0") -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{SF_NS}">']
    for t, members in members_by_type.items():
        parts.append("<types>")
        for m in members:
            parts.append(f"<members>{m}</members>")
        parts.append(f"<name>{t}</name></types>")
    parts.append(f"<version>{version}</version></Package>")
    return "\n".join(parts)


class _FakeHandler:
    """Bare object exposing just the methods under test (avoids constructing a
    real BaseHTTPRequestHandler, which requires a live socket)."""

    api_version = "66.0"
    _MANIFEST_BATCH_SIZE = DiffUIHandler._MANIFEST_BATCH_SIZE
    _resolve_components_via_sf = DiffUIHandler._resolve_components_via_sf
    _serialize_manifest = DiffUIHandler._serialize_manifest
    _parse_manifest_members = staticmethod(DiffUIHandler._parse_manifest_members)
    _left_context_dirs = DiffUIHandler._left_context_dirs
    _build_manifest = DiffUIHandler._build_manifest
    _collect_delta_files = DiffUIHandler._collect_delta_files
    _build_delta_manifests = DiffUIHandler._build_delta_manifests
    _bundle_source_entries = DiffUIHandler._bundle_source_entries


@pytest.fixture
def handler():
    return _FakeHandler()


def _mock_sf(monkeypatch, resolver):
    """Patch mct.safety.run: `resolver(batch_paths)` returns members_by_type
    written as package.xml into the -d dir."""
    import mct.safety as safety

    calls = []

    def fake_run(cmd, **kwargs):
        assert cmd[:4] == ["sf", "project", "generate", "manifest"]
        batch = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
        calls.append(batch)
        out_dir = Path(cmd[cmd.index("-d") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "package.xml").write_text(_package_xml(resolver(batch)), encoding="utf-8")

    monkeypatch.setattr(safety, "run", fake_run)
    return calls


class TestResolveComponents:
    def test_empty_paths_returns_empty_dict_no_error(self, handler):
        members, err = handler._resolve_components_via_sf([])
        assert members == {}
        assert err is None

    def test_resolves_members_by_type(self, handler, monkeypatch, tmp_path):
        _mock_sf(monkeypatch, lambda batch: {"ApexClass": ["Foo"], "CustomField": ["Account.X__c"]})
        members, err = handler._resolve_components_via_sf([tmp_path / "classes" / "Foo.cls"])
        assert err is None
        assert members == {"ApexClass": {"Foo"}, "CustomField": {"Account.X__c"}}

    def test_batches_large_path_lists_and_merges(self, handler, monkeypatch, tmp_path):
        calls = _mock_sf(
            monkeypatch,
            lambda batch: {"ApexClass": [Path(p).stem for p in batch]},
        )
        n = handler._MANIFEST_BATCH_SIZE * 2 + 10
        paths = [tmp_path / "classes" / f"C{i}.cls" for i in range(n)]
        members, err = handler._resolve_components_via_sf(paths)
        assert err is None
        assert len(calls) == 3  # two full batches + remainder
        assert all(len(b) <= handler._MANIFEST_BATCH_SIZE for b in calls)
        assert members["ApexClass"] == {f"C{i}" for i in range(n)}

    def test_sf_failure_returns_error_not_exception(self, handler, monkeypatch, tmp_path):
        import mct.safety as safety

        def fake_run(cmd, **kwargs):
            raise RuntimeError("Command failed (exit 1)")

        monkeypatch.setattr(safety, "run", fake_run)
        members, err = handler._resolve_components_via_sf([tmp_path / "classes" / "Foo.cls"])
        assert members is None
        assert "Command failed" in err

    def test_missing_output_file_returns_error(self, handler, monkeypatch, tmp_path):
        import mct.safety as safety

        monkeypatch.setattr(safety, "run", lambda cmd, **kw: None)  # writes nothing
        members, err = handler._resolve_components_via_sf([tmp_path / "classes" / "Foo.cls"])
        assert members is None
        assert "did not produce" in err

    def test_temp_dirs_cleaned_up(self, handler, monkeypatch, tmp_path):
        import mct.safety as safety

        seen = []

        def fake_run(cmd, **kwargs):
            out_dir = Path(cmd[cmd.index("-d") + 1])
            seen.append(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "package.xml").write_text(_package_xml({"ApexClass": ["A"]}), encoding="utf-8")

        monkeypatch.setattr(safety, "run", fake_run)
        handler._resolve_components_via_sf([tmp_path / "classes" / "A.cls"])
        assert seen and all(not d.exists() for d in seen)


class TestSerializeManifest:
    def test_empty_returns_empty_string(self, handler):
        assert handler._serialize_manifest({}) == ""
        assert handler._serialize_manifest({"ApexClass": set()}) == ""

    def test_sorted_types_and_members_with_api_version(self, handler):
        xml = handler._serialize_manifest({"Flow": {"B_Flow", "A_Flow"}, "ApexClass": {"Zed"}})
        assert xml.index("ApexClass") < xml.index("Flow")
        assert xml.index("A_Flow") < xml.index("B_Flow")
        assert "<version>66.0</version>" in xml
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    def test_roundtrips_through_parser(self, handler):
        original = {"ApexClass": {"Foo", "Bar"}, "CustomField": {"Account.X__c"}}
        xml = handler._serialize_manifest(original)
        parsed: dict[str, set[str]] = {}
        for t, m in handler._parse_manifest_members(xml):
            parsed.setdefault(t, set()).add(m)
        assert parsed == original


class TestLeftContextDirs:
    def test_maps_parent_dirs_that_exist_on_left(self, handler, tmp_path):
        left = tmp_path / "left"
        (left / "lwc" / "myComp").mkdir(parents=True)
        handler.left_root = left
        dirs = handler._left_context_dirs(
            ["lwc/myComp/extra.css", "lwc/gone/x.js", "classes/Foo.cls"]
        )
        assert (left / "lwc" / "myComp").resolve() in dirs
        # neither the missing bundle dir nor the missing classes dir qualifies
        assert len(dirs) == 1

    def test_dedupes_dirs(self, handler, tmp_path):
        left = tmp_path / "left"
        (left / "classes").mkdir(parents=True)
        handler.left_root = left
        dirs = handler._left_context_dirs(["classes/A.cls", "classes/B.cls"])
        assert len(dirs) == 1


class TestDestructiveOverlapProtection:
    """A right-only file inside a component that exists on both sides (extra
    file in an LWC bundle) must NOT produce a destructive entry for the whole
    component — deploying the source component is the correct remediation."""

    def _setup_trees(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        for root in (left, right):
            (root / "lwc" / "myComp").mkdir(parents=True)
            (root / "lwc" / "myComp" / "myComp.js").write_text("export default class {}\n")
        # org-only extra file inside the shared bundle
        (right / "lwc" / "myComp" / "extra.css").write_text(".a{}\n")
        # genuinely org-only component -> legitimate destructive candidate
        (right / "classes").mkdir(parents=True)
        (right / "classes" / "OrgOnly.cls").write_text("public class OrgOnly {}\n")
        return left, right

    def test_bundle_member_moved_from_destructive_to_deploy(self, handler, monkeypatch, tmp_path):
        left, right = self._setup_trees(tmp_path)
        handler.left_root = left
        handler.right_root = right

        def resolver(batch):
            out: dict[str, list[str]] = {}
            for p in batch:
                if "myComp" in p:
                    out.setdefault("LightningComponentBundle", []).append("myComp")
                if "OrgOnly" in p or p.rstrip("/").endswith("classes"):
                    out.setdefault("ApexClass", []).append("OrgOnly")
            return out or {"ApexClass": ["Ignore"]}

        _mock_sf(monkeypatch, resolver)

        captured = {}
        handler._serve_json = lambda data, status=200: captured.update(data)
        handler._read_body = lambda: {}
        _mod.get_comparison.cache_clear()
        DiffUIHandler._handle_export_manifest(handler)

        # Bundle deploys, is NOT destroyed; genuinely org-only class IS destroyed.
        assert "myComp" in captured["package_xml"]
        assert "myComp" not in captured["destructive_changes_xml"]
        assert "OrgOnly" in captured["destructive_changes_xml"]
        assert "LightningComponentBundle:myComp" in captured.get("notes", "")

    def test_bundle_source_expansion(self, handler, tmp_path):
        """A changed .cls pulls in its -meta.xml; a changed bundle file pulls
        in the whole bundle."""
        left = tmp_path / "left"
        right = tmp_path / "right"
        for name, content in [
            ("classes/Foo.cls", "public class Foo {}"),
            ("classes/Foo.cls-meta.xml", "<ApexClass/>"),
            ("lwc/myComp/myComp.js", "export default class {}"),
            ("lwc/myComp/myComp.html", "<template></template>"),
            ("lwc/myComp/myComp.js-meta.xml", "<LightningComponentBundle/>"),
        ]:
            p = left / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        right.mkdir(parents=True)  # empty right → everything is only_left

        handler.left_root = left
        handler.right_root = right
        _mod.get_comparison.cache_clear()

        # Simulate a delta containing just Foo.cls and one bundle file.
        deploy_files = [
            (left / "classes" / "Foo.cls", "classes/Foo.cls"),
            (left / "lwc" / "myComp" / "myComp.js", "lwc/myComp/myComp.js"),
        ]
        entries = handler._bundle_source_entries(deploy_files)
        displays = sorted(d for _, d in entries)
        assert displays == [
            "classes/Foo.cls",
            "classes/Foo.cls-meta.xml",
            "lwc/myComp/myComp.html",
            "lwc/myComp/myComp.js",
            "lwc/myComp/myComp.js-meta.xml",
        ]

    def test_sf_failure_with_destructive_content_fails_closed(
        self, handler, monkeypatch, tmp_path
    ):
        """With org-only files at stake, an unresolvable member set must error —
        folder-name guessing in destructiveChanges.xml can delete the wrong
        component."""
        import mct.safety as safety

        left, right = self._setup_trees(tmp_path)
        handler.left_root = left
        handler.right_root = right

        def fake_run(cmd, **kwargs):
            raise RuntimeError("sf not installed")

        monkeypatch.setattr(safety, "run", fake_run)

        captured = {}

        def _serve(data, status=200):
            captured["status"] = status
            captured.update(data)

        handler._serve_json = _serve
        handler._read_body = lambda: {}
        _mod.get_comparison.cache_clear()
        DiffUIHandler._handle_export_manifest(handler)

        assert captured["status"] >= 400
        assert "error" in captured
        assert "destructive_changes_xml" not in captured

    def test_fallback_to_heuristic_on_sf_failure(self, handler, monkeypatch, tmp_path):
        """Deploy-side-only deltas (nothing org-only) still get the heuristic
        fallback with a warning when `sf` is unavailable."""
        import mct.safety as safety

        left = tmp_path / "left"
        right = tmp_path / "right"
        (left / "classes").mkdir(parents=True)
        (left / "classes" / "Foo.cls").write_text("public class Foo {}\n")
        right.mkdir(parents=True)
        handler.left_root = left
        handler.right_root = right

        def fake_run(cmd, **kwargs):
            raise RuntimeError("sf not installed")

        monkeypatch.setattr(safety, "run", fake_run)

        captured = {}
        handler._serve_json = lambda data, status=200: captured.update(data)
        handler._read_body = lambda: {}
        _mod.get_comparison.cache_clear()
        DiffUIHandler._handle_export_manifest(handler)

        assert "warning" in captured
        assert "ApexClass" in captured["package_xml"] or captured["package_xml"] == ""
        assert captured["destructive_changes_xml"] == ""
