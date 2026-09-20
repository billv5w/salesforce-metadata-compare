"""Phase 8 regressions: the exported HTML report must be self-contained.

The report is opened from disk (file://) after the UI server is gone, so it
cannot rely on CDN links, relative asset URLs, the live server, or the
session token. It must inline the vendored diff2html assets, carry the diff
content, and clearly separate active / accepted / ignored content.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_spec = importlib.util.spec_from_file_location(
    "serve_diff_ui", _SCRIPTS_DIR / "serve-diff-ui.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

DiffUIHandler = _mod.DiffUIHandler

_VENDOR_CSS = (
    _SCRIPTS_DIR / "ui" / "kit" / "vendor" / "diff2html.min.css"
).read_text(encoding="utf-8")
_VENDOR_JS = (
    _SCRIPTS_DIR / "ui" / "kit" / "vendor" / "diff2html.min.js"
).read_text(encoding="utf-8")


class _FakeHandler:
    _handle_export_html = DiffUIHandler._handle_export_html

    def __init__(self):
        self.served = []

    def _serve_json(self, data, status=200, filename=None):
        self.served.append(
            (status, json.dumps(data).encode(), {"Content-Type": "application/json"})
        )


def _report_html(handler) -> str:
    DiffUIHandler._handle_export_html(handler)
    status, body, _ = handler.served[-1]
    assert status == 200, f"export failed: {status} {body[:200]}"
    data = json.loads(body)
    assert "html" in data, "export-html must return the assembled document"
    return data["html"]


@pytest.fixture
def handler(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "BASELINE_FILE", None)
    monkeypatch.setattr(_mod, "PAIR_KEY", None)
    _mod.get_comparison.cache_clear()
    h = _FakeHandler()
    h.left_root = tmp_path / "left"
    h.right_root = tmp_path / "right"
    h.left_rel = "left"
    h.right_rel = "right"
    return h


def _trees(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    (left / "classes").mkdir(parents=True)
    (right / "classes").mkdir(parents=True)
    (left / "classes" / "Foo.cls").write_text(
        "public class Foo {\n  void m() { System.debug('left'); }\n}\n"
    )
    (right / "classes" / "Foo.cls").write_text(
        "public class Foo {\n  void m() { System.debug('right'); }\n}\n"
    )
    (left / "classes" / "LeftOnly.cls").write_text("public class LeftOnly {}\n")
    (right / "classes" / "RightOnly.cls").write_text("public class RightOnly {}\n")
    return left, right


class TestOfflineReport:
    def test_no_external_resource_references(self, handler, tmp_path):
        left, right = _trees(tmp_path)
        handler.left_root, handler.right_root = left, right

        html = _report_html(handler)

        # No remote or server-relative fetches: works offline from file://.
        assert not re.search(r"<script[^>]*\ssrc\s*=", html), html[:500]
        assert not re.search(r"<link[^>]*href\s*=", html)
        assert not re.search(r"<img[^>]*src\s*=", html)
        assert "url(http" not in html
        assert "cdn.jsdelivr" not in html
        assert "unpkg.com" not in html

    def test_vendored_assets_inlined(self, handler, tmp_path):
        left, right = _trees(tmp_path)
        handler.left_root, handler.right_root = left, right

        html = _report_html(handler)

        # The vendored renderer is embedded, not referenced.
        assert _VENDOR_CSS[:200] in html
        assert _VENDOR_JS[:200] in html
        assert re.search(r"<style>.*d2h-wrapper", html, re.S)
        assert re.search(r"<script>.*Diff2HtmlUI", html, re.S)

    def test_diff_content_embedded_and_classified(self, handler, tmp_path):
        left, right = _trees(tmp_path)
        handler.left_root, handler.right_root = left, right

        html = _report_html(handler)

        # Changed file appears with its (percent-encoded) diff body.
        assert "Foo.cls" in html
        assert "LeftOnly.cls" in html
        assert "RightOnly.cls" in html
        # diff text is embedded for the offline renderer to pick up
        assert "data-diff=" in html
        # status sections exist so active/accepted/ignored are distinguishable
        assert "Accepted" in html
        assert "Ignored" in html

    def test_accepted_diff_absent_from_active_section(
        self, handler, tmp_path, monkeypatch
    ):
        """R5: a baseline-accepted diff must render under 'Accepted diffs',
        never inside the 'Changed files (active)' section."""
        left, right = _trees(tmp_path)
        handler.left_root, handler.right_root = left, right

        bf = tmp_path / "baseline.json"
        _mod._baseline.accept_diff(
            "classes/Foo.cls",
            left / "classes" / "Foo.cls",
            right / "classes" / "Foo.cls",
            baseline_file=bf,
        )
        monkeypatch.setattr(_mod, "BASELINE_FILE", bf)
        _mod.get_comparison.cache_clear()

        html = _report_html(handler)

        active = html.split("Changed files (active)", 1)[1].split("<h2>", 1)[0]
        assert "Foo.cls" not in active
        accepted = html.split("Accepted diffs", 1)[1]
        assert "Foo.cls" in accepted
        # The headline count agrees: Foo.cls is not counted as changed.
        assert "0 changed" in html.split('<div class="stats">', 1)[1]

    def test_no_token_or_server_state_in_report(self, handler, tmp_path):
        left, right = _trees(tmp_path)
        handler.left_root, handler.right_root = left, right

        html = _report_html(handler)

        # Nothing session-specific: the file must work after the server exits.
        assert "X-MCT-Session" not in html
        assert "/api/" not in html

    def test_embedded_assets_cannot_close_tags_early(self, handler, tmp_path):
        """Inlined JS/CSS must not contain a literal '</' that a browser would
        read as closing the <script>/<style> element."""
        left, right = _trees(tmp_path)
        handler.left_root, handler.right_root = left, right

        html = _report_html(handler)

        for m in re.finditer(r"<(script|style)>(.*?)</\1>", html, re.S):
            body = m.group(2)
            assert "</" not in body or "\\/" in body, (
                f"unescaped '</' inside <{m.group(1)}>"
            )

    def test_script_like_file_content_stays_inert(self, handler, tmp_path):
        """Diff content containing '</script>' and markup must remain data,
        never parsed as part of the report document."""
        left, right = _trees(tmp_path)
        (left / "classes" / "Evil.cls").write_text(
            "public class Evil {\n  String s = '</script><img src=x onerror=alert(1)>';\n}\n"
        )
        handler.left_root, handler.right_root = left, right

        html = _report_html(handler)

        # Content is percent-encoded inside data-diff, so the literal markup
        # sequence can only come from the report's own tags — never raw input.
        assert "%3C%2Fscript%3E" in html or "</script><img" not in html
        assert "<img src=x onerror" not in html

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows cannot create filenames containing <> — the escaping "
        "guarantee is exercised on POSIX filesystems where such names exist",
    )
    def test_paths_html_escaped(self, handler, tmp_path):
        """A path containing markup must not break out of the report DOM."""
        left, right = _trees(tmp_path)
        evil = 'classes/<img src=x onerror=alert(1)>.cls'
        (left / evil).parent.mkdir(parents=True, exist_ok=True)
        (left / evil).write_text("public class E {}\n")
        handler.left_root, handler.right_root = left, right

        html = _report_html(handler)

        assert "<img src=x" not in html
        assert "&lt;img" in html
