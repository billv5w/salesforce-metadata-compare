"""Phase 9 release acceptance: reruns the original defect reproductions at
behavior level, plus bounded server lifecycle and git-fixture coverage.

These are the checks a release gate reruns after packaging — deliberately
end-to-end (real subprocess server, real git fixture) rather than mocks,
except the Salesforce resolver which is stubbed the same way the unit
suites stub it (real-sf coverage is the separate opt-in acceptance).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
_SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "serve_diff_ui", _SCRIPTS_DIR / "serve-diff-ui.py"
)
_diff_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_diff_mod)

from mct import config as _cfg  # noqa: E402
from mct.comparison import run_compare_matrix  # noqa: E402
from mct.delta import collect_deploy_files  # noqa: E402
from mct.retrieved_folder_compare import compare_trees  # noqa: E402
import mct.baseline as _baseline  # noqa: E402

FLOW = """<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>66.0</apiVersion>
    <decisions>
        <name>check</name>
        <label>Check</label>
        <rules>
            <name>r1</name>
            <label>Rule One</label>
            <conditionLogic>and</conditionLogic>
            <conditions><leftValueReference>a</leftValueReference></conditions>
            <connector><targetReference>out1</targetReference></connector>
        </rules>
        <rules>
            <name>r2</name>
            <label>Rule Two</label>
            <conditionLogic>and</conditionLogic>
            <conditions><leftValueReference>b</leftValueReference></conditions>
            <connector><targetReference>out2</targetReference></connector>
        </rules>
    </decisions>
