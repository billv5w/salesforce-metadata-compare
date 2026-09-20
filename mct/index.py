"""Snapshot and comparison index persistence."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # Windows has no fcntl
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # POSIX has no msvcrt
    msvcrt = None  # type: ignore[assignment]

import mct.config as _cfg

# Drift management needs history: keep enough records for trending
# (`history --trend`), not just the last few runs.
COMPARISON_HISTORY_MAX_RECORDS = 50


@dataclass
class Snapshot:
    snapshot_id: str
    snapshot_type: str
    created_at: str
    path: str
    branch: str | None = None
    org_alias: str | None = None
    manifest_kind: str | None = None
    manifest_path: str | None = None
    source_subdir: str | None = None
    api_version: str | None = None
    # Bidirectional (union-manifest) snapshots: org-only types NOT retrieved
    # because they are untracked in source — the snapshot's blind spot.
    skipped_org_types: list[str] | None = None
    # Per-component problems the CLI reported during retrieve; a non-empty
    # list means the snapshot may be incomplete (missing files read as drift).
    retrieve_warnings: list[str] | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.snapshot_id,
            "type": self.snapshot_type,
            "created_at": self.created_at,
            "path": self.path,
            "branch": self.branch,
            "org_alias": self.org_alias,
            "manifest_kind": self.manifest_kind,
            "manifest_path": self.manifest_path,
            "source_subdir": self.source_subdir,
            "api_version": self.api_version,
            "skipped_org_types": self.skipped_org_types,
            "retrieve_warnings": self.retrieve_warnings,
        }


@dataclass
class ComparisonRecord:
    comparison_id: str
    created_at: str
    left_path: str
    right_path: str
    left_snapshot_id: str | None
    right_snapshot_id: str | None
    different_count: int
    only_left_count: int
    only_right_count: int
    identical_count: int
    total_left: int
    total_right: int
    different_files: list[str] = field(default_factory=list)
    only_left_files: list[str] = field(default_factory=list)
    only_right_files: list[str] = field(default_factory=list)
    baseline_applied: bool = False

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.comparison_id,
            "created_at": self.created_at,
            "left_path": self.left_path,
            "right_path": self.right_path,
            "left_snapshot_id": self.left_snapshot_id,
            "right_snapshot_id": self.right_snapshot_id,
            "different_count": self.different_count,
            "only_left_count": self.only_left_count,
            "only_right_count": self.only_right_count,
            "identical_count": self.identical_count,
            "total_left": self.total_left,
            "total_right": self.total_right,
            "different_files": self.different_files,
            "only_left_files": self.only_left_files,
            "only_right_files": self.only_right_files,
            "baseline_applied": self.baseline_applied,
        }


@contextlib.contextmanager
def _file_lock(lock_path: Path, exclusive: bool):
    """Portable advisory file lock.

    POSIX uses flock (shared/exclusive). Windows has no shared/exclusive split
    in msvcrt, so all locks are exclusive there — correct, just stricter.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Branch on sys.platform, not "module is not None": mypy resolves fcntl to
    # an empty stub on win32 targets (and vice versa for msvcrt), so attribute
    # access must sit inside a platform branch it can prove is unreachable.
    if sys.platform == "win32":
        if msvcrt is None:
            raise RuntimeError("No file-locking backend available on this platform")
        with open(lock_path, "a+b") as lf:
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lf.seek(0)
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
    elif fcntl is not None:
        with open(lock_path, "a", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    else:
        raise RuntimeError("No file-locking backend available on this platform")


def _write_json(path: Path, data: dict[str, Any], *, sort_keys: bool = False) -> None:
    """Atomically replace *path* with *data* (tmp file + os.replace). The temp
    file is removed if serialization or the replace fails."""
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, indent=2, sort_keys=sort_keys) + "\n", encoding="utf-8"
        )
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def locked_update(
    path: Path,
    default_factory: Callable[[], dict[str, Any]],
    mutate: Callable[[dict[str, Any]], Any],
    *,
    sort_keys: bool = False,
) -> Any:
    """Complete read-modify-write transaction under one exclusive lock.

    Reads *path* (or *default_factory()* when absent), passes it to *mutate*,
    atomically writes the result, and returns mutate's return value. Concurrent
    writers serialize on the lock, so updates are never lost or torn.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path.with_suffix(".lock"), exclusive=True):
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = default_factory()
        result = mutate(data)
        _write_json(path, data, sort_keys=sort_keys)
        return result


def _atomic_write(path: Path, data: dict[str, Any], *, sort_keys: bool = False) -> None:
    """Write *data* as JSON to *path* atomically (tmp file + os.replace) under an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path.with_suffix(".lock"), exclusive=True):
        _write_json(path, data, sort_keys=sort_keys)


