#!/usr/bin/env python3
"""
Hardened metadata compare orchestrator (CLI-first).

Key properties:
- Produces branch and org retrieval snapshots with timestamped folders.
- Persists snapshot metadata in a local index.
- Comdopares any two snapshots and can launch the existing diff UI.
- Enforces strict no-write-back behavior to git remotes and org deploy APIs.

This file is a thin compatibility wrapper. All logic lives in the ``mct``
package; this module re-exports every public symbol so that existing callers
(tests, orchestrator server, CLI) continue to work unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if sys.version_info < (3, 10):
    sys.exit(
        f"metadata-compare requires Python 3.10+. "
        f"Python 3.12+ is recommended for the tarfile security filter (tarfile.data_filter). "
        f"Detected: {sys.version}"
    )

# ---------------------------------------------------------------------------
# Ensure mct package is importable (repo-root on sys.path)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = SCRIPT_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# ---------------------------------------------------------------------------
# Re-export everything from the mct package
# ---------------------------------------------------------------------------
import mct.config as _cfg
import mct.safety as _safety
import mct.index as _index
import mct.snapshot as _snapshot
import mct.comparison as _comparison

from mct.retrieved_folder_compare import compare_trees, TreeCompareResult

# --- safety ---
_FORBIDDEN_VERBS = _safety._FORBIDDEN_VERBS
ensure_safe_command = _safety.ensure_safe_command

# --- config globals (initial snapshot) ---
BUNDLE_ROOT = _cfg.BUNDLE_ROOT
PROJECT_ROOT = _cfg.PROJECT_ROOT
STORAGE_ROOT = _cfg.STORAGE_ROOT
STATE_DIR = _cfg.STATE_DIR
INDEX_PATH = _cfg.INDEX_PATH
COMPARISON_INDEX_PATH = _cfg.COMPARISON_INDEX_PATH
MANIFEST_DIR = _cfg.MANIFEST_DIR
DEFAULT_API_VERSION = _cfg.DEFAULT_API_VERSION
_LEGACY_SOURCE_SUBDIR = _cfg._LEGACY_SOURCE_SUBDIR
READ_ONLY_GIT_REMOTE = _cfg.READ_ONLY_GIT_REMOTE

# --- config functions ---
normalize_api_version = _cfg.normalize_api_version
_storage_key = _cfg._storage_key
repo_context = _cfg.repo_context
sanitize_token = _cfg.sanitize_token
ts_local = _cfg.ts_local
storage_rel = _cfg.storage_rel
project_rel = _cfg.project_rel
log = _cfg.log
run = _cfg.run

# --- index ---
Snapshot = _index.Snapshot
ComparisonRecord = _index.ComparisonRecord
load_index = _index.load_index
save_index = _index.save_index
load_comparison_index = _index.load_comparison_index
save_comparison_index = _index.save_comparison_index
_snapshot_id_for_path = _index._snapshot_id_for_path
record_comparison = _index.record_comparison
register_snapshot = _index.register_snapshot
snapshot_from_row = _index.snapshot_from_row
load_branch_snapshot_by_id = _index.load_branch_snapshot_by_id
load_snapshot_row_by_id = _index.load_snapshot_row_by_id
resolve_snapshot_or_path = _index.resolve_snapshot_or_path
abs_snapshot_or_project_rel = _index.abs_snapshot_or_project_rel
dx_default_compare_root = _index.dx_default_compare_root
normalize_org_retrieve_compare_rel = _index.normalize_org_retrieve_compare_rel
print_snapshots = _index.print_snapshots
print_comparison_history = _index.print_comparison_history

# --- snapshot ---
branch_exists = _snapshot.branch_exists
remote_branch_exists = _snapshot.remote_branch_exists
require_sf = _snapshot.require_sf
count_files_under = _snapshot.count_files_under
read_package_dirs_from_branch = _snapshot.read_package_dirs_from_branch
_extract_archive_to = _snapshot._extract_archive_to
_merge_package_dirs = _snapshot._merge_package_dirs
materialize_branch_snapshot = _snapshot.materialize_branch_snapshot
generate_manifest_from_source = _snapshot.generate_manifest_from_source
generate_manifest_from_org = _snapshot.generate_manifest_from_org
retrieve_with_manifest = _snapshot.retrieve_with_manifest
snapshot_org_from_source = _snapshot.snapshot_org_from_source
snapshot_org_from_org = _snapshot.snapshot_org_from_org
snapshot_org_bidirectional = _snapshot.snapshot_org_bidirectional
parse_manifest_types = _snapshot.parse_manifest_types
build_union_manifest = _snapshot.build_union_manifest
write_manifest = _snapshot.write_manifest
_parse_installed_packages_payload = _snapshot._parse_installed_packages_payload
_package_key = _snapshot._package_key
_package_version = _snapshot._package_version
load_installed_packages_json = _snapshot.load_installed_packages_json
snapshot_installed_packages = _snapshot.snapshot_installed_packages

# --- comparison ---
run_diff = _comparison.run_diff
run_ui = _comparison.run_ui
_artifact_root_under_storage = _comparison._artifact_root_under_storage
_assert_under_storage = _comparison._assert_under_storage
snapshot_artifact_disk_usage_bytes = _comparison.snapshot_artifact_disk_usage_bytes
delete_snapshots_by_ids = _comparison.delete_snapshots_by_ids
run_delete_snapshots = _comparison.run_delete_snapshots
verify_snapshot_output = _comparison.verify_snapshot_output
default_packages_report_path = _comparison.default_packages_report_path
compare_installed_packages = _comparison.compare_installed_packages
run_compare_matrix = _comparison.run_compare_matrix

# ---------------------------------------------------------------------------
# Global variable forwarding: tests mutate env_compare.STORAGE_ROOT etc.
# and expect all functions (now in mct.*) to see the new value.
# ---------------------------------------------------------------------------
_FORWARDED_GLOBALS = frozenset({
    "PROJECT_ROOT", "STORAGE_ROOT", "STATE_DIR", "INDEX_PATH",
    "COMPARISON_INDEX_PATH", "MANIFEST_DIR", "BUNDLE_ROOT",
})

# Functions that tests may mock.patch on env_compare; forward writes to the
# actual module so that callers inside mct.* see the patched version.
_FORWARDED_FUNCTIONS: dict[str, Any] = {
    "record_comparison": _index,
    "run_ui": _comparison,
    "run_diff": _comparison,
    "load_comparison_index": _index,
    "save_comparison_index": _index,
    "save_index": _index,
    "load_index": _index,
    "_snapshot_id_for_path": _index,
}


def __getattr__(name: str) -> Any:
    """Read-through: return the live value from mct.config for forwarded globals."""
    if name in _FORWARDED_GLOBALS:
        return getattr(_cfg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# We need a custom module class to intercept __setattr__ at the module level.
import types as _types


class _EnvCompareModule(_types.ModuleType):
    """Thin wrapper that forwards global-variable writes to mct.config and function mocks to source modules."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _FORWARDED_GLOBALS:
            setattr(_cfg, name, value)
            super().__setattr__(name, value)
        elif name in _FORWARDED_FUNCTIONS:
            # Forward mock patches to the source module so internal callers see them
            setattr(_FORWARDED_FUNCTIONS[name], name, value)
            super().__setattr__(name, value)
        else:
            super().__setattr__(name, value)

    def __getattr__(self, name: str) -> Any:
        if name in _FORWARDED_GLOBALS:
            return getattr(_cfg, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Replace this module in sys.modules with our custom class instance
_self = sys.modules[__name__]
_new = _EnvCompareModule(__name__, __doc__)
_new.__dict__.update({k: v for k, v in _self.__dict__.items() if k != "__class__"})
_new.__file__ = _self.__file__
_new.__loader__ = getattr(_self, "__loader__", None)
_new.__spec__ = getattr(_self, "__spec__", None)
_new.__path__ = getattr(_self, "__path__", [])
sys.modules[__name__] = _new


# ---------------------------------------------------------------------------
# apply_repo_root wrapper — updates both mct.config and our local copies
# ---------------------------------------------------------------------------
def apply_repo_root(repo_root: str | None) -> None:
    """Set DX project root (git/sf) and per-project snapshot storage under this tool checkout."""
    _cfg.apply_repo_root(repo_root)
    # Sync local module dict so direct attribute reads see updated values
    _mod = sys.modules[__name__]
    for name in _FORWARDED_GLOBALS:
        _mod.__dict__[name] = getattr(_cfg, name)


# Patch the new module with our wrapper
setattr(sys.modules[__name__], "apply_repo_root", apply_repo_root)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hardened metadata compare orchestrator.")
    p.add_argument(
        "--repo-root",
        default=None,
        metavar="PATH",
        help="Salesforce DX project root (git repo with force-app). Default: this repository root.",
    )
    p.add_argument(
        "--api-version",
        default=DEFAULT_API_VERSION,
        metavar="VER",
        help=f"Metadata API version for manifest generation and retrieve (default: {DEFAULT_API_VERSION}).",
    )
    p.add_argument(
        "--retrieve-chunk-size",
        type=int,
        default=None,
        metavar="N",
        help="Manifests over N members retrieve in multiple requests "
             "(Metadata API caps a single retrieve at ~10k files). "
             "Default 4000; pass 0 to disable chunking.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp_list = sub.add_parser("list", help="List known snapshots from index.")
    sp_list.add_argument("--json", action="store_true", help="Print snapshots as JSON.")
    sp_list.set_defaults(_fn="list")

    sp_branch = sub.add_parser("snapshot-branch", help="Create read-only branch snapshot.")
    sp_branch.add_argument("--branch", required=True, help="Branch name to snapshot.")
    sp_branch.add_argument(
        "--source-subdir",
        default=None,
        metavar="PATH",
        help=(
            "Override: archive only this single subdirectory (e.g. force-app/main/default). "
            "Default: auto-detect all packageDirectories from sfdx-project.json and merge them."
        ),
    )
    sp_branch.add_argument("--fetch", action="store_true", help="Run read-only git fetch before resolving branch.")
    sp_branch.set_defaults(_fn="snapshot_branch")

    sp_src = sub.add_parser("snapshot-org-from-source", help="Retrieve org using source-derived manifest.")
    sp_src.add_argument("--org", required=True, help="Salesforce org alias.")
    sp_src.add_argument("--branch", required=True, help="Branch to snapshot for manifest source.")
    sp_src.add_argument(
        "--source-subdir",
        default=None,
        metavar="PATH",
        help="Override single-package subdir. Default: auto-detect from sfdx-project.json.",
    )
    sp_src.add_argument("--fetch", action="store_true")
    sp_src.add_argument("--wait-seconds", type=int, default=120)
    sp_src.set_defaults(_fn="snapshot_org_from_source")

    sp_ret_src = sub.add_parser(
        "retrieve-org-from-branch-snapshot",
        help="After a branch snapshot exists: generate manifest from that tree and retrieve from org.",
    )
    sp_ret_src.add_argument("--org", required=True, help="Salesforce org alias.")
    sp_ret_src.add_argument(
        "--branch-snapshot-id",
        required=True,
        metavar="ID",
        help="Existing branch snapshot id from the index (from snapshot-branch or list).",
    )
    sp_ret_src.add_argument("--wait-seconds", type=int, default=120)
    sp_ret_src.set_defaults(_fn="retrieve_org_from_branch_snapshot")

    sp_org = sub.add_parser("snapshot-org-from-org", help="Retrieve org using org-derived manifest.")
    sp_org.add_argument("--org", required=True, help="Salesforce org alias.")
    sp_org.add_argument("--wait-seconds", type=int, default=120)
    sp_org.set_defaults(_fn="snapshot_org_from_org")

    sp_bidi = sub.add_parser(
        "snapshot-org-bidirectional",
        help=(
            "Retrieve org using a UNION manifest (source manifest + org manifest scoped to "
            "source-tracked types) so the compare sees drift in both directions."
        ),
    )
    sp_bidi.add_argument("--org", required=True, help="Salesforce org alias.")
    sp_bidi.add_argument("--branch", required=True, help="Branch to snapshot for the source manifest.")
    sp_bidi.add_argument(
        "--source-subdir",
        default=None,
        metavar="PATH",
        help="Override single-package subdir. Default: auto-detect from sfdx-project.json.",
    )
    sp_bidi.add_argument("--fetch", action="store_true")
    sp_bidi.add_argument("--wait-seconds", type=int, default=120)
    sp_bidi.add_argument(
        "--include-org-type",
        action="append",
        default=None,
        metavar="TYPE",
        help=(
            "Also retrieve this org metadata type even if source does not track it "
            "(repeatable). E.g. --include-org-type Report"
        ),
    )
    sp_bidi.set_defaults(_fn="snapshot_org_bidirectional")

    sp_rdelta = sub.add_parser(
        "retrieve-delta",
        help=(
            "Reverse-sync (org → git): retrieve ONLY the org-side drift (changed + "
            "org-only components, minus baseline) into a fresh folder for PR-ing."
        ),
    )
    sp_rdelta.add_argument("--left", required=True, metavar="PATH|SNAP_ID",
                           help="Source-of-truth tree (e.g. branch snapshot id).")
    sp_rdelta.add_argument("--right", required=True, metavar="PATH|SNAP_ID",
                           help="Org tree (e.g. org retrieve snapshot id).")
    sp_rdelta.add_argument("--org", required=True, help="Salesforce org alias to retrieve from.")
    sp_rdelta.add_argument("--wait-seconds", type=int, default=120)
    sp_rdelta.add_argument("--no-baseline", action="store_true",
                           help="Also pull drift that the baseline ignores/accepts.")
    sp_rdelta.set_defaults(_fn="retrieve_delta")

    sp_val = sub.add_parser(
        "validate-deploy",
        help=(
            "Validate (check-only, --dry-run) that the left→right delta would deploy "
            "cleanly to an org. NOTHING is saved to the org. Supports Copado-style "
            "clean-and-retry and no-grant permission stripping."
        ),
    )
    sp_val.add_argument("--left", required=True, metavar="PATH|SNAP_ID",
                        help="Source-of-truth tree (what you would deploy).")
    sp_val.add_argument("--right", required=True, metavar="PATH|SNAP_ID",
                        help="Target-org tree (defines the delta).")
    sp_val.add_argument("--org", required=True, help="Org alias to validate against.")
    sp_val.add_argument("--wait-seconds", type=int, default=600)
    sp_val.add_argument("--strip-no-grant", action="store_true",
                        help="Strip Profile/PermissionSet entries that grant nothing "
                             "(editable/readable both false, enabled=false, hidden tabs…) "
                             "before validating — they break deploys when the target org "
                             "lacks the referenced field/feature, and removing them "
                             "changes no effective access.")
    sp_val.add_argument("--clean-retries", type=int, default=0, metavar="N",
                        help="On component failures, remove the failing components from "
                             "the validation set and revalidate, up to N times "
                             "(the report lists everything excluded).")
    sp_val.add_argument("--no-baseline", action="store_true",
                        help="Include baseline-ignored/accepted drift in the validated delta.")
    sp_val.set_defaults(_fn="validate_deploy")

    sp_all = sub.add_parser("snapshot-all", help="Create branch + both org retrieval snapshots.")
    sp_all.add_argument("--branch", required=True, help="Branch to snapshot.")
    sp_all.add_argument("--org", required=True, help="Salesforce org alias.")
    sp_all.add_argument(
        "--source-subdir",
        default=None,
        metavar="PATH",
        help="Override single-package subdir. Default: auto-detect from sfdx-project.json.",
    )
    sp_all.add_argument("--fetch", action="store_true")
    sp_all.add_argument("--wait-seconds", type=int, default=120)
    sp_all.set_defaults(_fn="snapshot_all")

    sp_ui = sub.add_parser("ui", help="Launch diff UI for any two snapshots/paths.")
    sp_ui.add_argument("--left", required=True, help="Snapshot id or repo-relative path.")
    sp_ui.add_argument("--right", required=True, help="Snapshot id or repo-relative path.")
    sp_ui.add_argument("--port", type=int, default=8089)
    sp_ui.add_argument("--no-open", action="store_true")
    sp_ui.set_defaults(_fn="ui")

    sp_snap_pkg = sub.add_parser(
        "snapshot-packages",
        help="Save sf package installed list JSON (read-only) under .metadata-compare/.",
    )
    sp_snap_pkg.add_argument("--org", required=True, help="Salesforce org alias.")
    sp_snap_pkg.set_defaults(_fn="snapshot_packages")

    sp_cmp_pkg = sub.add_parser(
        "compare-packages",
        help="Markdown report comparing two installed-package snapshot files or ids.",
    )
    sp_cmp_pkg.add_argument("--left", required=True, help="Snapshot id or repo-relative path to packages JSON.")
    sp_cmp_pkg.add_argument("--right", required=True, help="Snapshot id or repo-relative path to packages JSON.")
    sp_cmp_pkg.add_argument("-o", "--out", default=None, help="Report path relative to repo root.")
    sp_cmp_pkg.add_argument("--json", action="store_true", dest="as_json",
                            help="Print a JSON payload instead of writing markdown (unless -o given).")
    sp_cmp_pkg.add_argument("--fail-on-diff", action="store_true",
                            help="Exit 1 when any package was added, removed, or changed version.")
    sp_cmp_pkg.add_argument("--notify-webhook", default=None,
                            help="POST a JSON summary to this URL when package drift is found.")
    sp_cmp_pkg.set_defaults(_fn="compare_packages")

    sp_verify = sub.add_parser(
        "verify-snapshot",
        help="Verify a snapshot path exists under storage and count files (sanity-check after retrieve).",
    )
    sp_verify.add_argument("--id", required=True, dest="snapshot_id", metavar="ID", help="Snapshot id from list.")
    sp_verify.set_defaults(_fn="verify_snapshot")

    sp_del = sub.add_parser(
        "delete-snapshots",
        help="Remove snapshot index entries and delete artifacts under snapshot storage for this repo.",
    )
    sp_del.add_argument(
        "--id",
        action="append",
        dest="delete_ids",
        default=[],
        metavar="ID",
        help="Snapshot id to remove (repeatable).",
    )
    sp_del.add_argument("--all", action="store_true", help="Delete every snapshot in the index for this project.")
    sp_del.add_argument("--force", action="store_true", help="Required with --all.")
    sp_del.set_defaults(_fn="delete_snapshots")

    sp_hist = sub.add_parser("history", help="List past comparison runs (newest first).")
    sp_hist.add_argument("--json", action="store_true", help="Print as JSON.")
    sp_hist.add_argument("--left", default=None, metavar="FILTER",
                         help="Filter history to runs where left path contains FILTER.")
    sp_hist.add_argument("--right", default=None, metavar="FILTER",
                         help="Filter history to runs where right path contains FILTER.")
    sp_hist.add_argument("--trend", action="store_true",
                         help="Chronological drift counts (oldest first) with up/down markers.")
    sp_hist.set_defaults(_fn="history")

    sp_diff = sub.add_parser("diff", help="Compare two snapshots/paths; exit 1 if --fail-on-diff.")
    sp_diff.add_argument("--left", required=True, metavar="PATH|SNAP_ID",
                         help="Left tree: path or snapshot ID.")
    sp_diff.add_argument("--right", required=True, metavar="PATH|SNAP_ID",
                         help="Right tree: path or snapshot ID.")
    sp_diff.add_argument("--json", action="store_true", dest="as_json",
                         help="Output JSON instead of human-readable summary.")
    sp_diff.add_argument("--fail-on-diff", action="store_true",
                         help="Exit 1 if any differences are found (for CI pipelines).")
    sp_diff.add_argument("--fail-if-orphaned", action="store_true",
                         help="Exit 1 if any files exist only in the right tree (unapproved org metadata).")
    sp_diff.add_argument("--output-file", default=None, metavar="PATH",
                         help="Write JSON result (including file lists) to this path.")
    sp_diff.add_argument("--include-type", action="append", default=[], metavar="TYPE",
                         help="Limit diff to this metadata type folder (repeatable). E.g. --include-type classes")
    sp_diff.add_argument("--exclude-type", action="append", default=[], metavar="TYPE",
                         help="Exclude this metadata type folder from diff (repeatable).")
    sp_diff.add_argument(
        "--fail-if-diff-count-exceeds",
        type=int,
        default=None,
        metavar="N",
        help="Exit 1 if total difference count (different + only_left + only_right) exceeds N.",
    )
    sp_diff.add_argument(
        "--no-baseline",
        action="store_true",
        help="Ignore the project baseline (report ignored/accepted drift as active).",
    )
    sp_diff.add_argument(
        "--notify-webhook",
        default=None,
        metavar="URL",
        help="POST a JSON drift summary to this URL when differences are found "
             "(best-effort; never affects the exit code).",
    )
    sp_diff.set_defaults(_fn="diff")

    sp_base = sub.add_parser(
        "baseline",
        help="Show or edit the project baseline (ignore rules + accepted diffs).",
    )
    base_sub = sp_base.add_subparsers(dest="baseline_cmd", required=True)
    bs_show = base_sub.add_parser("show", help="Print baseline rules and accepted-diff entries.")
    bs_show.add_argument("--json", action="store_true", dest="as_json")
    bs_add = base_sub.add_parser("add-ignore", help="Add ignore rules (repeatable flags).")
    bs_add.add_argument("--type", action="append", default=[], metavar="FOLDER",
                        help="Ignore a metadata type folder (e.g. reports).")
    bs_add.add_argument("--path", action="append", default=[], metavar="GLOB",
                        help="Ignore display paths matching this glob (e.g. 'objects/Legacy_*').")
    bs_add.add_argument("--xml-element", action="append", default=[], metavar="NAME",
                        help="Strip this XML element (local name) from both sides before comparing "
                             "(e.g. apiVersion). Prefix with a type folder to scope the rule to that "
                             "type only (e.g. 'omniScripts:isActive').")
    bs_rm = base_sub.add_parser("remove-ignore", help="Remove ignore rules (repeatable flags).")
    bs_rm.add_argument("--type", action="append", default=[], metavar="FOLDER")
    bs_rm.add_argument("--path", action="append", default=[], metavar="GLOB")
    bs_rm.add_argument("--xml-element", action="append", default=[], metavar="NAME")
    bs_set = base_sub.add_parser("set", help="Set baseline options.")
    bs_set.add_argument("--strip-retrieve-defaults", choices=["on", "off"], default=None,
                        help="Suppress root-level XML elements equal to their Metadata-API "
                             "default value (e.g. <trackHistory>false</trackHistory>).")
    bs_clear = base_sub.add_parser("clear-accepted", help="Remove accepted-diff entries.")
    bs_clear.add_argument("--path", action="append", default=[], metavar="DISPLAY_PATH",
                          help="Remove this accepted entry (repeatable).")
    bs_clear.add_argument("--all", action="store_true", help="Remove ALL accepted entries.")
    sp_base.set_defaults(_fn="baseline")

    sp_matrix = sub.add_parser(
        "compare-matrix",
        help="Compare multiple snapshot pairs and print a summary table.",
    )
    sp_matrix.add_argument(
        "--pair",
        action="append",
        dest="pairs",
        default=[],
        metavar="LEFT:RIGHT",
        help="Snapshot id or path pair (repeatable). Format: left_id:right_id",
    )
    sp_matrix.add_argument("--json", action="store_true", dest="as_json",
                           help="Output JSON instead of table.")
    sp_matrix.add_argument("--no-baseline", action="store_true",
                           help="Bypass baseline ignore rules and accepted diffs (raw counts).")
    sp_matrix.add_argument("--fail-on-any-diff", action="store_true",
                           help="Exit 1 if any pair has differences.")
    sp_matrix.set_defaults(_fn="compare_matrix")

    sp_attr = sub.add_parser(
        "attribute",
        help="Who changed what, when — SetupAuditTrail entries (read-only SOQL).",
    )
    sp_attr.add_argument("--org", required=True)
    sp_attr.add_argument("--since-days", type=int, default=14)
    sp_attr.add_argument("--name", action="append", dest="names", default=[],
                         help="Filter by component name substring (repeatable).")
    sp_attr.add_argument("--json", action="store_true", dest="as_json")
    sp_attr.set_defaults(_fn="attribute")

    sp_mig = sub.add_parser(
        "migrate",
        help="Copy legacy snapshot/workspaces data into the per-user data "
             "location (non-destructive; originals are left in place).",
    )
    sp_mig.add_argument(
        "--legacy-root",
        default=None,
        help="Override the legacy snapshot-store root (default: "
             "<tool-checkout>/.metadata-compare/snapshot-store).",
    )
    sp_mig.add_argument("--json", action="store_true", dest="as_json")
    sp_mig.set_defaults(_fn="migrate")

    return p.parse_args()


# Patch parse_args onto the module too
setattr(sys.modules[__name__], "parse_args", parse_args)


def main() -> int:
    args = parse_args()
    apply_repo_root(args.repo_root)
    if getattr(args, "retrieve_chunk_size", None) is not None:
        _cfg.RETRIEVE_CHUNK_SIZE = max(0, args.retrieve_chunk_size)
    api_ver = normalize_api_version(
        (getattr(args, "api_version", None) or DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION
    )
    try:
        if args._fn == "list":
            return print_snapshots(as_json=args.json)
        if args._fn == "snapshot_branch":
            snap = materialize_branch_snapshot(args.branch, args.source_subdir, args.fetch)
            print(json.dumps({"snapshot_id": snap.snapshot_id, "status": "success"}), flush=True)
            return 0
        if args._fn == "snapshot_org_from_source":
            require_sf()
            print(
                "\n\u25b6 Step 1 \u2014 Branch snapshot from git (git archive \u2192 storage)\n",
                flush=True,
            )
            branch_snap = materialize_branch_snapshot(args.branch, args.source_subdir, args.fetch)
            org_snap = snapshot_org_from_source(args.org, branch_snap, args.wait_seconds, api_ver)
            print(json.dumps({"snapshot_id": org_snap.snapshot_id, "branch_snapshot_id": branch_snap.snapshot_id, "status": "success"}), flush=True)
            return 0
        if args._fn == "retrieve_org_from_branch_snapshot":
            require_sf()
            branch_snap = load_branch_snapshot_by_id(args.branch_snapshot_id)
            org_snap = snapshot_org_from_source(args.org, branch_snap, args.wait_seconds, api_ver)
            print(json.dumps({"snapshot_id": org_snap.snapshot_id, "status": "success"}), flush=True)
            return 0
        if args._fn == "snapshot_org_from_org":
            require_sf()
            snap = snapshot_org_from_org(args.org, args.wait_seconds, api_ver)
            print(json.dumps({"snapshot_id": snap.snapshot_id, "status": "success"}), flush=True)
            return 0
        if args._fn == "validate_deploy":
            require_sf()
            from mct.validate import run_validate_deploy
            return run_validate_deploy(
                args.left, args.right, args.org, api_ver, args.wait_seconds,
                use_baseline=not args.no_baseline,
                strip_no_grant=args.strip_no_grant,
                clean_retries=max(0, args.clean_retries),
            )
        if args._fn == "retrieve_delta":
            require_sf()
            delta_snap = _snapshot.retrieve_delta(
                args.org, args.left, args.right, args.wait_seconds, api_ver,
                use_baseline=not args.no_baseline,
            )
            if delta_snap is None:
                print(json.dumps({"snapshot_id": None, "status": "no_drift"}), flush=True)
            else:
                print(json.dumps({"snapshot_id": delta_snap.snapshot_id, "status": "success"}), flush=True)
            return 0
        if args._fn == "snapshot_org_bidirectional":
            require_sf()
            print(
                "\n▶ Step 1 — Branch snapshot from git (git archive → storage)\n",
                flush=True,
            )
            branch_snap = materialize_branch_snapshot(args.branch, args.source_subdir, args.fetch)
            org_snap = snapshot_org_bidirectional(
                args.org, branch_snap, args.wait_seconds, api_ver, args.include_org_type
            )
            print(json.dumps({"snapshot_id": org_snap.snapshot_id, "branch_snapshot_id": branch_snap.snapshot_id, "status": "success"}), flush=True)
            return 0
        if args._fn == "snapshot_all":
            require_sf()
            branch_snap = materialize_branch_snapshot(args.branch, args.source_subdir, args.fetch)
            src_snap = snapshot_org_from_source(args.org, branch_snap, args.wait_seconds, api_ver)
            org_snap = snapshot_org_from_org(args.org, args.wait_seconds, api_ver)
            print("\nCreated snapshot ids:")
            print(f"  - {branch_snap.snapshot_id}")
            print(f"  - {src_snap.snapshot_id}")
            print(f"  - {org_snap.snapshot_id}")
            try:
                pkg_snap = snapshot_installed_packages(args.org)
                print(f"  - {pkg_snap.snapshot_id}")
            except Exception as exc:
                print(f"  \u26a0 Package snapshot skipped: {exc}", flush=True)
            return 0
        if args._fn == "ui":
            return run_ui(args.left, args.right, args.port, args.no_open, api_ver)
        if args._fn == "snapshot_packages":
            snap = snapshot_installed_packages(args.org)
            print(json.dumps({"snapshot_id": snap.snapshot_id, "status": "success"}), flush=True)
            return 0
        if args._fn == "compare_packages":
            return compare_installed_packages(
                args.left, args.right, args.out,
                as_json=args.as_json, fail_on_diff=args.fail_on_diff,
                notify_webhook=args.notify_webhook,
            )
        if args._fn == "verify_snapshot":
            return verify_snapshot_output(args.snapshot_id)
        if args._fn == "history":
            return print_comparison_history(
                as_json=args.json, left_filter=args.left, right_filter=args.right,
                trend=getattr(args, "trend", False),
            )
        if args._fn == "diff":
            return run_diff(
                args.left, args.right, args.as_json, args.fail_on_diff,
                fail_if_orphaned=args.fail_if_orphaned,
                output_file=args.output_file,
                include_types=args.include_type or [],
                exclude_types=args.exclude_type or [],
                fail_if_diff_count_exceeds=args.fail_if_diff_count_exceeds,
                use_baseline=not args.no_baseline,
                notify_webhook=args.notify_webhook,
            )
        if args._fn == "baseline":
            from mct.baseline import run_baseline_command
            return run_baseline_command(args)
        if args._fn == "attribute":
            from mct.attribution import run_attribute
            return run_attribute(args.org, args.since_days, args.names, args.as_json)
        if args._fn == "compare_matrix":
            from mct.comparison import run_compare_matrix
            return run_compare_matrix(args.pairs, args.as_json, args.fail_on_any_diff,
                                      use_baseline=not args.no_baseline)
        if args._fn == "delete_snapshots":
            if args.all:
                return run_delete_snapshots(None, True, args.force)
            return run_delete_snapshots(list(args.delete_ids or []), False, False)
        if args._fn == "migrate":
            from mct.migration import run_migrate
            return run_migrate(args)
    except Exception as exc:
        print(f"\u2717 {exc}", file=sys.stderr)
        return 1
    return 0


setattr(sys.modules[__name__], "main", main)


if __name__ == "__main__":
    raise SystemExit(main())
