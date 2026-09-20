"""Phase 4 regressions: exported delta ZIPs must be internally consistent and
fail closed on uncertainty.

- Every package.xml member needs its source under delta-source/ (including
  components moved out of destructiveChanges into the deploy set).
- When `sf project generate manifest` cannot resolve the destructive set,
  guessing member names for deletions is unsafe — the export fails clearly
  instead of emitting a heuristic destructiveChanges.xml.
- A source file that vanishes between compare and export fails the export
  rather than shipping a ZIP whose manifest lists content it lacks.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import zipfile
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


def _package_xml(members_by_type: dict[str, list[str]], version: str = "66.0") -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{SF_NS}">']
    for t, members in members_by_type.items():
        parts.append("<types>")
        for m in members:
            parts.append(f"<members>{m}</members>")
        parts.append(f"<name>{t}</name></types>")
    parts.append(f"<version>{version}</version></Package>")
    return "\n".join(parts)


def _members(xml_text: str) -> dict[str, set[str]]:
    import xml.etree.ElementTree as ET

    out: dict[str, set[str]] = {}
    root = ET.fromstring(xml_text)
    for t in root.findall(f"{{{SF_NS}}}types"):
        name = t.find(f"{{{SF_NS}}}name").text
        for m in t.findall(f"{{{SF_NS}}}members"):
            out.setdefault(name, set()).add(m.text)
    return out


class _FakeHandler:
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
    _handle_export_manifest = DiffUIHandler._handle_export_manifest
    _handle_export_bundle = DiffUIHandler._handle_export_bundle

    def __init__(self):
        self.served = []

    def _serve_bytes(self, body, status=200, headers=None):
        self.served.append((status, body, headers or {}))

    def _serve_json(self, data, status=200, filename=None):
        import json

        self.served.append(
            (status, json.dumps(data).encode(), {"Content-Type": "application/json"})
        )

    def _read_body(self):
        return self.body


def _mock_sf(monkeypatch, resolver):
    import mct.safety as safety

    def fake_run(cmd, **kwargs):
        assert cmd[:4] == ["sf", "project", "generate", "manifest"]
        batch = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
        out_dir = Path(cmd[cmd.index("-d") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "package.xml").write_text(
            _package_xml(resolver(batch)), encoding="utf-8"
        )

    monkeypatch.setattr(safety, "run", fake_run)


def _default_resolver(batch):
    out: dict[str, list[str]] = {}
    for raw in batch:
        # The code under test passes absolute OS-native paths to sf — on
        # Windows they use backslashes, which real sf accepts; normalize so
        # the fake registry resolves them the same way.
        p = raw.replace("\\", "/").rstrip("/")
        parts = p.split("/")
        if "classes" in parts:
            name = parts[-1]
            if name == "classes":
                continue  # the type dir itself resolves to its members, not a name
            member = name.split(".")[0]  # Foo.cls / Foo.cls-meta.xml -> Foo
            out.setdefault("ApexClass", []).append(member)
        elif "lwc" in parts:
            i = parts.index("lwc")
            if i + 1 < len(parts):
                out.setdefault("LightningComponentBundle", []).append(parts[i + 1])
    return out or {"ApexClass": ["Placeholder"]}


@pytest.fixture
def handler(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "BASELINE_FILE", None)
    monkeypatch.setattr(_mod, "PAIR_KEY", None)
    _mod.get_comparison.cache_clear()
    h = _FakeHandler()
    h.body = {}
    h.left_root = tmp_path / "left"
    h.right_root = tmp_path / "right"
    h.left_rel = "left"
    h.right_rel = "right"
    return h


def _export_zip(handler) -> zipfile.ZipFile:
    DiffUIHandler._handle_export_bundle(handler)
    status, body, headers = handler.served[-1]
    assert status == 200, f"export failed: {status} {body[:200]}"
    assert headers.get("Content-Type") == "application/zip"
    return zipfile.ZipFile(io.BytesIO(body))


class TestBundleCompleteness:
    def _deploy_trees(self, tmp_path):
        left, right = tmp_path / "left", tmp_path / "right"
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
        right.mkdir(parents=True)
        return left, right

    def test_zip_contains_source_for_every_manifest_member(
        self, handler, monkeypatch, tmp_path
    ):
        left, right = self._deploy_trees(tmp_path)
        handler.left_root, handler.right_root = left, right
        _mock_sf(monkeypatch, _default_resolver)

        zf = _export_zip(handler)
        names = set(zf.namelist())
        package = _members(zf.read("package.xml").decode())
        assert package == {
            "ApexClass": {"Foo"},
            "LightningComponentBundle": {"myComp"},
        }
        # Every member's source content is in the bundle.
        assert "delta-source/classes/Foo.cls" in names
        assert "delta-source/classes/Foo.cls-meta.xml" in names
        assert "delta-source/lwc/myComp/myComp.js" in names
        assert "delta-source/lwc/myComp/myComp.html" in names
        assert "delta-source/lwc/myComp/myComp.js-meta.xml" in names
        assert "README.md" in names

    def test_expanded_static_resource_ships_complete(self, handler, monkeypatch, tmp_path):
        """R2: an expanded StaticResource is a directory component — the ZIP
        must contain every file under the dir AND the sibling
        .resource-meta.xml, or the artifact won't convert/deploy."""
        left, right = tmp_path / "left", tmp_path / "right"
        for side, value in ((left, "1"), (right, "2")):
            d = side / "staticresources" / "ReviewAsset"
            d.mkdir(parents=True)
            (d / "app.js").write_text(f"window.value={value};\n")
            (d / "unchanged.txt").write_text("required asset\n")
            (side / "staticresources" / "ReviewAsset.resource-meta.xml").write_text(
                '<StaticResource xmlns="http://soap.sforce.com/2006/04/metadata">'
                "<cacheControl>Public</cacheControl>"
                "<contentType>application/zip</contentType></StaticResource>\n"
            )
        handler.left_root, handler.right_root = left, right

        def resolver(batch):
            return {"StaticResource": ["ReviewAsset"]} if batch else {}

        _mock_sf(monkeypatch, resolver)

        zf = _export_zip(handler)
        names = set(zf.namelist())
        package = _members(zf.read("package.xml").decode())
        assert "ReviewAsset" in package.get("StaticResource", set())
        assert {
            "delta-source/staticresources/ReviewAsset/app.js",
            "delta-source/staticresources/ReviewAsset/unchanged.txt",
            "delta-source/staticresources/ReviewAsset.resource-meta.xml",
        }.issubset(names)

    def test_descriptor_only_static_resource_ships_payload(
        self, handler, monkeypatch, tmp_path
    ):
        """S1: changing only the descriptor (cacheControl) still requires the
        full component — a manifest member + descriptor but no payload is an
        invalid export."""
        left, right = tmp_path / "left", tmp_path / "right"
        for side, cache in ((left, "Private"), (right, "Public")):
            d = side / "staticresources" / "ReviewAsset"
            d.mkdir(parents=True)
            (d / "app.js").write_text("window.value=1;\n")
            (d / "unchanged.txt").write_text("required asset\n")
            (side / "staticresources" / "ReviewAsset.resource-meta.xml").write_text(
                '<StaticResource xmlns="http://soap.sforce.com/2006/04/metadata">'
                f"<cacheControl>{cache}</cacheControl>"
                "<contentType>application/zip</contentType></StaticResource>\n"
            )
        handler.left_root, handler.right_root = left, right

        def resolver(batch):
            return {"StaticResource": ["ReviewAsset"]} if batch else {}

        _mock_sf(monkeypatch, resolver)

        zf = _export_zip(handler)
        names = set(zf.namelist())
        package = _members(zf.read("package.xml").decode())
        assert "ReviewAsset" in package.get("StaticResource", set())
        assert {
            "delta-source/staticresources/ReviewAsset/app.js",
            "delta-source/staticresources/ReviewAsset/unchanged.txt",
            "delta-source/staticresources/ReviewAsset.resource-meta.xml",
        }.issubset(names)

    @pytest.mark.parametrize("changed", ["descriptor", "payload"])
    def test_native_extension_resource_ships_both_halves(
        self, handler, monkeypatch, tmp_path, changed
    ):
        """T1: Logo.png + Logo.resource-meta.xml — whichever half changed,
        the ZIP must contain both, or conversion/deploy is invalid."""
        png = b"\x89PNG\r\n\x1a\n"
        left, right = tmp_path / "left", tmp_path / "right"
        for root in (left, right):
            folder = root / "staticresources"
            folder.mkdir(parents=True)
            (folder / "Logo.png").write_bytes(
                png + (b"changed" if changed == "payload" and root == left else b"")
            )
            cache = "Private" if changed == "descriptor" and root == left else "Public"
            (folder / "Logo.resource-meta.xml").write_text(
                '<StaticResource xmlns="http://soap.sforce.com/2006/04/metadata">'
                f"<cacheControl>{cache}</cacheControl>"
                "<contentType>image/png</contentType></StaticResource>\n"
            )
        handler.left_root, handler.right_root = left, right

        def resolver(batch):
            return {"StaticResource": ["Logo"]} if batch else {}

        _mock_sf(monkeypatch, resolver)

        zf = _export_zip(handler)
        names = set(zf.namelist())
        package = _members(zf.read("package.xml").decode())
        assert "Logo" in package.get("StaticResource", set())
        assert {
            "delta-source/staticresources/Logo.png",
            "delta-source/staticresources/Logo.resource-meta.xml",
        }.issubset(names)
        # Payload bytes preserved exactly.
        assert zf.read("delta-source/staticresources/Logo.png").startswith(png)

    def test_custom_metadata_records_are_independent_components(
        self, handler, monkeypatch, tmp_path
    ):
        """U1: ReviewSettings.* records are independent components —
        selecting one must not pull its siblings into the ZIP even though
        they share a type-name prefix. Covers accepted, ignored, unselected,
        and unchanged siblings."""
        import mct.baseline as baseline

        left, right = tmp_path / "left", tmp_path / "right"
        selected = "customMetadata/ReviewSettings.Selected.md-meta.xml"
        siblings = {
            "accepted": "customMetadata/ReviewSettings.Accepted.md-meta.xml",
            "ignored": "customMetadata/ReviewSettings.Ignored.md-meta.xml",
            "unselected": "customMetadata/ReviewSettings.Unselected.md-meta.xml",
        }
        unchanged = "customMetadata/ReviewSettings.Unchanged.md-meta.xml"
        for root in (left, right):
            for rel in [selected, *siblings.values()]:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    '<CustomMetadata xmlns="http://soap.sforce.com/2006/04/metadata">'
                    f"<label>{rel} {root.name}</label>"
                    "<protected>false</protected></CustomMetadata>\n"
                )
            p = root / unchanged
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                '<CustomMetadata xmlns="http://soap.sforce.com/2006/04/metadata">'
                "<label>same</label><protected>false</protected></CustomMetadata>\n"
            )
        baseline_file = tmp_path / "baseline.json"
        baseline.accept_diff(
            siblings["accepted"],
            left / siblings["accepted"],
            right / siblings["accepted"],
            baseline_file=baseline_file,
        )
        data = baseline.load_baseline(baseline_file)
        data["ignore"]["paths"] = [siblings["ignored"]]
        baseline.save_baseline(data, baseline_file)
        monkeypatch.setattr(_mod, "BASELINE_FILE", baseline_file)
        _mod.get_comparison.cache_clear()

        handler.left_root, handler.right_root = left, right
        handler.body = {"selected_paths": [selected]}

        def resolver(batch):
            return {
                "CustomMetadata": [
                    p.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".md-meta.xml")
                    for p in batch
                ]
            } if batch else {}

        _mock_sf(monkeypatch, resolver)

        zf = _export_zip(handler)
        names = set(zf.namelist())
        package = _members(zf.read("package.xml").decode())
        assert package == {"CustomMetadata": {"ReviewSettings.Selected"}}
        assert f"delta-source/{selected}" in names
        for rel in [*siblings.values(), unchanged]:
            assert f"delta-source/{rel}" not in names, (
                f"component expansion reintroduced {rel}"
            )

    def test_packed_static_resource_descriptor_ships_payload(
        self, handler, monkeypatch, tmp_path
    ):
        """S1 adjacent: a packed (binary) resource whose descriptor changed
        ships the .resource payload via the companion rule."""
        left, right = tmp_path / "left", tmp_path / "right"
        for side, cache in ((left, "Private"), (right, "Public")):
            (side / "staticresources").mkdir(parents=True)
            (side / "staticresources" / "Logo.resource").write_bytes(b"\x89PNG\r\n")
            (side / "staticresources" / "Logo.resource-meta.xml").write_text(
                '<StaticResource xmlns="http://soap.sforce.com/2006/04/metadata">'
                f"<cacheControl>{cache}</cacheControl>"
                "<contentType>image/png</contentType></StaticResource>\n"
            )
        handler.left_root, handler.right_root = left, right

        def resolver(batch):
            return {"StaticResource": ["Logo"]} if batch else {}

        _mock_sf(monkeypatch, resolver)

        zf = _export_zip(handler)
        names = set(zf.namelist())
        package = _members(zf.read("package.xml").decode())
        assert "Logo" in package.get("StaticResource", set())
        assert {
            "delta-source/staticresources/Logo.resource",
            "delta-source/staticresources/Logo.resource-meta.xml",
        }.issubset(names)

    def test_moved_component_sources_ship_in_zip(self, handler, monkeypatch, tmp_path):
        """An org-only file inside a bundle that exists on both sides moves the
        component into package.xml — its left-side source must ship too,
        otherwise the manifest lists a member with no content."""
        left, right = tmp_path / "left", tmp_path / "right"
        shared = {
            "myComp.js": "export default class {}\n",
            "myComp.html": "<template></template>\n",
            "myComp.js-meta.xml": "<LightningComponentBundle/>\n",
        }
        for root in (left, right):
            (root / "lwc" / "myComp").mkdir(parents=True)
            for name, content in shared.items():
                (root / "lwc" / "myComp" / name).write_text(content, encoding="utf-8")
        (right / "lwc" / "myComp" / "extra.css").write_text(".a{}\n", encoding="utf-8")
        handler.left_root, handler.right_root = left, right
        _mock_sf(monkeypatch, _default_resolver)

        zf = _export_zip(handler)
        names = set(zf.namelist())
        package = _members(zf.read("package.xml").decode())
        destructive = (
            _members(zf.read("destructiveChanges.xml").decode())
            if "destructiveChanges.xml" in names
            else {}
        )
        assert "myComp" in package.get("LightningComponentBundle", set())
        assert "myComp" not in destructive.get("LightningComponentBundle", set())
        # The moved component's left source must be in the ZIP — the manifest
        # member must not be left without content.
        assert "delta-source/lwc/myComp/myComp.js" in names
        assert "delta-source/lwc/myComp/myComp.html" in names