def _locked_read(path: Path) -> str | None:
    """Read *path* under a shared lock; returns None if path does not exist."""
    with _file_lock(path.with_suffix(".lock"), exclusive=False):
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")


def load_index() -> dict[str, Any]:
    if not _cfg.INDEX_PATH.is_file():
        return {"version": 1, "snapshots": []}
    _cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    text = _locked_read(_cfg.INDEX_PATH)
    if text is None:
        return {"version": 1, "snapshots": []}
    return json.loads(text)


def save_index(data: dict[str, Any]) -> None:
    _cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(_cfg.INDEX_PATH, data)


def load_comparison_index() -> dict[str, Any]:
    if not _cfg.COMPARISON_INDEX_PATH.is_file():
        return {"version": 1, "comparisons": []}
    _cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    text = _locked_read(_cfg.COMPARISON_INDEX_PATH)
    if text is None:
        return {"version": 1, "comparisons": []}
    return json.loads(text)


def save_comparison_index(data: dict[str, Any]) -> None:
    _cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(_cfg.COMPARISON_INDEX_PATH, data)


def _snapshot_id_for_path(rel_path: str) -> str | None:
    """Return the snapshot id whose stored path matches rel_path, or None."""
    idx = load_index()
    for row in idx.get("snapshots", []):
        if str(row.get("path", "")).strip() == rel_path.strip():
            return str(row["id"])
    return None


def record_comparison(
    left_rel: str,
    right_rel: str,
    result: Any,
    active: dict[str, list[str]] | None = None,
) -> ComparisonRecord | None:
    """Append a comparison record to comparisons.json. Never raises -- history is best-effort.

    When *active* is given (keys: different_files / only_left_files /
    only_right_files, post-baseline), the record stores those counts so
    history and trend data match the printed summary instead of raw counts.
    """
    try:
        stamp = _cfg.ts_local()
        comp_id = f"cmp-{stamp}"
        if active is not None:
            different_files = list(active.get("different_files", []))
            only_left_files = list(active.get("only_left_files", []))
            only_right_files = list(active.get("only_right_files", []))
        else:
            different_files = [ld for _, _, ld, _ in result.differ_pairs]
            only_left_files = [result.left_ix[k][1] for k in result.only_left_keys]
            only_right_files = [result.right_ix[k][1] for k in result.only_right_keys]
        rec = ComparisonRecord(
            comparison_id=comp_id,
            created_at=stamp,
            left_path=left_rel,
            right_path=right_rel,
            left_snapshot_id=_snapshot_id_for_path(left_rel),
            right_snapshot_id=_snapshot_id_for_path(right_rel),
            different_count=len(different_files),
            only_left_count=len(only_left_files),
            only_right_count=len(only_right_files),
            identical_count=result.visible_identical_count,
            total_left=result.visible_total_left,
            total_right=result.visible_total_right,
            different_files=different_files,
            only_left_files=only_left_files,
            only_right_files=only_right_files,
            baseline_applied=active is not None,
        )
        def _add(data: dict[str, Any]) -> ComparisonRecord:
            comparisons = data.setdefault("comparisons", [])
            comparisons.append(rec.to_record())
            comparisons.sort(key=lambda row: row.get("created_at", ""), reverse=True)
            data["comparisons"] = comparisons[:COMPARISON_HISTORY_MAX_RECORDS]
            return rec

        return locked_update(
            _cfg.COMPARISON_INDEX_PATH,
            lambda: {"version": 1, "comparisons": []},
            _add,
        )
    except Exception as exc:
        print(f"  \u26a0 Could not record comparison history: {exc}", flush=True)
        return None


