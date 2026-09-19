"""Validate-only deploy of a drift delta, with Copado-style clean-and-retry.

Flow (`validate-deploy` subcommand):

1. Compare two snapshots (baseline-aware) and collect the deployable delta
   (changed + left-only files, expanded with -meta.xml companions and whole
   bundles).
2. Materialize a throwaway SFDX project containing just those files —
   optionally stripping no-grant permission entries (mct.permissions) that
   break validation when the target org lacks the referenced feature.
3. `sf project deploy start --dry-run --json` against the target org. The
   safety layer enforces --dry-run: Salesforce runs the full deploy pipeline
   but SAVES NOTHING.
4. On component failures: report them; with --clean-retries N, remove the
   failing components from the temp project and validate again, up to N
   times. The final report lists exactly what was excluded so the human
   knows what a real deploy must resolve.

A JSON validation report is written under the snapshot store.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import mct.config as _cfg


def _collect_delta(left: str, right: str, use_baseline: bool):
    """(expanded deploy files, destroy files) for the left→right delta.

    Both sides are baseline-filtered active drift. *destroy files* are the
    right-only entries — deletions the ``--dry-run`` validator cannot cover
    (a validation deploy only exercises what package.xml deploys, never
    destructiveChanges.xml). Callers must not discard them.
    """
    import mct.baseline as _bl
    import mct.delta as _delta
    from mct.index import abs_snapshot_or_project_rel, resolve_snapshot_or_path
    from mct.retrieved_folder_compare import compare_trees

    left_abs = abs_snapshot_or_project_rel(resolve_snapshot_or_path(left))
    right_abs = abs_snapshot_or_project_rel(resolve_snapshot_or_path(right))
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
    deploy_files, destroy_files = _delta.collect_deploy_files(
        result, baseline, pair_key=pair_key
    )

    return _delta.expand_deployable_files(result.left_ix, deploy_files), destroy_files


def _write_temp_project(
    entries: list[tuple[Path, str]], api_version: str, strip_no_grant: bool,
    trusted_root: Path | None = None,
) -> tuple[Path, list[str]]:
    """Materialize a throwaway SFDX project with the delta under force-app.

    *trusted_root* is the comparison's resolved left root: when given, every
    source path is verified against the full metadata-read policy (no link
    on any component beneath the root, containment enforced) before a byte
    is copied — a cached delta path whose ancestor became a link after
    indexing cannot smuggle out-of-tree content into the validation deploy.

    Returns (project dir, list of no-grant entries stripped).
    """
    from mct.permissions import strip_no_grant_permissions
    from mct.retrieved_folder_compare import check_metadata_read, read_metadata_bytes

    proj = Path(tempfile.mkdtemp(prefix="mct-validate-"))
    (proj / "sfdx-project.json").write_text(
        json.dumps({
            "packageDirectories": [{"path": "force-app", "default": True}],
            "namespace": "",
            "sourceApiVersion": api_version,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    stripped: list[str] = []
    src_root = proj / "force-app" / "main" / "default"
    for abs_path, disp in entries:
        if trusted_root is not None:
            check_metadata_read(trusted_root, abs_path)
            data = read_metadata_bytes(trusted_root, abs_path)
        else:
            if abs_path.is_symlink():
                raise RuntimeError(
                    f"Refusing to validate a delta containing a symbolic link "
                    f"(links can expose files outside the compared tree): {disp}"
                )
            data = abs_path.read_bytes()
        target = src_root / disp
        target.parent.mkdir(parents=True, exist_ok=True)
        if strip_no_grant and disp.lower().endswith(".xml"):
            text, removed = strip_no_grant_permissions(
                data.decode("utf-8", errors="replace")
            )
            if removed:
                stripped.extend(f"{disp} → {r}" for r in removed)
                data = text.encode("utf-8")
        target.write_bytes(data)
    return proj, stripped


def _parse_component_failures(stdout: str) -> tuple[bool, list[dict[str, str]]]:
    """(success, failures) from `sf project deploy start --json` output.

    Component failures carry componentType, fullName, problem, and fileName.
    A malformed payload is treated as failure with one synthetic entry so the
    caller never mistakes a parse error for a clean validation.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return False, [{"componentType": "?", "fullName": "?", "fileName": "",
                        "problem": f"Unparsable sf output: {stdout[:400]}"}]
    result = data.get("result") or {}
    success = bool(result.get("success"))
    details = result.get("details") or {}
    raw = details.get("componentFailures") or []
    if isinstance(raw, dict):
        raw = [raw]
    failures = [{
        "componentType": str(f.get("componentType") or f.get("type") or "?"),
        "fullName": str(f.get("fullName") or "?"),
        "fileName": str(f.get("fileName") or ""),
        "problem": str(f.get("problem") or f.get("error") or "?"),
    } for f in raw if isinstance(f, dict)]
    if not success and not failures:
        msg = str(data.get("message") or result.get("errorMessage") or "Validation failed")
        failures = [{"componentType": "?", "fullName": "?", "fileName": "", "problem": msg}]
    return success, failures


