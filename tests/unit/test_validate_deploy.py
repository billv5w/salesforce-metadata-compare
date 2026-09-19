"""Unit tests for validate-only deploy: safety gate, failure parsing,
clean-and-retry loop, and no-grant stripping integration."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import mct.config as _cfg
import mct.validate as validate
from mct.safety import ensure_safe_command

SF_NS = "http://soap.sforce.com/2006/04/metadata"


class TestSafetyGate:
    def test_dry_run_deploy_allowed(self):
        ensure_safe_command([
            "sf", "project", "deploy", "start",
            "--source-dir", "force-app", "--target-org", "uat", "--dry-run", "--json",
        ])  # must not raise

    def test_deploy_without_dry_run_blocked(self):
        with pytest.raises(RuntimeError, match="ONLY with --dry-run"):
            ensure_safe_command([
                "sf", "project", "deploy", "start",
                "--source-dir", "force-app", "--target-org", "uat", "--json",
            ])

    def test_other_deploy_forms_still_blocked(self):
        for cmd in (
            ["sf", "project", "deploy", "quick", "--job-id", "x"],
            ["sf", "project", "deploy", "resume", "--job-id", "x"],
            ["sf", "deploy", "--dry-run"],
        ):
            with pytest.raises(RuntimeError):
                ensure_safe_command(cmd)

    def test_unrelated_forbidden_verbs_still_blocked(self):
        with pytest.raises(RuntimeError):
            ensure_safe_command(["git", "push", "origin", "main"])


class TestParseComponentFailures:
    def test_success(self):
        out = json.dumps({"status": 0, "result": {"success": True, "details": {}}})
        success, failures = validate._parse_component_failures(out)
        assert success is True
        assert failures == []

    def test_failures_list(self):
        out = json.dumps({"status": 1, "result": {"success": False, "details": {
            "componentFailures": [
                {"componentType": "ApexClass", "fullName": "Bad",
                 "fileName": "force-app/main/default/classes/Bad.cls",
                 "problem": "Invalid type: Missing__c"},
            ]}}})
        success, failures = validate._parse_component_failures(out)
        assert success is False
        assert failures[0]["fullName"] == "Bad"
        assert "Missing__c" in failures[0]["problem"]

    def test_single_failure_dict_normalized(self):
        out = json.dumps({"result": {"success": False, "details": {
            "componentFailures": {"componentType": "Profile", "fullName": "Admin",
                                  "fileName": "", "problem": "Unknown user permission: X"}}}})
        success, failures = validate._parse_component_failures(out)
        assert success is False
        assert len(failures) == 1

    def test_unparsable_output_is_failure(self):
        success, failures = validate._parse_component_failures("ERROR not json")
        assert success is False
        assert failures and "Unparsable" in failures[0]["problem"]


class TestCleanRetryLoop:
    def _seed(self, tmp_path, monkeypatch):
        """Two trees whose delta is Good.cls + Bad.cls (+ metas)."""
        left = tmp_path / "left"
        right = tmp_path / "right"
        right.mkdir()
        for name, body in [
            ("classes/Good.cls", "public class Good {}"),
            ("classes/Good.cls-meta.xml", f'<ApexClass xmlns="{SF_NS}"/>'),
            ("classes/Bad.cls", "public class Bad {}"),
            ("classes/Bad.cls-meta.xml", f'<ApexClass xmlns="{SF_NS}"/>'),
        ]:
            p = left / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        return left, right

    def test_fail_then_clean_then_pass(self, tmp_path, monkeypatch):
        left, right = self._seed(tmp_path, monkeypatch)
        calls = []

        def fake_run(cmd, check=True, capture=False, cwd=None, timeout=None, **kw):
            ensure_safe_command(cmd)  # the gate must hold in the real flow too
            calls.append(cmd)
            proj = Path(cwd)
            bad = proj / "force-app" / "main" / "default" / "classes" / "Bad.cls"
            if bad.exists():
                payload = {"result": {"success": False, "details": {"componentFailures": [
                    {"componentType": "ApexClass", "fullName": "Bad",
                     "fileName": "force-app/main/default/classes/Bad.cls",
                     "problem": "Invalid type: Missing__c"}]}}}
            else:
                payload = {"result": {"success": True, "details": {}}}
            class R:
                stdout = json.dumps(payload)
                returncode = 0 if payload["result"]["success"] else 1
            return R()

        import mct.safety as safety
        monkeypatch.setattr(safety, "run", fake_run)

        rc = validate.run_validate_deploy(
            str(left), str(right), "uat", "66.0", 60, clean_retries=2
        )
        # Exit 2, not 0: the delta only validated after excluding components,
        # so it is NOT deployable as authored and CI must be able to tell.
        assert rc == 2
        assert len(calls) == 2  # fail → clean → pass
        reports = list((tmp_path / "store").glob("validation-report-*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text())
        assert report["success"] is True
        assert any("Bad.cls" in e for e in report["excluded_files"])

    def test_no_retries_reports_failure(self, tmp_path, monkeypatch):
        left, right = self._seed(tmp_path, monkeypatch)

        def fake_run(cmd, check=True, capture=False, cwd=None, timeout=None, **kw):
            payload = {"result": {"success": False, "details": {"componentFailures": [
                {"componentType": "ApexClass", "fullName": "Bad",
                 "fileName": "force-app/main/default/classes/Bad.cls",
                 "problem": "boom"}]}}}
            class R:
                stdout = json.dumps(payload)
                returncode = 1
            return R()

        import mct.safety as safety
        monkeypatch.setattr(safety, "run", fake_run)

        rc = validate.run_validate_deploy(str(left), str(right), "uat", "66.0", 60)
        assert rc == 1

    def test_strip_no_grant_applied_to_temp_project(self, tmp_path, monkeypatch):
        left = tmp_path / "left"
        right = tmp_path / "right"
        right.mkdir()
        prof = left / "profiles" / "Admin.profile-meta.xml"
        prof.parent.mkdir(parents=True)
        prof.write_text(
            f'<Profile xmlns="{SF_NS}">'
            "<userPermissions><enabled>false</enabled><name>GhostPerm</name></userPermissions>"
            "<userPermissions><enabled>true</enabled><name>RealPerm</name></userPermissions>"
            "</Profile>",
            encoding="utf-8",
        )
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)

        seen = {}

        def fake_run(cmd, check=True, capture=False, cwd=None, timeout=None, **kw):
            proj = Path(cwd)
            seen["profile"] = (proj / "force-app" / "main" / "default" /
                               "profiles" / "Admin.profile-meta.xml").read_text()
            class R:
                stdout = json.dumps({"result": {"success": True, "details": {}}})
                returncode = 0
            return R()

        import mct.safety as safety
        monkeypatch.setattr(safety, "run", fake_run)

        rc = validate.run_validate_deploy(
            str(left), str(right), "uat", "66.0", 60, strip_no_grant=True
        )
        assert rc == 0
        assert "GhostPerm" not in seen["profile"]
        assert "RealPerm" in seen["profile"]
        report = json.loads(next((tmp_path / "store").glob("validation-report-*.json")).read_text())
        assert any("GhostPerm" in s for s in report["stripped_entries"])


class TestDeltaComponentBoundaries:
    def test_validation_excludes_accepted_custom_metadata_sibling(
        self, tmp_path, monkeypatch
    ):
        """CustomMetadata records are independent components: accepting one
        must not let component expansion pull the sibling into the deploy set."""
        import mct.baseline as baseline

        left = tmp_path / "left"
        right = tmp_path / "right"
        selected = "customMetadata/ReviewSettings.Selected.md-meta.xml"
        accepted = "customMetadata/ReviewSettings.Accepted.md-meta.xml"
        for root in (left, right):
            for rel in (selected, accepted):
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    f'<CustomMetadata xmlns="{SF_NS}">'
                    f"<label>{rel} {root.name}</label>"
                    "<protected>false</protected></CustomMetadata>",
                    encoding="utf-8",
                )
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        baseline_file = tmp_path / "baseline.json"
        baseline.accept_diff(
            accepted, left / accepted, right / accepted,
            baseline_file=baseline_file,
        )
        current = baseline.load_baseline(baseline_file)
        monkeypatch.setattr(baseline, "load_baseline", lambda: current)

        entries, _destroy = validate._collect_delta(str(left), str(right), True)
        paths = {display for _, display in entries}
        assert selected in paths
        assert accepted not in paths


class TestCleanRetryExitCode:
    def test_clean_pass_without_exclusions_exits_zero(self, tmp_path, monkeypatch):
        left = tmp_path / "left"
        right = tmp_path / "right"
        (left / "classes").mkdir(parents=True)
        (right / "classes").mkdir(parents=True)
        (left / "classes" / "Good.cls").write_text("public class Good { Integer x; }")
        (right / "classes" / "Good.cls").write_text("public class Good { Integer y; }")
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)

        def fake_run(cmd, check=True, capture=False, cwd=None, timeout=None, **kw):
            ensure_safe_command(cmd)
            payload = {"result": {"success": True, "details": {}}}

            class R:
                stdout = json.dumps(payload)
                returncode = 0

            return R()

        import mct.safety as safety
        monkeypatch.setattr(safety, "run", fake_run)
        rc = validate.run_validate_deploy(
            str(left), str(right), "uat", "66.0", 60, clean_retries=2
        )
        assert rc == 0
