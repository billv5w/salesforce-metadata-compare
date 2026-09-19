"""mct - Metadata Compare Tool CLI dispatcher."""
from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path

# After `pipx install .`, scripts are packed into mct/scripts/ inside the wheel
# (via force-include in pyproject.toml).
# In editable/dev mode, they live at scripts/ in the repo root.
_HERE = Path(__file__).parent
_SCRIPTS_INSTALLED = _HERE / "scripts"
_SCRIPTS_DEV = _HERE.parent / "scripts"


def _scripts_dir() -> Path:
    if _SCRIPTS_INSTALLED.is_dir():
        return _SCRIPTS_INSTALLED
    if _SCRIPTS_DEV.is_dir():
        return _SCRIPTS_DEV
    raise RuntimeError(
        "Cannot locate scripts/ directory. "
        "Re-install with: pipx install --force /path/to/metadata-compare-tool"
    )


def main() -> None:
    """Entry point for the `mct` CLI command."""
    args = sys.argv[1:]
    scripts = _scripts_dir()

    if not args or args[0] == "start":
        # `mct start` launches the web orchestrator UI
        script = scripts / "serve-orchestrator-ui.py"
        sys.argv = [str(script)] + (args[1:] if args else [])
        runpy.run_path(str(script), run_name="__main__")
    else:
        # All other subcommands delegate to env-compare. The filename contains
        # a hyphen, so it must be loaded by path — a plain `import env_compare`
        # can never resolve it (tests mask this via conftest preloading).
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        env_compare = sys.modules.get("env_compare")
        if env_compare is None:
            spec = importlib.util.spec_from_file_location(
                "env_compare", scripts / "env-compare.py"
            )
            assert spec is not None and spec.loader is not None
            env_compare = importlib.util.module_from_spec(spec)
            sys.modules["env_compare"] = env_compare
            spec.loader.exec_module(env_compare)
        sys.exit(env_compare.main())
