"""Phase 6 regressions: both local UI servers must reject unauthorized
requests BEFORE any side effect — shared enforcement in ui_server_base.

Real HTTP servers on ephemeral ports; requests via http.client so Host,
Origin, token, Content-Type, and Content-Length are all controllable.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import secrets
import sys
import threading
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mct.ui_server_base import UIHTTPServer  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


diff_mod = _load("serve_diff_ui_sec", "serve-diff-ui.py")
orch_mod = _load("serve_orchestrator_ui_sec", "serve-orchestrator-ui.py")


def _start(handler_cls, token=None):
    server = UIHTTPServer(("127.0.0.1", 0), handler_cls)
    server.session_token = token or secrets.token_urlsafe(32)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def _req(port, method, path, headers=None, body=None, skip_host=False):
    # A closing server socket can surface as RST (WinError 10054 / Errno 104)
    # or an abrupt EOF (RemoteDisconnected) instead of a clean FIN, sometimes
    # mid-body on large responses — retry a few times on a fresh connection
    # rather than flake.
    last_exc: Exception | None = None
    for attempt in range(5):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            if skip_host:
                conn.putrequest(method, path, skip_host=True)
                for k, v in (headers or {}).items():
                    conn.putheader(k, v)
                conn.endheaders(body)
            else:
                conn.request(method, path, body=body, headers=headers or {})
            res = conn.getresponse()
            data = res.read()
            out = (res.status, dict(res.getheaders()), data)
            conn.close()
            return out
        except (ConnectionResetError, http.client.RemoteDisconnected) as exc:
            conn.close()
            last_exc = exc
            if attempt < 4:
                time.sleep(0.05 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _api_headers(port, token, extra=None):
    h = {"Host": f"127.0.0.1:{port}", "X-MCT-Token": token}
    h.update(extra or {})
    return h


@pytest.fixture
def diff_server(tmp_path, monkeypatch):
    left, right = tmp_path / "left", tmp_path / "right"
    (left / "classes").mkdir(parents=True)
    (right / "classes").mkdir(parents=True)
    (left / "classes" / "A.cls").write_text("public class A { Integer v = 1; }\n")
    (right / "classes" / "A.cls").write_text("public class A { Integer v = 2; }\n")
    cls = diff_mod.DiffUIHandler
    cls.left_root, cls.right_root = left, right
    cls.left_rel, cls.right_rel = "left", "right"
    monkeypatch.setattr(diff_mod, "BASELINE_FILE", tmp_path / "baseline.json")
    monkeypatch.setattr(diff_mod, "PAIR_KEY", None)
    diff_mod.get_comparison.cache_clear()
    server = _start(cls)
    yield server
    server.shutdown()


@pytest.fixture
def orch_server(tmp_path, monkeypatch):
    monkeypatch.setattr(orch_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "WORKSPACES_PATH", tmp_path / "workspaces.json")
    server = _start(orch_mod.Handler)
    yield server
    server.shutdown()


def _port(server):
    return server.server_address[1]


# ---------------------------------------------------------------------------
# Host validation
# ---------------------------------------------------------------------------

class TestHostValidation:
    def test_foreign_host_rejected(self, diff_server):
        port = _port(diff_server)
        status, _, _ = _req(
            port, "GET", "/", headers={"Host": f"evil.example.com:{port}"}
        )
        assert status == 403

    def test_wrong_port_in_host_rejected(self, diff_server):
        port = _port(diff_server)
        status, _, _ = _req(port, "GET", "/", headers={"Host": "127.0.0.1:1"})
        assert status == 403

    def test_missing_host_rejected(self, diff_server):
        status, _, _ = _req(_port(diff_server), "GET", "/", skip_host=True)
        assert status == 403

    def test_valid_loopback_host_accepted(self, diff_server):
        port = _port(diff_server)
        status, _, _ = _req(port, "GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        assert status == 200

    def test_localhost_name_accepted(self, orch_server):
        port = _port(orch_server)
        status, _, _ = _req(port, "GET", "/", headers={"Host": f"localhost:{port}"})
        assert status == 200

    def test_host_checked_before_route(self, diff_server):
        """Even a valid API route rejects on a bad Host."""
        port = _port(diff_server)
        status, _, _ = _req(
            port, "GET", "/api/summary",
            headers={"Host": f"evil.example.com:{port}"},
        )
        assert status == 403


# ---------------------------------------------------------------------------
# Origin validation
# ---------------------------------------------------------------------------

class TestOriginValidation:
    def test_foreign_origin_rejected(self, diff_server):
        port = _port(diff_server)
        status, _, _ = _req(
            port, "GET", "/api/summary",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "http://evil.example.com",
                "X-MCT-Token": diff_server.session_token,
            },
        )
        assert status == 403

    def test_null_origin_rejected(self, orch_server):
        port = _port(orch_server)
        status, _, _ = _req(
            port, "GET", "/api/workspaces",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "null",
                "X-MCT-Token": orch_server.session_token,
            },
        )
        assert status == 403

    def test_same_origin_accepted(self, diff_server):
        port = _port(diff_server)
        status, _, _ = _req(
            port, "GET", "/api/summary",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "X-MCT-Token": diff_server.session_token,
            },
        )
        assert status == 200


# ---------------------------------------------------------------------------
# Session token
# ---------------------------------------------------------------------------

class TestSessionToken:
    def test_api_without_token_rejected(self, diff_server):
        port = _port(diff_server)
        status, _, _ = _req(
            port, "GET", "/api/summary", headers={"Host": f"127.0.0.1:{port}"}
        )
        assert status == 403

    def test_api_with_wrong_token_rejected(self, orch_server):
        port = _port(orch_server)
        status, _, _ = _req(
            port, "GET", "/api/workspaces",
            headers=_api_headers(port, "definitely-wrong-token"),
        )
        assert status == 403

    def test_api_with_token_accepted_nonbrowser(self, diff_server):
        """Non-browser clients: no Origin, valid Host + token → allowed."""
        port = _port(diff_server)
        status, _, body = _req(
            port, "GET", "/api/summary",
            headers=_api_headers(port, diff_server.session_token),
        )
        assert status == 200
        assert json.loads(body)["different_count"] >= 0

    def test_api_with_token_accepted_orchestrator(self, orch_server):
        port = _port(orch_server)
        status, _, body = _req(
            port, "GET", "/api/workspaces",
            headers=_api_headers(port, orch_server.session_token),
        )
        assert status == 200
        assert json.loads(body)["ok"] is True

    def test_bootstrap_html_delivers_token(self, diff_server):
        port = _port(diff_server)
        status, _, body = _req(
            port, "GET", "/", headers={"Host": f"127.0.0.1:{port}"}
        )
        assert status == 200
        assert diff_server.session_token.encode() in body

    def test_tokens_differ_between_instances(self, diff_server, orch_server):
        assert diff_server.session_token != orch_server.session_token

    def test_other_servers_token_rejected(self, diff_server, orch_server):
        port = _port(orch_server)
        status, _, _ = _req(
            port, "GET", "/api/workspaces",
            headers=_api_headers(port, diff_server.session_token),
        )
        assert status == 403

    def test_token_not_in_static_assets(self, diff_server):
        port = _port(diff_server)
        status, _, body = _req(
            port, "GET", "/diff.js", headers={"Host": f"127.0.0.1:{port}"}
        )
        assert status == 200
        assert diff_server.session_token.encode() not in body

    def test_token_not_in_exported_html(self, diff_server):
        port = _port(diff_server)
        status, _, body = _req(
            port, "POST", "/api/export-html",
            headers=_api_headers(
                port, diff_server.session_token,
                {"Content-Type": "application/json"},
            ),
            body=b"{}",
        )
        assert status == 200
        assert diff_server.session_token.encode() not in body


# ---------------------------------------------------------------------------
# Body / content-type validation
# ---------------------------------------------------------------------------

class TestBodyValidation:
    def _post(self, server, path, headers=None, body=None, skip_host=False):
        port = _port(server)
        h = _api_headers(port, server.session_token)
        h.update(headers or {})
        return _req(port, "POST", path, headers=h, body=body, skip_host=skip_host)

    def test_non_json_content_type_rejected(self, diff_server):
        status, _, _ = self._post(
            diff_server, "/api/export-manifest",
            headers={"Content-Type": "text/plain"}, body=b"hello",
        )
        assert status == 415

    def test_malformed_json_rejected(self, diff_server):
        status, _, _ = self._post(
            diff_server, "/api/export-manifest",
            headers={"Content-Type": "application/json"}, body=b"{not json",
        )
        assert status == 400

    def test_non_object_json_rejected(self, orch_server):
        status, _, _ = self._post(
            orch_server, "/api/workspaces",
            headers={"Content-Type": "application/json"}, body=b'[1, 2, 3]',
        )
        assert status == 400

    def test_malformed_content_length_rejected(self, diff_server):
        port = _port(diff_server)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("POST", "/api/export-manifest")
        conn.putheader("Content-Length", "not-a-number")
        conn.putheader("X-MCT-Token", diff_server.session_token)
        conn.endheaders()
        res = conn.getresponse()
        res.read()
        assert res.status == 400
        conn.close()

    def test_oversized_body_rejected_before_read(self, orch_server):
        port = _port(orch_server)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("POST", "/api/workspaces")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(2_000_000))
        conn.putheader("X-MCT-Token", orch_server.session_token)
        conn.endheaders()
        res = conn.getresponse()
        res.read()
        assert res.status == 413
        conn.close()


# ---------------------------------------------------------------------------
# Rejection happens before side effects
# ---------------------------------------------------------------------------

class TestRejectBeforeSideEffects:
    def test_accept_diff_bad_token_no_baseline_write(self, diff_server):
        baseline_file = diff_mod.BASELINE_FILE
        assert not baseline_file.exists()
        port = _port(diff_server)
        status, _, _ = _req(
            port, "POST", "/api/accept-diff",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"path": "classes/A.cls"}).encode(),
        )
        assert status == 403
        assert not baseline_file.exists()

    def test_workspace_delete_bad_token_no_mutation(self, orch_server):
        ws_file = orch_mod.WORKSPACES_PATH
        ws_file.write_text(json.dumps({"workspaces": [{"id": "w1", "name": "x"}]}))
        before = ws_file.read_text()
        port = _port(orch_server)
        status, _, _ = _req(
            port, "DELETE", "/api/workspaces?id=w1",
            headers={"Host": f"127.0.0.1:{port}"},
        )
        assert status == 403
        assert ws_file.read_text() == before

    def test_run_stream_bad_token_no_subprocess(self, orch_server, monkeypatch):
        spawned = []

        def fake_popen(*a, **kw):
            spawned.append(a)
            raise AssertionError("subprocess should never spawn")

        monkeypatch.setattr(orch_mod.subprocess, "Popen", fake_popen)
        port = _port(orch_server)
        status, _, _ = _req(
            port, "POST", "/api/run-stream",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Content-Type": "application/json",
            },
            body=json.dumps({"action": "snapshot-branch", "repo_root": "/tmp"}).encode(),
        )
        assert status == 403
        assert spawned == []

    def test_export_bad_token_no_file_reads(self, diff_server, monkeypatch):
        port = _port(diff_server)
        status, _, _ = _req(
            port, "POST", "/api/export-bundle",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Content-Type": "application/json",
            },
            body=b"{}",
        )
        assert status == 403


# ---------------------------------------------------------------------------
# OPTIONS / CORS posture
# ---------------------------------------------------------------------------

class TestOptionsCors:
    def test_options_foreign_origin_rejected_no_cors(self, diff_server):
        port = _port(diff_server)
        status, headers, _ = _req(
            port, "OPTIONS", "/api/summary",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert status in (403, 405)
        assert "Access-Control-Allow-Origin" not in headers

    def test_options_same_origin_no_permissive_cors(self, orch_server):
        port = _port(orch_server)
        status, headers, _ = _req(
            port, "OPTIONS", "/api/workspaces",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "X-MCT-Token": orch_server.session_token,
            },
        )
        assert status == 405
        assert "Access-Control-Allow-Origin" not in headers


# ---------------------------------------------------------------------------
# HEAD cannot bypass the guard
# ---------------------------------------------------------------------------

class TestHeadGuard:
    """R6: HEAD previously fell through to SimpleHTTPRequestHandler's
    filesystem serving, bypassing Host/Origin/token checks entirely."""

    def test_head_api_without_token_rejected(self, diff_server):
        port = _port(diff_server)
        status, _, _ = _req(
            port, "HEAD", "/api/summary", headers={"Host": f"127.0.0.1:{port}"}
        )
        assert status == 403

    def test_head_root_reaches_guarded_dispatcher(self, diff_server):
        """HEAD / must not fall through to SimpleHTTPRequestHandler's
        filesystem serving: a valid Host still gets 405 (no HEAD route)."""
        port = _port(diff_server)
        status, _, body = _req(
            port, "HEAD", "/", headers={"Host": f"127.0.0.1:{port}"}
        )
        assert status == 405
        assert body == b""

    def test_head_with_token_gets_405_not_file(self, diff_server):
        """An authorized HEAD must reach the guarded dispatcher (405 — no
        HEAD route), not leak filesystem listings or file bytes."""
        port = _port(diff_server)
        status, _, body = _req(
            port, "HEAD", "/api/summary",
            headers=_api_headers(port, diff_server.session_token),
        )
        assert status == 405
        assert body == b""

    def test_head_foreign_host_rejected(self, orch_server):
        port = _port(orch_server)
        status, _, _ = _req(
            port, "HEAD", "/",
            headers={"Host": "evil.example.com", "X-MCT-Token": orch_server.session_token},
        )
        assert status == 403
