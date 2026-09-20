"""
Shared tree comparison for retrieved metadata folders (walk + filecmp).

Uses case-insensitive path keys so the same logical file is not treated as
missing on one side due to casing. No dependency on the `diff` CLI.
"""

from __future__ import annotations

import errno
import filecmp
import os
from dataclasses import dataclass
from pathlib import Path

from mct.xml_normalizer import xml_semantically_equal
from mct.json_normalizer import json_semantically_equal

# Text metadata whose bodies Salesforce stores without a trailing newline —
# a retrieve then differs from the POSIX-terminated git copy by one byte.
# Kept to an explicit suffix set so binary content is never normalized.
_TEXT_SUFFIXES = {
    ".cls", ".trigger", ".apex", ".js", ".css", ".html", ".cmp", ".page",
    ".component", ".app", ".evt", ".design", ".svg", ".txt", ".md", ".csv",
}


def text_equal_ignoring_line_endings(a: Path, b: Path) -> bool:
    """True when files differ only by CRLF vs LF and/or trailing whitespace."""

    def canon(p: Path) -> bytes:
        return p.read_bytes().replace(b"\r\n", b"\n").rstrip(b"\r\n\t ")

    try:
        return canon(a) == canon(b)
    except OSError:
        return False


def effective_ignore_for(
    display: str,
    global_ignore: frozenset[str] | None,
    by_type: dict[str, frozenset[str]] | None,
) -> frozenset[str] | None:
    """Combine global XML element ignores with any scoped to *display*'s first
    path segment. The single source of truth for scope matching — used by both
    the tree comparison and baseline fingerprints, which MUST agree or
    accepted diffs go stale on rules the compare applied.
    """
    if not by_type:
        return global_ignore
    top = display.replace("\\", "/").split("/")[0].lower()
    scoped = by_type.get(top)
    if not scoped:
        return global_ignore
    return (global_ignore or frozenset()) | scoped


def norm_key(rel: Path) -> str:
    """Case-insensitive path key on every platform.

    Windows AND default macOS filesystems are case-insensitive, and two SFDX
    files differing only by case are the same logical component anyway, so
    case-folding everywhere avoids false only-left/only-right pairs. Display
    paths keep their original casing.
    """
    return rel.as_posix().lower()


class LinkedMetadataError(RuntimeError):
    """A file or directory inside a compared tree is a symbolic link.

    Metadata trees must be real files: a link can point outside the
    comparison root, leaking arbitrary local files into reports and
    exports. Links are therefore rejected loudly rather than silently
    skipped or followed.
    """


def check_metadata_read(
    root: Path, path: Path, *, anchor: Path | None = None
) -> Path:
    """Verify *path* is safe to read as metadata inside trusted *root*;
    return the resolved path.

    Three independent fail-closed checks:

    1. Anchor stability — when *anchor* (the canonical root captured when
       the comparison was established, e.g. ``TreeCompareResult.left_root``)
       is given, it must still resolve to itself: a link swapped in AT the
       root or at any ancestor above it changes its resolution. The spelled
       root must likewise still resolve to that anchor — the boundary may
       never be redefined by re-resolving a replaced root. Legitimate system
       aliases (``/tmp`` → ``/private/tmp``) are canonicalized at selection
       time, so the established anchor keeps working; a redirected root or
       ancestor is rejected.
    2. Containment — the fully resolved path must stay beneath the
       resolved root.
    3. No symlink components beneath the root — every component between
       the root and the target is lstat-checked on the path's OWN
       spelling. Resolving first and testing ``is_symlink()`` afterwards
       would erase the evidence: a link pointing back inside the root
       would pass containment and leak through the ancestor.

    Raises LinkedMetadataError for links and escapes. Callers that cache
    comparison results MUST re-verify at read time — a cached index path
    can be swapped for a linked ancestor between requests — and MUST pass
    the established canonical root as *anchor* so a replaced root cannot
    re-anchor trust.
    """
    root_path = Path(os.path.normpath(str(root)))
    p = Path(path)
    if not p.is_absolute():
        p = root_path / p
    p = Path(os.path.normpath(str(p)))
    try:
        root_res = root_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise LinkedMetadataError(
            f"Trusted comparison root is not resolvable (link loop?): {root_path}"
        ) from exc
    if anchor is not None:
        anchor_path = Path(os.path.normpath(str(anchor)))
        try:
            anchor_res = anchor_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise LinkedMetadataError(
                "Trusted comparison root is not resolvable (link loop?): "
                f"{anchor_path}"
            ) from exc
        if anchor_path.is_symlink() or anchor_res != anchor_path:
            raise LinkedMetadataError(
                "Trusted comparison root was replaced or redirected after "
                "selection — refusing to re-anchor the boundary: "
                f"{anchor_path} → {anchor_res}"
            )
        if root_res != anchor_path:
            raise LinkedMetadataError(
                "Metadata path's root no longer resolves to the trusted "
                "comparison root established at selection: "
                f"{root_path} → {root_res}"
            )
        root_res = anchor_path
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:
        raise LinkedMetadataError(
            f"Metadata path is not resolvable (link loop?): {p}"
        ) from exc
    try:
        resolved.relative_to(root_res)
    except ValueError:
        raise LinkedMetadataError(
            "Metadata path resolves outside its trusted comparison root "
            f"(linked or traversed out of the tree): {p}"
        ) from None
    try:
        rel_parts = p.relative_to(root_path).parts
        walk_root = root_path
    except ValueError:
        try:
            rel_parts = p.relative_to(root_res).parts
            walk_root = root_res
        except ValueError:
            # Spelled outside both the given and the resolved root yet
            # resolves inside — reachable only through a link.
            raise LinkedMetadataError(
                f"Metadata path is only reachable through a link: {p}"
            ) from None
    cur = walk_root
    for part in rel_parts:
        cur = cur / part
        if cur.is_symlink():
            raise LinkedMetadataError(
                "Metadata path traverses a symbolic link beneath the "
                f"comparison root (links are never read): {cur}"
            )
    return resolved


