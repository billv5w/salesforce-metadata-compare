"""Unit tests for serve-orchestrator-ui.py — run_streaming and task status."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "serve_orchestrator_ui",
    Path(__file__).parent.parent.parent / "scripts" / "serve-orchestrator-ui.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

run_streaming = _mod.run_streaming
_tasks = _mod._tasks
_tasks_lock = _mod._tasks_lock


def _collect(gen):
    """Drain a run_streaming generator and return list of event dicts."""
    return list(gen)


def test_run_streaming_yields_lines():
    task_id = "test-yield-lines"
    cmd = [sys.executable, "-c", "print('hello'); print('world')"]
    events = _collect(run_streaming(cmd, task_id))

    data_events = [e for e in events if e["type"] == "data"]
    assert any(e["data"] == "hello" for e in data_events)
    assert any(e["data"] == "world" for e in data_events)

    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    payload = json.loads(done_events[0]["data"])
    assert payload["exit_code"] == 0


def test_run_streaming_captures_stderr():
    task_id = "test-stderr"
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('err-msg\\n')"]
    events = _collect(run_streaming(cmd, task_id))

    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    payload = json.loads(done_events[0]["data"])
    assert "err-msg" in payload["stderr"]


def test_run_streaming_timeout():
    task_id = "test-timeout"
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    events = _collect(run_streaming(cmd, task_id, timeout=1))

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert "timed out" in error_events[0]["data"].lower()

    with _tasks_lock:
        assert _tasks[task_id]["status"] == "timeout"


def test_task_status_lifecycle():
    task_id = "test-lifecycle"
    cmd = [sys.executable, "-c", "print('step1')"]

    with _tasks_lock:
        _tasks.pop(task_id, None)

    assert task_id not in _tasks

    events = _collect(run_streaming(cmd, task_id))

    with _tasks_lock:
        task = dict(_tasks.get(task_id, {}))

    assert task["status"] == "done"
    assert task["exit_code"] == 0
    assert any(line == "step1" for line in task["output_lines"])
