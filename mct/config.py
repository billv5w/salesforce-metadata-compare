"""Global configuration, path variables, and utility functions."""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from mct.safety import run as _run_safe

BUNDLE_ROOT = Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """Per-user data root for snapshots, indexes, and baselines.

    Lives outside the installed package so reinstalls never lose state.
    ``MCT_DATA_DIR`` overrides everything (tests, portable installs).
    """
    override = os.environ.get("MCT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "mct"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mct"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share") / "mct"


def config_root() -> Path:
    """Per-user config root (orchestrator workspaces file). ``MCT_CONFIG_DIR``
    overrides; otherwise the platform's per-user config location."""
    override = os.environ.get("MCT_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "mct"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mct"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "mct"


def workspaces_path() -> Path:
    """Orchestrator workspace registry.

    The pre-externalization location (``~/.config/mct/workspaces.json``) is
    preserved while it exists — upgrades keep reading it until the user runs
    the explicit ``migrate`` command. Once the migrated copy exists under
    config_root(), it wins: the preserved legacy file is a backup, not the
    live registry. New installs land in config_root().
    """
    if os.environ.get("MCT_CONFIG_DIR"):
        return config_root() / "workspaces.json"
    migrated = config_root() / "workspaces.json"
    if migrated.is_file():
        return migrated
    legacy = Path.home() / ".config" / "mct" / "workspaces.json"
    if legacy.is_file():
        return legacy
    return migrated


# Salesforce DX project root (git + sf cwd, manifest/ lives here).
PROJECT_ROOT = Path.cwd().resolve()
# Snapshot trees, packages JSON, and snapshots.json — per-user data dir,
# outside the installed package, keyed by project.
STORAGE_ROOT = data_root() / "snapshot-store" / "initial"
STATE_DIR = STORAGE_ROOT
INDEX_PATH = STATE_DIR / "snapshots.json"
COMPARISON_INDEX_PATH = STATE_DIR / "comparisons.json"
MANIFEST_DIR = PROJECT_ROOT / "manifest"
DEFAULT_API_VERSION = "66.0"
# Manifests over this many members retrieve in multiple requests (the
# Metadata API caps a single retrieve at ~10,000 files / 39 MB zipped;
# source format runs ~1-2 files per member). 0 disables chunking.
RETRIEVE_CHUNK_SIZE = 4000
# Legacy single-package default retained only for the --source-subdir override path.
_LEGACY_SOURCE_SUBDIR = "force-app/main/default"
READ_ONLY_GIT_REMOTE = "origin"


def normalize_api_version(v: str) -> str:
    """Salesforce CLI expects versions like '66.0', not '66'."""
    s = (v or "").strip() or DEFAULT_API_VERSION
    if "." not in s:
        return f"{s}.0"
    return s


def _storage_key(project_root: Path) -> str:
    normalized = str(project_root.resolve())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def require_dx_project() -> None:
    """Fail with a clear message when PROJECT_ROOT is not a Salesforce DX project."""
    if not (PROJECT_ROOT / "sfdx-project.json").is_file():
        print(
            f"\u2717 {PROJECT_ROOT} is not a Salesforce DX project (no sfdx-project.json found).\n"
            "  Run mct from inside your DX project, or pass --repo-root /path/to/dx-project.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def apply_repo_root(repo_root: str | None) -> None:
    """Set DX project root (git/sf) and per-project snapshot storage under the
    per-user data root (MCT_DATA_DIR or the platform's data location)."""
    global PROJECT_ROOT, STORAGE_ROOT, STATE_DIR, INDEX_PATH, COMPARISON_INDEX_PATH, MANIFEST_DIR
    if repo_root:
        PROJECT_ROOT = Path(repo_root).expanduser().resolve()
    else:
        PROJECT_ROOT = Path.cwd().resolve()
    STORAGE_ROOT = (
        data_root() / "snapshot-store" / _storage_key(PROJECT_ROOT)
    )
    STATE_DIR = STORAGE_ROOT
    INDEX_PATH = STATE_DIR / "snapshots.json"
    COMPARISON_INDEX_PATH = STATE_DIR / "comparisons.json"
    MANIFEST_DIR = PROJECT_ROOT / "manifest"

    legacy_key = (
        BUNDLE_ROOT / ".metadata-compare" / "snapshot-store" / _storage_key(PROJECT_ROOT)
    )
    if legacy_key.is_dir() and not STORAGE_ROOT.exists():
        print(
            f"Note: legacy snapshot data exists at {legacy_key} — run "
            "`mct migrate` to copy it into the new data location "
            "(the legacy copy is left untouched).",
            file=sys.stderr,
        )


@contextlib.contextmanager
def repo_context(repo_root: str | None):
    """Context manager that applies a repo root and restores previous globals on exit.

    Useful in tests and anywhere temporary repo switching is needed without
    risking global state leaks if an exception occurs.
    """
    # Save current values
    saved = {
        "PROJECT_ROOT": PROJECT_ROOT,
        "STORAGE_ROOT": STORAGE_ROOT,
        "STATE_DIR": STATE_DIR,
        "INDEX_PATH": INDEX_PATH,
        "COMPARISON_INDEX_PATH": COMPARISON_INDEX_PATH,
        "MANIFEST_DIR": MANIFEST_DIR,
    }
    try:
        apply_repo_root(repo_root)
        yield
    finally:
        # Restore all globals
        import mct.config as _self
        for k, v in saved.items():
            setattr(_self, k, v)


def log(msg: str) -> None:
    print(f"\n{'─' * 60}\n▶ {msg}\n{'─' * 60}")


def sanitize_token(value: str) -> str:
    token = value.strip().replace("\\", "-").replace("/", "-")
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if not token:
        # Fall back to a short hash so all-special-character values don't collide on "unknown"
        token = "x" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return token


def ts_local() -> str:
    """Local wall-clock time plus numeric offset (e.g. ``20260327-153045-0500`` for 3:30:45 PM at UTC-5)."""
    dt = datetime.now().astimezone()
    return dt.strftime("%Y%m%d-%H%M%S") + dt.strftime("%z")


def storage_rel(path: Path) -> str:
    return path.resolve().relative_to(STORAGE_ROOT.resolve()).as_posix()


def project_rel(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    text: bool | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Convenience wrapper that passes PROJECT_ROOT as cwd."""
    return _run_safe(cmd, check=check, capture=capture, text=text, cwd=str(PROJECT_ROOT), timeout=timeout)
