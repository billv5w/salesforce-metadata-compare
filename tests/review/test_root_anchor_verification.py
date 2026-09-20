"""Independent root-replacement regression checks using synthetic data only."""

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "root_anchor_ui", ROOT / "scripts/serve-diff-ui.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


@pytest.mark.parametrize("route", ["diff", "report", "bundle"])
def test_cached_root_replacement_cannot_reanchor_trust(tmp_path, monkeypatch, route):
    ui.get_comparison.cache_clear()
    monkeypatch.setattr(ui, "BASELINE_FILE", None)
    monkeypatch.setattr(ui, "INCLUDE_TYPES", None)
    monkeypatch.setattr(ui, "EXCLUDE_TYPES", None)
    left = tmp_path / "left"
    right = tmp_path / "right"
    external = tmp_path / "external"
    for root, body in [
        (left, "old source"),
        (right, "old target"),
        (external, "EXTERNAL_ROOT_MARKER"),
    ]:
        (root / "classes").mkdir(parents=True)
        (root / "classes/A.cls").write_text(body)

    ui.get_comparison(left, right)
    left.rename(tmp_path / "original-left")
    left.symlink_to(external, target_is_directory=True)

    handler = object.__new__(ui.DiffUIHandler)
    handler.left_root = left
    handler.right_root = right
    handler.left_rel = "left"
    handler.right_rel = "right"
    handler.left_info = handler.right_info = None
    handler.api_version = "66.0"
    output = {}
    handler._read_body = lambda: {}
    handler._serve_json = lambda body, status=200: output.update(status=status, json=body)
    handler._serve_bytes = lambda body, status=200, headers=None: output.update(
        status=status, bytes=body
    )

    if route == "diff":
        handler.path = "/api/diff?path=classes/A.cls"
        handler._route_GET()
        exposed = "EXTERNAL_ROOT_MARKER" in json.dumps(output)
    elif route == "report":
        handler._handle_export_html()
        exposed = "EXTERNAL_ROOT_MARKER" in unquote(json.dumps(output))
    else:
        # Stub only CLI component-name resolution; exercise real export reads.
        handler._resolve_components_via_sf = lambda paths: ({"ApexClass": {"A"}}, None)
        handler._handle_export_bundle()
        exposed = False
        if "bytes" in output:
            with zipfile.ZipFile(io.BytesIO(output["bytes"])) as archive:
                exposed = any(
                    b"EXTERNAL_ROOT_MARKER" in archive.read(name)
                    for name in archive.namelist()
                )

    assert not exposed, f"{route} reads outside the originally selected root"