def read_metadata_bytes(
    root: Path, path: Path, *, anchor: Path | None = None
) -> bytes:
    """check_metadata_read + byte read, with a no-follow open on the leaf
    where the platform supports it (O_NOFOLLOW) so a file swapped for a
    link after the checks cannot be read through."""
    safe = check_metadata_read(root, path, anchor=anchor)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(safe, flags), "rb") as fh:
            return fh.read()
    except OSError as exc:
        if exc.errno == errno.ELOOP or safe.is_symlink():
            raise LinkedMetadataError(
                f"Metadata file is a symbolic link (links are never read): {safe}"
            ) from exc
        raise


def index_tree(base: Path) -> dict[str, tuple[Path, str]]:
    """Map norm_key -> (absolute path, display path relative to base)."""
    out: dict[str, tuple[Path, str]] = {}
    base = base.resolve()
    for p in base.rglob("*"):
        if p.is_symlink():
            raise LinkedMetadataError(
                "Metadata tree contains a symbolic link (links are never "
                f"followed or compared): {p.relative_to(base).as_posix()}"
            )
        if not p.is_file():
            continue
        # Dotfiles (.gitkeep, .forceignore, .DS_Store) are git/OS placeholders —
        # a Salesforce org can never contain them, so they are comparison noise.
        if p.name.startswith("."):
            continue
        rel = p.relative_to(base)
        disp = rel.as_posix()
        k = norm_key(rel)
        if k in out:
            continue
        out[k] = (p, disp)
    return out


@dataclass(frozen=True)
class TreeCompareResult:
    left_ix: dict[str, tuple[Path, str]]
    right_ix: dict[str, tuple[Path, str]]
    only_left_keys: tuple[str, ...]
    only_right_keys: tuple[str, ...]
    identical_count: int
    # (left_abs, right_abs, left_disp, right_disp)
    differ_pairs: tuple[tuple[Path, Path, str, str], ...]
    identical_normalized_pairs: tuple[tuple[Path, Path, str, str], ...] = ()
    # Resolved comparison roots — trusted anchors for check_metadata_read.
    left_root: Path | None = None
    right_root: Path | None = None
    # In-scope reporting metrics, set only when a metadata-type scope was
    # applied (apply_type_scope). The indexes and identical_count above stay
    # whole-tree — companions, component expansion and destructive-overlap
    # checks need that context — while scoped_* are the values summaries,
    # history and reports must show so displayed numbers match the
    # advertised filter. Never mix their denominators.
    scoped_identical_count: int | None = None
    scoped_total_left: int | None = None
    scoped_total_right: int | None = None

    @property
    def visible_identical_count(self) -> int:
        return (
            self.identical_count
            if self.scoped_identical_count is None
            else self.scoped_identical_count
        )

    @property
    def visible_total_left(self) -> int:
        return (
            len(self.left_ix)
            if self.scoped_total_left is None
            else self.scoped_total_left
        )

    @property
    def visible_total_right(self) -> int:
        return (
            len(self.right_ix)
            if self.scoped_total_right is None
            else self.scoped_total_right
        )


