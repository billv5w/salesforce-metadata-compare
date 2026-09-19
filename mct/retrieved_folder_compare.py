"""
Shared tree comparison for retrieved metadata folders (walk + filecmp).

Uses case-insensitive path keys so the same logical file is not treated as
missing on one side due to casing. No dependency on the `diff` CLI.
"""

from __future__ import annotations

import filecmp
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


def index_tree(base: Path) -> dict[str, tuple[Path, str]]:
    """Map norm_key -> (absolute path, display path relative to base)."""
    out: dict[str, tuple[Path, str]] = {}
    base = base.resolve()
    for p in base.rglob("*"):
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


def compare_trees(
    left_root: Path,
    right_root: Path,
    xml_ignore_elements: frozenset[str] | None = None,
    xml_strip_defaults: bool = False,
    xml_ignore_by_type: dict[str, frozenset[str]] | None = None,
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
                xml_strip_defaults):
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
    )