</Flow>
"""


def _flow_rules_swapped() -> str:
    """Same Flow with the two <rules> blocks exchanged (order matters)."""
    m = re.search(r"(\s*<rules>.*?</rules>)(\s*<rules>.*?</rules>)", FLOW, re.S)
    assert m
    return FLOW[: m.start()] + m.group(2) + m.group(1) + FLOW[m.end() :]


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    left, right = tmp_path / "left", tmp_path / "right"
    for root in (left, right):
        (root / "classes").mkdir(parents=True)
        (root / "flows").mkdir(parents=True)
    return left, right


# ---------------------------------------------------------------------------
# Original reproduction 1: order-sensitive Flow change must be drift
# ---------------------------------------------------------------------------


def test_flow_rule_order_change_is_drift(tmp_path):
    left, right = _trees(tmp_path)
    (left / "flows" / "F.flow-meta.xml").write_text(FLOW)
    (right / "flows" / "F.flow-meta.xml").write_text(_flow_rules_swapped())

    result = compare_trees(left, right)

    differing = {ld for _, _, ld, _ in result.differ_pairs}
    assert "flows/F.flow-meta.xml" in differing
    normalized = {ld for _, _, ld, _ in result.identical_normalized_pairs}
    assert "flows/F.flow-meta.xml" not in normalized


# ---------------------------------------------------------------------------
# Original reproduction 2: accepted-only delta exports nothing
# ---------------------------------------------------------------------------


def test_accepted_only_delta_is_empty(tmp_path):
    left, right = _trees(tmp_path)
    (left / "classes" / "Foo.cls").write_text("public class Foo { int x = 1; }\n")
    (right / "classes" / "Foo.cls").write_text("public class Foo { int x = 2; }\n")

    baseline_file = tmp_path / "baseline.json"
    result = compare_trees(left, right)
    baseline = _baseline.load_baseline(baseline_file)

    deploy, destroy = collect_deploy_files(result, baseline)
    assert deploy or destroy  # the drift is real before acceptance

    _baseline.accept_diff(
        "classes/Foo.cls", left / "classes" / "Foo.cls",
        right / "classes" / "Foo.cls", baseline_file=baseline_file,
    )
    baseline = _baseline.load_baseline(baseline_file)
    deploy, destroy = collect_deploy_files(result, baseline)

    assert deploy == [] and destroy == []


# ---------------------------------------------------------------------------
# Original reproduction 3: compare-matrix errors exit 2 and surface in JSON
# ---------------------------------------------------------------------------


def test_matrix_error_exit_code(tmp_path, capsys):
    left, right = _trees(tmp_path)
    missing = tmp_path / "does-not-exist"
    code = run_compare_matrix(
        [f"{left}:{right}", f"{missing}:{right}"],
        as_json=True,
        fail_on_any_diff=False,
        use_baseline=False,
    )
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["has_errors"] is True


# ---------------------------------------------------------------------------
# Original reproduction 4: promoted (bundle) exports carry member source
# ---------------------------------------------------------------------------


def test_exported_bundle_has_source_for_members(tmp_path, monkeypatch):
    left, right = _trees(tmp_path)
    (left / "classes" / "Foo.cls").write_text("public class Foo {}\n")
    (left / "classes" / "Foo.cls-meta.xml").write_text("<ApexClass/>\n")
    right.mkdir(exist_ok=True)

    import mct.safety as safety

    sf_ns = "http://soap.sforce.com/2006/04/metadata"

    def fake_run(cmd, **kwargs):
        out_dir = Path(cmd[cmd.index("-d") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "package.xml").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?>\n<Package xmlns="{sf_ns}">'
            f"<types><members>Foo</members><name>ApexClass</name></types>"
            f"<version>66.0</version></Package>"
        )

    monkeypatch.setattr(safety, "run", fake_run)
    monkeypatch.setattr(_diff_mod, "BASELINE_FILE", None)
    monkeypatch.setattr(_diff_mod, "PAIR_KEY", None)
    _diff_mod.get_comparison.cache_clear()

    class _H:
        api_version = "66.0"
        _handle_export_bundle = _diff_mod.DiffUIHandler._handle_export_bundle
        _build_delta_manifests = _diff_mod.DiffUIHandler._build_delta_manifests
        _collect_delta_files = _diff_mod.DiffUIHandler._collect_delta_files
        _resolve_components_via_sf = _diff_mod.DiffUIHandler._resolve_components_via_sf
        _serialize_manifest = _diff_mod.DiffUIHandler._serialize_manifest
        _parse_manifest_members = staticmethod(
            _diff_mod.DiffUIHandler._parse_manifest_members
        )
        _left_context_dirs = _diff_mod.DiffUIHandler._left_context_dirs
        _build_manifest = _diff_mod.DiffUIHandler._build_manifest
        _bundle_source_entries = _diff_mod.DiffUIHandler._bundle_source_entries
        _MANIFEST_BATCH_SIZE = _diff_mod.DiffUIHandler._MANIFEST_BATCH_SIZE
        left_root, right_root = left, right
        left_rel, right_rel = "left", "right"
        body = {}

        def __init__(self):
            self.served = []

        def _serve_bytes(self, body, status=200, headers=None):
            self.served.append((status, body, headers or {}))

        def _serve_json(self, data, status=200, filename=None):
            self.served.append(
                (status, json.dumps(data).encode(), {"Content-Type": "application/json"})
            )

        def _read_body(self):
            return self.body

    h = _H()
    _diff_mod.DiffUIHandler._handle_export_bundle(h)
    status, body, headers = h.served[-1]
    assert status == 200 and headers.get("Content-Type") == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(body))
    names = set(zf.namelist())
    assert "package.xml" in names
    assert "delta-source/classes/Foo.cls" in names
    assert "delta-source/classes/Foo.cls-meta.xml" in names


# ---------------------------------------------------------------------------
# Original reproduction 5: unauthenticated HTTP requests fail, no side effects
# ---------------------------------------------------------------------------


def _start_diff_server(left: Path, right: Path, data_dir: Path, timeout=20):
    env = {**os.environ, "MCT_DATA_DIR": str(data_dir)}
    proc = subprocess.Popen(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "serve-diff-ui.py"),
            "--left", str(left), "--right", str(right),
            "--port", "0", "--no-open",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out_lines: list[str] = []
    deadline = time.time() + timeout
    url = None
    import threading

    def _reader():
        for line in proc.stdout:
            out_lines.append(line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    while time.time() < deadline and url is None:
        m = re.search(
            r"METADATA_COMPARE_DIFF_UI_URL=(\S+)", "".join(out_lines)
        )
        if m:
            url = m.group(1)
        elif proc.poll() is not None:
            raise RuntimeError(
                f"diff server exited {proc.returncode}: {''.join(out_lines)}"
            )
        else:
            time.sleep(0.05)
    if url is None:
        proc.kill()
        raise RuntimeError(f"diff server never printed URL: {''.join(out_lines)}")
    return proc, url, out_lines


def test_unauthenticated_api_request_rejected_without_side_effects(tmp_path):
    left, right = _trees(tmp_path)
    (left / "classes" / "Foo.cls").write_text("public class Foo { int x = 1; }\n")
    (right / "classes" / "Foo.cls").write_text("public class Foo { int x = 2; }\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    proc, url, _ = _start_diff_server(left, right, data_dir)
    try:
        # No X-MCT-Token header — must be refused before any state change.
        req = urllib.request.Request(
            f"{url}/api/accept-diff",
            data=json.dumps({"path": "classes/Foo.cls"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=10)
        assert exc.value.code in (401, 403)

        # No baseline was written anywhere under the data dir.
        assert not (data_dir / "snapshot-store").exists() or not any(
            (data_dir / "snapshot-store").rglob("baseline.json")
        )
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_server_lifecycle_is_bounded_and_uses_actual_port(tmp_path):
    """Startup must report the real bound URL (port 0 → ephemeral), and
    teardown must free the port — no leaked processes or hanging readers."""
    left, right = _trees(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    proc, url, _ = _start_diff_server(left, right, data_dir)
    port = int(url.rsplit(":", 1)[1].strip("/"))
    assert port != 0  # actual bound port, not the requested one
    try:
        with urllib.request.urlopen(f"{url}/", timeout=10) as resp:
            assert resp.status == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    assert proc.returncode is not None
    # No listener remains once the process exits — a connect must be refused.
    deadline = time.time() + 10
    refused = False
    while time.time() < deadline:
        s = socket.socket()
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            refused = True
        s.close()
        if refused:
            break
        time.sleep(0.1)
    assert refused, "server port still accepting connections after shutdown"


# ---------------------------------------------------------------------------
# Original reproduction 6: user state lives outside the package/repo
# ---------------------------------------------------------------------------


def test_state_root_is_external_to_install(tmp_path, monkeypatch):
    """Reinstalls replace the package dir; state must not live under it."""
    monkeypatch.setenv("MCT_DATA_DIR", str(tmp_path / "user-data"))
    _cfg.apply_repo_root(str(tmp_path / "dx-project"))

    assert str(_cfg.STORAGE_ROOT).startswith(str(tmp_path / "user-data"))
    pkg_dir = Path(_cfg.__file__).resolve().parent
    assert pkg_dir not in _cfg.STORAGE_ROOT.parents
    _cfg.apply_repo_root(None)


# ---------------------------------------------------------------------------
# Temporary git/DX fixture: branch snapshot materializes real source
# ---------------------------------------------------------------------------


def test_branch_snapshot_from_git_fixture(tmp_path, monkeypatch):
    dx = tmp_path / "dx-project"
    src = dx / "force-app" / "main" / "default" / "classes"
    src.mkdir(parents=True)
    (src / "Foo.cls").write_text("public class Foo {}\n")
    (dx / "sfdx-project.json").write_text(
        json.dumps({"packageDirectories": [{"path": "force-app", "default": True}]})
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=dx, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dx, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=dx, check=True, capture_output=True,
    )

    monkeypatch.setenv("MCT_DATA_DIR", str(tmp_path / "user-data"))
    _cfg.apply_repo_root(str(dx))
    try:
        from mct.snapshot import materialize_branch_snapshot

        snap = materialize_branch_snapshot(
            "main", "force-app/main/default", fetch=False
        )
        tree = (_cfg.STORAGE_ROOT / snap.path).resolve()
        assert (tree / "classes" / "Foo.cls").is_file()
    finally:
        _cfg.apply_repo_root(None)
