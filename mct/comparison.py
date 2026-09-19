"""Comparison, diff, and deletion operations."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import mct.config as _cfg
import mct.index as _idx
from mct.index import (
    abs_snapshot_or_project_rel,
    load_comparison_index,
    load_index,
    load_snapshot_row_by_id,
    resolve_snapshot_or_path,
    save_index,
)
from mct.safety import SCRIPT_DIR
from mct.snapshot import (
    _package_key,
    _package_version,
    count_files_under,
    load_installed_packages_json,
)


def _classify_result(
    result: Any, baseline: dict[str, Any], pair_key: str | None = None
) -> dict[str, list[Any]]:
    """Split a TreeCompareResult into active / ignored / accepted per the baseline.

    Returns lists under: different_files, only_left_files, only_right_files
    (active drift, including accepted-but-changed), ignored_files,
    accepted_files, accepted_stale_files.
    """
    import mct.baseline as _bl

    different_files: list[str] = []
    only_left_files: list[str] = []
    only_right_files: list[str] = []
    ignored_files: list[dict[str, str]] = []
    accepted_files: list[dict[str, str]] = []
    accepted_stale_files: list[str] = []

    for lp, rp, ld, _ in result.differ_pairs:
        status, detail = _bl.classify_entry(ld, lp, rp, baseline, pair_key=pair_key)
        if status == "ignored":
            ignored_files.append({"path": ld, "rule": detail or ""})
        elif status == "accepted":
            accepted_files.append({"path": ld, "accepted_at": detail or ""})
        else:
            if status == "accepted_stale":
                accepted_stale_files.append(ld)
            different_files.append(ld)

    for k in result.only_left_keys:
        lp, disp = result.left_ix[k]
        status, detail = _bl.classify_entry(disp, lp, None, baseline, pair_key=pair_key)
        if status == "ignored":
            ignored_files.append({"path": disp, "rule": detail or ""})
        elif status == "accepted":
            accepted_files.append({"path": disp, "accepted_at": detail or ""})
        else:
            if status == "accepted_stale":
                accepted_stale_files.append(disp)
            only_left_files.append(disp)

    for k in result.only_right_keys:
        rp, disp = result.right_ix[k]
        status, detail = _bl.classify_entry(disp, None, rp, baseline, pair_key=pair_key)
        if status == "ignored":
            ignored_files.append({"path": disp, "rule": detail or ""})
        elif status == "accepted":
            accepted_files.append({"path": disp, "accepted_at": detail or ""})
        else:
            if status == "accepted_stale":
                accepted_stale_files.append(disp)
            only_right_files.append(disp)

    return {
        "different_files": different_files,
        "only_left_files": only_left_files,
        "only_right_files": only_right_files,
        "ignored_files": ignored_files,
        "accepted_files": accepted_files,
        "accepted_stale_files": accepted_stale_files,
    }


def run_diff(
    left: str,
    right: str,
    as_json: bool,
    fail_on_diff: bool,
    fail_if_orphaned: bool = False,
    output_file: str | None = None,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    fail_if_diff_count_exceeds: int | None = None,
    use_baseline: bool = True,
    notify_webhook: str | None = None,
) -> int:
    from mct.retrieved_folder_compare import compare_trees, TreeCompareResult

    import mct.baseline as _bl

    left_rel = resolve_snapshot_or_path(left)
    right_rel = resolve_snapshot_or_path(right)
    left_abs = abs_snapshot_or_project_rel(left_rel)
    right_abs = abs_snapshot_or_project_rel(right_rel)

    if not left_abs.is_dir():
        raise RuntimeError(f"Left path is not a directory: {left_abs}")
    if not right_abs.is_dir():
        raise RuntimeError(f"Right path is not a directory: {right_abs}")

    left_info = _snapshot_provenance(left)
    right_info = _snapshot_provenance(right)
    prov_warns = _provenance_warnings(left_info, right_info)
    pair_key = _bl.pair_key_for(left_info, right_info)

    baseline = _bl.load_baseline() if use_baseline else _bl.default_baseline()
    ignore_elements = _bl.xml_ignore_elements(baseline) or None
    ignore_by_type = _bl.xml_ignore_by_type(baseline) or None

    result = compare_trees(
        left_abs, right_abs, ignore_elements, _bl.strip_retrieve_defaults(baseline),
        xml_ignore_by_type=ignore_by_type,
    )

    def _type_of(display: str) -> str:
        return display.split("/")[0].split("\\")[0].lower()

    if include_types or exclude_types:
        inc = {t.lower() for t in (include_types or [])}
        exc = {t.lower() for t in (exclude_types or [])}

        def _keep(display: str) -> bool:
            t = _type_of(display)
            if inc and t not in inc:
                return False
            if t in exc:
                return False
            return True

        result = TreeCompareResult(
            identical_count=result.identical_count,
            differ_pairs=tuple(p for p in result.differ_pairs if _keep(p[2])),
            only_left_keys=tuple(k for k in result.only_left_keys if _keep(result.left_ix[k][1])),
            only_right_keys=tuple(k for k in result.only_right_keys if _keep(result.right_ix[k][1])),
            left_ix=result.left_ix,
            right_ix=result.right_ix,
        )

    # Baseline classification: ignored/accepted drift is reported separately
    # and does not trip fail-on-diff; accepted-but-changed entries resurface.
    cls = _classify_result(result, baseline, pair_key=pair_key)
    different_files = cls["different_files"]
    only_left_files = cls["only_left_files"]
    only_right_files = cls["only_right_files"]
    ignored_files = cls["ignored_files"]
    accepted_files = cls["accepted_files"]
    accepted_stale_files = cls["accepted_stale_files"]

    # History stores the post-baseline (active) view so trend data matches
    # the printed summary.
    _idx.record_comparison(
        left_rel, right_rel, result,
        active=cls if use_baseline else None,
    )

    different = len(different_files)
    only_left = len(only_left_files)
    only_right = len(only_right_files)
    identical = result.identical_count
    has_diff = (different + only_left + only_right) > 0

    # XML files above the normalization size cap were compared raw — their
    # "different" status may be pure formatting noise. The UI shows a badge;
    # surface the same information here for CLI/CI consumers.
    from mct.xml_normalizer import _MAX_XML_BYTES

    def _capped(p: Path | None) -> bool:
        try:
            return (p is not None and p.suffix.lower() == ".xml"
                    and p.stat().st_size > _MAX_XML_BYTES)
        except OSError:
            return False

    normalization_capped_files = sorted(
        {ld for lp, rp, ld, _ in result.differ_pairs if _capped(lp) or _capped(rp)}
    )

    # Build per-metadata-type breakdown
    type_counts: dict[str, dict[str, int]] = {}
    for f in different_files:
        t = _type_of(f)
        type_counts.setdefault(t, {"different": 0, "only_left": 0, "only_right": 0})
        type_counts[t]["different"] += 1
    for f in only_left_files:
        t = _type_of(f)
        type_counts.setdefault(t, {"different": 0, "only_left": 0, "only_right": 0})
        type_counts[t]["only_left"] += 1
    for f in only_right_files:
        t = _type_of(f)
        type_counts.setdefault(t, {"different": 0, "only_left": 0, "only_right": 0})
        type_counts[t]["only_right"] += 1
    type_counts_sorted = dict(sorted(type_counts.items(), key=lambda x: x[0].lower()))

    if as_json or output_file:
        payload = {
            "left": left_rel, "right": right_rel,
            "different_count": different, "only_left_count": only_left,
            "only_right_count": only_right, "identical_count": identical,
            "total_left": len(result.left_ix), "total_right": len(result.right_ix),
            "has_diff": has_diff,
            "different_files": different_files,
            "only_left_files": only_left_files,
            "only_right_files": only_right_files,
            "type_counts": type_counts_sorted,
            "baseline_applied": use_baseline,
            "ignored_count": len(ignored_files),
            "accepted_count": len(accepted_files),
            "ignored_files": ignored_files,
            "accepted_files": accepted_files,
            "accepted_stale_files": accepted_stale_files,
            "normalization_capped_files": normalization_capped_files,
            "provenance_warnings": prov_warns,
        }
        if as_json:
            print(json.dumps(payload, indent=2))
        if output_file:
            out_path = Path(output_file)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"  Wrote diff result \u2192 {output_file}", flush=True)
    else:
        status = "DIFFERENCES FOUND" if has_diff else "IDENTICAL"
        print(f"\nDiff summary: {status}")
        for w in prov_warns:
            print(f"  ⚠ {w}")
        print(f"  Left:         {left_rel}")
        print(f"  Right:        {right_rel}")
        print(f"  Different:    {different}")
        print(f"  Only left:    {only_left}")
        print(f"  Only right:   {only_right}")
        print(f"  Identical:    {identical}")
        if ignored_files:
            print(f"  Ignored:      {len(ignored_files)}  (baseline rules)")
        if accepted_files:
            print(f"  Accepted:     {len(accepted_files)}  (baseline fingerprints)")
        if accepted_stale_files:
            print(f"  ⚠ Accepted-but-changed (resurfaced as active): {len(accepted_stale_files)}")
        if normalization_capped_files:
            print(
                f"  ⚠ Compared raw (over {_MAX_XML_BYTES // (1024 * 1024)} MB "
                f"normalization cap, ordering noise possible): "
                f"{len(normalization_capped_files)}"
            )
        if type_counts_sorted:
            print("\n  By metadata type:")
            for t, counts in type_counts_sorted.items():
                parts = []
                if counts["different"]:
                    parts.append(f"{counts['different']} different")
                if counts["only_left"]:
                    parts.append(f"{counts['only_left']} only-left")
                if counts["only_right"]:
                    parts.append(f"{counts['only_right']} only-right")
                print(f"    {t}: {', '.join(parts)}")
        if has_diff:
            print()
            stale = set(accepted_stale_files)
            for ld in different_files:
                mark = "  (changed since accepted)" if ld in stale else ""
                print(f"  ~ {ld}{mark}")
            for disp in only_left_files:
                print(f"  - {disp}  (left only)")
            for disp in only_right_files:
                print(f"  + {disp}  (right only)")

    if notify_webhook and has_diff:
        _post_webhook(notify_webhook, {
            "source": "metadata-compare-tool",
            "left": left_rel, "right": right_rel,
            "different_count": different, "only_left_count": only_left,
            "only_right_count": only_right,
            "ignored_count": len(ignored_files), "accepted_count": len(accepted_files),
            "accepted_stale_count": len(accepted_stale_files),
            "text": (
                f"Metadata drift: {left_rel} vs {right_rel} — {different} different, "
                f"{only_left} only-left, {only_right} only-right"
            ),
        })

    orphaned = only_right > 0
    if fail_if_orphaned and orphaned:
        print(
            f"\n\u2717 Orphaned metadata detected: {only_right} file(s) exist in right tree "
            f"(org) but not in source. Use 'diff --json' to see file list.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if fail_if_diff_count_exceeds is not None and (different + only_left + only_right) > fail_if_diff_count_exceeds:
        print(
            f"\n\u2717 Diff count threshold exceeded: {different + only_left + only_right} differences "
            f"(threshold: {fail_if_diff_count_exceeds})",
            file=sys.stderr, flush=True,
        )
        return 1
    return 1 if (fail_on_diff and has_diff) else 0


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    """POST a drift notification. Best-effort: a notify failure must never
    change the diff outcome or exit code (CI relies on the exit code)."""
    import urllib.error
    import urllib.request

    if not url.lower().startswith(("http://", "https://")):
        print(f"  ⚠ Webhook skipped (not an http(s) URL): {url}", flush=True)
        return
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  ✓ Webhook notified ({resp.status})", flush=True)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  ⚠ Webhook notify failed: {exc}", flush=True)


def _snapshot_provenance(arg: str) -> dict[str, Any] | None:
    """Provenance info for a snapshot id (None when *arg* is a plain path)."""
    try:
        row = load_snapshot_row_by_id(arg)
    except Exception:
        return None
    return {
        "id": row.get("id"),
        "type": row.get("type"),
        "created_at": row.get("created_at"),
        "manifest_kind": row.get("manifest_kind"),
        "branch": row.get("branch"),
        "org_alias": row.get("org_alias"),
        "api_version": row.get("api_version"),
        "skipped_org_types": row.get("skipped_org_types"),
        "retrieve_warnings": row.get("retrieve_warnings"),
    }


def _provenance_warnings(
    left_info: dict[str, Any] | None, right_info: dict[str, Any] | None
) -> list[str]:
    """Human-readable comparability warnings derived from snapshot provenance."""
    warns: list[str] = []
    if left_info and right_info:
        la, ra = left_info.get("api_version"), right_info.get("api_version")
        if la and ra and la != ra:
            warns.append(
                f"API version mismatch (left {la}, right {ra}) — schema changes "
                f"between versions can create phantom diffs"
            )
        lo, ro = left_info.get("org_alias"), right_info.get("org_alias")
        if lo and ro and lo != ro:
            warns.append(
                f"Org-to-org compare ({lo} vs {ro}) — acceptances are scoped to "
                f"this pair; legacy pair-less acceptances still apply"
            )
    for side, info in (("left", left_info), ("right", right_info)):
        if not info:
            continue
        skipped = info.get("skipped_org_types") or []
        if skipped:
            warns.append(
                f"{side} snapshot skipped org-only type(s) not tracked in source: "
                f"{', '.join(skipped)} — drift there is invisible"
            )
        if info.get("retrieve_warnings"):
            warns.append(
                f"{side} snapshot had {len(info['retrieve_warnings'])} retrieve "
                f"warning(s) — it may be incomplete; missing files show as drift"
            )
    return warns


def run_ui(left: str, right: str, port: int, no_open: bool, api_version: str | None = None) -> int:
    from mct.retrieved_folder_compare import compare_trees

    left_rel = resolve_snapshot_or_path(left)
    right_rel = resolve_snapshot_or_path(right)
    left_abs = abs_snapshot_or_project_rel(left_rel)
    right_abs = abs_snapshot_or_project_rel(right_rel)

    try:
        _result = compare_trees(left_abs, right_abs)
        _idx.record_comparison(left_rel, right_rel, _result)
    except Exception as exc:
        print(f"  \u26a0 History record skipped: {exc}", flush=True)

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "serve-diff-ui.py"),
        "--left",
        str(left_abs),
        "--right",
        str(right_abs),
        "--port",
        str(port),
        "--api-version",
        api_version or _cfg.DEFAULT_API_VERSION,
    ]
    left_info = _snapshot_provenance(left)
    right_info = _snapshot_provenance(right)
    if left_info:
        cmd += ["--left-info", json.dumps(left_info)]
    if right_info:
        cmd += ["--right-info", json.dumps(right_info)]

    import mct.baseline as _bl
    cmd += ["--baseline", str(_bl.baseline_path())]
    if no_open:
        cmd.append("--no-open")
    _cfg.run(cmd)
    return 0


def _artifact_root_under_storage(row: dict[str, Any]) -> Path:
    """Absolute path of the top-level file or directory to remove for this index row."""
    rel = str(row.get("path") or "").strip()
    if not rel or rel.startswith("/"):
        raise RuntimeError(f"invalid snapshot path: {rel!r}")
    for part in Path(rel).parts:
        if part in ("", ".", ".."):
            raise RuntimeError(f"invalid snapshot path: {rel!r}")
    stype = row.get("type") or ""
    if stype == "branch":
        top = rel.split("/")[0]
        return (_cfg.STORAGE_ROOT / top).resolve()
    if stype == "org_retrieve":
        top = rel.split("/")[0]
        if top.startswith("retrieved-org-"):
            return (_cfg.STORAGE_ROOT / top).resolve()
    return (_cfg.STORAGE_ROOT / rel).resolve()


def _assert_under_storage(path: Path) -> None:
    path = path.resolve()
    base = _cfg.STORAGE_ROOT.resolve()
    path.relative_to(base)


def snapshot_artifact_disk_usage_bytes(row: dict[str, Any]) -> int | None:
    """Total file bytes for the snapshot's storage artifact (folder tree or single file)."""
    try:
        target = _artifact_root_under_storage(row)
        _assert_under_storage(target)
    except (RuntimeError, ValueError):
        return None
    if not target.exists():
        return None
    if target.is_file():
        try:
            return target.stat().st_size
        except OSError:
            return None
    if target.is_dir():
        total = 0
        for p in target.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total
    return None


