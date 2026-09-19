"""Command safety: forbidden-verb blocklist and subprocess allowlist."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Forbidden command verbs checked as whole tokens (case-insensitive).
# Using a frozenset for O(1) lookup; substring matches in branch names or flags
# are intentionally NOT blocked — only exact argv elements are compared.
_FORBIDDEN_VERBS = frozenset({"push", "commit", "merge", "rebase", "deploy", "delete", "update"})

_mct_dir = Path(__file__).resolve().parent
SCRIPT_DIR = _mct_dir / "scripts" if (_mct_dir / "scripts").is_dir() else _mct_dir.parent / "scripts"


# The ONE deploy-shaped command permitted: validation-only. `--dry-run` makes
# Salesforce run the full deploy pipeline (compile, tests per org settings)
# WITHOUT saving anything. Explicit owner decision (roadmap 3.11) — every
# other deploy form stays blocked.
_VALIDATE_ONLY_PREFIX = ("sf", "project", "deploy", "start")


def ensure_safe_command(cmd: list[str]) -> None:
    if not cmd:
        raise ValueError("Empty command")
    command_text = " ".join(cmd)
    if tuple(cmd[:4]) == _VALIDATE_ONLY_PREFIX:
        if "--dry-run" not in cmd:
            raise RuntimeError(
                "Blocked: 'sf project deploy start' is allowed ONLY with --dry-run "
                f"(validate-only, nothing saved to the org): {command_text}"
            )
        return
    # Check forbidden verbs as whole tokens only (not substrings),
    # so branch names like feature/update-readme or deploy-config are not blocked.
    if any(part.lower() in _FORBIDDEN_VERBS for part in cmd):
        raise RuntimeError(f"Blocked unsafe command: {command_text}")

    allowed_prefixes = (
        ("git", "rev-parse"),
        ("git", "fetch"),
        ("git", "archive"),
        ("sf", "--version"),
        ("sf", "project", "generate", "manifest"),
        ("sf", "project", "retrieve", "start"),
        ("sf", "package", "installed", "list"),
        ("sf", "data", "query"),  # read-only SOQL (drift attribution)
        (sys.executable, str(SCRIPT_DIR / "serve-diff-ui.py")),
    )
    if not any(tuple(cmd[: len(prefix)]) == prefix for prefix in allowed_prefixes):
        raise RuntimeError(f"Command not in allowlist: {command_text}")


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    text: bool | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run a safe-listed subprocess.

    *cwd* defaults to the caller-supplied value; when called from other ``mct``
    modules the value is typically ``str(mct.config.PROJECT_ROOT)``.
    *timeout* (seconds) is passed directly to subprocess.run; None means no limit.
    """
    ensure_safe_command(cmd)
    print(f"  $ {' '.join(cmd)}")
    if text is None:
        text = True if capture else None
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=text,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed (exit {result.returncode})")
    return result