def register_snapshot(s: Snapshot) -> Snapshot:
    def _add(data: dict[str, Any]) -> Snapshot:
        data["snapshots"] = [
            row for row in data.get("snapshots", []) if row.get("id") != s.snapshot_id
        ]
        data["snapshots"].append(s.to_record())
        return s

    locked_update(
        _cfg.INDEX_PATH,
        lambda: {"version": 1, "snapshots": []},
        _add,
    )
    print(f"  \u2713 Snapshot registered: {s.snapshot_id}", flush=True)
    print(f"  \u2713 Path: {s.path}", flush=True)
    return s


def snapshot_from_row(row: dict[str, Any]) -> Snapshot:
    return Snapshot(
        snapshot_id=str(row["id"]),
        snapshot_type=str(row.get("type", "")),
        created_at=str(row.get("created_at", "")),
        path=str(row["path"]),
        branch=row.get("branch"),
        org_alias=row.get("org_alias"),
        manifest_kind=row.get("manifest_kind"),
        manifest_path=row.get("manifest_path"),
        source_subdir=row.get("source_subdir"),
        api_version=row.get("api_version"),
        skipped_org_types=row.get("skipped_org_types"),
        retrieve_warnings=row.get("retrieve_warnings"),
    )


def load_branch_snapshot_by_id(snapshot_id: str) -> Snapshot:
    idx = load_index()
    for row in idx.get("snapshots", []):
        if row.get("id") == snapshot_id:
            if row.get("type") != "branch":
                raise RuntimeError(
                    f"Snapshot {snapshot_id!r} is not a branch snapshot (type={row.get('type')!r})"
                )
            return snapshot_from_row(row)
    raise RuntimeError(f"Snapshot id not found: {snapshot_id}")


def load_snapshot_row_by_id(snapshot_id: str) -> dict[str, Any]:
    idx = load_index()
    for row in idx.get("snapshots", []):
        if row.get("id") == snapshot_id:
            return row
    raise RuntimeError(f"Snapshot id not found: {snapshot_id}")


def resolve_snapshot_or_path(value: str) -> str:
    raw = value.strip()
    p = Path(raw)
    if p.is_absolute():
        pr = p.resolve()
        if pr.is_dir() or pr.is_file():
            try:
                return normalize_org_retrieve_compare_rel(
                    pr.relative_to(_cfg.STORAGE_ROOT.resolve()).as_posix()
                )
            except ValueError:
                pass
            try:
                return pr.relative_to(_cfg.PROJECT_ROOT.resolve()).as_posix()
            except ValueError:
                raise RuntimeError(f"Path outside snapshot storage and DX project: {value}") from None
    sp = (_cfg.STORAGE_ROOT / raw).resolve()
    if sp.is_dir() or sp.is_file():
        return normalize_org_retrieve_compare_rel(sp.relative_to(_cfg.STORAGE_ROOT.resolve()).as_posix())
    pp = (_cfg.PROJECT_ROOT / raw).resolve()
    if pp.is_dir() or pp.is_file():
        return pp.relative_to(_cfg.PROJECT_ROOT.resolve()).as_posix()
    idx = load_index()
    for item in idx.get("snapshots", []):
        if item.get("id") == raw:
            return normalize_org_retrieve_compare_rel(str(item["path"]))
    raise RuntimeError(f"Unknown snapshot id or path: {value}")


def abs_snapshot_or_project_rel(rel: str) -> Path:
    """Resolve index or user path string to an absolute path (storage first, then DX project)."""
    sp = (_cfg.STORAGE_ROOT / rel).resolve()
    if sp.is_dir() or sp.is_file():
        return sp
    pp = (_cfg.PROJECT_ROOT / rel).resolve()
    if pp.is_dir() or pp.is_file():
        return pp
    raise RuntimeError(f"Path not found: {rel}")


def dx_default_compare_root(retrieve_output_dir: Path) -> Path:
    """Resolve to the same logical root as git's ``force-app/main/default``."""
    candidates = [
        retrieve_output_dir / "main" / "default",
        retrieve_output_dir / "force-app" / "main" / "default",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return retrieve_output_dir


def normalize_org_retrieve_compare_rel(rel: str) -> str:
    """If ``rel`` is the top-level ``retrieved-org-*`` folder, map to ``…/main/default`` when SF placed metadata there."""
    rel = rel.strip().replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p]
    if not parts or not parts[0].startswith("retrieved-org-"):
        return rel
    top = parts[0]
    retrieve_root = (_cfg.STORAGE_ROOT / top).resolve()
    if not retrieve_root.is_dir():
        return rel
    ideal = dx_default_compare_root(retrieve_root)
    try:
        ideal_rel = ideal.resolve().relative_to(_cfg.STORAGE_ROOT.resolve()).as_posix()
    except ValueError:
        return rel
    p = (_cfg.STORAGE_ROOT / rel).resolve()
    try:
        p.relative_to(ideal.resolve())
        return rel
    except ValueError:
        pass
    if p.resolve() == retrieve_root.resolve():
        return ideal_rel
    return rel


