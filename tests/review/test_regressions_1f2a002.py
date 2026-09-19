"""Independent follow-up regressions. Synthetic fixtures; no Salesforce access.

Run explicitly: python3 -m pytest tests/review/test_regressions_1f2a002.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mct import config, migration

spec = importlib.util.spec_from_file_location(
    "second_review_diff_ui", REPO / "scripts/serve-diff-ui.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


def put(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MCT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(ui, "BASELINE_FILE", None)
    monkeypatch.setattr(ui, "PAIR_KEY", None)
    ui.get_comparison.cache_clear()
    yield
    ui.get_comparison.cache_clear()


def test_descriptor_only_static_resource_export_includes_payload(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    for root, cache in ((left, "Private"), (right, "Public")):
        put(root, "staticresources/Asset/app.js", "window.value = 1;\n")
        put(root, "staticresources/Asset.resource-meta.xml", (
            '<StaticResource xmlns="http://soap.sforce.com/2006/04/metadata">'
            f"<cacheControl>{cache}</cacheControl>"
            "<contentType>application/zip</contentType></StaticResource>"
        ))
    handler = object.__new__(ui.make_handler(left, right, "left", "right"))
    handler._read_body = lambda: {}
    # Resolution is isolated here; the review also uses the actual sf converter.
    handler._resolve_components_via_sf = lambda paths: (
        {"StaticResource": {"Asset"}} if paths else {}, None
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
        assert "delta-source/staticresources/Asset/app.js" in archive.namelist(), (
            "descriptor-only change produced a successful ZIP with no resource payload"
        )


def test_interrupted_workspace_migration_keeps_legacy_active_and_can_retry(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MCT_CONFIG_DIR")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config, "config_root", lambda: tmp_path / "new-config")
    legacy = put(tmp_path, ".config/mct/workspaces.json", json.dumps({
        "version": 1, "workspaces": [{"id": "preserved", "name": "Review"}]
    }))
    original_copy = migration.shutil.copy2

    def interrupted(source, target, *args, **kwargs):
        Path(target).write_text('{"version":', encoding="utf-8")
        raise OSError("injected interrupted workspace copy")

    monkeypatch.setattr(migration.shutil, "copy2", interrupted)
    try:
        migration.migrate_workspaces()
    except OSError:
        pass  # Either structured errors or a raised error can be acceptable.
    assert config.workspaces_path() == legacy, (
        "failed migration switched the application to incomplete workspace JSON"
    )
    monkeypatch.setattr(migration.shutil, "copy2", original_copy)
    assert migration.migrate_workspaces()["migrated"], "partial copy blocked retry"
    assert json.loads(config.workspaces_path().read_text()) == json.loads(legacy.read_text())


def test_two_processes_can_create_same_second_snapshots(tmp_path):
    """Control scheduling after name selection, before publication.

    Two real interpreters each start their process-local sequence at zero.
    The branch source boundary is synthetic; naming, mkdir and registration
    execute the actual code. A reservation/locking fix should remove this race.
    """
    script = r'''
import json, os, sys, time
from pathlib import Path
from mct import config, snapshot
root, worker = Path(sys.argv[1]), sys.argv[2]
os.environ['MCT_DATA_DIR'] = str(root / 'data')
config.apply_repo_root(str(root / 'project'))
config.ts_local = lambda: '20260919-120000+1200'
snapshot.branch_exists = lambda branch: True

def extract(ref, subdir, destination):
    path = destination / subdir
    path.mkdir(parents=True)
    (path / 'Fixture.cls').write_text('public class Fixture {}')

snapshot._extract_archive_to = extract
original_name = snapshot._unique_name

def synchronized_name(directory, filename):
    name = original_name(directory, filename)
    (root / ('ready-' + worker)).touch()
    deadline = time.monotonic() + 5
    while len(list(root.glob('ready-*'))) < 2:
        if time.monotonic() > deadline:
            raise TimeoutError('review barrier timed out')
        time.sleep(0.01)
    return name

snapshot._unique_name = synchronized_name
try:
    result = snapshot.materialize_branch_snapshot('main', 'src', False)
    output = {'ok': True, 'id': result.snapshot_id, 'path': result.path}
except Exception as exc:
    output = {'ok': False, 'error': type(exc).__name__, 'message': str(exc)}
(root / ('result-' + worker + '.json')).write_text(json.dumps(output))
'''
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(i)],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(2)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    results = [json.loads((tmp_path / f"result-{i}.json").read_text()) for i in range(2)]
    assert all(result["ok"] for result in results), results
    assert len({result["id"] for result in results}) == 2
    assert len({result["path"] for result in results}) == 2
