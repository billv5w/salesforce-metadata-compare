#!/usr/bin/env python3
"""
Local web server that renders metadata comparison results in a GitHub-style diff UI.

Usage:
  # Compare two directories (same args as the other scripts)
  python scripts/serve-diff-ui.py --left force-app/main/default --right retrieved-sit

  # Specify port
  python scripts/serve-diff-ui.py --left force-app/main/default --right retrieved-sit --port 8090

  # Launched automatically by env-compare.py ui
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_DIR = Path(__file__).resolve().parent

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
# Dev checkout (not pipx-installed): repo root holds the `mct` package, needed
# by _generate_manifest_via_sf's `import mct.safety`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mct.retrieved_folder_compare import (
    LinkedMetadataError,
    TreeCompareResult,
    apply_type_scope,
    check_metadata_read,
    compare_trees,
    norm_key,
    read_metadata_bytes,
)
from mct.xml_normalizer import _MAX_XML_BYTES
from mct.ui_server_base import BaseUIHandler, serve_ui

import mct.baseline as _baseline
import mct.config as _cfg
import mct.delta as _delta

UI_DIR = _SCRIPT_DIR / "ui"

# Project baseline file (set from --baseline at startup). None when launched
# standalone with plain paths — baseline features are then disabled.
BASELINE_FILE: Path | None = None
# Comparison-pair scope for acceptances (branch:main↔org:prod style);
# None when either side lacks provenance — acceptances are then global.
PAIR_KEY: str | None = None
# Installed-package namespaces of the org behind each side (empty when the
# side is not an org snapshot or no packages snapshot exists). Managed
# components present on only one side are classified apart from active
# drift, and profile grants referencing them normalise away.
MANAGED_LEFT: frozenset[str] = frozenset()
MANAGED_RIGHT: frozenset[str] = frozenset()
# Metadata-type scope (--include-type / --exclude-type, repeatable, from the
# orchestrator form or env-compare ui). None/empty = unscoped comparison.
INCLUDE_TYPES: frozenset[str] | None = None
EXCLUDE_TYPES: frozenset[str] | None = None


def _scope_key() -> tuple[frozenset[str], frozenset[str]]:
    """Effective (include, exclude) type scope — part of the comparison
    cache key so a scope change can never reuse another scope's result."""
    return (frozenset(INCLUDE_TYPES or ()), frozenset(EXCLUDE_TYPES or ()))


def active_baseline() -> dict:
    """Load the baseline fresh (accept/unaccept mutate it between requests)."""
    if BASELINE_FILE is None:
        return _baseline.default_baseline()
    return _baseline.load_baseline(BASELINE_FILE)


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _cached_comparison(
    left: Path,
    right: Path,
    scope_key: tuple[frozenset[str], frozenset[str]],
) -> TreeCompareResult:
    # XML element ignore rules affect the compare itself; they are read once
    # per (left, right) thanks to the cache — edit rules, restart the server.
    # The type scope is part of the cache key: results can never leak
    # between filter configurations.
    bl = active_baseline()
    ignore = _baseline.xml_ignore_elements(bl) or None
    result = compare_trees(
        left, right, ignore, _baseline.strip_retrieve_defaults(bl),
        xml_ignore_by_type=_baseline.xml_ignore_by_type(bl) or None,
        managed_namespaces=(MANAGED_LEFT | MANAGED_RIGHT) or None,
    )
    return apply_type_scope(result, scope_key[0], scope_key[1])


def get_comparison(
    left: Path,
    right: Path,
    scope_key: tuple[frozenset[str], frozenset[str]] | None = None,
) -> TreeCompareResult:
    """Comparison for (left, right) under the current module type scope.

    Thin uncached wrapper over the lru_cached worker: the LIVE scope is
    folded into the lookup key at call time, so a scope change can never
    return a previously cached result. Passing an explicit *scope_key*
    bypasses the module globals (tests)."""
    return _cached_comparison(
        left, right, _scope_key() if scope_key is None else scope_key
    )


# Tests/ops clear the comparison cache through the public name.
get_comparison.cache_clear = _cached_comparison.cache_clear  # type: ignore[attr-defined]