def delete_snapshots_by_ids(snapshot_ids: list[str]) -> list[str]:
    """Remove given ids from the index and delete their artifacts under STORAGE_ROOT."""
    data = load_index()
    rows = list(data.get("snapshots", []))
    want = {str(i).strip() for i in snapshot_ids if str(i).strip()}
    if not want:
        raise RuntimeError("no snapshot ids to delete")
    to_remove = [r for r in rows if str(r.get("id", "")) in want]
    found = {str(r["id"]) for r in to_remove}
    missing = want - found
    if missing:
        raise RuntimeError("snapshot id(s) not found: " + ", ".join(sorted(missing)))

    removed: list[str] = []
    for row in to_remove:
        sid = str(row["id"])
        target = _artifact_root_under_storage(row)
        try:
            _assert_under_storage(target)
        except ValueError as e:
            raise RuntimeError(f"refusing delete outside snapshot storage for {sid!r}: {target}") from e
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
        removed.append(sid)

    data["snapshots"] = [r for r in rows if str(r.get("id", "")) not in want]
    save_index(data)
    return removed


def run_delete_snapshots(ids: list[str] | None, delete_all: bool, force: bool) -> int:
    if delete_all:
        if not force:
            print("\u2717 --all requires --force", file=sys.stderr)
            return 1
        idx = load_index()
        ids = [str(r["id"]) for r in idx.get("snapshots", []) if r.get("id")]
        if not ids:
            print("No snapshots in the index (nothing to delete).", flush=True)
            return 0
    if not ids:
        print("\u2717 No snapshot ids to delete. Use --id ID (repeatable) or --all --force.", file=sys.stderr)
        return 1
    try:
        removed = delete_snapshots_by_ids(ids)
    except RuntimeError as e:
        print(f"\u2717 {e}", file=sys.stderr)
        return 1
    for sid in removed:
        print(f"\u2713 Deleted snapshot {sid}", flush=True)
    return 0