def print_snapshots(as_json: bool = False) -> int:
    from mct.comparison import snapshot_artifact_disk_usage_bytes

    idx = load_index()
    rows = idx.get("snapshots", [])
    legacy = _cfg.PROJECT_ROOT / ".metadata-compare" / "snapshots.json"
    if (
        not rows
        and legacy.is_file()
        and _cfg.PROJECT_ROOT.resolve() != _cfg.BUNDLE_ROOT.resolve()
    ):
        print(
            "Note: A snapshot index exists under your DX project (.metadata-compare/snapshots.json). "
            "New snapshots are stored under the metadata-compare tool checkout. Re-run snapshots or move data if needed.",
            file=sys.stderr,
        )
    if as_json:
        enriched: list[dict[str, Any]] = []
        for row in rows:
            r = dict(row)
            sz = snapshot_artifact_disk_usage_bytes(row)
            r["size_bytes"] = sz
            enriched.append(r)
        print(json.dumps({"version": idx.get("version", 1), "snapshots": enriched}, indent=2))
        return 0
    if not rows:
        print("No snapshots recorded.")
        return 0
    for row in sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True):
        print(
            f"{row.get('id')} | {row.get('type')} | {row.get('created_at')} | "
            f"{row.get('path')} | branch={row.get('branch') or '-'} | org={row.get('org_alias') or '-'}"
        )
    return 0


def print_comparison_trend(records: list[dict[str, Any]]) -> int:
    """Chronological drift counts so divergence over time is visible."""
    rows = sorted(records, key=lambda r: r.get("created_at", ""))
    if not rows:
        print("No comparisons recorded yet.")
        return 0
    header = f"{'WHEN':<22} {'DIFF':>6} {'ONLY-L':>7} {'ONLY-R':>7} {'TOTAL':>6}  PAIR"
    print(header)
    print("─" * len(header))
    prev_total: int | None = None
    for r in rows:
        d = r.get("different_count", 0)
        ol = r.get("only_left_count", 0)
        orr = r.get("only_right_count", 0)
        total = d + ol + orr
        arrow = ""
        if prev_total is not None and total != prev_total:
            arrow = " ↑" if total > prev_total else " ↓"
        prev_total = total
        pair = f"{r.get('left_path', '?')} vs {r.get('right_path', '?')}"
        print(f"{r.get('created_at', '?'):<22} {d:>6} {ol:>7} {orr:>7} {total:>6}{arrow}  {pair}")
    return 0


def print_comparison_history(
    as_json: bool = False,
    left_filter: str | None = None,
    right_filter: str | None = None,
    trend: bool = False,
) -> int:
    data = load_comparison_index()
    records = data.get("comparisons", [])
    if left_filter:
        records = [r for r in records if left_filter in r.get("left_path", "")]
    if right_filter:
        records = [r for r in records if right_filter in r.get("right_path", "")]
    if trend and not as_json:
        return print_comparison_trend(records)
    rows = sorted(
        records,
        key=lambda r: r.get("created_at", ""),
        reverse=True,
    )
    if as_json:
        print(json.dumps({"version": data.get("version", 1), "comparisons": rows}, indent=2))
        return 0
    if not rows:
        print("No comparisons recorded yet.")
        return 0
    for row in rows:
        diff_total = (
            row.get("different_count", 0)
            + row.get("only_left_count", 0)
            + row.get("only_right_count", 0)
        )
        clean = "clean" if diff_total == 0 else f"{diff_total} diff(s)"
        print(
            f"{row.get('id')} | {row.get('created_at')} | {clean} | "
            f"left={row.get('left_path')} | right={row.get('right_path')}"
        )
    return 0
