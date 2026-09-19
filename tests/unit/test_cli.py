"""Tests for mct.cli dispatcher."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure repo root is on path so `mct` package is importable from source
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mct.cli import _scripts_dir, main


def test_scripts_dir_resolves():
    """_scripts_dir() must return an existing directory containing both scripts."""
    d = _scripts_dir()
    assert d.is_dir(), f"scripts dir not found: {d}"
    assert (d / "env-compare.py").exists()
    assert (d / "serve-orchestrator-ui.py").exists()


def test_main_start_dispatches_to_orchestrator(monkeypatch):
    """mct start → serve-orchestrator-ui.py with no extra args."""
    captured = {}

    def fake_run_path(path, run_name):
        captured["path"] = path
        captured["argv"] = sys.argv[:]

    monkeypatch.setattr("mct.cli.runpy.run_path", fake_run_path)
    monkeypatch.setattr(sys, "argv", ["mct", "start"])

    main()

    assert captured["path"].endswith("serve-orchestrator-ui.py")
    assert captured["argv"][0].endswith("serve-orchestrator-ui.py")
    assert captured["argv"][1:] == []


def test_main_start_passes_extra_flags(monkeypatch):
    """mct start --port 9000 --no-open → flags forwarded to server script."""
    captured = {}

    def fake_run_path(path, run_name):
        captured["argv"] = sys.argv[:]

    monkeypatch.setattr("mct.cli.runpy.run_path", fake_run_path)
    monkeypatch.setattr(sys, "argv", ["mct", "start", "--port", "9000", "--no-open"])

    main()

    assert captured["argv"][1:] == ["--port", "9000", "--no-open"]


def test_main_no_args_dispatches_to_orchestrator(monkeypatch):
    """mct with no args → same as mct start."""
    captured = {}

    def fake_run_path(path, run_name):
        captured["path"] = path

    monkeypatch.setattr("mct.cli.runpy.run_path", fake_run_path)
    monkeypatch.setattr(sys, "argv", ["mct"])

    main()

    assert captured["path"].endswith("serve-orchestrator-ui.py")


def test_main_list_dispatches_to_env_compare(monkeypatch):
    """`mct list` → env_compare.main() called with sys.argv containing ['list']."""
    captured = {}

    def fake_env_main():
        captured["argv"] = sys.argv[:]
        return 0

    import env_compare
    monkeypatch.setattr(env_compare, "main", fake_env_main)
    monkeypatch.setattr(sys, "argv", ["mct", "list"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert captured["argv"][1:] == ["list"]


def test_main_ui_passes_all_args(monkeypatch):
    """`mct ui --left a --right b` → all args forwarded to env_compare.main()."""
    captured = {}

    def fake_env_main():
        captured["argv"] = sys.argv[:]
        return 0

    import env_compare
    monkeypatch.setattr(env_compare, "main", fake_env_main)
    monkeypatch.setattr(
        sys, "argv", ["mct", "ui", "--left", "abc", "--right", "def"]
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    assert captured["argv"][1:] == ["ui", "--left", "abc", "--right", "def"]


def test_main_subcommand_works_without_preloaded_module(monkeypatch, tmp_path, capsys):
    """The installed CLI must load env-compare.py itself (hyphenated filename —
    a plain `import env_compare` can never succeed). conftest pre-registers the
    module for other tests, which masked exactly this bug in the pipx install."""
    saved = sys.modules.pop("env_compare", None)
    try:
        monkeypatch.setattr(
            sys, "argv",
            ["mct", "--repo-root", str(tmp_path), "list", "--json"],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert '"snapshots"' in capsys.readouterr().out
    finally:
        if saved is not None:
            sys.modules["env_compare"] = saved