def repo_relative(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def is_binary(path: Path) -> bool:
    if path.is_symlink():
        # Links are never read — treat as non-text so no caller proceeds
        # to a text read through them.
        return True
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return True
    return b"\x00" in chunk


def file_diff(left_path: Path, right_path: Path, ignore_ws: bool = False, ignore_case: bool = False,
              *, left_root: Path | None = None, right_root: Path | None = None) -> dict:
    """Generate unified diff data for a single file pair.

    With *left_root*/*right_root* the full metadata-read policy applies:
    no symlink may appear on ANY component beneath the trusted comparison
    root and containment is verified before a byte is read — a cached
    index path whose ancestor was swapped for a link after the compare
    cannot leak external content into the diff. The roots double as the
    established anchors: pass the canonical ``TreeCompareResult`` roots so
    a replaced root or ancestor is rejected instead of re-anchoring trust."""
    def _linked_error() -> dict:
        return {
            "error": "Symbolic links are never read as metadata "
                     "(a link can point outside the compared tree)",
            "diff": "",
        }

    try:
        if left_root is not None:
            left_path = check_metadata_read(left_root, left_path, anchor=left_root)
        if right_root is not None:
            right_path = check_metadata_read(right_root, right_path, anchor=right_root)
    except LinkedMetadataError:
        return _linked_error()
    if left_path.is_symlink() or right_path.is_symlink():
        return _linked_error()
    if not left_path.is_file() or not right_path.is_file():
        return {"error": "File missing on one side", "diff": ""}

    if is_binary(left_path) or is_binary(right_path):
        return {"error": "Binary file", "diff": ""}

    try:
        if left_path.suffix.lower() == ".xml":
            from mct.xml_normalizer import normalize_xml_lines
            try:
                l_lines = normalize_xml_lines(left_path)
                r_lines = normalize_xml_lines(right_path)
            except Exception:
                l_lines = left_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
                r_lines = right_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
        elif left_path.suffix.lower() == ".json":
            from mct.json_normalizer import normalize_json
            try:
                l_txt = normalize_json(left_path.read_text(encoding="utf-8", errors="replace"))
                r_txt = normalize_json(right_path.read_text(encoding="utf-8", errors="replace"))
                l_lines = l_txt.splitlines(keepends=True)
                r_lines = r_txt.splitlines(keepends=True)
            except Exception:
                l_lines = left_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
                r_lines = right_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
        else:
            l_lines = left_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
            r_lines = right_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    except OSError as e:
        return {"error": str(e), "diff": ""}

    if ignore_ws:
        def strip_ws(lines):
            return [line.strip() + "\n" for line in lines]
        l_lines = strip_ws(l_lines)
        r_lines = strip_ws(r_lines)

    if ignore_case:
        l_lines = [line.lower() for line in l_lines]
        r_lines = [line.lower() for line in r_lines]

    diff_text = "".join(difflib.unified_diff(
        l_lines, r_lines,
        fromfile=repo_relative(left_path),
        tofile=repo_relative(right_path),
    ))

    if not diff_text:
        diff_text = ""

    return {"error": None, "diff": diff_text}


def metadata_type(disp_path: str) -> str:
    """Extract the first folder component as the metadata type."""
    parts = disp_path.split("/")
    return parts[0] if parts else "(root)"


def build_summary(left_root: Path, right_root: Path, left_rel: str, right_rel: str) -> dict:
    """Build the full comparison summary as a JSON-serializable dict."""
    cmp = get_comparison(left_root, right_root)
    baseline = active_baseline()

    # Baseline classification: ignored/accepted drift renders in its own
    # sections, never counted as active and never silently dropped. Accepted
    # entries whose content changed resurface as active with a stale flag.
    ignored: list[dict] = []
    accepted: list[dict] = []

    def _classify(disp: str, lp: Path | None, rp: Path | None, raw_status: str):
        base = {"path": disp, "type": metadata_type(disp), "side_status": raw_status}
        # Managed-package components present on only one side are
        # installation drift, not source drift — classified before baseline.
        if lp is None or rp is None:
            from mct.comparison import managed_namespace_of
            side = MANAGED_LEFT if rp is None else MANAGED_RIGHT
            ns = managed_namespace_of(disp, side)
            if ns is not None:
                ignored.append({**base, "status": "ignored", "rule": f"managed:{ns}"})
                return None
        status, detail = _baseline.classify_entry(
            disp, lp, rp, baseline, pair_key=PAIR_KEY,
            managed_namespaces=(MANAGED_LEFT | MANAGED_RIGHT) or None,
            left_root=cmp.left_root, right_root=cmp.right_root,
        )
        if status == "ignored":
            ignored.append({**base, "status": "ignored", "rule": detail or ""})
            return None
        if status == "accepted":
            accepted.append({**base, "status": "accepted", "accepted_at": detail or ""})
            return None
        return status  # "active" or "accepted_stale"

    only_left = []
    for k in cmp.only_left_keys:
        lp, disp = cmp.left_ix[k]
        status = _classify(disp, lp, None, "only_left")
        if status is None:
            continue
        entry: dict[str, Any] = {"path": disp, "type": metadata_type(disp), "status": "only_left"}
        if status == "accepted_stale":
            entry["accepted_stale"] = True
        only_left.append(entry)

    only_right = []
    for k in cmp.only_right_keys:
        rp, disp = cmp.right_ix[k]
        status = _classify(disp, None, rp, "only_right")
        if status is None:
            continue
        entry = {"path": disp, "type": metadata_type(disp), "status": "only_right"}
        if status == "accepted_stale":
            entry["accepted_stale"] = True
        only_right.append(entry)

    differ = []
    for lp, rp, ld, rd in cmp.differ_pairs:
        status = _classify(ld, lp, rp, "different")
        if status is None:
            continue
        entry = {
            "path": ld,
            "left_abs": str(lp),
            "right_abs": str(rp),
            "type": metadata_type(ld),
            "status": "different",
        }
        if status == "accepted_stale":
            entry["accepted_stale"] = True
        # Binary bodies have no text diff to render — flag them so reports
        # and the detail pane can explain instead of showing nothing.
        try:
            lp_safe: Path | None = check_metadata_read(
                left_root, lp, anchor=cmp.left_root)
            rp_safe: Path | None = check_metadata_read(
                right_root, rp, anchor=cmp.right_root)
        except LinkedMetadataError:
            lp_safe = rp_safe = None
        try:
            if (lp_safe is not None and rp_safe is not None
                    and lp_safe.is_file() and rp_safe.is_file()
                    and (is_binary(lp_safe) or is_binary(rp_safe))):
                entry["binary"] = True
        except OSError:
            pass
        # XML files over the normalizer's size cap were compared byte-for-byte
        # only — formatting noise was NOT filtered, so this "different" verdict
        # is lower-confidence than the rest. Surface that instead of hiding it.
        if ld.lower().endswith(".xml") and lp_safe is not None and rp_safe is not None:
            try:
                if (lp_safe.stat().st_size > _MAX_XML_BYTES
                        or rp_safe.stat().st_size > _MAX_XML_BYTES):
                    entry["normalization_skipped"] = True
            except OSError:
                pass
        differ.append(entry)

    identical_normalized = []
    for lp, rp, ld, rd in cmp.identical_normalized_pairs:
        identical_normalized.append({
            "path": ld,
            "left_abs": str(lp),
            "right_abs": str(rp),
            "type": metadata_type(ld),
            "status": "identical_normalized",
        })

    # Type breakdown counts
    type_counts: dict[str, dict[str, int]] = {}
    for item in only_left:
        t = item["type"]
        type_counts.setdefault(t, {"only_left": 0, "only_right": 0, "different": 0})
        type_counts[t]["only_left"] += 1
    for item in only_right:
        t = item["type"]
        type_counts.setdefault(t, {"only_left": 0, "only_right": 0, "different": 0})
        type_counts[t]["only_right"] += 1
    for item in differ:
        t = item["type"]
        type_counts.setdefault(t, {"only_left": 0, "only_right": 0, "different": 0})
        type_counts[t]["different"] += 1

    return {
        "left": left_rel,
        "right": right_rel,
        # Scoped view: totals/identical reconcile with the advertised
        # include/exclude scope (out-of-scope files are not counted).
        "total_left": cmp.visible_total_left,
        "total_right": cmp.visible_total_right,
        "identical_count": cmp.visible_identical_count,
        # Whole-tree context kept separate — the indexes stay complete for
        # component expansion; these raw numbers are context, not the
        # denominator for scoped drift metrics.
        "whole_tree": {
            "total_left": len(cmp.left_ix),
            "total_right": len(cmp.right_ix),
            "identical_count": cmp.identical_count,
        },
        "identical_normalized_count": len(cmp.identical_normalized_pairs),
        "different_count": len(differ),
        "only_left_count": len(only_left),
        "only_right_count": len(only_right),
        "ignored_count": len(ignored),
        "accepted_count": len(accepted),
        "only_left": sorted(only_left, key=lambda x: x["path"].lower()),
        "only_right": sorted(only_right, key=lambda x: x["path"].lower()),
        "differ": sorted(differ, key=lambda x: x["path"].lower()),
        "identical_normalized": sorted(identical_normalized, key=lambda x: x["path"].lower()),
        "ignored": sorted(ignored, key=lambda x: x["path"].lower()),
        "accepted": sorted(accepted, key=lambda x: x["path"].lower()),
        "baseline_enabled": BASELINE_FILE is not None,
        "type_counts": dict(sorted(type_counts.items(), key=lambda x: x[0].lower())),
        "scope": {
            "include_types": sorted(INCLUDE_TYPES or ()),
            "exclude_types": sorted(EXCLUDE_TYPES or ()),
        },
    }


def _embed_safe(text: str, tag: str) -> str:
    """Neutralize the only sequences inside embedded <script>/<style> content
    that can end the element early: ``</tag`` and ``<!--``. A blanket ``</``
    escape would corrupt JS regex literals like ``/<\\//g``; inside strings
    and regexes ``<\\/tag`` and ``<\\!--`` evaluate identically."""
    return text.replace(f"</{tag}", f"<\\/{tag}").replace("<!--", "<\\!--")


def _file_meta(p: Path, root: Path, *, anchor: Path | None = None) -> str:
    """Human-readable size+hash summary for report entries (binary changes).
    Reads through the trusted-root policy — linked paths are never hashed."""
    import hashlib

    try:
        data = read_metadata_bytes(root, p, anchor=anchor)
    except (OSError, LinkedMetadataError):
        return "unreadable"
    return f"{len(data)} bytes · sha256 {hashlib.sha256(data).hexdigest()[:12]}"


def _build_standalone_report(handler) -> str:
    """Complete offline HTML report: vendored diff2html assets inlined, diff
    content embedded, retrieval-completeness warnings, and
    active/accepted/ignored status lists. No CDN, no server, no token —
    everything needed renders from this one file.

    Every active changed file gets an entry — a binary body, a vanished or
    linked file, or a render failure all produce an explanatory section so
    a changed count can never contradict an empty file list.
    """
    import html as _esc
    from urllib.parse import quote

    from mct.comparison import _provenance_warnings

    cmp = get_comparison(handler.left_root, handler.right_root)
    summary = build_summary(
        handler.left_root, handler.right_root, handler.left_rel, handler.right_rel
    )

    stale = {d["path"] for d in summary["differ"] if d.get("accepted_stale")}
    # The "Changed files (active)" section shows only baseline-classified
    # active drift — accepted/ignored entries appear in their own sections,
    # never as unlabeled active diffs contradicting the counts.
    active_paths = {d["path"] for d in summary["differ"]}
    # One entry per active changed file: either renderable text or an
    # explicit explanation. (path, kind, diff_text, notice, is_stale)
    entries: list[dict[str, Any]] = []
    for lp, rp, ld, _ in cmp.differ_pairs:
        if ld not in active_paths:
            continue
        entry: dict[str, Any] = {
            "path": ld, "stale": ld in stale, "diff": None,
            "kind": "diff", "notice": "", "meta": "",
        }
        try:
            lp_safe: Path | None = check_metadata_read(
                handler.left_root, lp, anchor=cmp.left_root)
            rp_safe: Path | None = check_metadata_read(
                handler.right_root, rp, anchor=cmp.right_root)
        except LinkedMetadataError:
            lp_safe = rp_safe = None
        if lp_safe is None or rp_safe is None:
            entry.update(
                kind="linked",
                notice="Symbolic link — link content is never read or rendered",
            )
        elif not lp_safe.is_file() or not rp_safe.is_file():
            entry.update(
                kind="missing",
                notice="File missing or unreadable on one side",
            )
        elif is_binary(lp_safe) or is_binary(rp_safe):
            entry.update(
                kind="binary",
                notice="Binary content changed — no text diff available",
                meta=(
                    f"left: {_file_meta(lp_safe, handler.left_root, anchor=cmp.left_root)} · "
                    f"right: {_file_meta(rp_safe, handler.right_root, anchor=cmp.right_root)}"
                ),
            )
        else:
            result = file_diff(
                lp_safe, rp_safe,
                left_root=cmp.left_root, right_root=cmp.right_root,
            )
            if result.get("error"):
                entry.update(
                    kind="error",
                    notice=f"Text diff unavailable: {result['error']}",
                )
            elif not result.get("diff"):
                entry.update(kind="empty", notice="No textual diff available")
            else:
                entry["diff"] = result["diff"]
        entries.append(entry)

    css = _embed_safe(
        (UI_DIR / "kit" / "vendor" / "diff2html.min.css").read_text(encoding="utf-8"),
        "style",
    )
    js = _embed_safe(
        (UI_DIR / "kit" / "vendor" / "diff2html.min.js").read_text(encoding="utf-8"),
        "script",
    )

    def esc(s) -> str:
        return _esc.escape(str(s), quote=True)

    def path_list(items, note_attr=None) -> str:
        if not items:
            return '<p class="muted">(none)</p>'
        rows = "".join(
            f'<li><code>{esc(i["path"])}</code>'
            + (
                f' <span class="muted">{esc(i[note_attr])}</span>'
                if note_attr and i.get(note_attr)
                else ""
            )
            + "</li>"
            for i in items
        )
        return f"<ul>{rows}</ul>"

    kind_labels = {
        "binary": "binary",
        "linked": "symbolic link",
        "missing": "unavailable",
        "error": "diff unavailable",
        "empty": "no text diff",
    }
    sections: list[str] = []
    for e in entries:
        badge = (
            ' <span class="badge stale">accepted — stale (changed since acceptance)</span>'
            if e["stale"]
            else ""
        )
        if e["diff"] is not None:
            sections.append(
                f'<section class="file"><h3><code>{esc(e["path"])}</code>{badge}</h3>'
                f'<div class="d2h" data-diff="{quote(e["diff"], safe="")}"></div></section>'
            )
        else:
            label = kind_labels.get(e["kind"], e["kind"])
            meta = f'<br><span class="muted">{esc(e["meta"])}</span>' if e["meta"] else ""
            sections.append(
                f'<section class="file"><h3><code>{esc(e["path"])}</code>{badge}'
                f' <span class="badge {esc(e["kind"])}">{esc(label)}</span></h3>'
                f'<p class="muted">{esc(e["notice"])}{meta}</p></section>'
            )
    changed_html = "\n".join(sections) or '<p class="muted">(no changed files)</p>'

    # Retrieval completeness: provenance warnings (skipped types, retrieve
    # warnings, API-version mismatch) plus the recorded per-component
    # retrieve problems — an incomplete snapshot must never read as a clean
    # comparison. Warning text is untrusted — escaped like everything else.
    left_info = getattr(handler, "left_info", None)
    right_info = getattr(handler, "right_info", None)
    prov_warns = _provenance_warnings(left_info, right_info)
    warning_rows = "".join(f"<li>{esc(w)}</li>" for w in prov_warns)
    for side_label, info in (("Left", left_info), ("Right", right_info)):
        for w in (info or {}).get("retrieve_warnings") or []:
            warning_rows += f"<li>{side_label} retrieve warning: {esc(w)}</li>"
    warnings_html = (
        f'<h2>Snapshot completeness warnings</h2>\n<ul class="warnings">{warning_rows}</ul>'
        if warning_rows
        else ""
    )

    # The comparison may be scoped to a subset of metadata types — the
    # report must say so or a filtered result reads as a complete compare.
    scope = summary["scope"]
    scope_note = ""
    if scope["include_types"]:
        scope_note += (
            f'<p class="muted">Scoped to metadata type(s): '
            f'<b>{esc(", ".join(scope["include_types"]))}</b> — '
            "other types are not shown in this report.</p>"
        )
    if scope["exclude_types"]:
        scope_note += (
            f'<p class="muted">Excluded metadata type(s): '
            f'<b>{esc(", ".join(scope["exclude_types"]))}</b>.</p>'
        )

    title = f"metadata-compare: {handler.left_rel} vs {handler.right_rel}"
    stats = (
        f'{summary["different_count"]} changed · '
        f'{summary["only_left_count"]} only left · '
        f'{summary["only_right_count"]} only right · '
        f'{summary["accepted_count"]} accepted · '
        f'{summary["ignored_count"]} ignored · '
        f'{summary["identical_normalized_count"]} identical after normalization'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- diff rendering: diff2html v3.4.48, MIT License (c) Rodrigo Fernandes —
     https://github.com/rtfpessoa/diff2html — vendored under scripts/ui/kit/vendor/ -->
<title>{esc(title)}</title>
<style>{css}</style>
<style>
body {{ font-family: ui-sans-serif, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; margin: 24px; }}
h1 {{ font-size: 20px; }}
h2 {{ font-size: 15px; margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
.stats {{ color: #555; margin-bottom: 16px; }}
.muted {{ color: #888; }}
.badge {{ font-size: 11px; border: 1px solid #b58105; color: #8a6104; border-radius: 10px; padding: 1px 8px; }}
.badge.binary, .badge.linked {{ border-color: #8250df; color: #8250df; }}
.warnings li {{ color: #8a6104; }}
.file {{ margin-top: 18px; }}
ul {{ line-height: 1.7; }}
</style>
</head><body>
<h1>{esc(title)}</h1>
<div class="stats">{esc(stats)}</div>
{scope_note}
{warnings_html}
<p class="muted">Self-contained report generated by metadata-compare-tool —
all renderer assets and diff content are inlined; works fully offline.</p>

<h2>Changed files (active)</h2>
{changed_html}

<h2>Only in left ({esc(handler.left_rel)})</h2>
{path_list(summary["only_left"])}

<h2>Only in right ({esc(handler.right_rel)})</h2>
{path_list(summary["only_right"])}

<h2>Accepted diffs</h2>
{path_list(summary["accepted"], "accepted_at")}

<h2>Ignored</h2>
{path_list(summary["ignored"], "rule")}

<details><summary class="muted">Identical after normalization ({summary["identical_normalized_count"]})</summary>
{path_list(summary["identical_normalized"])}
</details>

<script>{js}</script>
<script>
(function () {{
  var hosts = document.querySelectorAll(".d2h");
  for (var i = 0; i < hosts.length; i++) {{
    var host = hosts[i];
    var diffText = decodeURIComponent(host.getAttribute("data-diff") || "");
    new Diff2HtmlUI(host, diffText, {{
      drawFileList: false, matching: "lines",
      outputFormat: "line-by-line", highlight: true
    }}).draw();
  }}
}})();
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_KIT_CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript", ".md": "text/plain"}


class DiffUIHandler(BaseUIHandler):
    left_root: Path
    right_root: Path
    left_rel: str
    right_rel: str
    api_version: str = _cfg.DEFAULT_API_VERSION
    # Snapshot provenance (id/type/created_at/manifest_kind/branch/org_alias),
    # None when the tree was given as a plain path rather than a snapshot id.
    left_info: dict | None = None
    right_info: dict | None = None

    def _serve_kit_file(self, path: str):
        rel = path[len("/kit/") :]
        filepath = (UI_DIR / "kit" / rel).resolve()
        kit_root = (UI_DIR / "kit").resolve()
        if kit_root not in filepath.parents:
            self.send_error(404)
            return
        self._serve_file(filepath, _KIT_CONTENT_TYPES.get(filepath.suffix, "application/octet-stream"))

    def _route_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/export-manifest":
            self._handle_export_manifest()
        elif path == "/api/export-html":
            self._handle_export_html()
        elif path == "/api/export-bundle":
            self._handle_export_bundle()
        elif path == "/api/accept-diff":
            self._handle_accept_diff(accept=True)
        elif path == "/api/unaccept-diff":
            self._handle_accept_diff(accept=False)
        else:
            self.send_error(404)

    def _handle_accept_diff(self, accept: bool):
        if BASELINE_FILE is None:
            self._serve_json(
                {"ok": False, "error": "No baseline configured — launch the diff UI via "
                                        "`env-compare.py ui` (or the orchestrator) so the project "
                                        "baseline is attached."},
                status=400,
            )
            return
        body = self._read_body()
        if body is None:
            return
        disp = (body.get("path") or "").strip()
        if not disp:
            self._serve_json({"ok": False, "error": "Missing 'path'"}, status=400)
            return
        try:
            cmp = get_comparison(self.left_root, self.right_root)
        except Exception as exc:
            self._serve_json({"ok": False, "error": f"Comparison failed: {exc}"}, status=500)
            return
        key = norm_key(Path(disp))
        lp = cmp.left_ix[key][0] if key in cmp.left_ix else None
        rp = cmp.right_ix[key][0] if key in cmp.right_ix else None
        if lp is None and rp is None:
            self._serve_json({"ok": False, "error": f"File not found: {disp}"}, status=404)
            return
        if accept:
            try:
                entry = _baseline.accept_diff(
                    disp, lp, rp, note=(body.get("note") or ""), baseline_file=BASELINE_FILE,
                    pair_key=PAIR_KEY,
                    managed_namespaces=(MANAGED_LEFT | MANAGED_RIGHT) or None,
                    left_root=cmp.left_root, right_root=cmp.right_root,
                )
            except _baseline.FingerprintReadError as exc:
                self._serve_json({"ok": False, "error": str(exc)}, status=409)
                return
            self._serve_json({"ok": True, "accepted_at": entry["accepted_at"]})
        else:
            removed = _baseline.unaccept_diff(disp, baseline_file=BASELINE_FILE)
            self._serve_json({"ok": removed, "error": None if removed else "Not accepted"})

    def _handle_export_html(self):
        """Self-contained HTML report: vendored diff2html assets, diff
        content, and status lists are all inlined so the file works fully
        offline (no CDN, no server, no token)."""
        def _slug(s: str) -> str:
            return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "tree"

        try:
            html = _build_standalone_report(self)
        except Exception as exc:
            self._serve_json({"error": f"Report failed: {exc}"}, status=500)
            return
        self._serve_json({
            "html": html,
            "filename": (
                f"metadata-compare-{_slug(self.left_rel)}-vs-{_slug(self.right_rel)}.html"
            ),
        })

    def _collect_delta_files(self, selected_paths=None):
        """(deploy_files, destroy_files) as (abs path, display path) pairs,
        honoring baseline ignores/acceptances under the current pair scope —
        the same classification the summary shows. Selection may only narrow
        the active set, never smuggle accepted/ignored paths back in.

        ``selected_paths=None`` (omitted) means all active drift; an explicit
        list — including ``[]`` — means exactly those paths. Anything else is
        a client error and raises ValueError for the caller to reject.
        """
        if selected_paths is not None and (
            not isinstance(selected_paths, list)
            or not all(isinstance(p, str) for p in selected_paths)
        ):
            raise ValueError("selected_paths must be an array of strings")
        cmp = get_comparison(self.left_root, self.right_root)
        deploy_files, destroy_files = _delta.collect_deploy_files(
            cmp, active_baseline(), pair_key=PAIR_KEY,
            managed_left=MANAGED_LEFT, managed_right=MANAGED_RIGHT,
        )
        # Everything downstream — sf manifest resolution, the zip bundle,
        # context expansion — reads through these paths. A cached index
        # path whose ancestor was swapped for a link since the compare
        # could expose out-of-tree files, so each path is re-verified
        # against its trusted root right now (fail closed on ANY link
        # component beneath the root, not just the leaf). The comparison's
        # captured canonical roots anchor the check — a replaced root or
        # ancestor cannot re-anchor the boundary.
        for abs_path, _disp in deploy_files:
            check_metadata_read(self.left_root, abs_path, anchor=cmp.left_root)
        for abs_path, _disp in destroy_files:
            check_metadata_read(self.right_root, abs_path, anchor=cmp.right_root)
        if selected_paths is not None:
            sel = set(selected_paths)
            deploy_files = [e for e in deploy_files if e[1] in sel]
            destroy_files = [e for e in destroy_files if e[1] in sel]
        return deploy_files, destroy_files

    def _build_delta_manifests(self, deploy_files, destroy_files) -> dict:
        """Resolve delta files into package.xml / destructiveChanges.xml text
        plus warning/notes. Shared by manifest export and bundle export.

        The destructive side fails closed: when any org-only file exists, every
        resolution (deploy, destroy, and left-context for the overlap guard)
        must succeed, because a guessed member name in destructiveChanges.xml
        can delete the wrong component. The response then carries
        ``{"error": ..., "status": ...}`` instead of manifests.

        ``context_deploy_files`` lists extra left-tree files for components
        moved out of the destructive set — package.xml must never name a
        member whose source the bundle doesn't ship.
        """
        cmp = get_comparison(self.left_root, self.right_root)
        deploy_members, deploy_err = self._resolve_components_via_sf(
            [p for p, _ in deploy_files]
        )

        moved: list[str] = []
        context_deploy_files: list[tuple[Path, str]] = []
        destroy_members: dict[str, set[str]] = {}

        if destroy_files:
            destroy_members_res, destroy_err = self._resolve_components_via_sf(
                [p for p, _ in destroy_files]
            )
            try:
                context_dirs = self._left_context_dirs(
                    [d for _, d in destroy_files], anchor=cmp.left_root
                )
            except LinkedMetadataError as exc:
                return {
                    "error": (
                        f"{exc} — a context directory traverses a symbolic "
                        "link whose contents could escape the compared "
                        "tree; refusing export."
                    ),
                    "status": 409,
                }
            context_members, context_err = self._resolve_components_via_sf(context_dirs)
            problems = [
                e for e in (deploy_err, destroy_err, context_err) if e
            ]
            if (
                problems
                or deploy_members is None
                or destroy_members_res is None
                or context_members is None
            ):
                return {
                    "error": (
                        "Could not resolve the delta's components accurately "
                        f"({'; '.join(problems) or 'unknown error'}); refusing "
                        "to guess member names for a destructive export. "
                        "Retry when `sf project generate manifest` works."
                    ),
                    "status": 502,
                }
            destroy_members = destroy_members_res

            # Safety: a right-only file can live INSIDE a component that exists on
            # both sides (extra file in an LWC/Aura bundle, orphaned -meta.xml…).
            # SDR resolves such a file to its whole component, so a naive
            # destructiveChanges.xml would delete a component the source still
            # has. If the component also resolves from the left tree (deploy set
            # or the left copies of the destroy files' parent dirs), deploying it
            # is the correct remediation — move it out of the destructive set.
            for md_type, members in list(destroy_members.items()):
                deploy_set = deploy_members.get(md_type, set())
                context_set = context_members.get(md_type, set())
                overlap = members & (deploy_set | context_set)
                if overlap:
                    destroy_members[md_type] = members - overlap
                    deploy_members.setdefault(md_type, set()).update(overlap)
                    moved.extend(f"{md_type}:{m}" for m in sorted(overlap))

            if moved:
                # Components moved into package.xml purely via context overlap
                # contribute no files to deploy_files — ship their left-side
                # source so the manifest never lists a member without content.
                moved_set = set(moved)
                for d in context_dirs:
                    dir_members, dir_err = self._resolve_components_via_sf([d])
                    if dir_err or dir_members is None:
                        return {
                            "error": (
                                f"Could not verify which component {d.name} "
                                f"belongs to ({dir_err or 'unknown error'}); "
                                "refusing a destructive export."
                            ),
                            "status": 502,
                        }
                    hits = {
                        f"{t}:{m}"
                        for t, ms in dir_members.items()
                        for m in ms
                    } & moved_set
                    if hits:
                        for f in sorted(d.rglob("*")):
                            try:
                                check_metadata_read(
                                    self.left_root, f, anchor=cmp.left_root
                                )
                            except LinkedMetadataError:
                                return {
                                    "error": (
                                        f"Context component {d.name} contains a "
                                        f"symbolic link ({f.name}); links are "
                                        "never exported — refusing export."
                                    ),
                                    "status": 409,
                                }
                            if f.is_file():
                                context_deploy_files.append(
                                    (
                                        f,
                                        f.relative_to(
                                            cmp.left_root or self.left_root.resolve()
                                        ).as_posix(),
                                    )
                                )

        response: dict = {}
        if deploy_members is not None:
            response["package_xml"] = self._serialize_manifest(deploy_members)
        else:
            # No destructive content at stake — the folder-based heuristic is
            # acceptable for the deploy side alone (validation re-checks it).
            response["package_xml"] = self._build_manifest(
                [d for _, d in deploy_files]
            )
        response["destructive_changes_xml"] = self._serialize_manifest(destroy_members)
        if deploy_err:
            response["warning"] = (
                "Accurate manifest generation via `sf project generate manifest` failed "
                f"({deploy_err}); fell back to folder-based type inference, which "
                "may need manual review for non-standard or nested metadata types."
            )
        if moved:
            response["notes"] = (
                f"{len(moved)} component(s) had org-only files inside a component that "
                "also exists in source; deploying the source component replaces its "
                "contents, so they were moved from destructiveChanges.xml to package.xml: "
                + ", ".join(moved)
            )
        response["context_deploy_files"] = context_deploy_files
        return response

    def _handle_export_manifest(self):
        body = self._read_body()
        if body is None:
            return
        try:
            deploy_files, destroy_files = self._collect_delta_files(body.get("selected_paths"))
        except ValueError as e:
            self._serve_json({"error": str(e)}, status=400)
            return
        except Exception as exc:
            self._serve_json({"error": f"Comparison failed: {exc}"}, status=500)
            return
        manifests = self._build_delta_manifests(deploy_files, destroy_files)
        manifests.pop("context_deploy_files", None)
        status = manifests.pop("status", 200)
        self._serve_json(manifests, status=status)

    def _bundle_source_entries(self, deploy_files) -> list[tuple[Path, str]]:
        """Delta files expanded to a deployable set (companions + whole
        bundles); logic shared with validate-deploy via mct.delta."""
        cmp = get_comparison(self.left_root, self.right_root)
        return _delta.expand_deployable_files(cmp.left_ix, deploy_files)

    def _handle_export_bundle(self):
        """Zip of manifests + the actual delta source files, so the delta stays
        deployable after snapshots are pruned (roadmap 3.9)."""
        import io
        import zipfile

        body = self._read_body()
        if body is None:
            return
        strip_no_grant = bool(body.get("strip_no_grant"))
        try:
            deploy_files, destroy_files = self._collect_delta_files(body.get("selected_paths"))
        except ValueError as e:
            self._serve_json({"error": str(e)}, status=400)
            return
        except Exception as exc:
            self._serve_json({"error": f"Comparison failed: {exc}"}, status=500)
            return
        manifests = self._build_delta_manifests(deploy_files, destroy_files)
        context_files = manifests.pop("context_deploy_files", [])
        if manifests.get("error"):
            self._serve_json(
                {"error": manifests["error"]},
                status=manifests.get("status", 500),
            )
            return
        source_entries = self._bundle_source_entries(deploy_files + context_files)
        cmp = get_comparison(self.left_root, self.right_root)

        readme = (
            "# Metadata delta bundle\n\n"
            f"Left (source):  {self.left_rel}\n"
            f"Right (target): {self.right_rel}\n\n"
            "Contents:\n"
            "- `package.xml` — components to deploy (changed + missing on the target)\n"
            "- `destructiveChanges.xml` — components that exist only on the target\n"
            "- `delta-source/` — the source-format files behind package.xml, copied from the left tree\n\n"
            "## How to use\n\n"
            "1. Copy the contents of `delta-source/` into your DX project under\n"
            "   `force-app/main/default/` (paths line up 1:1).\n"
            "2. Validate first (no changes saved to the org):\n"
            "   `sf project deploy start --manifest package.xml --dry-run --target-org <alias>`\n"
            "3. Deploy: same command without `--dry-run`.\n"
            "4. To also delete the target-only components, add\n"
            "   `--post-destructive-changes destructiveChanges.xml` to the deploy —\n"
            "   review that file carefully first; deletions are not reversible.\n\n"
            "Generated by metadata-compare-tool. The tool itself never deploys —\n"
            "running the commands above is a deliberate, human action.\n"
        )
        if manifests.get("warning"):
            readme += f"\n> WARNING: {manifests['warning']}\n"
        if manifests.get("notes"):
            readme += f"\n> NOTE: {manifests['notes']}\n"

        stripped: list[str] = []

        def _file_bytes(abs_path: Path, disp: str) -> bytes:
            data = read_metadata_bytes(
                self.left_root, abs_path, anchor=cmp.left_root
            )
            if strip_no_grant and disp.lower().endswith(".xml"):
                from mct.permissions import strip_no_grant_permissions
                text, removed = strip_no_grant_permissions(
                    data.decode("utf-8", errors="replace")
                )
                if removed:
                    stripped.extend(f"{disp} → {r}" for r in removed)
                    return text.encode("utf-8")
            return data

        buf = io.BytesIO()
        missing: list[str] = []
        linked: list[str] = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if manifests.get("package_xml"):
                zf.writestr("package.xml", manifests["package_xml"])
            if manifests.get("destructive_changes_xml"):
                zf.writestr("destructiveChanges.xml", manifests["destructive_changes_xml"])
            for abs_path, disp in sorted(source_entries, key=lambda x: x[1].lower()):
                try:
                    safe_path = check_metadata_read(
                        self.left_root, abs_path, anchor=cmp.left_root
                    )
                except LinkedMetadataError:
                    # A path whose ancestor was swapped for a link after
                    # indexing could smuggle an out-of-tree file into the
                    # bundle — never read it.
                    linked.append(disp)
                    continue
                try:
                    zf.writestr(f"delta-source/{disp}", _file_bytes(safe_path, disp))
                except LinkedMetadataError:
                    # A root/ancestor redirection or a leaf swapped for a
                    # link between check and read — never read it.
                    linked.append(disp)
                except OSError:
                    missing.append(disp)
            if stripped:
                readme += (
                    f"\n## Stripped no-grant permission entries ({len(stripped)})\n\n"
                    "These granted no access and were removed to avoid deploy failures\n"
                    "for features/fields missing on the target:\n\n"
                    + "".join(f"- {s}\n" for s in stripped)
                )
            zf.writestr("README.md", readme)

        if linked:
            # A link in an exported bundle could leak an out-of-tree file —
            # fail the whole export rather than ship a partial one.
            self._serve_json(
                {
                    "error": (
                        f"{len(linked)} source file(s) are symbolic links — "
                        "links are never exported: "
                        + ", ".join(linked[:10])
                    )
                },
                status=409,
            )
            return

        if missing:
            # A manifest that names content the ZIP lacks is worse than no
            # export — the snapshot is stale; fail clearly instead.
            self._serve_json(
                {
                    "error": (
                        f"{len(missing)} source file(s) vanished between the "
                        "comparison and this export — rerun the comparison: "
                        + ", ".join(missing[:10])
                    )
                },
                status=409,
            )
            return

        self._serve_bytes(
            buf.getvalue(),
            200,
            {
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="metadata-delta-bundle.zip"',
            },
        )

    def _left_context_dirs(
        self, destroy_display_paths: list[str], *, anchor: Path | None = None
    ) -> list[Path]:
        """Left-tree directories corresponding to each destroy file's parent dir.

        A component's files are always colocated in one directory, so resolving
        these dirs tells us which destroy-side components also exist in source.

        Candidates are verified against the trusted left root BEFORE any
        resolution — resolving first and testing is_symlink() on the result
        would erase the evidence of a linked ancestor. *anchor* is the
        comparison's captured canonical root so a replaced root or ancestor
        is rejected rather than silently re-anchored."""
        dirs: dict[Path, None] = {}
        for disp in destroy_display_paths:
            parent = Path(disp).parent
            candidate = (
                self.left_root / parent if str(parent) != "." else self.left_root
            )
            candidate = check_metadata_read(
                self.left_root, candidate, anchor=anchor
            )
            if candidate.is_dir():
                dirs[candidate] = None
        return list(dirs)

    # Kept as class attributes/methods so tests and callers have one entry
    # point; the logic lives in mct.delta, shared with the CLI's retrieve-delta.
    _MANIFEST_BATCH_SIZE = _delta.MANIFEST_BATCH_SIZE

    def _resolve_components_via_sf(
        self, abs_paths: list[Path]
    ) -> tuple[dict[str, set[str]] | None, str | None]:
        return _delta.resolve_components_via_sf(abs_paths, self._MANIFEST_BATCH_SIZE)

    _parse_manifest_members = staticmethod(_delta.parse_manifest_members)

    def _serialize_manifest(self, members_by_type: dict[str, set[str]]) -> str:
        return _delta.serialize_manifest(members_by_type, self.api_version)


    def _build_manifest(self, file_paths: list[str]) -> str:
        if not file_paths:
            return ""

        # Group by first folder (approximate metadata type)
        # In a real SFDX tool this would use a proper registry, but this approximation works for most standard types
        type_mapping = {
            # Apex
            "classes": "ApexClass",
            "triggers": "ApexTrigger",
            "components": "ApexComponent",
            "pages": "ApexPage",
            # Automation
            "flows": "Flow",
            "flowDefinitions": "FlowDefinition",
            "workflows": "Workflow",
            "processBuilderFlows": "Flow",
            # Objects & Fields
            "objects": "CustomObject",
            "fields": "CustomField",
            "recordTypes": "RecordType",
            "listViews": "ListView",
            "validationRules": "ValidationRule",
            "webLinks": "WebLink",
            "sharingRules": "SharingRules",
            "compactLayouts": "CompactLayout",
            # UI
            "layouts": "Layout",
            "flexipages": "FlexiPage",
            "tabs": "CustomTab",
            "applications": "CustomApplication",
            "homePageLayouts": "HomePageLayout",
            "globalValueSets": "GlobalValueSet",
            "standardValueSets": "StandardValueSet",
            # Lightning
            "lwc": "LightningComponentBundle",
            "aura": "AuraDefinitionBundle",
            # Security & Access
            "permissionsets": "PermissionSet",
            "permissionsetgroups": "PermissionSetGroup",
            "profiles": "Profile",
            "roles": "Role",
            "groups": "Group",
            "sharingRules": "SharingRules",
            "customPermissions": "CustomPermission",
            # Settings & Metadata
            "labels": "CustomLabels",
            "settings": "Settings",
            "customMetadata": "CustomMetadata",
            "namedCredentials": "NamedCredential",
            "remoteSiteSettings": "RemoteSiteSetting",
            "cspTrustedSites": "CspTrustedSite",
            "connectedApps": "ConnectedApp",
            "authproviders": "AuthProvider",
            # Reports & Dashboards
            "reports": "Report",
            "reportTypes": "ReportType",
            "dashboards": "Dashboard",
            # Email
            "email": "EmailTemplate",
            "emailservices": "EmailServicesFunction",
            "letterhead": "Letterhead",
            # Integration
            "queues": "Queue",
            "topics": "Topic",
            "approvalProcesses": "ApprovalProcess",
            "escalationRules": "EscalationRules",
            "assignmentRules": "AssignmentRules",
            "autoResponseRules": "AutoResponseRules",
            # Static Resources & Content
            "staticresources": "StaticResource",
            "contentassets": "ContentAsset",
            "documents": "Document",
        }

        members_by_type: dict[str, set[str]] = {}

        for fp in file_paths:
            parts = fp.split("/")
            if len(parts) < 2:
                continue

            folder = parts[0]
            # Get base name without extension
            filename = parts[-1]
            if "." in filename:
                member = filename.split(".")[0]
            else:
                member = filename

            # For objects/fields it's ObjectName.FieldName
            if folder == "objects" and len(parts) > 2:
                obj_name = parts[1]
                if parts[2] == "fields":
                    member = f"{obj_name}.{member}"
                else:
                    # just object
                    member = obj_name

            # For LWC/Aura it's the folder name
            if folder in ("lwc", "aura") and len(parts) > 2:
                member = parts[1]

            md_type = type_mapping.get(folder, folder.capitalize())

            if md_type not in members_by_type:
                members_by_type[md_type] = set()

            if member and member not in members_by_type[md_type]:
                members_by_type[md_type].add(member)

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
        ]

        for md_type, members in sorted(members_by_type.items()):
            if not members:
                continue
            lines.append('    <types>')
            for member in sorted(members):
                lines.append(f'        <members>{member}</members>')
            lines.append(f'        <name>{md_type}</name>')
            lines.append('    </types>')

        lines.append(f'    <version>{self.api_version}</version>')
        lines.append('</Package>')

        return "\n".join(lines)

    def _route_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_bootstrap_html(UI_DIR / "index.html")
        elif path == "/diff.js":
            self._serve_file(UI_DIR / "diff.js", "application/javascript")
        elif path.startswith("/kit/"):
            self._serve_kit_file(path)
        elif path == "/api/summary":
            try:
                data = build_summary(
                    self.left_root, self.right_root, self.left_rel, self.right_rel
                )
            except Exception as exc:
                # Unsafe trees (e.g. a symbolic link under the compare root)
                # must fail closed with a readable error, not a dropped
                # connection or a partial summary.
                self._serve_json({"error": f"Comparison failed: {exc}"}, status=500)
                return
            from mct.comparison import _provenance_warnings
            data["provenance_warnings"] = _provenance_warnings(
                self.left_info, self.right_info
            )
            data["left_info"] = self.left_info
            data["right_info"] = self.right_info
            self._serve_json(data)
        elif path == "/api/diff":
            qs = parse_qs(parsed.query)
            file_path = qs.get("path", [None])[0]
            ignore_ws = qs.get("ignore_ws", ["0"])[0] == "1"
            ignore_case = qs.get("ignore_case", ["0"])[0] == "1"
            if not file_path:
                self._serve_json({"error": "Missing ?path= parameter"}, status=400)
                return
            try:
                cmp = get_comparison(self.left_root, self.right_root)
            except Exception as exc:
                self._serve_json({"error": f"Comparison failed: {exc}"}, status=500)
                return
            # Find the file pair
            key = norm_key(Path(file_path))
            if key in cmp.left_ix and key in cmp.right_ix:
                lp, _ = cmp.left_ix[key]
                rp, _ = cmp.right_ix[key]
                result = file_diff(
                    lp, rp, ignore_ws=ignore_ws, ignore_case=ignore_case,
                    left_root=cmp.left_root, right_root=cmp.right_root,
                )
                self._serve_json(result)
            else:
                # It's an only-left or only-right file, show full content
                if key in cmp.left_ix:
                    fp, _ = cmp.left_ix[key]
                    self._serve_file_content(fp, "left")
                elif key in cmp.right_ix:
                    fp, _ = cmp.right_ix[key]
                    self._serve_file_content(fp, "right")
                else:
                    self._serve_json({"error": f"File not found: {file_path}"}, status=404)
        else:
            self.send_error(404)

    def _serve_file_content(self, filepath: Path, side: str):
        """Serve content of a file that only exists on one side."""
        root = self.left_root if side == "left" else self.right_root
        try:
            cmp = get_comparison(self.left_root, self.right_root)
            anchor = cmp.left_root if side == "left" else cmp.right_root
            filepath = check_metadata_read(root, filepath, anchor=anchor)
        except LinkedMetadataError:
            self._serve_json({
                "error": "Symbolic links are never read as metadata "
                         "(a link can point outside the compared tree)",
                "diff": "", "side": side,
            })
            return
        if is_binary(filepath):
            self._serve_json({"error": "Binary file", "diff": "", "side": side})
            return
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self._serve_json({"error": str(e), "diff": "", "side": side})
            return

        lines = content.splitlines(keepends=True)
        rel = repo_relative(filepath)
        if side == "left":
            diff_text = "".join(difflib.unified_diff(lines, [], fromfile=rel, tofile="/dev/null"))
        else:
            diff_text = "".join(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=rel))

        self._serve_json({"error": None, "diff": diff_text, "side": side})


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------

def make_handler(left_root: Path, right_root: Path, left_rel: str, right_rel: str,
                 api_version: str = _cfg.DEFAULT_API_VERSION,
                 left_info: dict | None = None, right_info: dict | None = None):
    class _Handler(DiffUIHandler):
        pass
    _Handler.left_root = left_root
    _Handler.right_root = right_root
    _Handler.left_rel = left_rel
    _Handler.right_rel = right_rel
    _Handler.api_version = api_version
    _Handler.left_info = left_info
    _Handler.right_info = right_info
    return _Handler


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_tree_arg(arg: str) -> tuple[Path, str]:
    raw = arg.replace("\\", "/").strip()
    p = Path(raw)
    if p.is_absolute():
        resolved = p.resolve()
        try:
            rel = resolved.relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            rel = resolved.as_posix()
        return resolved, rel
    rel = raw.strip("/")
    return (ROOT / rel).resolve(), rel


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Serve a GitHub-style diff UI for metadata comparison.")
    p.add_argument("--left", required=True, metavar="PATH", help="Left tree (repo-relative or absolute).")
    p.add_argument("--right", required=True, metavar="PATH", help="Right tree (repo-relative or absolute).")
    p.add_argument("--port", type=int, default=8089, help="Port to serve on (default: 8089).")
    p.add_argument("--no-open", action="store_true", help="Do not auto-open the browser.")
    p.add_argument("--api-version", default=_cfg.default_api_version(),
                   help="Metadata API version stamped into exported manifests "
                        "(default: sfdx-project.json sourceApiVersion, else "
                        f"{_cfg.DEFAULT_API_VERSION}).")
    p.add_argument("--left-info", default=None, metavar="JSON",
                   help="Snapshot provenance JSON for the left tree (id/type/created_at/manifest_kind/...).")
    p.add_argument("--right-info", default=None, metavar="JSON",
                   help="Snapshot provenance JSON for the right tree.")
    p.add_argument("--baseline", default=None, metavar="PATH",
                   help="Project baseline.json (ignore rules + accepted diffs). "
                        "Enables the Accept-diff feature. XML element rules are read "
                        "once per compare — restart after editing them.")
    p.add_argument("--include-type", action="append", default=[], metavar="TYPE",
                   help="Limit the comparison to this metadata type folder "
                        "(repeatable, case-insensitive).")
    p.add_argument("--exclude-type", action="append", default=[], metavar="TYPE",
                   help="Exclude this metadata type folder from the comparison "
                        "(repeatable; exclusion wins over includes).")
    return p.parse_args()


def _parse_info(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def main() -> int:
    args = parse_args()
    left_root, left_rel = parse_tree_arg(args.left)
    right_root, right_rel = parse_tree_arg(args.right)

    if not left_root.is_dir():
        print(f"Not a directory: {left_root}", file=sys.stderr)
        return 1
    if not right_root.is_dir():
        print(f"Not a directory: {right_root}", file=sys.stderr)
        return 1

    if args.baseline:
        global BASELINE_FILE
        BASELINE_FILE = Path(args.baseline).expanduser()

    left_info = _parse_info(args.left_info)
    right_info = _parse_info(args.right_info)
    global PAIR_KEY
    PAIR_KEY = _baseline.pair_key_for(left_info, right_info)

    global MANAGED_LEFT, MANAGED_RIGHT
    from mct.comparison import managed_namespaces_for_org
    MANAGED_LEFT = (
        managed_namespaces_for_org(left_info["org_alias"])
        if left_info and left_info.get("org_alias") else frozenset()
    )
    MANAGED_RIGHT = (
        managed_namespaces_for_org(right_info["org_alias"])
        if right_info and right_info.get("org_alias") else frozenset()
    )

    global INCLUDE_TYPES, EXCLUDE_TYPES
    INCLUDE_TYPES = frozenset(
        str(t).strip().lower() for t in (args.include_type or []) if str(t).strip()
    ) or None
    EXCLUDE_TYPES = frozenset(
        str(t).strip().lower() for t in (args.exclude_type or []) if str(t).strip()
    ) or None

    handler_cls = make_handler(
        left_root, right_root, left_rel, right_rel, args.api_version,
        left_info, right_info,
    )

    print(f"  Left:  {left_rel}")
    print(f"  Right: {right_rel}")

    return serve_ui(
        handler_cls, args.port, url_marker="METADATA_COMPARE_DIFF_UI_URL", open_browser=not args.no_open
    )


if __name__ == "__main__":
    raise SystemExit(main())