class TestSelectionSemantics:
    """R4: an explicit empty selected_paths must export nothing — omitting
    the field selects all active drift; the two must not be conflated."""

    def _trees(self, tmp_path):
        left, right = tmp_path / "left", tmp_path / "right"
        left.mkdir()
        (right / "classes").mkdir(parents=True)
        (right / "classes" / "TargetOnly.cls").write_text(
            "public class TargetOnly {}\n"
        )
        return left, right

    def test_empty_selection_exports_nothing(self, handler, tmp_path):
        left, right = self._trees(tmp_path)
        handler.left_root, handler.right_root = left, right
        deploy, destroy = handler._collect_delta_files([])
        assert deploy == [] and destroy == []

    def test_omitted_selection_means_all_active(self, handler, tmp_path):
        left, right = self._trees(tmp_path)
        handler.left_root, handler.right_root = left, right
        deploy, destroy = handler._collect_delta_files()
        assert destroy != []  # TargetOnly.cls is active right-only drift

    def test_invalid_selection_type_rejected(self, handler, tmp_path):
        left, right = self._trees(tmp_path)
        handler.left_root, handler.right_root = left, right
        with pytest.raises(ValueError):
            handler._collect_delta_files("classes/TargetOnly.cls")
        with pytest.raises(ValueError):
            handler._collect_delta_files([42])

    def test_empty_selection_via_export_api_is_not_destructive(
        self, handler, tmp_path
    ):
        """End-to-end: POST body {"selected_paths": []} produces an empty
        destructive manifest, not the full active drift."""
        left, right = self._trees(tmp_path)
        handler.left_root, handler.right_root = left, right
        handler.body = {"selected_paths": []}
        DiffUIHandler._handle_export_manifest(handler)
        status, body, _ = handler.served[-1]
        import json

        data = json.loads(body)
        assert status == 200
        assert not data["destructive_changes_xml"]