def verify_snapshot_output(snapshot_id: str) -> int:
    """Exit 0 if snapshot path exists on disk and has content (files or non-empty JSON)."""
    row = load_snapshot_row_by_id(snapshot_id)
    stype = row.get("type") or ""
    rel = str(row.get("path", ""))
    if not rel:
        print("\u2717 Snapshot has no path in index.", file=sys.stderr)
        return 1
    target = (_cfg.STORAGE_ROOT / rel).resolve()
    if not target.exists():
        target = (_cfg.PROJECT_ROOT / rel).resolve()
    if stype == "installed_packages":
        if not target.is_file():
            print(f"\u2717 Missing packages JSON: {target}", file=sys.stderr)
            return 1
        n = target.stat().st_size
        print(f"\u2713 Installed-packages snapshot {snapshot_id}: {n} bytes ({rel})")
        return 0 if n > 0 else 1
    if not target.exists():
        print(f"\u2717 Missing path: {target}", file=sys.stderr)
        return 1
    root = target if target.is_dir() else target.parent
    if not root.is_dir():
        print(f"\u2717 Not a directory: {root}", file=sys.stderr)
        return 1
    n = count_files_under(root)
    print(f"\u2713 Snapshot {snapshot_id} ({stype}): {n} files under {rel}")
    if n == 0:
        print("\u2717 No files under path \u2014 retrieve may have failed or produced an empty tree.", file=sys.stderr)
        return 1
    return 0


