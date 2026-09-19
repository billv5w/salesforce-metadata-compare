"""Non-destructive migration of pre-externalization data.

Older installs kept all state inside the tool checkout at
``BUNDLE_ROOT/.metadata-compare/snapshot-store/<project-key>`` and the
orchestrator workspaces file at ``~/.config/mct/workspaces.json``. Current
installs use the platform's per-user data/config locations (see
``mct.config.data_root`` / ``config_root``).

``mct migrate`` copies legacy data into the new locations, verifies each
copied tree (file count + byte size), and reports what moved. It never
deletes the legacy source, never overwrites an existing destination, and
never merges unrelated project records — a destination that already exists
is reported as a conflict and left alone.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import mct.config as _cfg


def legacy_store_root() -> Path:
    """The only legacy store location we probe — no home-wide scanning."""
    return _cfg.BUNDLE_ROOT / ".metadata-compare" / "snapshot-store"


def legacy_workspaces_path() -> Path:
    return Path.home() / ".config" / "mct" / "workspaces.json"


def detect_legacy_projects(legacy_root: Path | None = None) -> list[str]:
    """Project-key directory names under the legacy store."""
    root = Path(legacy_root) if legacy_root else legacy_store_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _tree_stats(root: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for every file under *root*."""
    files = [f for f in root.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def _verify_tree_copy(src: Path, dst: Path) -> str | None:
    """Per-file content verification of a copied tree. Returns a description
    of the first mismatch, or None when every source file exists at the same
    relative path with identical bytes and no extra files were added."""
    src_files = {f.relative_to(src).as_posix(): f for f in src.rglob("*") if f.is_file()}
    dst_files = {f.relative_to(dst).as_posix(): f for f in dst.rglob("*") if f.is_file()}
    for rel in sorted(set(src_files) | set(dst_files)):
        s, d = src_files.get(rel), dst_files.get(rel)
        if s is None:
            return f"unexpected extra file {rel}"
        if d is None:
            return f"missing file {rel}"
        if s.read_bytes() != d.read_bytes():
            return f"content mismatch {rel}"
    return None


def migrate(
    legacy_root: Path | None = None, data_root: Path | None = None
) -> dict[str, Any]:
    """Copy each legacy project-key dir to the new store. Non-destructive:
    the source stays in place and existing destinations are never touched.

    Each project is copied to a sibling staging directory, content-verified,
    then published by an atomic same-filesystem rename — an interrupted copy
    can never leave a partial destination that later reads as a conflict.
    """
    src_root = Path(legacy_root).expanduser().resolve() if legacy_root else legacy_store_root()
    base = Path(data_root).expanduser().resolve() if data_root else _cfg.data_root()
    dst_root = base / "snapshot-store"
    result: dict[str, Any] = {
        "source": str(src_root),
        "destination": str(dst_root),
        "migrated": [],
        "skipped_conflicts": [],
        "files_copied": 0,
        "errors": [],
    }
    if src_root == dst_root or not src_root.is_dir():
        return result
    dst_root.mkdir(parents=True, exist_ok=True)
    for child in sorted(src_root.iterdir()):
        if not child.is_dir():
            continue
        dst = dst_root / child.name
        staging = dst_root / f".{child.name}.mct-staging"
        if dst.exists():
            result["skipped_conflicts"].append(child.name)
            continue
        try:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(child, staging)
            mismatch = _verify_tree_copy(child, staging)
            if mismatch is not None:
                raise OSError(f"verification failed: {mismatch}")
            if dst.exists():  # created concurrently during our copy
                result["skipped_conflicts"].append(child.name)
                continue
            staging.rename(dst)  # same filesystem → atomic publish
            src_count, _ = _tree_stats(child)
            result["migrated"].append(child.name)
            result["files_copied"] += src_count
        except OSError as exc:
            result["errors"].append(f"{child.name}: {exc}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return result


def migrate_workspaces() -> dict[str, Any]:
    """Copy the legacy workspaces file to config_root() when absent there.
    Non-destructive — the legacy file stays in place."""
    legacy = legacy_workspaces_path()
    dest = _cfg.config_root() / "workspaces.json"
    result: dict[str, Any] = {
        "source": str(legacy),
        "destination": str(dest),
        "migrated": False,
        "skipped_conflict": False,
        "error": None,
    }
    if not legacy.is_file() or legacy.resolve() == dest.resolve():
        return result
    if dest.exists():
        result["skipped_conflict"] = True
        return result
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Stage-verify-publish: workspaces_path() prefers dest the moment it
    # exists, so dest must only ever appear fully written and verified —
    # a truncated copy would take the live registry down with it and block
    # retry as a false "conflict". Staging is per-operation so simultaneous
    # migrations cannot share or clean each other's file.
    staging = dest.parent / (
        f".{dest.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.mct-staging"
    )
    try:
        shutil.copy2(legacy, staging)
        if staging.read_bytes() != legacy.read_bytes():
            raise OSError(f"workspace copy verification failed: {staging}")
        # Atomic no-overwrite publish: a registry created by another writer
        # while we were copying must win — os.link fails on existing dest,
        # unlike os.replace which would silently overwrite it.
        try:
            os.link(staging, dest)
        except FileExistsError:
            result["skipped_conflict"] = True
            return result
        result["migrated"] = True
    except OSError as exc:
        result["error"] = str(exc)
    finally:
        staging.unlink(missing_ok=True)
    return result


def run_migrate(args: Any) -> int:
    """CLI entry point for ``env-compare.py migrate``."""
    as_json = bool(getattr(args, "as_json", False))
    store = migrate(legacy_root=getattr(args, "legacy_root", None))
    workspaces = migrate_workspaces()
    payload = {"snapshot_store": store, "workspaces": workspaces}
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Legacy store:  {store['source']}")
        print(f"New location:  {store['destination']}")
        if store["migrated"]:
            print(
                f"  Copied {len(store['migrated'])} project store(s) "
                f"({store['files_copied']} files): {', '.join(store['migrated'])}"
            )
        if store["skipped_conflicts"]:
            print(
                f"  Left alone (destination already exists): "
                f"{', '.join(store['skipped_conflicts'])}"
            )
        for err in store["errors"]:
            print(f"  ERROR: {err}", file=sys.stderr)
        if not store["migrated"] and not store["skipped_conflicts"] and not store["errors"]:
            print("  Nothing to migrate — no legacy snapshot data found.")
        if workspaces["migrated"]:
            print(f"  Copied workspaces file to {workspaces['destination']}")
        elif workspaces["skipped_conflict"]:
            print(f"  Workspaces file already exists at {workspaces['destination']} — left alone.")
        if workspaces["error"]:
            print(f"  ERROR: workspaces: {workspaces['error']}", file=sys.stderr)
        print("\nLegacy data was copied, not moved — the originals remain in place.")
    return 1 if (store["errors"] or workspaces["error"]) else 0