def compare_trees(
    left_root: Path,
    right_root: Path,
    xml_ignore_elements: frozenset[str] | None = None,
    xml_strip_defaults: bool = False,
    xml_ignore_by_type: dict[str, frozenset[str]] | None = None,
    managed_namespaces: frozenset[str] | None = None,
) -> TreeCompareResult:
    left_ix = index_tree(left_root)
    right_ix = index_tree(right_root)

    lk = set(left_ix)
    rk = set(right_ix)

    only_left = tuple(sorted(lk - rk, key=str.lower))
    only_right = tuple(sorted(rk - lk, key=str.lower))
    common = lk & rk

    identical_count = 0
    differ_list: list[tuple[Path, Path, str, str]] = []
    identical_normalized_list: list[tuple[Path, Path, str, str]] = []

    for key in sorted(common, key=str.lower):
        lp, ld = left_ix[key]
        rp, rd = right_ix[key]
        if filecmp.cmp(lp, rp, shallow=False):
            identical_count += 1
        elif lp.suffix.lower() == ".xml" and xml_semantically_equal(
                lp, rp,
                effective_ignore_for(ld, xml_ignore_elements, xml_ignore_by_type),
                xml_strip_defaults,
                managed_namespaces=managed_namespaces):
            identical_count += 1  # formatting-only difference, not a real change
            identical_normalized_list.append((lp, rp, ld, rd))
        elif lp.suffix.lower() == ".json" and json_semantically_equal(lp, rp):
            identical_count += 1  # semantic equivalence for json
            identical_normalized_list.append((lp, rp, ld, rd))
        elif lp.suffix.lower() in _TEXT_SUFFIXES and text_equal_ignoring_line_endings(lp, rp):
            identical_count += 1  # line-ending / trailing-newline noise (Metadata API strips it)
            identical_normalized_list.append((lp, rp, ld, rd))
        else:
            differ_list.append((lp, rp, ld, rd))

    return TreeCompareResult(
        left_ix=left_ix,
        right_ix=right_ix,
        only_left_keys=only_left,
        only_right_keys=only_right,
        identical_count=identical_count,
        differ_pairs=tuple(differ_list),
        identical_normalized_pairs=tuple(identical_normalized_list),
        left_root=left_root.resolve(),
        right_root=right_root.resolve(),
    )


def type_scope_filter(
    include_types: list[str] | tuple[str, ...] | frozenset[str] | None,
    exclude_types: list[str] | tuple[str, ...] | frozenset[str] | None,
):
    """Return a predicate ``display_path -> bool`` matching the CLI diff
    scope rules: metadata folder (first path segment) names,
    case-insensitive; include list applied first, then exclude; exclusion
    wins."""
    inc = {str(t).strip().lower() for t in (include_types or []) if str(t).strip()}
    exc = {str(t).strip().lower() for t in (exclude_types or []) if str(t).strip()}

    def keep(display: str) -> bool:
        top = display.replace("\\", "/").split("/")[0].lower()
        if inc and top not in inc:
            return False
        return top not in exc

    return keep


def apply_type_scope(
    result: TreeCompareResult,
    include_types: list[str] | tuple[str, ...] | frozenset[str] | None,
    exclude_types: list[str] | tuple[str, ...] | frozenset[str] | None,
) -> TreeCompareResult:
    """Scope a comparison to include/exclude metadata type folders.

    The full file indexes are preserved — companions, whole components and
    destructive-overlap checks still need whole-tree context — while the
    drift lists are filtered so out-of-scope paths never surface in
    counts, selection or exports.
    """
    if not include_types and not exclude_types:
        return result
    keep = type_scope_filter(include_types, exclude_types)
    # In-scope metrics, computed against the full indexes: common keys are
    # partitioned into identical (exact + normalized) and differ, so the
    # scoped identical count is the scoped common minus the scoped differs —
    # normalized-identical pairs count as identical, matching identical_count.
    common = result.left_ix.keys() & result.right_ix.keys()
    scoped_common = sum(1 for k in common if keep(result.left_ix[k][1]))
    scoped_identical = scoped_common - sum(
        1 for p in result.differ_pairs if keep(p[2])
    )
    return TreeCompareResult(
        left_ix=result.left_ix,
        right_ix=result.right_ix,
        only_left_keys=tuple(
            k for k in result.only_left_keys if keep(result.left_ix[k][1])
        ),
        only_right_keys=tuple(
            k for k in result.only_right_keys if keep(result.right_ix[k][1])
        ),
        identical_count=result.identical_count,
        differ_pairs=tuple(p for p in result.differ_pairs if keep(p[2])),
        identical_normalized_pairs=tuple(
            p for p in result.identical_normalized_pairs if keep(p[2])
        ),
        left_root=result.left_root,
        right_root=result.right_root,
        scoped_identical_count=scoped_identical,
        scoped_total_left=sum(
            1 for _, d in result.left_ix.values() if keep(d)
        ),
        scoped_total_right=sum(
            1 for _, d in result.right_ix.values() if keep(d)
        ),
    )
