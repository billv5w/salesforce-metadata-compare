"""Fifth review: preserve metadata component boundaries and migration isolation.

Synthetic fixtures only. No Salesforce credentials or network access required.
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
from mct import baseline, config, validate

spec = importlib.util.spec_from_file_location(
    "fifth_review_diff_ui", REPO / "scripts/serve-diff-ui.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


@pytest.fixture
def records(tmp_path, monkeypatch):
    monkeypatch.setenv("MCT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "STORAGE_ROOT", tmp_path / "store")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "INDEX_PATH", tmp_path / "store/snapshots.json")
    monkeypatch.setattr(config, "COMPARISON_INDEX_PATH", tmp_path / "store/comparisons.json")
    left, right = tmp_path / "left", tmp_path / "right"
    selected = "customMetadata/ReviewSettings.Selected.md-meta.xml"
    accepted = "customMetadata/ReviewSettings.Accepted.md-meta.xml"
    for root in (left, right):
        for relative in (selected, accepted):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '<CustomMetadata xmlns="http://soap.sforce.com/2006/04/metadata">'
                f"<label>{path.name} {root.name}</label>"
                "<protected>false</protected></CustomMetadata>", encoding="utf-8",
            )
    baseline_file = tmp_path / "baseline.json"
    baseline.accept_diff(accepted, left / accepted, right / accepted,
                         baseline_file=baseline_file)
    monkeypatch.setattr(ui, "BASELINE_FILE", baseline_file)
    monkeypatch.setattr(ui, "PAIR_KEY", None)
    ui.get_comparison.cache_clear()
    yield left, right, selected, accepted, baseline_file
    ui.get_comparison.cache_clear()


def test_selected_custom_metadata_export_excludes_accepted_sibling(records):
    left, right, selected, accepted, _ = records
    handler = object.__new__(ui.make_handler(left, right, "left", "right"))
    handler._read_body = lambda: {"selected_paths": [selected]}
    handler._resolve_components_via_sf = lambda paths: (
        {"CustomMetadata": {p.name.removesuffix(".md-meta.xml") for p in paths}}
        if paths else {}, None,
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
        assert "delta-source/" + selected in archive.namelist()
        assert "delta-source/" + accepted not in archive.namelist(), (
            "component expansion reintroduced an accepted, unselected record"
        )


def test_validation_excludes_accepted_custom_metadata_sibling(records, monkeypatch):
    left, right, selected, accepted, baseline_file = records
    current_baseline = baseline.load_baseline(baseline_file)
    monkeypatch.setattr(baseline, "load_baseline", lambda: current_baseline)
    entries = validate._collect_delta(str(left), str(right), True)
    paths = {display for _, display in entries}
    assert selected in paths
    assert accepted not in paths, "dry-run source includes an unrelated accepted record"


def test_independent_workspace_migrations_publish_once_without_shared_staging(tmp_path):
    """Positive check of T2 using two real interpreters synchronized after copy."""
    legacy = tmp_path / "home/.config/mct/workspaces.json"
    legacy.parent.mkdir(parents=True)
    original = '{"version":1,"workspaces":[{"id":"preserved"}]}'
    legacy.write_text(original, encoding="utf-8")
    script = r'''
import json, os, sys, time
from pathlib import Path
from mct import config, migration
root, worker = Path(sys.argv[1]), sys.argv[2]
Path.home = classmethod(lambda cls: root / 'home')
config.config_root = lambda: root / 'config'
original_copy = migration.shutil.copy2

def synchronized_copy(source, staging, *args, **kwargs):
    result = original_copy(source, staging, *args, **kwargs)
    (root / ('ready-' + worker)).write_text(str(staging))
    deadline = time.monotonic() + 5
    while len(list(root.glob('ready-*'))) < 2:
        if time.monotonic() > deadline:
            raise TimeoutError('migration review barrier timed out')
        time.sleep(0.01)
    return result

migration.shutil.copy2 = synchronized_copy
result = migration.migrate_workspaces()
(root / ('result-' + worker + '.json')).write_text(json.dumps(result))
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
    assert sum(result["migrated"] for result in results) == 1, results
    assert sum(result["skipped_conflict"] for result in results) == 1, results
    assert all(result["error"] is None for result in results), results
    assert len({(tmp_path / f"ready-{i}").read_text() for i in range(2)}) == 2
    assert not list((tmp_path / "config").glob("*.mct-staging"))
    assert (tmp_path / "config/workspaces.json").read_text() == original
    assert legacy.read_text() == original
