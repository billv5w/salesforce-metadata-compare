"""Shared delta-manifest resolution: files → Salesforce components via the CLI.

Used by the diff UI's manifest/bundle export and the reverse-sync
``retrieve-delta`` command. Resolution shells out to the allowlisted
``sf project generate manifest`` so type/member names come from Salesforce's
own source-deploy-retrieve registry instead of folder-name guessing.
"""
from __future__ import annotations

import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

# Batch size for -p args per `sf` invocation. Paths are passed as argv, and
# macOS caps argv+env at ~1 MB (getconf ARG_MAX); ~150 absolute paths of
# snapshot-store depth stay well under that while keeping call count low.
MANIFEST_BATCH_SIZE = 150


def parse_manifest_members(xml_text: str) -> Iterator[tuple[str, str]]:
    """Yield (type name, member) pairs from a package.xml, namespace-agnostic."""

    def _local(tag: str) -> str:
        return tag.split("}", 1)[1] if tag.startswith("{") else tag

    root = ET.fromstring(xml_text)
    for types_el in root:
        if _local(types_el.tag) != "types":
            continue
        name = ""
        members = []
        for child in types_el:
            if _local(child.tag) == "name":
                name = (child.text or "").strip()
            elif _local(child.tag) == "members":
                members.append((child.text or "").strip())
        for m in members:
            if name and m:
                yield name, m


def resolve_components_via_sf(
    abs_paths: list[Path], batch_size: int = MANIFEST_BATCH_SIZE
) -> tuple[dict[str, set[str]] | None, str | None]:
    """Resolve files/dirs to {metadata type -> member names} via the Salesforce
    CLI's own metadata registry. Batched to stay under the platform argv limit.
    Returns (members_by_type, error); members_by_type is None on failure.
    """
    if not abs_paths:
        return {}, None

    import mct.safety as _safety

    members_by_type: dict[str, set[str]] = {}
    for start in range(0, len(abs_paths), batch_size):
        batch = abs_paths[start : start + batch_size]
        tmp_dir = tempfile.mkdtemp(prefix="mct-manifest-")
        try:
            cmd = ["sf", "project", "generate", "manifest"]
            for p in batch:
                cmd += ["-p", str(p)]
            cmd += ["-d", tmp_dir]
            _safety.run(cmd, capture=True, timeout=120)
            out_path = Path(tmp_dir) / "package.xml"
            if not out_path.is_file():
                return None, "sf did not produce package.xml"
            for md_type, member in parse_manifest_members(
                out_path.read_text(encoding="utf-8")
            ):
                members_by_type.setdefault(md_type, set()).add(member)
        except Exception as exc:
            return None, str(exc)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return members_by_type, None


def expand_deployable_files(
    left_ix: dict[str, tuple[Path, str]],
    deploy_files: list[tuple[Path, str]],
) -> list[tuple[Path, str]]:
    """Expand delta files to a deployable set from the left tree: -meta.xml
    companions are added (a .cls without its meta file will not deploy), and
    any file inside a composite component pulls in the whole component —
    LWC/Aura bundles deploy as a unit, and an expanded directory component
    (e.g. ``staticresources/ReviewAsset/app.js``) requires every file under
    ``staticresources/ReviewAsset/`` plus its sibling
    ``ReviewAsset.resource-meta.xml`` or the export is not deployable.
    *left_ix* is a norm_key -> (abs path, display) index.
    """
    from mct.retrieved_folder_compare import norm_key

    out: dict[str, tuple[Path, str]] = {}

    def add_key(key: str) -> None:
        if key in left_ix and key not in out:
            out[key] = left_ix[key]

    for _, disp in deploy_files:
        parts = disp.replace("\\", "/").split("/")
        comp_prefix = None
        if len(parts) > 2:
            prefix = f"{parts[0].lower()}/{parts[1].lower()}"
            is_bundle_type = parts[0].lower() in ("lwc", "aura")
            # Expanded-directory components are identified by a sibling
            # metadata descriptor: <dir>/<name>/... + <dir>/<name>.<ext>-meta.xml
            has_sibling_meta = any(
                k.startswith(prefix + ".") and k.endswith("-meta.xml")
                for k in left_ix
            )
            if is_bundle_type or has_sibling_meta:
                comp_prefix = prefix
        elif len(parts) == 2:
            # Type-root component boundary: siblings sharing the component
            # stem — a directory payload (<stem>/), descriptors
            # (<stem>.<x>-meta.xml), or single-extension payloads
            # (<stem>.<ext>). The stem strips only the LAST extension (and
            # -meta.xml): CustomMetadata's ReviewSettings.Selected and
            # ReviewSettings.Accepted are independent components, while a
            # StaticResource's Logo.png and Logo.resource-meta.xml are one.
            name = parts[1].lower()
            if name.endswith("-meta.xml"):
                name = name[: -len("-meta.xml")]
            stem = name.rsplit(".", 1)[0]
            prefix = f"{parts[0].lower()}/{stem}"

            def _sibling(rest: str) -> bool:
                # <stem>.<ext> payloads (no dot) or <stem>.<ext>-meta.xml
                # descriptors (<ext> itself contains no dot).
                if "." not in rest:
                    return True
                return rest.endswith("-meta.xml") and "." not in rest[
                    : -len("-meta.xml")
                ]

            self_key = norm_key(Path(disp))
            if any(
                k != self_key
                and (
                    k.startswith(prefix + "/")
                    or (k.startswith(prefix + ".") and _sibling(k[len(prefix) + 1:]))
                )
                for k in left_ix
            ):
                comp_prefix = prefix
        if comp_prefix is not None:
            add_key(norm_key(Path(disp)))
            for k in left_ix:
                if k.startswith(comp_prefix + "/"):
                    add_key(k)
                elif k.startswith(comp_prefix + "."):
                    rest = k[len(comp_prefix) + 1:]
                    if "." not in rest or (
                        k.endswith("-meta.xml")
                        and "." not in rest[: -len("-meta.xml")]
                    ):
                        add_key(k)
            continue
        add_key(norm_key(Path(disp)))
        if disp.endswith("-meta.xml"):
            add_key(norm_key(Path(disp[: -len("-meta.xml")])))
        else:
            add_key(norm_key(Path(disp + "-meta.xml")))
    return list(out.values())


