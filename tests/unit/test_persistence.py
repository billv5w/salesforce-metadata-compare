"""Phase 7 regressions: portable, externalized persistence.

- Data lives in a per-user data dir (MCT_DATA_DIR or platform location),
  never inside the installed package.
- File locking works without fcntl (Windows import path).
- Read-modify-write transactions hold one lock — concurrent writes aren't
  lost, and failed atomic writes leave the previous JSON intact.
- Legacy stores migrate explicitly and non-destructively.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

import mct.baseline as _bl
import mct.config as _cfg
import mct.index as _index
import mct.migration as _mig


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point all config globals at a temp data root."""
    monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
    monkeypatch.setattr(_cfg, "STATE_DIR", tmp_path / "store")
    monkeypatch.setattr(_cfg, "INDEX_PATH", tmp_path / "store" / "snapshots.json")
    monkeypatch.setattr(_cfg, "COMPARISON_INDEX_PATH", tmp_path / "store" / "comparisons.json")
    return tmp_path


class TestDataLocation:
    def test_mct_data_dir_overrides_storage_root(self, tmp_path, monkeypatch):
        data = tmp_path / "custom-data"
        monkeypatch.setenv("MCT_DATA_DIR", str(data))
        _cfg.apply_repo_root(str(tmp_path / "proj"))
        assert str(_cfg.STORAGE_ROOT).startswith(str(data.resolve()))
        # project-key separation preserved
        assert _cfg.STORAGE_ROOT.name == _cfg._storage_key(tmp_path / "proj")

    def test_project_keys_still_separate_projects(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCT_DATA_DIR", str(tmp_path / "d"))
        _cfg.apply_repo_root(str(tmp_path / "a"))
        key_a = _cfg.STORAGE_ROOT.name
        _cfg.apply_repo_root(str(tmp_path / "b"))
        assert _cfg.STORAGE_ROOT.name != key_a
        _cfg.apply_repo_root(str(tmp_path / "a"))
        assert _cfg.STORAGE_ROOT.name == key_a

    def test_default_data_root_outside_package(self):
        root = _cfg.data_root()
        assert _cfg.BUNDLE_ROOT.resolve() not in root.resolve().parents

    def test_storage_consistent_across_processes(self, tmp_path):
        """Two interpreter processes with the same MCT_DATA_DIR resolve the
        same index — CLI, orchestrator, and diff subprocesses agree."""
        env = dict(os.environ, MCT_DATA_DIR=str(tmp_path / "d"),
                   PYTHONPATH=str(REPO_ROOT))
        script = "import mct.config as c; c.apply_repo_root('/tmp/proj'); print(c.INDEX_PATH)"
        a = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True, check=True)
        b = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True, check=True)
        assert a.stdout.strip() == b.stdout.strip()
        assert str(tmp_path / "d") in a.stdout

    def test_data_survives_reinstall_location(self, tmp_path):
        """A snapshot written by one install location is readable by another —
        the data dir doesn't depend on where the package is installed."""
        copied = tmp_path / "reinstalled" / "mct"
        copied.parent.mkdir(parents=True)
        shutil.copytree(REPO_ROOT / "mct", copied)
        env_a = dict(os.environ, MCT_DATA_DIR=str(tmp_path / "d"),
                     PYTHONPATH=str(REPO_ROOT))
        env_b = dict(os.environ, MCT_DATA_DIR=str(tmp_path / "d"),
                     PYTHONPATH=str(tmp_path / "reinstalled"))
        proj = str(tmp_path / "proj")
        write = (
            "import mct.config as c, mct.index as i;"
            f"c.apply_repo_root({proj!r});"
            "i.register_snapshot(i.Snapshot('s1','branch','t','p'))"
        )
        subprocess.run([sys.executable, "-c", write], env=env_a,
                       capture_output=True, text=True, check=True)
        read = (
            "import mct.config as c, mct.index as i, json;"
            f"c.apply_repo_root({proj!r});"
            "print(json.dumps(i.load_index()))"
        )
        out = subprocess.run([sys.executable, "-c", read], env=env_b,
                             capture_output=True, text=True, check=True)
        assert json.loads(out.stdout)["snapshots"][0]["id"] == "s1"

    def test_import_without_fcntl(self):
        """Windows has no fcntl — the module must still import (locks fall
        back to msvcrt there). Simulated by blocking the fcntl import."""
        script = (
            "import sys, importlib.abc\n"
            "class Blocker(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'fcntl':\n"
            "            raise ImportError('blocked fcntl')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "import mct.index\n"
            "print('import-ok')\n"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        out = subprocess.run([sys.executable, "-c", script], env=env,
                             capture_output=True, text=True)
        assert "import-ok" in out.stdout, out.stderr


class TestConcurrentTransactions:
    def test_concurrent_snapshot_registration_loses_none(self, store):
        def reg(i):
            _index.register_snapshot(
                _index.Snapshot(f"snap-{i}", "branch", "t", f"p{i}")
            )

        threads = [threading.Thread(target=reg, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {r["id"] for r in _index.load_index()["snapshots"]}
        assert ids == {f"snap-{i}" for i in range(10)}

    def test_concurrent_accept_diff_loses_none(self, store, tmp_path):
        baseline_file = tmp_path / "baseline.json"
        f = tmp_path / "f.txt"
        f.write_text("content\n")

        def accept(i):
            _bl.accept_diff(
                f"path-{i}.txt", f, None, baseline_file=baseline_file
            )

        threads = [threading.Thread(target=accept, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        accepted = _bl.load_baseline(baseline_file)["accepted"]
        assert set(accepted) == {f"path-{i}.txt" for i in range(8)}

    def test_failed_write_preserves_previous_json(self, store, monkeypatch):
        _index.register_snapshot(_index.Snapshot("s1", "branch", "t", "p"))
        before = _cfg.INDEX_PATH.read_bytes()

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(_index.os, "replace", boom)
        with pytest.raises(OSError):
            _index.register_snapshot(_index.Snapshot("s2", "branch", "t", "p"))
        assert _cfg.INDEX_PATH.read_bytes() == before
        assert not _cfg.INDEX_PATH.with_suffix(".tmp").exists()


class TestMigration:
    def _legacy_tree(self, root: Path):
        key = "abc123def456"
        store = root / key
        (store / "snap-a").mkdir(parents=True)
        (store / "snap-a" / "f.txt").write_text("data\n")
        (store / "snapshots.json").write_text(
            json.dumps({"version": 1, "snapshots": [{"id": "s1", "path": "snap-a"}]})
        )
        (store / "baseline.json").write_text(json.dumps(_bl.default_baseline()))
        return key, store

    def test_migrate_copies_and_preserves_source(self, tmp_path):
        legacy = tmp_path / "legacy"
        key, _ = self._legacy_tree(legacy)
        dest = tmp_path / "new"
        result = _mig.migrate(legacy_root=legacy, data_root=dest)
        assert result["migrated"] == [key]
        assert (dest / "snapshot-store" / key / "snapshots.json").is_file()
        # Source untouched — non-destructive.
        assert (legacy / key / "snapshots.json").is_file()
        assert (legacy / key / "snap-a" / "f.txt").read_text() == "data\n"

    def test_migrate_never_overwrites_conflict(self, tmp_path):
        legacy = tmp_path / "legacy"
        key, _ = self._legacy_tree(legacy)
        dest = tmp_path / "new"
        existing = dest / "snapshot-store" / key
        existing.mkdir(parents=True)
        (existing / "snapshots.json").write_text('{"version": 1, "snapshots": []}')
        result = _mig.migrate(legacy_root=legacy, data_root=dest)
        assert result["skipped_conflicts"] == [key]
        assert result["migrated"] == []
        # Pre-existing destination content preserved verbatim.
        assert json.loads((existing / "snapshots.json").read_text())["snapshots"] == []

    def test_migrate_reports_nothing_when_no_legacy(self, tmp_path):
        result = _mig.migrate(legacy_root=tmp_path / "absent", data_root=tmp_path / "new")
        assert result["migrated"] == []
        assert result["errors"] == []

    def test_interrupted_migration_retries_cleanly(self, tmp_path, monkeypatch):
        """R3: a copy interrupted after the index but before its artifacts
        must not leave a destination that blocks retry as a conflict."""
        legacy = tmp_path / "legacy"
        key, _ = self._legacy_tree(legacy)
        dest = tmp_path / "new"
        real_copy = _mig.shutil.copytree

        def partial(src_dir, dst_dir, *args, **kwargs):
            (Path(dst_dir)).mkdir(parents=True, exist_ok=True)
            (Path(dst_dir) / "snapshots.json").write_text(
                (Path(src_dir) / "snapshots.json").read_text()
            )
            raise OSError("simulated interruption")

        monkeypatch.setattr(_mig.shutil, "copytree", partial)
        first = _mig.migrate(legacy_root=legacy, data_root=dest)
        assert first["errors"]
        # No published partial destination.
        assert not (dest / "snapshot-store" / key).exists()

        monkeypatch.setattr(_mig.shutil, "copytree", real_copy)
        again = _mig.migrate(legacy_root=legacy, data_root=dest)
        assert again["migrated"] == [key]
        assert again["errors"] == []
        assert (dest / "snapshot-store" / key / "snap-a" / "f.txt").read_text() == "data\n"

    def test_failed_verification_removes_staging(self, tmp_path, monkeypatch):
        """Content verification runs before publish — a tampered copy is
        reported and cleaned up, never renamed into place."""
        legacy = tmp_path / "legacy"
        key, _ = self._legacy_tree(legacy)
        dest = tmp_path / "new"
        real_copy = _mig.shutil.copytree

        def corrupt(src_dir, dst_dir, *args, **kwargs):
            real_copy(src_dir, dst_dir, *args, **kwargs)
            (Path(dst_dir) / "snap-a" / "f.txt").write_text("corrupted\n")

        monkeypatch.setattr(_mig.shutil, "copytree", corrupt)
        result = _mig.migrate(legacy_root=legacy, data_root=dest)
        assert result["errors"]
        assert result["migrated"] == []
        assert not (dest / "snapshot-store" / key).exists()
        # No leftover staging dirs either.
        assert not list((dest / "snapshot-store").glob(".*mct-staging*"))

    def test_migrate_cli_json_output(self, tmp_path):
        legacy = tmp_path / "legacy"
        key, _ = self._legacy_tree(legacy)
        env = dict(os.environ, MCT_DATA_DIR=str(tmp_path / "new"),
                   PYTHONPATH=str(REPO_ROOT))
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "env-compare.py"),
             "migrate", "--legacy-root", str(legacy), "--json"],
            env=env, capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        payload = json.loads(out.stdout)  # --json stays machine-readable
        assert payload["snapshot_store"]["migrated"] == [key]

    def test_legacy_detection_without_home_scan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mig._cfg, "BUNDLE_ROOT", tmp_path)
        key, _ = self._legacy_tree(
            tmp_path / ".metadata-compare" / "snapshot-store"
        )
        assert _mig.detect_legacy_projects() == [key]


class TestWorkspacesPath:
    def test_mct_config_dir_honored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCT_CONFIG_DIR", str(tmp_path / "cfg"))
        assert _cfg.workspaces_path() == tmp_path / "cfg" / "workspaces.json"

    def test_legacy_workspaces_preserved(self, tmp_path, monkeypatch):
        """An existing ~/.config/mct/workspaces.json keeps its location until
        explicitly migrated."""
        monkeypatch.delenv("MCT_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = tmp_path / ".config" / "mct"
        legacy.mkdir(parents=True)
        (legacy / "workspaces.json").write_text('{"workspaces": []}')
        assert _cfg.workspaces_path() == legacy / "workspaces.json"

    def test_new_install_uses_config_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCT_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        path = _cfg.workspaces_path()
        assert ".config/mct" not in str(path) or sys.platform != "darwin"
        assert path.name == "workspaces.json"

    def test_migrate_workspaces_copies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("MCT_CONFIG_DIR", str(tmp_path / "cfg"))
        legacy = tmp_path / ".config" / "mct"
        legacy.mkdir(parents=True)
        (legacy / "workspaces.json").write_text('{"workspaces": [{"id":"w1"}]}')
        result = _mig.migrate_workspaces()
        assert result["migrated"] is True
        assert json.loads((tmp_path / "cfg" / "workspaces.json").read_text()) == {
            "workspaces": [{"id": "w1"}]
        }
        # Legacy source preserved.
        assert (legacy / "workspaces.json").is_file()

    def test_migration_switches_writes_to_new_location(self, tmp_path, monkeypatch):
        """R7: after migrate_workspaces succeeds, workspaces_path() must
        resolve to the migrated copy — otherwise later writes keep going to
        the legacy file and the advertised new copy goes stale."""
        monkeypatch.delenv("MCT_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = tmp_path / ".config" / "mct" / "workspaces.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"workspaces": []}')
        assert _cfg.workspaces_path() == legacy  # pre-migration preference

        assert _mig.migrate_workspaces()["migrated"]

        expected = _cfg.config_root() / "workspaces.json"
        assert _cfg.workspaces_path() == expected
        assert expected.read_text() == '{"workspaces": []}'
        # The preserved legacy file is a backup — still present, still byte-identical.
        assert legacy.read_text() == '{"workspaces": []}'

    def test_interrupted_workspace_migration_keeps_legacy_and_retries(
        self, tmp_path, monkeypatch
    ):
        """S2: a copy interrupted mid-write must not activate a truncated
        workspaces.json — workspaces_path() prefers the destination the
        moment it exists, so it may only appear fully written."""
        monkeypatch.delenv("MCT_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(_cfg, "config_root", lambda: tmp_path / "new-config")
        legacy = tmp_path / ".config" / "mct" / "workspaces.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"version": 1, "workspaces": [{"id": "w1"}]}')

        original_copy = _mig.shutil.copy2

        def interrupted(source, target, *args, **kwargs):
            Path(target).write_text('{"version":', encoding="utf-8")
            raise OSError("simulated interrupted workspace copy")

        monkeypatch.setattr(_mig.shutil, "copy2", interrupted)
        try:
            _mig.migrate_workspaces()
        except OSError:
            pass
        # The truncated destination must not have been published.
        assert _cfg.workspaces_path() == legacy

        monkeypatch.setattr(_mig.shutil, "copy2", original_copy)
        assert _mig.migrate_workspaces()["migrated"]
        dest = _cfg.workspaces_path()
        assert dest == tmp_path / "new-config" / "workspaces.json"
        assert json.loads(dest.read_text()) == json.loads(legacy.read_text())

    def test_workspace_migration_verification_failure_unpublishes(
        self, tmp_path, monkeypatch
    ):
        """A byte-level mismatch after copy must also leave no destination —
        the staged file is removed, legacy stays active."""
        monkeypatch.delenv("MCT_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(_cfg, "config_root", lambda: tmp_path / "new-config")
        legacy = tmp_path / ".config" / "mct" / "workspaces.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"workspaces": []}')

        def corrupt_copy(source, target, *args, **kwargs):
            Path(target).write_text('{"corrupted": true}')

        monkeypatch.setattr(_mig.shutil, "copy2", corrupt_copy)
        result = _mig.migrate_workspaces()
        assert not result["migrated"]
        assert _cfg.workspaces_path() == legacy
        assert not (tmp_path / "new-config" / "workspaces.json").exists()

    def test_workspace_migration_preserves_concurrent_destination(
        self, tmp_path, monkeypatch
    ):
        """T2: a registry created by another writer while migration copies
        must win — publish is atomic no-overwrite (os.link), so the
        destination is reported as a conflict, not clobbered."""
        monkeypatch.delenv("MCT_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(_cfg, "config_root", lambda: tmp_path / "new-config")
        legacy = tmp_path / ".config" / "mct" / "workspaces.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"workspaces":[{"id":"legacy"}]}')
        destination = tmp_path / "new-config" / "workspaces.json"
        newer_registry = '{"workspaces":[{"id":"created-during-migration"}]}'
        original_copy = _mig.shutil.copy2

        def concurrent_writer(source, staging, *args, **kwargs):
            original_copy(source, staging, *args, **kwargs)
            destination.write_text(newer_registry, encoding="utf-8")

        monkeypatch.setattr(_mig.shutil, "copy2", concurrent_writer)
        result = _mig.migrate_workspaces()

        assert destination.read_text() == newer_registry
        assert not result["migrated"]
        assert result["skipped_conflict"]
        # No staging residue either.
        assert not list(destination.parent.glob(".*.mct-staging"))
        # Retry after the conflict clears still works.
        monkeypatch.setattr(_mig.shutil, "copy2", original_copy)
        destination.unlink()
        assert _mig.migrate_workspaces()["migrated"]
        assert destination.read_text() == legacy.read_text()
