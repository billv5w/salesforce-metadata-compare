"""Root-anchor regression checks: a comparison's canonical root must never
be re-anchored by replacing the root — or an ancestor above it — with a
link after the comparison was established.

Synthetic fixtures only; no Salesforce access.
"""
from __future__ import annotations

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

from mct.retrieved_folder_compare import (
    LinkedMetadataError,
    check_metadata_read,
    read_metadata_bytes,
)

spec = importlib.util.spec_from_file_location(
    "root_anchor_unit_ui", ROOT / "scripts/serve-diff-ui.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)

MARKER = "EXTERNAL_ANCESTOR_MARKER"


def _seed(left: Path, right: Path, external: Path) -> None:
    for root, body in (
        (left, "old source"),
        (right, "old target"),
        (external, MARKER),
    ):
        (root / "classes").mkdir(parents=True)
        (root / "classes/A.cls").write_text(body)


def _handler(left: Path, right: Path) -> tuple:
    handler = object.__new__(ui.DiffUIHandler)
    handler.left_root = left
    handler.right_root = right
    handler.left_rel = "left"
    handler.right_rel = "right"
    handler.left_info = handler.right_info = None
    handler.api_version = "66.0"
    out: dict = {}
    handler._read_body = lambda: {}
    handler._serve_json = lambda body, status=200: out.update(status=status, json=body)
    handler._serve_bytes = lambda body, status=200, headers=None: out.update(
        status=status, bytes=body
    )
    return handler, out


@pytest.fixture
def cmp_env(tmp_path, monkeypatch):
    ui.get_comparison.cache_clear()
    monkeypatch.setattr(ui, "BASELINE_FILE", None)
    monkeypatch.setattr(ui, "INCLUDE_TYPES", None)
    monkeypatch.setattr(ui, "EXCLUDE_TYPES", None)
    yield tmp_path
    ui.get_comparison.cache_clear()


def _exposed(route: str, handler, out: dict) -> bool:
    if route == "diff":
        handler.path = "/api/diff?path=classes/A.cls"
        handler._route_GET()
        return MARKER in json.dumps(out)
    if route == "report":
        handler._handle_export_html()
        return MARKER in unquote(json.dumps(out))
    handler._resolve_components_via_sf = lambda paths: ({"ApexClass": {"A"}}, None)
    handler._handle_export_bundle()
    if "bytes" not in out:
        return False
    with zipfile.ZipFile(io.BytesIO(out["bytes"])) as archive:
        return any(
            MARKER.encode() in archive.read(name) for name in archive.namelist()
        )


@pytest.mark.parametrize("route", ["diff", "report", "bundle"])
def test_ancestor_above_root_replacement_cannot_reanchor_trust(
    cmp_env, tmp_path, route
):
    """Replace an ANCESTOR of the established root (not the root itself)
    with a symlink — reads must still refuse to re-anchor the boundary."""
    container = tmp_path / "container"
    left = container / "left"
    right = tmp_path / "right"
    external = tmp_path / "external-container"
    _seed(left, right, external / "left")

    ui.get_comparison(left, right)
    container.rename(tmp_path / "original-container")
    container.symlink_to(external, target_is_directory=True)

    handler, out = _handler(left, right)
    assert not _exposed(route, handler, out), (
        f"{route} reads outside the originally selected root "
        "after an ancestor was replaced"
    )


def test_check_metadata_read_rejects_replaced_root(tmp_path):
    root = tmp_path / "left"
    external = tmp_path / "external"
    _seed(root, tmp_path / "right", external)
    anchor = root.resolve()

    root.rename(tmp_path / "original-left")
    root.symlink_to(external, target_is_directory=True)

    with pytest.raises(LinkedMetadataError):
        check_metadata_read(root, root / "classes/A.cls", anchor=anchor)
    with pytest.raises(LinkedMetadataError):
        read_metadata_bytes(root, root / "classes/A.cls", anchor=anchor)


def test_check_metadata_read_rejects_replaced_ancestor(tmp_path):
    container = tmp_path / "container"
    root = container / "left"
    external = tmp_path / "external-container"
    _seed(root, tmp_path / "right", external / "left")
    anchor = root.resolve()

    container.rename(tmp_path / "original-container")
    container.symlink_to(external, target_is_directory=True)

    with pytest.raises(LinkedMetadataError):
        check_metadata_read(root, root / "classes/A.cls", anchor=anchor)


def test_check_metadata_read_rejects_spelled_root_redirection(tmp_path):
    """The spelled root resolving somewhere other than the established
    anchor is a boundary redefinition — reject even if the anchor itself
    is untouched."""
    root = tmp_path / "left"
    other = tmp_path / "other"
    _seed(root, tmp_path / "right", tmp_path / "external")
    (other / "classes").mkdir(parents=True)
    anchor = root.resolve()

    # Root itself intact, but the caller's spelling resolves elsewhere.
    with pytest.raises(LinkedMetadataError):
        check_metadata_read(other, other / "classes/A.cls", anchor=anchor)


def test_anchored_read_still_works_when_boundary_intact(tmp_path):
    """Canonical anchor + legit alias spellings keep working: the anchor
    is canonicalized at selection, so /var→/private/var style spellings
    of the same root still verify."""
    root = tmp_path / "left"
    _seed(root, tmp_path / "right", tmp_path / "external")
    anchor = root.resolve()
    p = anchor / "classes/A.cls"

    assert check_metadata_read(anchor, p, anchor=anchor) == p.resolve()
    # Spelled (possibly non-canonical) root + relative and absolute paths.
    assert check_metadata_read(root, p, anchor=anchor) == p.resolve()
    assert check_metadata_read(
        root, Path("classes/A.cls"), anchor=anchor
    ) == p.resolve()
    assert read_metadata_bytes(root, p, anchor=anchor) == b"old source"


def test_unanchored_spelled_root_keeps_legacy_behavior(tmp_path):
    """Without an anchor the check falls back to the spelled root's fresh
    resolution — the contract for initial-selection reads."""
    root = tmp_path / "left"
    _seed(root, tmp_path / "right", tmp_path / "external")
    p = root / "classes/A.cls"
    assert check_metadata_read(root, p) == p.resolve()
    assert read_metadata_bytes(root, p) == b"old source"
