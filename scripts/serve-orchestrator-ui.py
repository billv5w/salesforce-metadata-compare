#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mct.config as _cfg
from mct.ui_server_base import BaseUIHandler, serve_ui

UI_DIR = Path(__file__).resolve().parent / "ui"
ENV_COMPARE = Path(__file__).resolve().parent / "env-compare.py"
WORKSPACES_PATH = _cfg.workspaces_path()
WORKSPACES_DIR = WORKSPACES_PATH.parent
DEFAULT_API_VERSION = "66.0"
TASK_CLEANUP_SECS = 300

_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


def repo_cmd_prefix(repo_root: str | None) -> list[str]:
    if repo_root and str(repo_root).strip():
        return ["--repo-root", str(repo_root).strip()]
    return []


def retrieve_cmd_prefix(repo_root: str | None, payload: dict) -> list[str]:
    """repo-root + Metadata API version for manifest/retrieve snapshot commands."""
    out = list(repo_cmd_prefix(repo_root))
    v = (payload.get("api_version") or DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION
    if "." not in v:
        v = f"{v}.0"
    out.extend(["--api-version", v])
    return out


def run_streaming(cmd: list[str], task_id: str, timeout: int = 600):
    """Spawn cmd via Popen and yield SSE event dicts. Registers task in _tasks."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "running",
            "output_lines": [],
            "exit_code": None,
            "stderr": "",
        }
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stderr_chunks: list[str] = []
        line_q: queue.Queue[tuple[str, str] | None] = queue.Queue()

        def _read_stdout() -> None:
            if proc.stdout:
                for raw in iter(proc.stdout.readline, ""):
                    line_q.put(("data", raw.rstrip("\n")))
            line_q.put(None)

        def _drain_stderr() -> None:
            if proc.stderr:
                for line in iter(proc.stderr.readline, ""):
                    stderr_chunks.append(line)

        threading.Thread(target=_read_stdout, daemon=True).start()
        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            remaining = timeout - elapsed
            if remaining <= 0:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except OSError:
                    pass
                with _tasks_lock:
                    _tasks[task_id]["status"] = "timeout"
                yield {"type": "error", "data": f"Operation timed out after {timeout}s"}
                return
            try:
                item = line_q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if item is None:
                break
            _, line = item
            with _tasks_lock:
                _tasks[task_id]["output_lines"].append(line)
            yield {"type": "data", "data": line}

        proc.wait()
        stderr_thread.join(timeout=5)
        stderr_text = "".join(stderr_chunks)

        with _tasks_lock:
            _tasks[task_id]["status"] = "done"
            _tasks[task_id]["exit_code"] = proc.returncode
            _tasks[task_id]["stderr"] = stderr_text

        yield {"type": "done", "data": json.dumps({"exit_code": proc.returncode, "stderr": stderr_text})}

        def _cleanup() -> None:
            time.sleep(TASK_CLEANUP_SECS)
            with _tasks_lock:
                _tasks.pop(task_id, None)

        threading.Thread(target=_cleanup, daemon=True).start()

    except (OSError, subprocess.SubprocessError) as e:
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
        yield {"type": "error", "data": str(e)}


def run_json(cmd: list[str], timeout: int = 600) -> tuple[int, dict]:
    """Run env-compare subprocess; never raises (so the HTTP handler always returns JSON)."""
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, {"stdout": "", "stderr": f"Command timed out after {timeout}s"}
    except (OSError, subprocess.SubprocessError) as e:
        return 1, {"stdout": "", "stderr": f"Could not run orchestrator: {e}"}
    return proc.returncode, {
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def load_workspaces() -> dict:
    if not WORKSPACES_PATH.is_file():
        return {"version": 1, "workspaces": []}
    return json.loads(WORKSPACES_PATH.read_text(encoding="utf-8"))


def save_workspaces(data: dict) -> None:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


_KIT_CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript", ".md": "text/plain"}


class Handler(BaseUIHandler):
    def _serve_kit_file(self, path: str):
        rel = path[len("/kit/") :]
        filepath = (UI_DIR / "kit" / rel).resolve()
        kit_root = (UI_DIR / "kit").resolve()
        if kit_root not in filepath.parents:
            self.send_error(404)
            return
        self._serve_file(filepath, _KIT_CONTENT_TYPES.get(filepath.suffix, "application/octet-stream"))

    def _route_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._serve_bootstrap_html(UI_DIR / "orchestrator.html")
            return
        if parsed.path == "/orchestrator.js":
            self._serve_file(UI_DIR / "orchestrator.js", "application/javascript")
            return
        if parsed.path.startswith("/kit/"):
            self._serve_kit_file(parsed.path)
            return
        if parsed.path == "/api/list":
            qs = parse_qs(parsed.query)
            rr = qs.get("repo_root", [None])[0]
            code, data = run_json(
                [sys.executable, str(ENV_COMPARE), *repo_cmd_prefix(rr), "list"]
            )
            self._serve_json({"ok": code == 0, **data}, 200 if code == 0 else 500)
            return
        if parsed.path == "/api/snapshots":
            qs = parse_qs(parsed.query)
            rr = qs.get("repo_root", [None])[0]
            code, data = run_json(
                [sys.executable, str(ENV_COMPARE), *repo_cmd_prefix(rr), "list", "--json"]
            )
            if code != 0:
                self._serve_json({"ok": False, **data}, 500)
                return
            try:
                payload = json.loads(data.get("stdout") or "{}")
            except json.JSONDecodeError:
                self._serve_json({"ok": False, "stderr": "Invalid JSON from list --json", **data}, 500)
                return
            self._serve_json({"ok": True, "data": payload}, 200)
            return
        if parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            rr = qs.get("repo_root", [None])[0]
            code, data = run_json([sys.executable, str(ENV_COMPARE), *repo_cmd_prefix(rr), "history", "--json"])
            if code != 0:
                self._serve_json({"ok": False, **data}, 500)
                return
            try:
                payload = json.loads(data.get("stdout") or "{}")
            except json.JSONDecodeError:
                self._serve_json({"ok": False, "stderr": "Invalid JSON from history --json", **data}, 500)
                return
            self._serve_json({"ok": True, "data": payload}, 200)
            return
        if parsed.path == "/api/workspaces":
            try:
                ws_data = load_workspaces()
            except OSError as e:
                self._serve_json({"ok": False, "error": str(e)}, 500)
                return
            self._serve_json({"ok": True, "workspaces": ws_data.get("workspaces", [])}, 200)
            return
        if parsed.path == "/api/task-status":
            qs = parse_qs(parsed.query)
            task_id = qs.get("id", [None])[0]
            if not task_id:
                self._serve_json({"ok": False, "error": "id query parameter required"}, 400)
                return
            with _tasks_lock:
                task = _tasks.get(task_id)
            if task is None:
                self._serve_json({"ok": False, "error": "task not found"}, 404)
                return
            self._serve_json(
                {
                    "ok": True,
                    "status": task["status"],
                    "output_lines": list(task["output_lines"]),
                    "exit_code": task["exit_code"],
                    "stderr": task["stderr"],
                },
                200,
            )
            return
        if parsed.path == "/api/validate-repo":
            qs = parse_qs(parsed.query)
            rr = (qs.get("repo_root", [""])[0] or "").strip()
            if not rr:
                self._serve_json({"ok": False, "error": "repo_root query parameter required"}, 400)
                return
            p = Path(rr).expanduser().resolve()
            exists = p.is_dir()
            has_git = (p / ".git").exists()
            has_force_app = (p / "force-app" / "main" / "default").is_dir()
            self._serve_json(
                {
                    "ok": True,
                    "exists": exists,
                    "has_git": has_git,
                    "has_force_app_default": has_force_app,
                },
                200,
            )
            return
        if parsed.path == "/api/snapshot-files":
            qs = parse_qs(parsed.query)
            snap_path_raw = (qs.get("snapshot_path", [None])[0] or "").strip()
            if not snap_path_raw:
                self._serve_json({"ok": False, "error": "snapshot_path query parameter required"}, 400)
                return
            snap_dir = Path(snap_path_raw).expanduser().resolve()
            if not snap_dir.is_dir():
                self._serve_json({"ok": False, "error": f"Snapshot directory not found: {snap_dir}"}, 404)
                return
            try:
                all_files = sorted(
                    str(p.relative_to(snap_dir))
                    for p in snap_dir.rglob("*")
                    if p.is_file()
                )
                truncated = len(all_files) > 2000
                files = all_files[:2000]
            except OSError as e:
                self._serve_json({"ok": False, "error": str(e)}, 500)
                return
            self._serve_json({"ok": True, "files": files, "count": len(files), "truncated": truncated}, 200)
            return
        self.send_error(404)

    @staticmethod
    def _validate_payload_fields(payload: dict, *required_fields: str) -> "str | None":
        """Return an error message if any required field is missing or empty, else None."""
        for field in required_fields:
            val = payload.get(field)
            if not val or not str(val).strip():
                return f"Missing or empty required field: '{field}'"
            if len(str(val)) > 500:
                return f"Field '{field}' exceeds maximum length (500 chars)"
        return None

    def _route_POST(self):
        parsed = urlparse(self.path)
        payload = self._read_body()
        if payload is None:
            return

        if parsed.path == "/api/run-stream":
            action = payload.get("action", "")
            task_id = str(uuid.uuid4())
            rr = payload.get("repo_root")
            if action == "snapshot-branch":
                err = self._validate_payload_fields(payload, "branch")
                if err:
                    self._serve_json({"ok": False, "error": err}, 400)
                    return
                cmd = [
                    sys.executable, str(ENV_COMPARE),
                    *repo_cmd_prefix(rr),
                    "snapshot-branch",
                    "--branch", payload.get("branch", ""),
                ]
                if payload.get("fetch"):
                    cmd.append("--fetch")
            elif action == "snapshot-org-from-source":
                err = self._validate_payload_fields(payload, "branch", "org")
                if err:
                    self._serve_json({"ok": False, "error": err}, 400)
                    return
                cmd = [
                    sys.executable, str(ENV_COMPARE),
                    *retrieve_cmd_prefix(rr, payload),
                    "snapshot-org-from-source",
                    "--org", payload.get("org", ""),
                    "--branch", payload.get("branch", ""),
                    "--wait-seconds", str(payload.get("wait_seconds", 120)),
                ]
                if payload.get("fetch"):
                    cmd.append("--fetch")
            elif action == "snapshot-org-from-org":
                err = self._validate_payload_fields(payload, "org")
                if err:
                    self._serve_json({"ok": False, "error": err}, 400)
                    return
                cmd = [
                    sys.executable, str(ENV_COMPARE),
                    *retrieve_cmd_prefix(rr, payload),
                    "snapshot-org-from-org",
                    "--org", payload.get("org", ""),
                    "--wait-seconds", str(payload.get("wait_seconds", 120)),
                ]
            elif action == "snapshot-org-bidirectional":
                err = self._validate_payload_fields(payload, "branch", "org")
                if err:
                    self._serve_json({"ok": False, "error": err}, 400)
                    return
                cmd = [
                    sys.executable, str(ENV_COMPARE),
                    *retrieve_cmd_prefix(rr, payload),
                    "snapshot-org-bidirectional",
                    "--org", payload.get("org", ""),
                    "--branch", payload.get("branch", ""),
                    "--wait-seconds", str(payload.get("wait_seconds", 120)),
                ]
                if payload.get("fetch"):
                    cmd.append("--fetch")
                for t in (payload.get("include_org_types") or []):
                    if t:
                        cmd.extend(["--include-org-type", t])
            elif action == "snapshot-all":
                err = self._validate_payload_fields(payload, "branch", "org")
                if err:
                    self._serve_json({"ok": False, "error": err}, 400)
                    return
                cmd = [
                    sys.executable, str(ENV_COMPARE),
                    *retrieve_cmd_prefix(rr, payload),
                    "snapshot-all",
                    "--branch", payload.get("branch", ""),
                    "--org", payload.get("org", ""),
                    "--wait-seconds", str(payload.get("wait_seconds", 120)),
                ]
                if payload.get("fetch"):
                    cmd.append("--fetch")
            else:
                self._serve_json({"ok": False, "error": f"Unknown action: {action}"}, 400)
                return
            self._serve_sse(run_streaming(cmd, task_id))
            return

        if parsed.path == "/api/snapshot-branch":
            rr = payload.get("repo_root")
            cmd = [
                sys.executable,
                str(ENV_COMPARE),
                *repo_cmd_prefix(rr),
                "snapshot-branch",
                "--branch",
                payload.get("branch", ""),
            ]
            if payload.get("fetch"):
                cmd.append("--fetch")
            code, data = run_json(cmd)
            self._serve_json({"ok": code == 0, **data}, 200 if code == 0 else 500)
            return

        if parsed.path == "/api/compare":
            self._serve_json(
                {
                    "ok": False,
                    "error": "Endpoint '/api/compare' is deprecated. Open the diff viewer via '/api/launch-diff-ui' and generate reports from the diff page.",
                },
                410,
            )
            return

        if parsed.path == "/api/launch-diff-ui":
            port = int(payload.get("port", 8090))
            if not (1 <= port <= 65535):
                self._serve_json({"ok": False, "error": "port must be between 1 and 65535"}, 400)
                return
            rr = payload.get("repo_root")
            # retrieve_cmd_prefix also forwards --api-version so exported delta
            # manifests are stamped with the workspace's Metadata API version.
            cmd = [
                sys.executable,
                str(ENV_COMPARE),
                *retrieve_cmd_prefix(rr, payload),
                "ui",
                "--left",
                payload.get("left", ""),
                "--right",
                payload.get("right", ""),
                "--port",
                str(port),
                "--no-open",
            ]
            for t in (payload.get("include_types") or []):
                if t:
                    cmd.extend(["--include-type", t])
            for t in (payload.get("exclude_types") or []):
                if t:
                    cmd.extend(["--exclude-type", t])
            child_env = os.environ.copy()
            child_env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
            )
            url: str | None = None
            lines: list[str] = []
            stderr_chunks: list[str] = []

            def _drain_stderr() -> None:
                if proc.stderr:
                    for line in iter(proc.stderr.readline, ""):
                        stderr_chunks.append(line)

            def _read_until_url() -> None:
                if not proc.stdout:
                    return
                for line in iter(proc.stdout.readline, ""):
                    lines.append(line)
                    if line.startswith("METADATA_COMPARE_DIFF_UI_URL="):
                        return

            threading.Thread(target=_drain_stderr, daemon=True).start()
            reader = threading.Thread(target=_read_until_url, daemon=True)
            reader.start()
            reader.join(timeout=60.0)
            for line in lines:
                if line.startswith("METADATA_COMPARE_DIFF_UI_URL="):
                    url = line.strip().split("=", 1)[1].strip()
                    break
            if not url:
                err_tail = "".join(stderr_chunks)[-2000:]
                if proc.poll() is not None and proc.returncode != 0:
                    self._serve_json(
                        {
                            "ok": False,
                            "error": "Diff UI failed to start (see stderr). If the port was busy, try again or pick another Diff UI port.",
                            "stderr": err_tail,
                        },
                        500,
                    )
                    return
                # Process is still running but did not print its URL within the timeout — terminate it.
                try:
                    proc.terminate()
                except OSError:
                    pass
                self._serve_json(
                    {
                        "ok": False,
                        "error": "Diff UI did not print its URL in time — often because stdout was read too early or the process is still binding. "
                        "Stop any old Diff UI on that port (or use port 0 in the CLI) and try again.",
                        "stderr": err_tail,
                    },
                    500,
                )
                return
            self._serve_json({"ok": True, "pid": proc.pid, "url": url}, 200)
            return

        if parsed.path == "/api/snapshot-packages":
            rr = payload.get("repo_root")
            cmd = [
                sys.executable,
                str(ENV_COMPARE),
                *repo_cmd_prefix(rr),
                "snapshot-packages",
                "--org",
                payload.get("org", ""),
            ]
            code, data = run_json(cmd)
            self._serve_json({"ok": code == 0, **data}, 200 if code == 0 else 500)
            return

        if parsed.path == "/api/compare-packages":
            rr = payload.get("repo_root")
            cmd = [
                sys.executable,
                str(ENV_COMPARE),
                *repo_cmd_prefix(rr),
                "compare-packages",
                "--left",
                payload.get("left", ""),
                "--right",
                payload.get("right", ""),
            ]
            out = payload.get("out")
            if out:
                cmd.extend(["-o", out])
            code, data = run_json(cmd)
            self._serve_json({"ok": code == 0, **data}, 200 if code == 0 else 500)
            return

        if parsed.path == "/api/delete-snapshots":
            rr = (payload.get("repo_root") or "").strip()
            if not rr:
                self._serve_json({"ok": False, "error": "repo_root is required"}, 400)
                return
            cmd = [sys.executable, str(ENV_COMPARE), *repo_cmd_prefix(rr), "delete-snapshots"]
            if payload.get("all"):
                cmd.extend(["--all", "--force"])
            else:
                raw_ids = payload.get("ids")
                if not isinstance(raw_ids, list) or not raw_ids:
                    self._serve_json(
                        {"ok": False, "error": "ids (non-empty array) required unless all is true"},
                        400,
                    )
                    return
                for i in raw_ids:
                    if not isinstance(i, str) or not i.strip():
                        self._serve_json({"ok": False, "error": "each id must be a non-empty string"}, 400)
                        return
                    cmd.extend(["--id", i.strip()])
            code, data = run_json(cmd)
            self._serve_json({"ok": code == 0, **data}, 200 if code == 0 else 500)
            return

        if parsed.path == "/api/workspaces":
            name = (payload.get("name") or "").strip()
            if not name:
                self._serve_json({"ok": False, "error": "name is required"}, 400)
                return
            entry = {
                "id": f"ws-{uuid.uuid4().hex[:12]}",
                "name": name,
                "repo_root": (payload.get("repo_root") or "").strip(),
                "branch": (payload.get("branch") or "").strip(),
                "org": (payload.get("org") or "").strip(),
                "api_version": (payload.get("api_version") or DEFAULT_API_VERSION).strip()
                or DEFAULT_API_VERSION,
                "created_at": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ"),
            }
            ws_data = load_workspaces()
            ws_data.setdefault("version", 1)
            workspaces = ws_data.setdefault("workspaces", [])
            existing_idx = next(
                (i for i, w in enumerate(workspaces) if w.get("name") == name), None
            )
            if existing_idx is not None:
                # Preserve original id and created_at; update all other fields
                entry["id"] = workspaces[existing_idx]["id"]
                entry["created_at"] = workspaces[existing_idx]["created_at"]
                workspaces[existing_idx] = entry
            else:
                workspaces.append(entry)
            try:
                save_workspaces(ws_data)
            except OSError as e:
                self._serve_json({"ok": False, "error": str(e)}, 500)
                return
            self._serve_json({"ok": True, "workspace": entry}, 200)
            return

        self._serve_json({"ok": False, "error": f"Unknown API path: {parsed.path}"}, 404)

    def _route_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/workspaces":
            qs = parse_qs(parsed.query)
            wid = qs.get("id", [None])[0]
            if not wid:
                self._serve_json({"ok": False, "error": "id query parameter required"}, 400)
                return
            try:
                ws_data = load_workspaces()
            except OSError as e:
                self._serve_json({"ok": False, "error": str(e)}, 500)
                return
            before = len(ws_data.get("workspaces", []))
            ws_data["workspaces"] = [w for w in ws_data.get("workspaces", []) if w.get("id") != wid]
            if len(ws_data["workspaces"]) == before:
                self._serve_json({"ok": False, "error": "workspace not found"}, 404)
                return
            try:
                save_workspaces(ws_data)
            except OSError as e:
                self._serve_json({"ok": False, "error": str(e)}, 500)
                return
            self._serve_json({"ok": True}, 200)
            return
        self.send_error(404)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mct start",
        description="Start the local web interface for creating snapshots and comparing metadata.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8091,
        metavar="N",
        help="Listen port (default 8091). Use 0 for an OS-assigned free port. "
        "If the chosen port is busy, the next free port is tried automatically.",
    )
    p.add_argument("--no-open", action="store_true", help="Do not open a browser automatically.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return serve_ui(
        Handler, args.port, url_marker="METADATA_COMPARE_ORCHESTRATOR_UI_URL", open_browser=not args.no_open
    )


if __name__ == "__main__":
    raise SystemExit(main())
