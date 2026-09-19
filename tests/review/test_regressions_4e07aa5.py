"""Fourth review: synthetic export/migration regressions and retrieve isolation.

Run explicitly with pytest. No Salesforce connection or credentials required.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mct import config, migration, snapshot

spec = importlib.util.spec_from_file_location(
    "fourth_review_diff_ui", REPO / "scripts/serve-diff-ui.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(ui, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(ui, "PAIR_KEY", None)
    ui.get_comparison.cache_clear()
    yield
    ui.get_comparison.cache_clear()


@pytest.mark.parametrize("changed", ["descriptor", "payload"])
def test_native_extension_resource_export_contains_both_files(tmp_path, changed):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
        "x8AAwMCAO+aF2kAAAAASUVORK5CYII="
    )
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
            "<contentType>image/png</contentType></StaticResource>",
            encoding="utf-8",
        )
    handler = object.__new__(ui.make_handler(left, right, "left", "right"))
    handler._read_body = lambda: {}
    # The separate review also validates original/exported fixtures with real sf.
    handler._resolve_components_via_sf = lambda paths: (
        {"StaticResource": {"Logo"}} if paths else {}, None
    )
    captured = {}
    handler._serve_bytes = lambda body, status=200, headers=None: captured.update(
        body=body, status=status
    )
    handler._serve_json = lambda body, status=200: captured.update(
        error=body, status=status
    )
    handler._handle_export_bundle()
    assert captured["status"] == 200, captured.get("error")
    with zipfile.ZipFile(io.BytesIO(captured["body"])) as archive:
        expected = {
            "delta-source/staticresources/Logo.png",
            "delta-source/staticresources/Logo.resource-meta.xml",
        }
        assert expected.issubset(archive.namelist()), (
            "successful native-extension resource export omitted its payload or descriptor",
            archive.namelist(),
        )


def test_workspace_migration_preserves_destination_created_during_copy(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MCT_CONFIG_DIR")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config, "config_root", lambda: tmp_path / "new-config")
    legacy = tmp_path / ".config/mct/workspaces.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"workspaces":[{"id":"legacy"}]}', encoding="utf-8")
    destination = config.config_root() / "workspaces.json"
    newer_registry = '{"workspaces":[{"id":"created-during-migration"}]}'
    original_copy = migration.shutil.copy2

    def concurrent_writer(source, staging, *args, **kwargs):
        original_copy(source, staging, *args, **kwargs)
        # Deterministic scheduling: another writer publishes after the initial
        # exists check and before this migration publishes its verified stage.
        destination.write_text(newer_registry, encoding="utf-8")

    monkeypatch.setattr(migration.shutil, "copy2", concurrent_writer)
    result = migration.migrate_workspaces()
    assert destination.read_text(encoding="utf-8") == newer_registry, (
        "migration overwrote a destination created while it was copying",
        result,
    )
    assert not result["migrated"]
    assert result["skipped_conflict"]
    assert json.loads(legacy.read_text())["workspaces"][0]["id"] == "legacy"


def test_simultaneous_retrieves_keep_distinct_staging_and_final_content(
    tmp_path, monkeypatch
):
    """Positive adjacent check for S3; two in-flight retrieves share a base."""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(config, "STORAGE_ROOT", tmp_path / "store")
    monkeypatch.setattr(config, "RETRIEVE_CHUNK_SIZE", 0)
    manifest = tmp_path / "package.xml"
    manifest.write_text(
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
        "<types><members>Fixture</members><name>ApexClass</name></types>"
        "<version>66.0</version></Package>", encoding="utf-8",
    )
    barrier = threading.Barrier(2)
    staging_paths = []

    def fake_retrieve(manifest, org_alias, staging_dir, timeout, api_version):
        staging_paths.append(staging_dir)
        staging_dir.mkdir(parents=True)
        (staging_dir / "marker.txt").write_text(org_alias, encoding="utf-8")
        barrier.wait(timeout=5)
        return [], '{"result":{"success":true}}'

    monkeypatch.setattr(snapshot, "_retrieve_request", fake_retrieve)

    def retrieve(alias):
        return snapshot.retrieve_with_manifest(
            manifest, alias, "same-base", 10, "66.0"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(retrieve, ["fixture-a", "fixture-b"]))
    assert len(set(staging_paths)) == 2
    assert len(set(results)) == 2
    assert [(path / "marker.txt").read_text() for path in results] == [
        "fixture-a", "fixture-b"
    ]
    assert all(not path.exists() for path in staging_paths)
