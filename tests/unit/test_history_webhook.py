"""Unit tests for history trend view and webhook notification."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import mct.comparison as comparison
import mct.index as index_mod


class TestTrend:
    def test_trend_prints_chronological_with_markers(self, capsys):
        records = [
            {"created_at": "20260708-020000+1200", "different_count": 3, "only_left_count": 1,
             "only_right_count": 2, "left_path": "l", "right_path": "r"},
            {"created_at": "20260701-020000+1200", "different_count": 3, "only_left_count": 1,
             "only_right_count": 0, "left_path": "l", "right_path": "r"},
            {"created_at": "20260711-020000+1200", "different_count": 1, "only_left_count": 0,
             "only_right_count": 0, "left_path": "l", "right_path": "r"},
        ]
        rc = index_mod.print_comparison_trend(records)
        out = capsys.readouterr().out
        assert rc == 0
        lines = [l for l in out.splitlines() if l.startswith("2026")]
        # chronological: oldest first
        assert lines[0].startswith("20260701")
        assert lines[2].startswith("20260711")
        assert "↑" in lines[1]  # 4 → 6
        assert "↓" in lines[2]  # 6 → 1

    def test_trend_empty(self, capsys):
        assert index_mod.print_comparison_trend([]) == 0
        assert "No comparisons" in capsys.readouterr().out

    def test_retention_is_50(self):
        assert index_mod.COMPARISON_HISTORY_MAX_RECORDS == 50


class TestWebhook:
    def test_non_http_url_skipped(self, capsys):
        comparison._post_webhook("ftp://nope", {"a": 1})
        assert "skipped" in capsys.readouterr().out

    def test_posts_json_payload(self, monkeypatch, capsys):
        import urllib.request

        captured = {}

        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return _Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        comparison._post_webhook("https://hooks.example/x", {"text": "drift!"})
        out = capsys.readouterr().out
        assert "Webhook notified" in out
        assert captured["url"] == "https://hooks.example/x"
        assert b'"text": "drift!"' in captured["body"]
        assert captured["content_type"] == "application/json"

    def test_failure_never_raises(self, monkeypatch, capsys):
        import urllib.error
        import urllib.request

        def boom(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        comparison._post_webhook("https://hooks.example/x", {"a": 1})  # must not raise
        assert "notify failed" in capsys.readouterr().out