class TestDestructiveFailClosed:
    def _trees(self, tmp_path):
        left, right = tmp_path / "left", tmp_path / "right"
        (right / "classes").mkdir(parents=True)
        (right / "classes" / "OrgOnly.cls").write_text("public class OrgOnly {}\n")
        left.mkdir(parents=True)
        return left, right

    def _fail_sf(self, monkeypatch):
        import mct.safety as safety

        def fake_run(cmd, **kwargs):
            raise RuntimeError("sf not installed")

        monkeypatch.setattr(safety, "run", fake_run)

    def test_manifest_export_fails_when_destructive_unresolved(
        self, handler, monkeypatch, tmp_path
    ):
        """Guessing member names for deletions is unsafe: when the destructive
        set can't be resolved, the export reports an error instead of emitting
        a heuristic destructiveChanges.xml."""
        left, right = self._trees(tmp_path)
        handler.left_root, handler.right_root = left, right
        self._fail_sf(monkeypatch)

        DiffUIHandler._handle_export_manifest(handler)
        status, body, _ = handler.served[-1]
        import json

        data = json.loads(body)
        assert status >= 400
        assert "error" in data
        assert "destructive_changes_xml" not in data

    def test_bundle_export_fails_when_destructive_unresolved(
        self, handler, monkeypatch, tmp_path
    ):
        left, right = self._trees(tmp_path)
        handler.left_root, handler.right_root = left, right
        self._fail_sf(monkeypatch)

        DiffUIHandler._handle_export_bundle(handler)
        status, body, headers = handler.served[-1]
        assert status >= 400
        assert headers.get("Content-Type") != "application/zip"

    def test_deploy_side_failure_still_exports_with_warning(
        self, handler, monkeypatch, tmp_path
    ):
        """Only the destructive side fails closed — a deploy-side resolve
        failure with an empty destroy set keeps the warning+fallback path."""
        left, right = tmp_path / "left", tmp_path / "right"
        (left / "classes").mkdir(parents=True)
        (left / "classes" / "Foo.cls").write_text("public class Foo {}\n")
        right.mkdir(parents=True)
        handler.left_root, handler.right_root = left, right
        self._fail_sf(monkeypatch)

        DiffUIHandler._handle_export_manifest(handler)
        status, body, _ = handler.served[-1]
        import json

        data = json.loads(body)
        assert status == 200
        assert "warning" in data
        assert "package_xml" in data

    def test_vanished_source_file_fails_export(self, handler, monkeypatch, tmp_path):
        """A file deleted between compare and export must fail the export, not
        silently ship a ZIP whose manifest lists content it lacks."""
        left, right = tmp_path / "left", tmp_path / "right"
        (left / "classes").mkdir(parents=True)
        victim = left / "classes" / "Foo.cls"
        victim.write_text("public class Foo {}\n")
        right.mkdir(parents=True)
        handler.left_root, handler.right_root = left, right
        _mock_sf(monkeypatch, _default_resolver)

        # Warm the comparison cache, then remove the file.
        handler._collect_delta_files()
        victim.unlink()

        DiffUIHandler._handle_export_bundle(handler)
        status, body, headers = handler.served[-1]
        assert status >= 400
        assert headers.get("Content-Type") != "application/zip"
