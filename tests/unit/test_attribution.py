"""Unit tests for drift attribution (SetupAuditTrail, read-only)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import mct.attribution as attribution
import mct.safety as safety
from mct.safety import ensure_safe_command

ROWS = [
    {"CreatedDate": "2026-08-01T10:00:00.000+0000",
     "CreatedBy": {"Name": "Ada Admin"},
     "Action": "changedApexClass", "Section": "Apex Class",
     "Display": "Changed FooService Apex Class code"},
    {"CreatedDate": "2026-08-02T11:00:00.000+0000",
     "CreatedBy": {"Name": "Bob Builder"},
     "Action": "changedFlow", "Section": "Flow",
     "Display": "Changed Flow Bar_Flow"},
]


class TestSafetyAllowlist:
    def test_sf_data_query_allowed(self):
        ensure_safe_command(["sf", "data", "query", "--query", "SELECT Id FROM X",
                             "--target-org", "uat", "--json"])  # must not raise


class TestRunAttribute:
    def _fake_run(self, cmd, **kw):
        ensure_safe_command(cmd)

        class R:
            stdout = json.dumps({"result": {"records": ROWS}})
            returncode = 0

        return R()

    def test_json_output_filtered_by_name(self, monkeypatch):
        monkeypatch.setattr(safety, "run", self._fake_run)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = attribution.run_attribute("uat", 14, ["FooService"], as_json=True)
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert len(payload["entries"]) == 1
        assert payload["entries"][0]["who"] == "Ada Admin"

    def test_unfiltered_returns_all(self, monkeypatch):
        monkeypatch.setattr(safety, "run", self._fake_run)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            attribution.run_attribute("uat", 14, [], as_json=True)
        assert len(json.loads(buf.getvalue())["entries"]) == 2

    def test_human_output_lists_who_and_what(self, monkeypatch):
        monkeypatch.setattr(safety, "run", self._fake_run)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            attribution.run_attribute("uat", 14, [], as_json=False)
        out = buf.getvalue()
        assert "Ada Admin" in out and "Bar_Flow" in out
