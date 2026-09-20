"""Branch and org snapshot creation."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import threading
import uuid
from pathlib import Path
from typing import Any

import mct.config as _cfg
from mct.index import (
    Snapshot,
    dx_default_compare_root,
    register_snapshot,
)

_stamp_lock = threading.Lock()
_last_stamp = ""
_stamp_seq = 0


def _unique_stamp() -> str:
    """``_cfg.ts_local()`` guaranteed not to repeat within this process.

    ts_local resolves to the second — two retrieves in the same second would
    mint identical snapshot ids and output directory names, so a sequence
    suffix is appended on repeats.
    """
    global _last_stamp, _stamp_seq
    with _stamp_lock:
        stamp = _cfg.ts_local()
        if stamp == _last_stamp:
            _stamp_seq += 1
            stamp = f"{stamp}-{_stamp_seq:02d}"
        else:
            _last_stamp, _stamp_seq = stamp, 0
        return stamp


def _unique_name(directory: Path, filename: str) -> str:
    """Return *filename* (or ``name-N.ext``) that does not yet exist in
    *directory*. Non-atomic — only a hint; prefer the atomic claim helpers
    below where a collision is possible."""
    if not (directory / filename).exists():
        return filename
    stem, dot, suffix = filename.partition(".")
    n = 2
    while True:
        candidate = f"{stem}-{n}{dot}{suffix}" if dot else f"{filename}-{n}"
        if not (directory / candidate).exists():
            return candidate
        n += 1


def _claim_dir(directory: Path, base: str) -> Path:
    """Atomically create *directory*/*base* (or ``base-N`` on collision) and
    return the created path. ``mkdir`` IS the cross-process claim — two
    simultaneous processes can never receive the same name."""
    directory.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        candidate = directory / (base if n == 0 else f"{base}-{n + 1}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            n += 1


def _claim_file(directory: Path, filename: str) -> Path:
    """Atomically reserve *filename* (or ``name-N.ext``) in *directory* and
    return the path. ``O_EXCL`` create is the cross-process claim; the
    caller writes into the reserved (empty) file."""
    directory.mkdir(parents=True, exist_ok=True)
    stem, dot, suffix = filename.partition(".")
    n = 0
    while True:
        name = filename if n == 0 else (
            f"{stem}-{n + 1}{dot}{suffix}" if dot else f"{filename}-{n + 1}"
        )
        path = directory / name
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return path


def _manifest_name(prefix: str, stamp: str) -> str:
    """Collision-free transient manifest name: *stamp* is unique within this
    process and the pid separates simultaneous processes in the same
    second. Manifests are transient inputs to ``sf``/union builds — the
    suffix never surfaces in snapshot ids."""
    return f"{prefix}-{stamp}-p{os.getpid()}"


def branch_exists(branch: str) -> bool:
    r = _cfg.run(["git", "rev-parse", "--verify", branch], capture=True, check=False)
    return r.returncode == 0


def remote_branch_exists(branch: str, remote: str | None = None) -> bool:
    if remote is None:
        remote = _cfg.READ_ONLY_GIT_REMOTE
    r = _cfg.run(["git", "rev-parse", "--verify", f"{remote}/{branch}"], capture=True, check=False)
    return r.returncode == 0


def require_sf() -> None:
    r = _cfg.run(["sf", "--version"], capture=True, check=False)
    if r.returncode != 0:
        raise RuntimeError("Salesforce CLI not available. Install/login first.")
    print(f"Using: {r.stdout.strip()}")


def count_files_under(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def read_package_dirs_from_branch(ref: str) -> list[str]:
    """Read packageDirectories paths from sfdx-project.json on *ref* via git archive.

    Returns paths like ``["force-app", "omnistudio", "datacloud", "jsp"]``.
    Falls back to ``["force-app"]`` if the file is absent or malformed.
    """
    try:
        archive_bytes = _cfg.run(
            ["git", "archive", "--format=tar", ref, "sfdx-project.json"],
            capture=True,
            text=False,
            timeout=120,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as tf:
            member = tf.getmember("sfdx-project.json")
            fobj = tf.extractfile(member)
            if fobj is None:
                return ["force-app"]
            data = json.loads(fobj.read().decode("utf-8"))
        dirs = [
            d["path"].strip("/").strip()
            for d in data.get("packageDirectories", [])
            if isinstance(d, dict) and d.get("path")
        ]
        return dirs if dirs else ["force-app"]
    except Exception as exc:
        print(
            f"  \u26a0 Could not read sfdx-project.json from {ref!r} ({exc}); "
            "falling back to force-app",
            flush=True,
        )
        return ["force-app"]


def _check_tar_member(root: Path, member: tarfile.TarInfo, rel_name: str | None = None) -> Path:
    """Validate one archive member for extraction under *root*; return its
    resolved target path.

    Metadata archives must contain only directories and regular files
    located inside the destination: symbolic and hard links, device/fifo
    special files, absolute paths, and traversal are all rejected before
    anything is written. Containment is checked on resolved path
    components (``relative_to``), never string prefixes — a sibling like
    ``out_evil`` is NOT inside ``out``.

    *root* must already be resolved. *rel_name* defaults to the member
    name; the merge path passes the subdir-relative name instead.
    """
    name = rel_name if rel_name is not None else member.name
    if member.issym() or member.islnk():
        raise RuntimeError(f"Tar member is a link (links are never extracted): {member.name}")
    if not (member.isdir() or member.isfile()):
        raise RuntimeError(
            f"Tar member is not a file or directory (rejected): {member.name}"
        )
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise RuntimeError(f"Tar path escapes target: {member.name}") from None
    return target


def _extract_archive_to(ref: str, subdir: str, out_dir: Path) -> None:
    """Extract git archive of ``ref:subdir`` into *out_dir* (security-checked).

    Every member is validated before extraction on every supported Python
    version — the tarfile ``filter=`` keyword (3.12+) is additional
    hardening, not the primary guard.
    """
    archive_bytes = _cfg.run(
        ["git", "archive", "--format=tar", ref, subdir],
        capture=True,
        text=False,
        timeout=120,
    ).stdout
    out_resolved = out_dir.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as tf:
        members = tf.getmembers()
        for m in members:
            _check_tar_member(out_resolved, m)
        if sys.version_info >= (3, 12):
            tf.extractall(path=out_dir, filter="data")
        else:
            # Pre-3.12 extractall applies stored modes verbatim; sanitize them
            # the way filter="data" does — strip suid/sgid/sticky bits and make
            # dirs traversable and files readable regardless of archive modes.
            for m in members:
                m.mode = (m.mode | (0o755 if m.isdir() else 0o644)) & 0o777
            tf.extractall(path=out_dir, members=members)


def _merge_package_dirs(ref: str, pkg_dirs: list[str], out_dir: Path) -> tuple[Path, list[str]]:
    """Archive each package's ``main/default/`` and merge into ``out_dir/main/default/``.

    Produces a single flat tree that matches what ``sf project retrieve`` outputs,
    making branch snapshots directly comparable to org retrieves regardless of how
    many packageDirectories the project defines.

    Files from later packages silently win on collision -- consistent with Salesforce
    retrieve behaviour where all metadata lands in one flat output folder.

    Returns ``(merged_root, collisions)`` where *merged_root* is
    ``out_dir/main/default/`` and *collisions* lists every relative path that was
    overwritten by a later package.
    """
    merged_root = out_dir / "main" / "default"
    merged_root.mkdir(parents=True, exist_ok=True)
    merged_resolved = merged_root.resolve()

    collisions: list[str] = []
    extracted_any = False
    for pkg_dir in pkg_dirs:
        pkg_source = f"{pkg_dir}/main/default"
        prefix = pkg_source + "/"
        try:
            archive_bytes = _cfg.run(
                ["git", "archive", "--format=tar", ref, pkg_source],
                capture=True,
                text=False,
                timeout=120,
            ).stdout
        except RuntimeError:
            print(f"  \u26a0 Skipping {pkg_source!r} (path not found in branch {ref!r})", flush=True)
            continue

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as tf:
            for member in tf.getmembers():
                if not member.name.startswith(prefix):
                    continue
                rel_path = member.name[len(prefix):]
                if not rel_path:
                    continue
                # Links, special files and escaping paths raise — unsafe
                # members must never be silently discarded.
                target = _check_tar_member(merged_resolved, member, rel_path)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        collisions.append(rel_path)
                    fobj = tf.extractfile(member)
                    if fobj is not None:
                        target.write_bytes(fobj.read())
                        extracted_any = True

    if not extracted_any:
        raise RuntimeError(
            f"No files extracted from branch {ref!r} "
            f"(checked packages: {pkg_dirs}). "
            "Verify that packageDirectories in sfdx-project.json match the branch structure."
        )

    n = count_files_under(merged_root)
    print(f"  \u2713 Merged {n} files from {len(pkg_dirs)} package(s) \u2192 main/default/", flush=True)
    return merged_root, collisions


def materialize_branch_snapshot(branch: str, source_subdir: str | None, fetch: bool) -> Snapshot:
    """Create a local snapshot of a git branch's SFDX source."""
    _cfg.log(f"Branch snapshot: {branch}")
    if fetch:
        _cfg.run(["git", "fetch", _cfg.READ_ONLY_GIT_REMOTE], check=False)
    ref = branch
    if not branch_exists(branch):
        if remote_branch_exists(branch):
            ref = f"{_cfg.READ_ONLY_GIT_REMOTE}/{branch}"
        else:
            raise RuntimeError(f"Branch not found locally or on {_cfg.READ_ONLY_GIT_REMOTE}: {branch}")

    stamp = _unique_stamp()
    branch_safe = _cfg.sanitize_token(branch)
    out_dir = _claim_dir(
        _cfg.STORAGE_ROOT, f"retrieved-branch-{branch_safe}-{stamp}"
    )
    out_name = out_dir.name

    if source_subdir:
        _extract_archive_to(ref, source_subdir, out_dir)
        src_root = out_dir / source_subdir
        if not src_root.is_dir():
            raise RuntimeError(f"Snapshot source path missing after archive extraction: {src_root}")
        recorded_subdir = source_subdir
    else:
        pkg_dirs = read_package_dirs_from_branch(ref)
        print(f"  Package directories: {pkg_dirs}", flush=True)
        src_root, collisions = _merge_package_dirs(ref, pkg_dirs, out_dir)
        if collisions:
            print(f"  \u26a0 {len(collisions)} file collision(s) during merge:", flush=True)
            for c in collisions:
                print(f"    - {c}", flush=True)
        recorded_subdir = "main/default"

    snap_id = out_name.removeprefix("retrieved-")
    return register_snapshot(
        Snapshot(
            snapshot_id=snap_id,
            snapshot_type="branch",
            created_at=stamp,
            path=_cfg.storage_rel(src_root),
            branch=branch,
            source_subdir=recorded_subdir,
        )
    )


def generate_manifest_from_source(source_dir_rel: str, name: str, api_version: str) -> Path:
    _cfg.MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    source_abs = (_cfg.STORAGE_ROOT / source_dir_rel).resolve()
    if not source_abs.is_dir():
        raise RuntimeError(f"Manifest source dir missing: {source_abs}")
    _cfg.run(
        [
            "sf",
            "project",
            "generate",
            "manifest",
            "--source-dir",
            str(source_abs),
            "--name",
            name,
            "--output-dir",
            "manifest",
            "--api-version",
            api_version,
        ]
    )
    path = _cfg.MANIFEST_DIR / f"{name}.xml"
    if not path.is_file():
        raise RuntimeError(f"Manifest missing: {path}")
    return path


def generate_manifest_from_org(org_alias: str, name: str, api_version: str) -> Path:
    _cfg.MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    _cfg.run(
        [
            "sf",
            "project",
            "generate",
            "manifest",
            "--from-org",
            org_alias,
            "--name",
            name,
            "--output-dir",
            "manifest",
            "--api-version",
            api_version,
        ]
    )
    path = _cfg.MANIFEST_DIR / f"{name}.xml"
    if not path.is_file():
        raise RuntimeError(f"Manifest missing: {path}")
    return path


def _retrieve_request(
    manifest: Path,
    org_alias: str,
    staging_dir: Path,
    timeout: int,
    api_version: str,
) -> tuple[list[str], str]:
    """One sf retrieve request into *staging_dir*; returns (per-component
    warnings, raw JSON payload text). Raises on any failure — including
    sf exiting 0 while the Metadata API reports status Failed (observed
    live: errorStatusCode LIMIT_EXCEEDED). Caller owns staging cleanup."""
    print("  Retrieve running (output captured for completeness check)…", flush=True)
    result = _cfg.run(
        [
            "sf",
            "project",
            "retrieve",
            "start",
            "--manifest",
            _cfg.project_rel(manifest),
            "--target-org",
            org_alias,
            "--output-dir",
            str(staging_dir.resolve()),
            "--api-version",
            api_version,
            "--ignore-conflicts",
            # -w takes minutes; the CLI flag is --wait-seconds. Round up.
            "-w",
            str(max(1, -(-timeout // 60))),
            "--json",
        ],
        capture=True,
        check=False,
    )

    payload_text = getattr(result, "stdout", "") or ""

    def _dump_payload() -> None:
        # The CLI's --json error report is in captured stdout — surface it,
        # or a failed retrieve is undiagnosable.
        if payload_text.strip():
            print(payload_text[:4000], flush=True)

    if result.returncode != 0:
        _dump_payload()
        sf_msg = ""
        try:
            sf_msg = str(json.loads(payload_text).get("message") or "")
        except (json.JSONDecodeError, AttributeError):
            pass
        raise RuntimeError(
            f"Retrieve failed (exit {result.returncode})"
            + (f": {sf_msg}" if sf_msg else "")
        )

    try:
        result_obj = json.loads(payload_text).get("result") or {}
    except (json.JSONDecodeError, AttributeError):
        result_obj = {}
    if isinstance(result_obj, dict) and (
        result_obj.get("success") is False or result_obj.get("status") == "Failed"
    ):
        _dump_payload()
        code = result_obj.get("errorStatusCode") or "unknown error"
        detail = result_obj.get("errorMessage") or ""
        raise RuntimeError(
            f"Retrieve failed: {code}"
            + (f" — {detail}" if detail and detail != code else "")
        )

    warnings: list[str] = []
    msgs = result_obj.get("messages") or [] if isinstance(result_obj, dict) else []
    if isinstance(msgs, dict):
        msgs = [msgs]
    for m in msgs:
        if isinstance(m, dict):
            warnings.append(f"{m.get('fileName', '?')}: {m.get('problem', '?')}")
    if not payload_text.strip():
        warnings.append("retrieve output was not parseable JSON — completeness unknown")
    return warnings, payload_text


def retrieve_with_manifest(
    manifest: Path,
    org_alias: str,
    out_name: str,
    timeout: int,
    api_version: str,
    collected: dict | None = None,
) -> Path:
    """Retrieve to a staging dir under the DX project, then move to STORAGE_ROOT.

    When *collected* is given it is filled with ``{"warnings": [...],
    "file_count": n}`` — per-component problems from the CLI's JSON result.
    A retrieve with warnings may be incomplete; files it could not return
    would otherwise read as deletion drift in the compare.
    """
    # Staging is process-unique: a shared name would let a simultaneous
    # retrieve delete or interleave with this one's in-flight content.
    staging_parent = _cfg.PROJECT_ROOT / "retrieved-metadata-compare-staging"
    staging_dir = staging_parent / f"{out_name}.{uuid.uuid4().hex[:8]}.staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.parent.mkdir(parents=True, exist_ok=True)

    chunk_size = getattr(_cfg, "RETRIEVE_CHUNK_SIZE", 4000) or 0
    members = parse_manifest_types(manifest)
    total_members = sum(len(m) for m in members.values())

    warnings: list[str] = []
    n_chunks = 1
    last_payload = ""
    try:
        if chunk_size > 0 and total_members > chunk_size:
            # Profile-like types get one dedicated request carrying the whole
            # manifest's scope-defining members; the rest is greedy-packed.
            regular = {t: m for t, m in members.items() if t not in SCOPE_DEPENDENT_TYPES}
            chunk_sets = split_manifest_members(regular, chunk_size)
            scope_req, scope_warn = build_scope_request(members, chunk_size)
            has_scope = any(t in scope_req for t in SCOPE_DEPENDENT_TYPES)
            n_chunks = len(chunk_sets) + (1 if has_scope else 0)
            print(
                f"  Manifest has {total_members} members (over the {chunk_size}-member "
                f"chunk cap) — retrieving in {n_chunks} requests.",
                flush=True,
            )
            if has_scope and scope_warn:
                warnings.append(scope_warn)
            staging_dir.mkdir(parents=True, exist_ok=True)
            # Every request lands in its own directory and is merged
            # stub-aware: a request that merely *references* a component
            # (profiles do, for every object) writes an empty placeholder
            # for it, which must not overwrite a body another request
            # retrieved. Observed live: 93 CustomObjects reduced to
            # <CustomObject/> by the profiles chunk.
            plan: list[tuple[dict[str, set[str]], frozenset[str] | None]] = [
                (cm, None) for cm in chunk_sets
            ]
            if has_scope:
                plan.append((scope_req, _SCOPE_DEPENDENT_FOLDERS))
            for i, (chunk_members, keep_dirs) in enumerate(plan, 1):
                chunk_path = manifest.parent / f"{manifest.stem}-chunk{i}.xml"
                write_manifest(chunk_path, chunk_members, api_version)
                n_m = sum(len(m) for m in chunk_members.values())
                label = " (Profile/PermissionSet with full scope)" if keep_dirs else ""
                print(f"  ▶ Chunk {i}/{n_chunks} ({n_m} members){label}", flush=True)
                request_dir = staging_dir / f"request-{i}"
                try:
                    chunk_warnings, last_payload = _retrieve_request(
                        chunk_path, org_alias, request_dir, timeout, api_version
                    )
                    warnings += chunk_warnings
                except RuntimeError as exc:
                    raise RuntimeError(f"chunk {i}/{n_chunks}: {exc}") from exc
                if request_dir.is_dir():
                    merge_retrieved_tree(request_dir, staging_dir, keep_dirs)
        else:
            single_warnings, last_payload = _retrieve_request(
                manifest, org_alias, staging_dir, timeout, api_version
            )
            warnings += single_warnings
    except Exception:
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    if not staging_dir.is_dir():
        # Show what sf reported — an empty retrieve is otherwise undiagnosable.
        if last_payload.strip():
            print(last_payload[:4000], flush=True)
        raise RuntimeError(f"Retrieve output missing: {out_name}")
    n = count_files_under(staging_dir)

    if warnings:
        print(f"  \u26a0 {len(warnings)} retrieve warning(s) \u2014 snapshot may be incomplete:", flush=True)
        for w in warnings[:20]:
            print(f"    - {w}", flush=True)
    if collected is not None:
        collected["warnings"] = warnings
        collected["file_count"] = n
        collected["chunks"] = n_chunks
    if n == 0:
        print(
            "  \u26a0 Warning: retrieve succeeded but 0 files were found. "
            "This may mean the org has no metadata matching the manifest, "
            "the org alias is wrong, or the retrieve output landed in an unexpected location.",
            flush=True,
        )

    # Atomic publish: mkdir is the cross-process claim, so a simultaneous
    # same-second retrieve lands under a -N sibling instead of racing.
    final_dir = _claim_dir(_cfg.STORAGE_ROOT, out_name)
    print(f"  ✓ Retrieved {n} files under {final_dir.name}\n", flush=True)
    try:
        for child in staging_dir.iterdir():
            shutil.move(str(child), str(final_dir / child.name))
        staging_dir.rmdir()
    except Exception:
        # final_dir is ours — claimed above; removing it touches no other
        # process's data.
        shutil.rmtree(final_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    try:
        if staging_parent.is_dir() and not any(staging_parent.iterdir()):
            staging_parent.rmdir()
    except OSError:
        pass

    return final_dir


def snapshot_org_from_source(
    org_alias: str, branch_snapshot: Snapshot, timeout: int, api_version: str
) -> Snapshot:
    print(
        "\n\u25b6 Step A \u2014 Generate manifest from branch snapshot tree (sf project generate manifest)\n",
        flush=True,
    )
    stamp = _unique_stamp()
    org_safe = _cfg.sanitize_token(org_alias)
    manifest_name = _manifest_name(f"package-{org_safe}-manifest-repo", stamp)
    manifest = generate_manifest_from_source(branch_snapshot.path, manifest_name, api_version)
    print(f"  Manifest file: {_cfg.project_rel(manifest)}\n", flush=True)
    print(
        "\u25b6 Step B \u2014 Retrieve metadata from org using that manifest (sf project retrieve start)\n"
        "  This is usually the longest step; the CLI may print little until it finishes.\n",
        flush=True,
    )
    collected: dict = {}
    out_dir = retrieve_with_manifest(
        manifest, org_alias, f"retrieved-org-{org_safe}-manifest-repo-{stamp}",
        timeout, api_version, collected=collected,
    )
    compare_root = dx_default_compare_root(out_dir)
    snap_id = out_dir.name.removeprefix("retrieved-")
    return register_snapshot(
        Snapshot(
            snapshot_id=snap_id,
            snapshot_type="org_retrieve",
            created_at=stamp,
            path=_cfg.storage_rel(compare_root),
            org_alias=org_alias,
            manifest_kind="repo-manifest",
            manifest_path=_cfg.project_rel(manifest),
            source_subdir=branch_snapshot.path,
            branch=branch_snapshot.branch,
            api_version=api_version,
            retrieve_warnings=collected.get("warnings") or None,
        )
    )


def snapshot_org_from_org(org_alias: str, timeout: int, api_version: str) -> Snapshot:
    _cfg.log("Org retrieve from org-derived manifest")
    stamp = _unique_stamp()
    org_safe = _cfg.sanitize_token(org_alias)
    manifest_name = _manifest_name(f"package-{org_safe}-manifest-org", stamp)
    manifest = generate_manifest_from_org(org_alias, manifest_name, api_version)
    collected: dict = {}
    out_dir = retrieve_with_manifest(
        manifest, org_alias, f"retrieved-org-{org_safe}-manifest-org-{stamp}",
        timeout, api_version, collected=collected,
    )
    compare_root = dx_default_compare_root(out_dir)
    snap_id = out_dir.name.removeprefix("retrieved-")
    return register_snapshot(
        Snapshot(
            snapshot_id=snap_id,
            snapshot_type="org_retrieve",
            created_at=stamp,
            path=_cfg.storage_rel(compare_root),
            org_alias=org_alias,
            manifest_kind="org-manifest",
            manifest_path=_cfg.project_rel(manifest),
            api_version=api_version,
            retrieve_warnings=collected.get("warnings") or None,
        )
    )


def parse_manifest_types(path: Path) -> dict[str, set[str]]:
    """Parse a package.xml into {type name -> member set} (namespace-agnostic)."""
    import xml.etree.ElementTree as ET

    def _local(tag: str) -> str:
        return tag.split("}", 1)[1] if tag.startswith("{") else tag

    out: dict[str, set[str]] = {}
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError) as exc:
        raise RuntimeError(f"Cannot read manifest {path}: {exc}") from exc
    for types_el in root:
        if _local(types_el.tag) != "types":
            continue
        name = ""
        members: list[str] = []
        for child in types_el:
            if _local(child.tag) == "name":
                name = (child.text or "").strip()
            elif _local(child.tag) == "members":
                m = (child.text or "").strip()
                if m:
                    members.append(m)
        if name:
            out.setdefault(name, set()).update(members)
    return out


def write_manifest(path: Path, members_by_type: dict[str, set[str]], api_version: str) -> None:
    """Write {type -> members} as a canonical package.xml."""
    from xml.sax.saxutils import escape

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">',
    ]
    for md_type in sorted(members_by_type):
        members = members_by_type[md_type]
        if not members:
            continue
        lines.append("    <types>")
        for member in sorted(members):
            lines.append(f"        <members>{escape(member)}</members>")
        lines.append(f"        <name>{escape(md_type)}</name>")
        lines.append("    </types>")
    lines.append(f"    <version>{api_version}</version>")
    lines.append("</Package>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def split_manifest_members(
    members: dict[str, set[str]], max_members: int
) -> list[dict[str, set[str]]]:
    """Greedy-pack manifest members into chunks of at most *max_members* each,
    for the Metadata API's per-retrieve size limits.

    Types stay whole when they fit (Profile content especially depends on
    request scope); a single type larger than the cap is split across
    consecutive chunks. Deterministic: types and members processed sorted.
    """
    chunks: list[dict[str, set[str]]] = []
    current: dict[str, set[str]] = {}
    current_n = 0

    def _flush() -> None:
        nonlocal current, current_n
        if current:
            chunks.append(current)
            current, current_n = {}, 0

    for t in sorted(members, key=str.lower):
        ms = sorted(members[t], key=str.lower)
        if len(ms) <= max_members:
            if current_n + len(ms) > max_members:
                _flush()
            current.setdefault(t, set()).update(ms)
            current_n += len(ms)
        else:
            i = 0
            while i < len(ms):
                room = max_members - current_n
                if room == 0:
                    _flush()
                    room = max_members
                take = ms[i:i + room]
                current.setdefault(t, set()).update(take)
                current_n += len(take)
                i += len(take)
                if current_n == max_members:
                    _flush()
    _flush()
    return chunks


# Types whose retrieved content is a projection onto the *other* members of
# the same request. Retrieved alone they come back hollow (observed live:
# 75 profiles with zero fieldPermissions/classAccesses/tabVisibilities).
SCOPE_DEPENDENT_TYPES: frozenset[str] = frozenset({
    "Profile", "PermissionSet", "MutingPermissionSet",
})

# Source-format folders the scope-dependent types decompose into. Only these
# are merged from the dedicated scope request; everything else it returns is
# a duplicate of (or a stub for) what the regular chunks retrieved.
_SCOPE_DEPENDENT_FOLDERS: frozenset[str] = frozenset({
    "profiles", "permissionsets", "mutingpermissionsets",
})

# Types that define what a Profile/PermissionSet can grant on. Their members
# ride along in the scope request so the returned profiles are complete.
SCOPE_DEFINING_TYPES: frozenset[str] = frozenset({
    "ApexClass", "ApexPage", "CustomApplication", "CustomField",
    "CustomMetadata", "CustomObject", "CustomPermission", "CustomTab",
    "DataCategoryGroup", "ExternalCredential", "ExternalDataSource", "Flow",
    # flowAccesses in a returned Profile are keyed on the FlowDefinition
    # members in the same request — Flow alone is not enough (observed live:
    # profiles came back with zero flowAccesses when only Flow was scoped).
    "FlowDefinition",
    "Layout", "RecordType",
})


def build_scope_request(
    members: dict[str, set[str]], max_members: int
) -> tuple[dict[str, set[str]], str | None]:
    """Members for the dedicated Profile/PermissionSet request: the profile
    types plus every scope-defining member of the whole manifest.

    The request is deliberately not split — splitting is what hollows
    profiles out. The member cap is only a proxy for the Metadata API's
    per-retrieve file/size limits (CustomField members fold into their
    object's file), so an over-cap request is sent as-is with a warning.
    Returns ``(request_members, warning_or_None)``.
    """
    req: dict[str, set[str]] = {
        t: set(m) for t, m in members.items()
        if (t in SCOPE_DEPENDENT_TYPES or t in SCOPE_DEFINING_TYPES) and m
    }
    n = sum(len(m) for m in req.values())
    if max_members <= 0 or n <= max_members:
        return req, None
    return req, (
        f"profile scope request has {n} members (over the {max_members} cap) — "
        "it is sent whole because Profile/PermissionSet content depends on the "
        "request scope; if it fails with LIMIT_EXCEEDED, narrow the manifest"
    )


def _is_empty_stub(path: Path) -> bool:
    """True for the placeholder ``<Type/>`` file the CLI writes when a request
    referenced a component (via a child or a profile) without retrieving its
    body. Anything unparsable or with content is not a stub."""
    import xml.etree.ElementTree as ET

    try:
        if path.stat().st_size > 512:
            return False
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError):
        return False
    return len(root) == 0 and not (root.text or "").strip() and not root.attrib


def merge_retrieved_tree(src: Path, dst: Path, only_top_dirs: frozenset[str] | None = None) -> int:
    """Move *src* into *dst*, file by file. A file already in *dst* is kept
    when the incoming one is an empty stub; a stub already in *dst* is
    replaced by an incoming body. Otherwise the incoming file wins.

    *only_top_dirs* restricts the merge to those first-level folders of
    *src*. Returns the number of files placed. *src* is consumed.
    """
    placed = 0
    for top in sorted(src.iterdir()):
        if only_top_dirs is not None and top.name not in only_top_dirs:
            continue
        for p in sorted(top.rglob("*")) if top.is_dir() else [top]:
            if not p.is_file():
                continue
            target = dst / p.relative_to(src)
            if target.exists() and _is_empty_stub(p) and not _is_empty_stub(target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(target))
            placed += 1
    shutil.rmtree(src, ignore_errors=True)
    return placed


def build_union_manifest(
    source_manifest: Path,
    org_manifest: Path,
    out_path: Path,
    api_version: str,
    extra_org_types: list[str] | None = None,
) -> dict[str, Any]:
    """Union the source manifest with the org manifest, org side scoped to types
    the source already tracks (plus *extra_org_types* opt-ins).

    This is the bidirectional retrieve scope: a source-only manifest can never
    surface org-only components (only_right is structurally empty), while a raw
    org manifest floods the diff with standard/system types the repo never
    tracked. Scoping the org side to source-tracked types keeps only_right
    meaningful without the noise.

    Returns a stats dict: kept/skipped org types and member counts.
    """
    src = parse_manifest_types(source_manifest)
    org = parse_manifest_types(org_manifest)

    keep_types = set(src) | {t.strip() for t in (extra_org_types or []) if t.strip()}
    union: dict[str, set[str]] = {t: set(m) for t, m in src.items()}
    skipped: list[str] = []
    for t, members in org.items():
        if t in keep_types:
            union.setdefault(t, set()).update(members)
        else:
            skipped.append(t)

    write_manifest(out_path, union, api_version)
    return {
        "source_types": len(src),
        "org_types": len(org),
        "union_types": len(union),
        "union_members": sum(len(m) for m in union.values()),
        "skipped_org_types": sorted(skipped, key=str.lower),
    }


def snapshot_org_bidirectional(
    org_alias: str,
    branch_snapshot: Snapshot,
    timeout: int,
    api_version: str,
    extra_org_types: list[str] | None = None,
) -> Snapshot:
    """Retrieve once with a union manifest so the compare sees drift in BOTH
    directions: components the org lacks (source manifest) and org-only
    components of source-tracked types (scoped org manifest)."""
    stamp = _unique_stamp()
    org_safe = _cfg.sanitize_token(org_alias)

    print(
        "\n▶ Step A — Generate manifest from branch snapshot tree (sf project generate manifest)\n",
        flush=True,
    )
    src_manifest = generate_manifest_from_source(
        branch_snapshot.path,
        _manifest_name(f"package-{org_safe}-bidi-src", stamp),
        api_version,
    )

    print(
        "\n▶ Step B — Generate manifest from org inventory (sf project generate manifest --from-org)\n",
        flush=True,
    )
    org_manifest = generate_manifest_from_org(
        org_alias,
        _manifest_name(f"package-{org_safe}-bidi-org", stamp),
        api_version,
    )

    print("\n▶ Step C — Union manifests (org side scoped to source-tracked types)\n", flush=True)
    union_path = _cfg.MANIFEST_DIR / (
        _manifest_name(f"package-{org_safe}-manifest-union", stamp) + ".xml"
    )
    stats = build_union_manifest(src_manifest, org_manifest, union_path, api_version, extra_org_types)
    print(
        f"  Union: {stats['union_types']} types / {stats['union_members']} members "
        f"(source {stats['source_types']} types, org {stats['org_types']} types)",
        flush=True,
    )
    if stats["skipped_org_types"]:
        print(
            f"  Skipped {len(stats['skipped_org_types'])} org-only type(s) not tracked in source "
            f"(use --include-org-type to opt in): {', '.join(stats['skipped_org_types'])}",
            flush=True,
        )

    print(
        "\n▶ Step D — Retrieve metadata from org using the union manifest (sf project retrieve start)\n"
        "  This is usually the longest step; the CLI may print little until it finishes.\n",
        flush=True,
    )
    collected: dict = {}
    out_dir = retrieve_with_manifest(
        union_path, org_alias, f"retrieved-org-{org_safe}-manifest-union-{stamp}",
        timeout, api_version, collected=collected,
    )
    compare_root = dx_default_compare_root(out_dir)
    snap_id = out_dir.name.removeprefix("retrieved-")
    return register_snapshot(
        Snapshot(
            snapshot_id=snap_id,
            snapshot_type="org_retrieve",
            created_at=stamp,
            path=_cfg.storage_rel(compare_root),
            org_alias=org_alias,
            manifest_kind="union-manifest",
            manifest_path=_cfg.project_rel(union_path),
            source_subdir=branch_snapshot.path,
            branch=branch_snapshot.branch,
            api_version=api_version,
            skipped_org_types=stats["skipped_org_types"],
            retrieve_warnings=collected.get("warnings") or None,
        )
    )


def retrieve_delta(
    org_alias: str,
    left: str,
    right: str,
    timeout: int,
    api_version: str,
    use_baseline: bool = True,
) -> Snapshot | None:
    """Reverse-sync (org → git): retrieve ONLY the org-side drift into a fresh
    folder ready for PR-ing back into the repo.

    *left*/*right* are snapshot ids or paths from an existing compare (left =
    source of truth, right = org retrieve). The org side of the drift (changed
    + org-only files, minus baseline-ignored/accepted) is resolved to
    components via the CLI registry and retrieved in one pass. Returns None
    when there is no drift to pull.
    """
    import mct.baseline as _bl
    import mct.delta as _delta
    from mct.index import abs_snapshot_or_project_rel, resolve_snapshot_or_path
    from mct.retrieved_folder_compare import compare_trees

    left_rel = resolve_snapshot_or_path(left)
    right_rel = resolve_snapshot_or_path(right)
    left_abs = abs_snapshot_or_project_rel(left_rel)
    right_abs = abs_snapshot_or_project_rel(right_rel)
    if not left_abs.is_dir() or not right_abs.is_dir():
        raise RuntimeError(f"Both sides must be directories: {left_abs} / {right_abs}")

    baseline = _bl.load_baseline() if use_baseline else _bl.default_baseline()
    result = compare_trees(
        left_abs, right_abs,
        _bl.xml_ignore_elements(baseline) or None,
        _bl.strip_retrieve_defaults(baseline),
        xml_ignore_by_type=_bl.xml_ignore_by_type(baseline) or None,
    )

    from mct.comparison import _snapshot_provenance

    pair_key = _bl.pair_key_for(
        _snapshot_provenance(left), _snapshot_provenance(right)
    )
    delta_files = _delta.collect_reverse_sync_files(
        result, baseline, pair_key=pair_key
    )

    # Deletions cannot be retrieved: components present in source but absent
    # in the org would otherwise silently survive a reverse-sync PR.
    deletions: list[str] = []
    for k in result.only_left_keys:
        lp, disp = result.left_ix[k]
        status, _ = _bl.classify_entry(
            disp, lp, None, baseline, pair_key=pair_key,
            left_root=result.left_root, right_root=result.right_root,
        )
        if status in ("active", "accepted_stale"):
            deletions.append(disp)
    deletions.sort()

    if not delta_files and not deletions:
        print("No org-side drift to pull — nothing to retrieve.", flush=True)
        return None

    stamp = _unique_stamp()
    org_safe = _cfg.sanitize_token(org_alias)
    out_base = f"retrieved-delta-{org_safe}-{stamp}"
    manifest: Path | None = None
    collected: dict = {}

    if delta_files:
        print(f"  Org-side drift: {len(delta_files)} file(s)", flush=True)
        from mct.retrieved_folder_compare import check_metadata_read
        ranchor = result.right_root or right_abs
        safe_paths = [
            check_metadata_read(ranchor, p, anchor=ranchor)
            for p, _ in delta_files
        ]
        members, err = _delta.resolve_components_via_sf(safe_paths)
        if members is None:
            raise RuntimeError(
                f"Component resolution failed ({err}). retrieve-delta needs accurate "
                "member names; run from a machine with the Salesforce CLI available."
            )
        if not members and not deletions:
            print("No resolvable components in the drift — nothing to retrieve.", flush=True)
            return None
        if members:
            _cfg.MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
            manifest = _cfg.MANIFEST_DIR / (
                _manifest_name(f"package-{org_safe}-delta", stamp) + ".xml"
            )
            write_manifest(manifest, members, api_version)
            n_members = sum(len(m) for m in members.values())
            print(f"  Delta manifest: {_cfg.project_rel(manifest)} "
                  f"({len(members)} types / {n_members} members)", flush=True)

    if manifest is not None:
        out_dir = retrieve_with_manifest(manifest, org_alias, out_base, timeout, api_version,
                                         collected=collected)
    else:
        # Deletions-only drift: nothing to retrieve, but the PR folder (and
        # its deletion list) is still the deliverable.
        out_dir = _claim_dir(_cfg.STORAGE_ROOT, out_base)
    compare_root = dx_default_compare_root(out_dir)

    if deletions:
        (out_dir / "DELETED_IN_ORG.txt").write_text("\n".join(deletions) + "\n", encoding="utf-8")
        print(f"  ⚠ {len(deletions)} component file(s) deleted in the org but present in source —"
              f" listed in DELETED_IN_ORG.txt; delete them from git in the same PR.", flush=True)

    snap = register_snapshot(
        Snapshot(
            snapshot_id=out_dir.name.removeprefix("retrieved-"),
            snapshot_type="org_retrieve",
            created_at=stamp,
            path=_cfg.storage_rel(compare_root),
            org_alias=org_alias,
            manifest_kind="delta-manifest",
            manifest_path=_cfg.project_rel(manifest) if manifest else None,
            api_version=api_version,
            retrieve_warnings=collected.get("warnings") or None,
        )
    )
    print(
        "\nNext steps (reverse-sync into git):\n"
        f"  1. Copy the retrieved files from the snapshot folder into your DX project:\n"
        f"       {compare_root}\n"
        "  2. If DELETED_IN_ORG.txt is present, delete those files from git too.\n"
        "  3. Create a branch, review the diff, and open a PR.\n"
        "  This tool never writes to git — the copy and commit are yours to make.",
        flush=True,
    )
    return snap


def _parse_installed_packages_payload(data: Any) -> list[dict[str, Any]]:
    """Normalize sf package installed list CLI JSON into a list of package dicts."""
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    if isinstance(result, dict):
        rec = result.get("records")
        if isinstance(rec, list):
            return [x for x in rec if isinstance(x, dict)]
    rec = data.get("records")
    if isinstance(rec, list):
        return [x for x in rec if isinstance(x, dict)]
    return []


def _package_key(row: dict[str, Any]) -> str:
    ns = (row.get("SubscriberPackageNamespace") or row.get("namespace") or "").strip()
    name = (row.get("SubscriberPackageName") or row.get("Name") or row.get("name") or "").strip()
    if not name and row.get("SubscriberPackageId"):
        name = str(row.get("SubscriberPackageId"))
    return f"{ns}::{name}" if ns else name or "(unknown)"


def _package_version(row: dict[str, Any]) -> str:
    vid = row.get("SubscriberPackageVersionId") or row.get("subscriberPackageVersionId") or ""
    return str(vid).strip() if vid else ""


def load_installed_packages_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _parse_installed_packages_payload(data)


def snapshot_installed_packages(org_alias: str) -> Snapshot:
    """Read-only: sf package installed list --json; writes local JSON under .metadata-compare/."""
    _cfg.log("Installed packages snapshot")
    require_sf()
    stamp = _unique_stamp()
    org_safe = _cfg.sanitize_token(org_alias)
    out_path = _claim_file(
        _cfg.STATE_DIR, f"packages-{org_safe}-{stamp}.json"
    )
    result = _cfg.run(
        [
            "sf",
            "package",
            "installed",
            "list",
            "--target-org",
            org_alias,
            "--json",
        ],
        capture=True,
        text=True,
    )
    out_path.write_text(result.stdout, encoding="utf-8")
    snap_id = out_path.stem
    return register_snapshot(
        Snapshot(
            snapshot_id=snap_id,
            snapshot_type="installed_packages",
            created_at=stamp,
            path=_cfg.storage_rel(out_path),
            org_alias=org_alias,
        )
    )