def collect_deploy_files(
    result,
    baseline,
    pair_key: str | None = None,
    managed_left: frozenset[str] | None = None,
    managed_right: frozenset[str] | None = None,
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """(deploy_files, destroy_files) from ACTIVE drift only, as (abs, display)
    tuples. deploy = left copies of changed + left-only; destroy = right-only.

    Baseline-ignored and currently accepted entries are excluded everywhere the
    delta is consumed (manifests, ZIP bundles, validation), matching the
    summary classification. Stale acceptances count as active. *pair_key*
    scopes pair-qualified acceptances to this comparison.

    *managed_left* / *managed_right*: installed-package namespaces of the
    corresponding side's org. A file that exists only on one side and whose
    component is namespaced under a package installed there is installation
    drift — managed components cannot be deployed or destroyed via the
    Metadata API anyway, so they are excluded like ignored entries.
    """
    from mct.baseline import classify_entry
    from mct.comparison import managed_namespace_of

    lroot = getattr(result, "left_root", None)
    rroot = getattr(result, "right_root", None)
    managed_union = (managed_left or frozenset()) | (managed_right or frozenset())
    deploy: list[tuple[Path, str]] = []
    destroy: list[tuple[Path, str]] = []
    for lp, rp, ld, _ in result.differ_pairs:
        status, _ = classify_entry(
            ld, lp, rp, baseline, pair_key=pair_key,
            managed_namespaces=managed_union or None,
            left_root=lroot, right_root=rroot,
        )
        if status in ("active", "accepted_stale"):
            deploy.append((lp, ld))
    for k in result.only_left_keys:
        lp, disp = result.left_ix[k]
        if managed_namespace_of(disp, managed_left or frozenset()):
            continue
        status, _ = classify_entry(
            disp, lp, None, baseline, pair_key=pair_key,
            managed_namespaces=managed_union or None,
            left_root=lroot, right_root=rroot,
        )
        if status in ("active", "accepted_stale"):
            deploy.append((lp, disp))
    for k in result.only_right_keys:
        rp, disp = result.right_ix[k]
        if managed_namespace_of(disp, managed_right or frozenset()):
            continue
        status, _ = classify_entry(
            disp, None, rp, baseline, pair_key=pair_key,
            managed_namespaces=managed_union or None,
            left_root=lroot, right_root=rroot,
        )
        if status in ("active", "accepted_stale"):
            destroy.append((rp, disp))
    return deploy, destroy


def collect_reverse_sync_files(
    result,
    baseline,
    pair_key: str | None = None,
    managed_right: frozenset[str] | None = None,
) -> list[tuple[Path, str]]:
    """Right-tree files representing org-side drift worth pulling back into git:
    changed files (right copy) and right-only files, minus baseline-ignored and
    accepted entries (accepted-but-stale still counts — that drift moved again).

    *result* is a TreeCompareResult; returns (absolute right path, display) pairs.
    *pair_key* scopes pair-qualified acceptances to this comparison.
    *managed_right*: installed-package namespaces of the right side's org —
    right-only managed components are installation drift, not source drift,
    and are excluded like ignored entries.
    """
    from mct.baseline import classify_entry
    from mct.comparison import managed_namespace_of

    lroot = getattr(result, "left_root", None)
    rroot = getattr(result, "right_root", None)
    out: list[tuple[Path, str]] = []
    for lp, rp, ld, _ in result.differ_pairs:
        status, _detail = classify_entry(
            ld, lp, rp, baseline, pair_key=pair_key,
            managed_namespaces=managed_right,
            left_root=lroot, right_root=rroot,
        )
        if status in ("active", "accepted_stale"):
            out.append((rp, ld))
    for k in result.only_right_keys:
        rp, disp = result.right_ix[k]
        if managed_namespace_of(disp, managed_right or frozenset()):
            continue
        status, _detail = classify_entry(
            disp, None, rp, baseline, pair_key=pair_key,
            managed_namespaces=managed_right,
            left_root=lroot, right_root=rroot,
        )
        if status in ("active", "accepted_stale"):
            out.append((rp, disp))
    return out


def serialize_manifest(members_by_type: dict[str, set[str]], api_version: str) -> str:
    """Serialize {type -> members} as canonical package.xml text ('' if empty)."""
    from xml.sax.saxutils import escape

    members_by_type = {t: m for t, m in members_by_type.items() if m}
    if not members_by_type:
        return ""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">',
    ]
    for md_type in sorted(members_by_type):
        lines.append("    <types>")
        for member in sorted(members_by_type[md_type]):
            lines.append(f"        <members>{escape(member)}</members>")
        lines.append(f"        <name>{escape(md_type)}</name>")
        lines.append("    </types>")
    lines.append(f"    <version>{api_version}</version>")
    lines.append("</Package>")
    return "\n".join(lines) + "\n"
