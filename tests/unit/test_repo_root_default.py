"""Default --repo-root resolution and DX-project validation.

After ``pipx install`` the package lives in site-packages, so the historical
fallback to BUNDLE_ROOT pointed git/sf at site-packages. The default must be
the caller's working directory, and commands that need a DX project must say
so clearly instead of failing with a misleading ``Branch not found``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import mct.config as _cfg
from mct.cli import main


@pytest.fixture(autouse=True)
def _restore_repo_root():
    saved = _cfg.PROJECT_ROOT
    yield
    _cfg.apply_repo_root(str(saved))


def test_default_repo_root_is_cwd_not_bundle_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cfg.apply_repo_root(None)
    assert _cfg.PROJECT_ROOT == tmp_path.resolve()
    assert _cfg.PROJECT_ROOT != _cfg.BUNDLE_ROOT.resolve()


def test_default_repo_root_is_cwd_in_subprocess(tmp_path):
    script = "import mct.config as c; c.apply_repo_root(None); print(c.PROJECT_ROOT)"
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent)},
    ).stdout.strip()
    assert Path(out) == tmp_path.resolve()


@pytest.mark.parametrize(
    "argv",
    [
        ["snapshot-branch", "--branch", "main"],
        ["snapshot-org-from-source", "--branch", "main", "--org", "x"],
        ["snapshot-org-bidirectional", "--branch", "main", "--org", "x"],
        ["snapshot-all", "--branch", "main", "--org", "x"],
    ],
)
def test_dx_commands_fail_clearly_outside_dx_project(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mct", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "sfdx-project.json" in err
    assert "--repo-root" in err
    assert str(tmp_path.resolve()) in err
    assert "Branch not found" not in err


def test_dx_check_passes_when_sfdx_project_present(tmp_path, monkeypatch, capsys):
    (tmp_path / "sfdx-project.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mct", "snapshot-branch", "--branch", "definitely-missing-branch"])
    with pytest.raises(SystemExit) as exc:
        main()
    # Past the project check: the ordinary branch lookup runs and fails (exit 1).
    assert exc.value.code == 1
    assert "sfdx-project.json" not in capsys.readouterr().err


def test_list_works_outside_dx_project(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mct", "list", "--json"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert '"snapshots"' in capsys.readouterr().out


def test_top_level_help_mentions_start_and_cwd_default(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mct", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "mct start" in out
    assert "current directory" in out.lower()
    assert "this repository root" not in out
    assert ".metadata-compare/" not in out


def test_start_help_uses_mct_prog(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["mct", "start", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: mct start" in out
    assert "serve-orchestrator-ui.py" not in out
