#!/usr/bin/env python3
"""Release packaging verification (Phase 9).

Builds wheel + sdist, checks artifact contents (UI/vendor/license assets
present; .sf/.sfdx/state/test output absent), installs the wheel into a
fresh venv outside the checkout, and smoke-runs the installed CLI
(`mct --help`, `mct list --json`, `mct start --port 0 --no-open` bounded).
Also rebuilds a wheel from the unpacked sdist.

Usage:  python scripts/verify_dist.py
Exit:   0 on success, 1 on any failed check.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def run(cmd, cwd=None, env=None, timeout=300, capture=True):
    return subprocess.run(
        cmd, cwd=cwd, env=env, timeout=timeout,
        capture_output=capture, text=True,
    )


def wheel_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def check_artifacts(wheel: Path, sdist: Path) -> None:
    names = wheel_names(wheel)
    required = [
        "mct/cli.py",
        "mct/scripts/env-compare.py",
        "mct/scripts/serve-diff-ui.py",
        "mct/scripts/serve-orchestrator-ui.py",
        "mct/scripts/ui/index.html",
        "mct/scripts/ui/diff.js",
        "mct/scripts/ui/kit/theme.css",
        "mct/scripts/ui/kit/shell.js",
        "mct/scripts/ui/kit/vendor/diff2html.min.js",
        "mct/scripts/ui/kit/vendor/diff2html.min.css",
        "mct/py.typed",
    ]
    for req in required:
        check(req in names, f"wheel contains {req}")
    check(any(n.endswith("LICENSE") or n == "LICENSE" for n in names),
          "wheel contains LICENSE")
    state_banned = re.compile(r"(\.sf/|\.sfdx/|\.metadata-compare/|test-results|"
                              r"playwright-report|__pycache__|\.pytest_cache|"
                              r"node_modules/)")
    bad = [n for n in names if state_banned.search(n) or n.startswith("tests/")]
    check(not bad, "wheel excludes state/test artifacts", ", ".join(bad[:5]))

    with tarfile.open(sdist) as tf:
        sdist_names = tf.getnames()
    check(any("pyproject.toml" in n for n in sdist_names), "sdist has pyproject.toml")
    # Test *sources* in an sdist are fine; state stores and test *output* are not.
    bad = [n for n in sdist_names if state_banned.search(n)]
    check(not bad, "sdist excludes state/test-output artifacts", ", ".join(bad[:5]))


def _scrape_url(proc, timeout=30) -> str:
    lines: list[str] = []

    def _reader():
        for line in proc.stdout:
            lines.append(line)

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = re.search(r"METADATA_COMPARE_ORCHESTRATOR_UI_URL=(\S+)", "".join(lines))
        if m:
            return m.group(1)
        if proc.poll() is not None:
            raise RuntimeError(f"server exited {proc.returncode}: {''.join(lines)}")
        time.sleep(0.05)
    raise RuntimeError(f"no URL marker within {timeout}s: {''.join(lines)}")


def smoke_install(wheel: Path, work: Path) -> None:
    venv = work / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)],
                   check=True, capture_output=True)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    data_dir = work / "user-data"
    env = {**os.environ, "MCT_DATA_DIR": str(data_dir),
           "MCT_CONFIG_DIR": str(work / "user-config")}

    r = run([str(py), "-m", "pip", "install", "--quiet", str(wheel)], env=env)
    check(r.returncode == 0, "wheel installs into fresh venv", r.stderr[-400:])

    # Invoke the installed console script — the real user entry point.
    mct_bin = venv / ("Scripts/mct.exe" if os.name == "nt" else "bin/mct")
    check(mct_bin.exists(), "mct console script installed")

    def mct(*args, timeout=60):
        return run([str(mct_bin), *args], env=env, timeout=timeout)

    r = mct("--help")
    check(r.returncode == 0 and "usage" in (r.stdout + r.stderr).lower(),
          "mct --help", r.stderr[-300:])

    r = mct("list", "--json")
    check(r.returncode == 0, "mct list --json exit 0", r.stderr[-300:])
    if r.returncode == 0:
        try:
            payload = json.loads(r.stdout or "{}")
            check(isinstance(payload, (dict, list)), "mct list --json parses")
        except json.JSONDecodeError:
            check(False, "mct list --json parses", r.stdout[:200])

    # mct start --port 0 --no-open: bounded startup on the actual bound URL.
    proc = subprocess.Popen(
        [str(mct_bin), "start", "--port", "0", "--no-open"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        url = _scrape_url(proc)
        check(True, "mct start prints bound URL (ephemeral port)")
        try:
            with urllib.request.urlopen(f"{url}/", timeout=10) as resp:
                check(resp.status == 200, "orchestrator page responds 200")
        except Exception as e:  # noqa: BLE001
            check(False, "orchestrator page responds 200", str(e))
    except Exception as e:  # noqa: BLE001
        check(False, "mct start prints bound URL", str(e))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            check(False, "mct start shuts down on SIGTERM")


def _build_python(work: Path) -> Path:
    """Interpreter that can run `python -m build`. If the current one lacks
    the `build` module (PEP 668 environments can't pip-install it), create a
    dedicated tooling venv."""
    import importlib.util

    if importlib.util.find_spec("build") is not None:
        return Path(sys.executable)
    venv = work / "build-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)],
                   check=True, capture_output=True)
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    r = run([str(py), "-m", "pip", "install", "--quiet", "build"])
    if r.returncode != 0:
        raise RuntimeError(f"could not install 'build' into tooling venv: {r.stderr}")
    return py


def rebuild_from_sdist(sdist: Path, work: Path, build_py: Path) -> Path | None:
    extract = work / "sdist-src"
    with tarfile.open(sdist) as tf:
        tf.extractall(extract, filter="data")
    src = next(extract.iterdir())
    out = work / "sdist-dist"
    out.mkdir()
    r = run([str(build_py), "-m", "build", "--wheel", "--outdir", str(out)],
            cwd=src)
    check(r.returncode == 0, "wheel rebuilds from unpacked sdist",
          (r.stdout + r.stderr)[-400:])
    wheels = list(out.glob("*.whl"))
    if not wheels:
        check(False, "wheel rebuilds from unpacked sdist", "no wheel produced")
        return None
    return wheels[0]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="mct-dist-check-"))
    dist = work / "dist"
    print(f"verify_dist: workdir {work}")
    try:
        build_py = _build_python(work)
        r = run([str(build_py), "-m", "build", "--outdir", str(dist)], cwd=REPO)
        if r.returncode != 0:
            print((r.stdout or "") + (r.stderr or ""))
            print("ERROR: python -m build failed")
            return 1
        wheel = next(dist.glob("*.whl"))
        sdist = next(dist.glob("*.tar.gz"))
        print(f"built: {wheel.name}, {sdist.name}")

        print("\n[artifact contents]")
        check_artifacts(wheel, sdist)

        print("\n[wheel install + CLI smoke]")
        smoke_install(wheel, work)

        print("\n[sdist rebuild]")
        rebuilt = rebuild_from_sdist(sdist, work, build_py)
        if rebuilt:
            names = wheel_names(rebuilt)
            check(
                "mct/scripts/ui/kit/vendor/diff2html.min.js" in names,
                "sdist-built wheel retains vendored assets",
            )
    finally:
        if os.environ.get("MCT_VERIFY_KEEP"):
            print(f"kept workdir: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILURES:
        print(f"verify_dist FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("verify_dist PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