def default_packages_report_path(left_rel: str, right_rel: str) -> Path:
    def slug(s: str) -> str:
        return s.replace("/", "-").replace("\\", "-").strip("-")

    return _cfg.PROJECT_ROOT / "docs" / f"packages-{slug(left_rel)}-vs-{slug(right_rel)}-diff-report.md"


def compare_installed_packages(
    left: str,
    right: str,
    out: str | None,
    as_json: bool = False,
    fail_on_diff: bool = False,
    notify_webhook: str | None = None,
) -> int:
    """Compare two installed-package snapshots.

    Default output is the markdown report. ``as_json`` prints a machine
    payload instead (markdown only when *out* is explicitly given);
    ``fail_on_diff`` exits 1 on any added/removed/version-changed package;
    ``notify_webhook`` POSTs a summary when drift is found — the same CI
    affordances as ``diff``, so package drift joins the alert loop.
    """
    left_rel = resolve_snapshot_or_path(left)
    right_rel = resolve_snapshot_or_path(right)
    left_abs = abs_snapshot_or_project_rel(left_rel)
    right_abs = abs_snapshot_or_project_rel(right_rel)
    if not left_abs.is_file() or not right_abs.is_file():
        raise RuntimeError(
            f"Installed package snapshot paths must be files: {left_abs} / {right_abs}"
        )
    left_rows = load_installed_packages_json(left_abs)
    right_rows = load_installed_packages_json(right_abs)

    left_map = {_package_key(r): r for r in left_rows}
    right_map = {_package_key(r): r for r in right_rows}

    lk = set(left_map)
    rk = set(right_map)
    only_left = sorted(lk - rk, key=str.lower)
    only_right = sorted(rk - lk, key=str.lower)
    common = lk & rk
    version_changed: list[tuple[str, str, str]] = []
    for k in sorted(common, key=str.lower):
        lv = _package_version(left_map[k])
        rv = _package_version(right_map[k])
        if lv != rv:
            version_changed.append((k, lv, rv))

    has_diff = bool(only_left or only_right or version_changed)

    if as_json:
        print(json.dumps({
            "left": left_rel, "right": right_rel,
            "added": only_right, "removed": only_left,
            "changed": [
                {"package": k, "left_version": lv, "right_version": rv}
                for k, lv, rv in version_changed
            ],
            "has_diff": has_diff,
        }, indent=2))

    if notify_webhook and has_diff:
        _post_webhook(notify_webhook, {
            "source": "metadata-compare-tool",
            "kind": "package-drift",
            "left": left_rel, "right": right_rel,
            "added_count": len(only_right), "removed_count": len(only_left),
            "changed_count": len(version_changed),
            "text": (
                f"Package drift: {left_rel} vs {right_rel} — "
                f"{len(version_changed)} version-changed, {len(only_right)} added, "
                f"{len(only_left)} removed"
            ),
        })

    if as_json and not out:
        # JSON consumers asked for a payload, not a markdown artifact.
        return 1 if (fail_on_diff and has_diff) else 0

    if out:
        out_p = Path(out.replace("\\", "/"))
        out_path = out_p if out_p.is_absolute() else (_cfg.PROJECT_ROOT / out)
    else:
        out_path = default_packages_report_path(left_rel, right_rel)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    def esc(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ")

    with out_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(f"# Installed packages diff: `{left_rel}` vs `{right_rel}`\n\n")
        f.write(
            "Generated from two `sf package installed list --json` snapshots (files under the compare tool's "
            "`snapshot-store/<key>/`). Read-only; no org or git writes.\n\n"
        )
        f.write("## Summary\n\n")
        f.write("| Category | Count |\n| --- | ---: |\n")
        f.write(f"| Packages (left) | {len(left_rows)} |\n")
        f.write(f"| Packages (right) | {len(right_rows)} |\n")
        f.write(f"| Only in left | {len(only_left)} |\n")
        f.write(f"| Only in right | {len(only_right)} |\n")
        f.write(f"| Same key, different version id | {len(version_changed)} |\n\n")

        f.write(f"## Only in left (`{esc(left_rel)}`)\n\n")
        if not only_left:
            f.write("_None._\n\n")
        else:
            f.write("| Key | Version id |\n| --- | --- |\n")
            for k in only_left:
                f.write(f"| `{esc(k)}` | `{esc(_package_version(left_map[k]))}` |\n")
            f.write("\n")

        f.write(f"## Only in right (`{esc(right_rel)}`)\n\n")
        if not only_right:
            f.write("_None._\n\n")
        else:
            f.write("| Key | Version id |\n| --- | --- |\n")
            for k in only_right:
                f.write(f"| `{esc(k)}` | `{esc(_package_version(right_map[k]))}` |\n")
            f.write("\n")

        f.write("## Same package, different version id\n\n")
        if not version_changed:
            f.write("_None._\n\n")
        else:
            f.write("| Key | Left version id | Right version id |\n| --- | --- | --- |\n")
            for k, lv, rv in version_changed:
                f.write(f"| `{esc(k)}` | `{esc(lv)}` | `{esc(rv)}` |\n")
            f.write("\n")

    try:
        rel_out = out_path.resolve().relative_to(_cfg.PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        rel_out = str(out_path.resolve())
    print(f"Wrote {rel_out}")
    return 1 if (fail_on_diff and has_diff) else 0


def _split_pair(pair_str: str) -> tuple[str, str] | None:
    """Split a ``LEFT:RIGHT`` pair, or None when no separator is present.

    A ':' is a Windows drive letter — not the pair separator — when it sits
    at index 1 after a single alpha character followed by a path separator
    (``C:\\path`` or ``C:/path``). ``C:\\a:C:\\b`` therefore splits after the
    left path; a bare drive prefix like ``C:x:y`` still splits at ``x:y``
    exactly as before.
    """
    for i, ch in enumerate(pair_str):
        if ch != ":":
            continue
        if i == 1 and pair_str[0].isalpha() and pair_str[2:3] in ("\\", "/"):
            continue
        return pair_str[:i], pair_str[i + 1:]
    return None


def run_compare_matrix(
    pairs: list[str], as_json: bool, fail_on_any_diff: bool, use_baseline: bool = True
) -> int:
    """Compare multiple snapshot pairs and print a summary table.

    Applies the same baseline classification as ``run_diff`` so counts and
    the fail-on-any-diff exit code reflect only active drift.
    """
    import json as _json

    import mct.baseline as _bl
    from mct.retrieved_folder_compare import compare_trees

    baseline = _bl.load_baseline() if use_baseline else _bl.default_baseline()
    ignore_elements = _bl.xml_ignore_elements(baseline) or None
    ignore_by_type = _bl.xml_ignore_by_type(baseline) or None

    results: list[dict[str, object]] = []
    any_diff = False
    for pair_str in pairs:
        split = _split_pair(pair_str)
        if split is None:
            results.append({
                "left": pair_str, "right": None,
                "different": None, "only_left": None, "only_right": None,
                "identical": None, "has_diff": None,
                "error": f"Invalid pair format (expected LEFT:RIGHT): {pair_str!r}",
            })
            continue
        left, right = split
        try:
            left_rel = resolve_snapshot_or_path(left)
            right_rel = resolve_snapshot_or_path(right)
            left_abs = abs_snapshot_or_project_rel(left_rel)
            right_abs = abs_snapshot_or_project_rel(right_rel)
            result = compare_trees(
                left_abs, right_abs, ignore_elements,
                _bl.strip_retrieve_defaults(baseline),
                xml_ignore_by_type=ignore_by_type,
            )
            pair_key = _bl.pair_key_for(
                _snapshot_provenance(left), _snapshot_provenance(right)
            )
            cls = _classify_result(result, baseline, pair_key=pair_key)
            different = len(cls["different_files"])
            only_left = len(cls["only_left_files"])
            only_right = len(cls["only_right_files"])
            total_diff = different + only_left + only_right
            has_diff = total_diff > 0
            if has_diff:
                any_diff = True
            results.append({
                "left": left_rel, "right": right_rel,
                "different": different, "only_left": only_left,
                "only_right": only_right,
                "identical": result.identical_count,
                "has_diff": has_diff,
                "error": None,
            })
        except Exception as exc:
            results.append({
                "left": left, "right": right,
                "different": None, "only_left": None, "only_right": None,
                "identical": None, "has_diff": None,
                "error": str(exc),
            })

    has_errors = any(r["error"] for r in results)

    if as_json:
        print(_json.dumps(
            {"pairs": results, "any_diff": any_diff, "has_errors": has_errors},
            indent=2,
        ))
    else:
        header = f"{'LEFT':<40} {'RIGHT':<40} {'DIFF':>6} {'ONLY-L':>7} {'ONLY-R':>7} {'STATUS'}"
        print(f"\n{header}")
        print("\u2500" * len(header))
        for r in results:
            if r["error"]:
                print(f"{r['left']!s:<40} {r['right']!s:<40} {'ERROR':>6}  {r['error']}")
            else:
                status = "DIFF" if r["has_diff"] else "OK"
                print(f"{r['left']!s:<40} {r['right']!s:<40} {r['different']!s:>6} {r['only_left']!s:>7} {r['only_right']!s:>7}  {status}")
        total_pairs = len(results)
        diff_pairs = sum(1 for r in results if r["has_diff"])
        print(f"\n{diff_pairs}/{total_pairs} pairs have differences.")

    # Per-pair failures outrank drift: a matrix that errored must never exit
    # like a clean compare (0) or a mere diff (1).
    if has_errors:
        return 2
    return 1 if (fail_on_any_diff and any_diff) else 0