def _remove_failing_files(proj: Path, failures: list[dict[str, str]]) -> list[str]:
    """Delete the temp-project files behind each failure (plus companions /
    whole bundles). Returns removed project-relative paths."""
    removed: list[str] = []
    src_root = (proj / "force-app" / "main" / "default").resolve()
    for f in failures:
        rel = f.get("fileName") or ""
        rel = rel.replace("\\", "/").lstrip("/")
        # sf reports paths like force-app/main/default/classes/X.cls
        for prefix in ("force-app/main/default/", "force-app/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        if not rel:
            continue
        target = (src_root / rel).resolve()
        try:
            target.relative_to(src_root)
        except ValueError:
            continue
        parts = rel.split("/")
        if len(parts) > 2 and parts[0].lower() in ("lwc", "aura"):
            bundle_dir = src_root / parts[0] / parts[1]
            if bundle_dir.is_dir():
                shutil.rmtree(bundle_dir, ignore_errors=True)
                removed.append(f"{parts[0]}/{parts[1]}/ (whole bundle)")
            continue
        candidates = {target}
        if target.name.endswith("-meta.xml"):
            candidates.add(target.parent / target.name.removesuffix("-meta.xml"))
        else:
            candidates.add(target.parent / (target.name + "-meta.xml"))
        for candidate in candidates:
            if candidate.is_file():
                candidate.unlink()
                removed.append(str(candidate.relative_to(src_root)))
    return removed


def run_validate_deploy(
    left: str,
    right: str,
    org_alias: str,
    api_version: str,
    timeout: int,
    use_baseline: bool = True,
    strip_no_grant: bool = False,
    clean_retries: int = 0,
) -> int:
    from mct.safety import run as _run_safe

    entries, destroy_files = _collect_delta(left, right, use_baseline)
    if not entries and not destroy_files:
        print("No active drift to validate — nothing to do.", flush=True)
        return 0
    if destroy_files:
        # Destructive changes are outside validate-deploy's supported scope:
        # a dry-run only exercises what package.xml deploys — it can never
        # validate deletions. Failing closed beats silently validating just
        # the additions/changes of a deletion-bearing delta and reporting a
        # misleading pass.
        destroy_displays = sorted(d for _, d in destroy_files)
        print(
            f"✗ Cannot validate: {len(destroy_displays)} component file(s) exist only "
            "in the target tree (deletions). validate-deploy covers deployable "
            "additions/changes only — destructive changes are outside its "
            "supported scope and were NOT validated.",
            flush=True,
        )
        for d in destroy_displays:
            print(f"    - {d}", flush=True)
        report = {
            "left": left, "right": right, "org": org_alias,
            "created_at": _cfg.ts_local(),
            "success": False,
            "error": "unsupported_delta_scope",
            "unvalidated_deletions": destroy_displays,
            "deployable_files": len(entries),
            "attempts": [],
        }
        _cfg.STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        report_path = _cfg.STORAGE_ROOT / f"validation-report-{_cfg.ts_local()}.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\n  Report: {report_path}", flush=True)
        return 1
    print(f"  Delta: {len(entries)} deployable file(s)", flush=True)

    from mct.index import abs_snapshot_or_project_rel, resolve_snapshot_or_path
    left_root = abs_snapshot_or_project_rel(resolve_snapshot_or_path(left))
    proj, stripped = _write_temp_project(
        entries, api_version, strip_no_grant, trusted_root=left_root
    )
    if stripped:
        print(f"  Stripped {len(stripped)} no-grant permission entr"
              f"{'y' if len(stripped) == 1 else 'ies'} before validation.", flush=True)

    attempts: list[dict[str, Any]] = []
    excluded: list[str] = []
    success = False
    try:
        for attempt in range(1, clean_retries + 2):
            cmd = [
                "sf", "project", "deploy", "start",
                "--source-dir", "force-app",
                "--target-org", org_alias,
                "--dry-run",
                "--json",
                "--api-version", api_version,
                "-w", str(max(1, timeout // 60)),
            ]
            result = _run_safe(cmd, check=False, capture=True, cwd=str(proj), timeout=timeout + 60)
            success, failures = _parse_component_failures(result.stdout or "")
            attempts.append({
                "attempt": attempt,
                "success": success,
                "failures": failures,
            })
            if success:
                print(f"\n✓ Validation PASSED (attempt {attempt})"
                      + (f" with {len(excluded)} component file(s) excluded" if excluded else ""),
                      flush=True)
                break

            print(f"\n✗ Validation attempt {attempt}: {len(failures)} component failure(s)", flush=True)
            for f in failures[:50]:
                print(f"    {f['componentType']} {f['fullName']}: {f['problem']}", flush=True)
            if len(failures) > 50:
                print(f"    … and {len(failures) - 50} more", flush=True)

            if attempt > clean_retries:
                if clean_retries == 0:
                    print("\n  Tip: re-run with --clean-retries 2 to auto-remove failing "
                          "components and revalidate the rest; add --strip-no-grant if "
                          "failures mention permissions for missing fields/features.", flush=True)
                break
            removed = _remove_failing_files(proj, failures)
            if not removed:
                print("  ✗ Could not map failures to delta files — stopping retries.", flush=True)
                break
            excluded.extend(removed)
            print(f"  Cleaned {len(removed)} file(s); revalidating…", flush=True)
    finally:
        report = {
            "left": left, "right": right, "org": org_alias,
            "created_at": _cfg.ts_local(),
            "strip_no_grant": strip_no_grant,
            "stripped_entries": stripped,
            "excluded_files": excluded,
            "success": success,
            "attempts": attempts,
        }
        _cfg.STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        report_path = _cfg.STORAGE_ROOT / f"validation-report-{_cfg.ts_local()}.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\n  Report: {report_path}", flush=True)
        shutil.rmtree(proj, ignore_errors=True)

    if success and excluded:
        print("\n  Excluded from the validated set (must be fixed before a real deploy):", flush=True)
        for e in excluded:
            print(f"    - {e}", flush=True)
    if not success:
        return 1
    # Exit 2: validated, but only after excluding components — the delta is
    # NOT deployable as authored, and exit-code-only CI consumers must see that.
    return 2 if excluded else 0
