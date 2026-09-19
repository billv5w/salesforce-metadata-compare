"""Tests for retrieve_with_manifest CLI argument construction."""
import json
from pathlib import Path

import mct.config as _cfg
import mct.snapshot as snapshot


class TestRetrieveWaitUnits:
    """`sf project retrieve start -w` takes MINUTES; the CLI flag is --wait-seconds."""

    def _captured_retrieve_cmd(self, tmp_path, monkeypatch, timeout: int) -> list[str]:
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        manifest = tmp_path / "manifest" / "package.xml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("<Package/>", encoding="utf-8")

        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            out_dir = Path(cmd[cmd.index("--output-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "objects.xml").write_text("<a/>", encoding="utf-8")

            class R:
                stdout = '{"result": {"messages": [], "files": []}}'
                returncode = 0

            return R()

        monkeypatch.setattr(_cfg, "run", fake_run)
        snapshot.retrieve_with_manifest(manifest, "some-org", "retrieved-org-test", timeout, "66.0")
        return captured["cmd"]

    def test_wait_seconds_converted_to_minutes(self, tmp_path, monkeypatch):
        cmd = self._captured_retrieve_cmd(tmp_path, monkeypatch, timeout=120)
        assert cmd[cmd.index("-w") + 1] == "2"

    def test_wait_rounds_up_to_at_least_one_minute(self, tmp_path, monkeypatch):
        cmd = self._captured_retrieve_cmd(tmp_path, monkeypatch, timeout=30)
        assert cmd[cmd.index("-w") + 1] == "1"

    def test_wait_partial_minutes_round_up(self, tmp_path, monkeypatch):
        cmd = self._captured_retrieve_cmd(tmp_path, monkeypatch, timeout=150)
        assert cmd[cmd.index("-w") + 1] == "3"


class TestSnapshotApiVersionProvenance:
    """Snapshots must record the Metadata API version they were taken with —
    cross-version comparisons are a phantom-diff source and need a warning."""

    def test_snapshot_record_roundtrips_api_version(self):
        from mct.index import Snapshot, snapshot_from_row

        snap = Snapshot(
            snapshot_id="org-x", snapshot_type="org_retrieve",
            created_at="20260806-000000+0000", path="retrieved-org-x",
            org_alias="uat", api_version="66.0",
        )
        rec = snap.to_record()
        assert rec["api_version"] == "66.0"
        assert snapshot_from_row(rec).api_version == "66.0"

    def test_snapshot_org_from_org_records_api_version(self, tmp_path, monkeypatch):
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            snapshot, "generate_manifest_from_org", lambda org, name, api: tmp_path / "m.xml"
        )
        monkeypatch.setattr(
            snapshot, "retrieve_with_manifest", lambda m, o, n, t, a, **kw: tmp_path / "out"
        )
        monkeypatch.setattr(snapshot, "dx_default_compare_root", lambda p: p)
        monkeypatch.setattr(snapshot, "register_snapshot", lambda s: captured.setdefault("snap", s))
        monkeypatch.setattr(_cfg, "storage_rel", lambda p: "retrieved-org-x")
        monkeypatch.setattr(_cfg, "project_rel", lambda p: "manifest/m.xml")
        snapshot.snapshot_org_from_org("uat", 60, "66.0")
        assert captured["snap"].api_version == "66.0"


class TestSkippedOrgTypesRecorded:
    """The bidirectional union manifest silently skips org-only types; the skip
    list must land on the snapshot row (not just stdout) so CI/JSON consumers
    see the blind spot."""

    def test_bidirectional_snapshot_records_skipped_types(self, tmp_path, monkeypatch):
        from mct.index import Snapshot

        ns = "http://soap.sforce.com/2006/04/metadata"

        def manifest(path, types):
            body = "".join(
                f"<types>{''.join(f'<members>{m}</members>' for m in ms)}<name>{t}</name></types>"
                for t, ms in types.items()
            )
            path.write_text(
                f'<?xml version="1.0" encoding="UTF-8"?><Package xmlns="{ns}">'
                f"{body}<version>66.0</version></Package>",
                encoding="utf-8",
            )
            return path

        src = manifest(tmp_path / "src.xml", {"ApexClass": ["Foo"]})
        org = manifest(tmp_path / "org.xml", {"ApexClass": ["Bar"], "Report": ["R1"]})

        monkeypatch.setattr(_cfg, "MANIFEST_DIR", tmp_path)
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(snapshot, "generate_manifest_from_source", lambda *a: src)
        monkeypatch.setattr(snapshot, "generate_manifest_from_org", lambda *a: org)
        monkeypatch.setattr(snapshot, "retrieve_with_manifest", lambda m, o, n, t, a, **kw: tmp_path / "out")
        monkeypatch.setattr(snapshot, "dx_default_compare_root", lambda p: p)
        captured: dict[str, object] = {}
        monkeypatch.setattr(snapshot, "register_snapshot", lambda s: captured.setdefault("snap", s))
        monkeypatch.setattr(_cfg, "storage_rel", lambda p: "retrieved-org-x")
        monkeypatch.setattr(_cfg, "project_rel", lambda p: "manifest/u.xml")

        branch_snap = Snapshot(
            snapshot_id="branch-main-x", snapshot_type="branch",
            created_at="20260806-000000+0000", path="retrieved-branch-main-x",
            branch="main",
        )
        snapshot.snapshot_org_bidirectional("uat", branch_snap, 60, "66.0")
        snap = captured["snap"]
        assert snap.skipped_org_types == ["Report"]
        assert snap.to_record()["skipped_org_types"] == ["Report"]


class TestRetrieveWarnings:
    """Per-component retrieve problems must land on the snapshot row, not
    scroll by in stdout — a partial retrieve otherwise reads as deletion drift."""

    def _run_with_sf_payload(self, tmp_path, monkeypatch, payload: dict):
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        manifest = tmp_path / "manifest" / "package.xml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("<Package/>", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            out_dir = Path(cmd[cmd.index("--output-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "objects.xml").write_text("<a/>", encoding="utf-8")

            class R:
                stdout = json.dumps(payload)
                returncode = 0

            return R()

        monkeypatch.setattr(_cfg, "run", fake_run)
        collected: dict = {}
        snapshot.retrieve_with_manifest(
            manifest, "uat", "retrieved-org-warn", 120, "66.0", collected=collected
        )
        return collected

    def test_messages_collected_as_warnings(self, tmp_path, monkeypatch):
        payload = {"result": {"messages": [
            {"fileName": "unpackaged/objects/Broken__c.object",
             "problem": "Entity of type 'CustomObject' named 'Broken__c' cannot be found"},
        ], "files": []}}
        collected = self._run_with_sf_payload(tmp_path, monkeypatch, payload)
        assert collected["warnings"] == [
            "unpackaged/objects/Broken__c.object: Entity of type 'CustomObject' "
            "named 'Broken__c' cannot be found"
        ]

    def test_clean_retrieve_collects_no_warnings(self, tmp_path, monkeypatch):
        collected = self._run_with_sf_payload(
            tmp_path, monkeypatch, {"result": {"messages": [], "files": []}}
        )
        assert collected["warnings"] == []
        assert collected["file_count"] == 1

    def test_snapshot_row_roundtrips_retrieve_warnings(self):
        from mct.index import Snapshot, snapshot_from_row

        snap = Snapshot(
            snapshot_id="org-w", snapshot_type="org_retrieve",
            created_at="20260806-000000+0000", path="retrieved-org-w",
            retrieve_warnings=["a: b"],
        )
        assert snap.to_record()["retrieve_warnings"] == ["a: b"]
        assert snapshot_from_row(snap.to_record()).retrieve_warnings == ["a: b"]


class TestRetrieveDeltaDeletions:
    def test_org_side_removals_written_to_deletions_file(self, tmp_path, monkeypatch):
        left = tmp_path / "left"
        right = tmp_path / "right"
        (left / "classes").mkdir(parents=True)
        right.mkdir()
        (left / "classes" / "Gone.cls").write_text("public class Gone {}")

        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        (tmp_path / "store").mkdir()

        snap = snapshot.retrieve_delta("uat", str(left), str(right), 60, "66.0")
        assert snap is not None
        out_root = _cfg.STORAGE_ROOT / Path(snap.path).parts[0]
        deletions = (out_root / "DELETED_IN_ORG.txt").read_text().splitlines()
        assert deletions == ["classes/Gone.cls"]

    def test_baseline_ignored_removals_not_listed(self, tmp_path, monkeypatch):
        left = tmp_path / "left"
        right = tmp_path / "right"
        (left / "classes").mkdir(parents=True)
        right.mkdir()
        (left / "classes" / "Gone.cls").write_text("public class Gone {}")

        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        (tmp_path / "store").mkdir()
        (tmp_path / "store" / "baseline.json").write_text(json.dumps({
            "version": 1,
            "ignore": {"types": [], "paths": ["classes/Gone.cls"], "xml_elements": []},
            "options": {"strip_retrieve_defaults": False},
            "accepted": {},
        }))

        snap = snapshot.retrieve_delta("uat", str(left), str(right), 60, "66.0")
        assert snap is None  # the only drift is baseline-ignored


class TestRetrieveFailureSurfacesCliOutput:
    """With --json captured, sf's error report lives in stdout — it must be
    printed when the retrieve fails or produces nothing, or failures become
    undiagnosable."""

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        manifest = tmp_path / "manifest" / "package.xml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("<Package/>", encoding="utf-8")
        return manifest

    def test_nonzero_exit_raises_with_sf_message(self, tmp_path, monkeypatch, capsys):
        manifest = self._setup(tmp_path, monkeypatch)

        def fake_run(cmd, check=True, capture=False, **kw):
            class R:
                stdout = json.dumps({"name": "SizeLimitExceeded",
                                     "message": "Maximum size of request reached.",
                                     "status": 1})
                returncode = 1
            return R()

        monkeypatch.setattr(_cfg, "run", fake_run)
        import pytest
        with pytest.raises(RuntimeError, match="Maximum size of request reached"):
            snapshot.retrieve_with_manifest(manifest, "uat", "retrieved-org-f", 120, "66.0")
        assert "Maximum size of request reached" in capsys.readouterr().out

    def test_missing_output_dir_prints_captured_payload(self, tmp_path, monkeypatch, capsys):
        manifest = self._setup(tmp_path, monkeypatch)

        def fake_run(cmd, check=True, capture=False, **kw):
            class R:
                stdout = json.dumps({"result": {"files": [], "messages": []},
                                     "status": 0})
                returncode = 0
            return R()

        monkeypatch.setattr(_cfg, "run", fake_run)
        import pytest
        with pytest.raises(RuntimeError, match="Retrieve output missing"):
            snapshot.retrieve_with_manifest(manifest, "uat", "retrieved-org-e", 120, "66.0")
        assert '"files": []' in capsys.readouterr().out


class TestRetrieveFailedStatusInPayload:
    """Live finding (pss demo orgs): sf exits 0 even when the retrieve reports
    status=Failed (e.g. LIMIT_EXCEEDED on a >10k-member manifest). The error
    must name the API failure, not just 'output missing'."""

    def test_failed_result_raises_with_error_status(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        manifest = tmp_path / "manifest" / "package.xml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("<Package/>", encoding="utf-8")

        def fake_run(cmd, check=True, capture=False, **kw):
            class R:
                stdout = json.dumps({"status": 0, "result": {
                    "errorStatusCode": "LIMIT_EXCEEDED",
                    "errorMessage": "LIMIT_EXCEEDED: limit exceeded",
                    "status": "Failed", "success": False,
                    "fileProperties": [], "messages": [], "files": []}})
                returncode = 0
            return R()

        monkeypatch.setattr(_cfg, "run", fake_run)
        import pytest
        with pytest.raises(RuntimeError, match="LIMIT_EXCEEDED"):
            snapshot.retrieve_with_manifest(manifest, "uat", "retrieved-org-lim", 120, "66.0")


class TestSplitManifestMembers:
    """Chunking a manifest for the Metadata API's ~10k-files-per-retrieve cap."""

    def test_under_cap_returns_single_chunk(self):
        members = {"ApexClass": {"A", "B"}, "CustomObject": {"Account"}}
        chunks = snapshot.split_manifest_members(members, 10)
        assert len(chunks) == 1
        assert chunks[0] == {"ApexClass": {"A", "B"}, "CustomObject": {"Account"}}

    def test_union_of_chunks_equals_input_no_duplicates(self):
        members = {f"Type{i}": {f"m{j}" for j in range(7)} for i in range(5)}  # 35 members
        chunks = snapshot.split_manifest_members(members, 10)
        assert all(sum(len(m) for m in c.values()) <= 10 for c in chunks)
        merged: dict[str, set[str]] = {}
        total = 0
        for c in chunks:
            for t, ms in c.items():
                merged.setdefault(t, set()).update(ms)
                total += len(ms)
        assert merged == members
        assert total == 35  # no member appears twice

    def test_small_type_never_splits_across_chunks(self):
        members = {"A": {"a1", "a2", "a3"}, "B": {"b1", "b2", "b3"}, "C": {"c1", "c2"}}
        chunks = snapshot.split_manifest_members(members, 4)
        for t in members:
            holders = [c for c in chunks if t in c]
            assert len(holders) == 1, f"type {t} split across chunks"

    def test_oversized_type_splits(self):
        members = {"ApexClass": {f"C{i:03d}" for i in range(25)}}
        chunks = snapshot.split_manifest_members(members, 10)
        assert len(chunks) == 3
        assert sum(len(c["ApexClass"]) for c in chunks) == 25

    def test_deterministic_output(self):
        members = {"B": {"b2", "b1"}, "A": {"a9", "a1", "a5"}}
        assert (snapshot.split_manifest_members(members, 3)
                == snapshot.split_manifest_members(dict(reversed(members.items())), 3))


class TestChunkedRetrieve:
    """Manifests over the chunk cap retrieve in multiple requests into the
    same staging dir — the fix for LIMIT_EXCEEDED on large from-org manifests."""

    NS = "http://soap.sforce.com/2006/04/metadata"

    def _manifest(self, path, types):
        body = "".join(
            f"<types>{''.join(f'<members>{m}</members>' for m in sorted(ms))}<name>{t}</name></types>"
            for t, ms in types.items()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><Package xmlns="{self.NS}">'
            f"{body}<version>66.0</version></Package>", encoding="utf-8")
        return path

    def _run(self, tmp_path, monkeypatch, types, chunk_size, warn_on_call=None):
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(_cfg, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(_cfg, "RETRIEVE_CHUNK_SIZE", chunk_size, raising=False)
        manifest = self._manifest(tmp_path / "manifest" / "package.xml", types)
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            out_dir = Path(cmd[cmd.index("--output-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"file{len(calls)}.xml").write_text("<a/>", encoding="utf-8")
            msgs = []
            if warn_on_call == len(calls):
                msgs = [{"fileName": "bad/f.xml", "problem": "skipped"}]

            class R:
                stdout = json.dumps({"result": {"messages": msgs, "files": []}})
                returncode = 0

            return R()

        monkeypatch.setattr(_cfg, "run", fake_run)
        collected: dict = {}
        snapshot.retrieve_with_manifest(
            manifest, "uat", "retrieved-org-chunked", 120, "66.0", collected=collected
        )
        return calls, collected

    def test_over_cap_retrieves_in_chunks(self, tmp_path, monkeypatch):
        types = {"ApexClass": {f"C{i}" for i in range(5)}}
        calls, collected = self._run(tmp_path, monkeypatch, types, chunk_size=2)
        assert len(calls) == 3
        chunk_manifests = [c[c.index("--manifest") + 1] for c in calls]
        assert len(set(chunk_manifests)) == 3
        assert all("-chunk" in m for m in chunk_manifests)
        assert collected["chunks"] == 3
        assert collected["file_count"] == 3  # one file per fake chunk retrieve

    def test_under_cap_single_retrieve_with_original_manifest(self, tmp_path, monkeypatch):
        types = {"ApexClass": {"A", "B"}}
        calls, collected = self._run(tmp_path, monkeypatch, types, chunk_size=100)
        assert len(calls) == 1
        assert calls[0][calls[0].index("--manifest") + 1].endswith("package.xml")
        assert collected.get("chunks", 1) == 1

    def test_chunk_warnings_aggregate(self, tmp_path, monkeypatch):
        types = {"ApexClass": {f"C{i}" for i in range(4)}}
        calls, collected = self._run(tmp_path, monkeypatch, types, chunk_size=2,
                                     warn_on_call=2)
        assert any("skipped" in w for w in collected["warnings"])

    def test_profile_scope_warning_when_chunked(self, tmp_path, monkeypatch):
        types = {"Profile": {"Admin", "Standard"}, "ApexClass": {f"C{i}" for i in range(4)}}
        calls, collected = self._run(tmp_path, monkeypatch, types, chunk_size=3)
        assert any("Profile" in w and "scope" in w for w in collected["warnings"])

    def test_no_profile_warning_when_single_request(self, tmp_path, monkeypatch):
        types = {"Profile": {"Admin"}, "ApexClass": {"A"}}
        calls, collected = self._run(tmp_path, monkeypatch, types, chunk_size=100)
        assert collected["warnings"] == []


class TestChunkSizeCliWiring:
    def test_global_flag_sets_config(self, tmp_path, monkeypatch, capsys):
        import sys as _sys
        import env_compare
        monkeypatch.setattr(_sys, "argv",
                            ["env-compare.py", "--repo-root", str(tmp_path),
                             "--retrieve-chunk-size", "1234", "list", "--json"])
        rc = env_compare.main()
        assert rc == 0
        assert _cfg.RETRIEVE_CHUNK_SIZE == 1234

    def test_zero_disables_chunking(self, tmp_path, monkeypatch):
        import sys as _sys
        import env_compare
        monkeypatch.setattr(_sys, "argv",
                            ["env-compare.py", "--repo-root", str(tmp_path),
                             "--retrieve-chunk-size", "0", "list", "--json"])
        env_compare.main()
        assert _cfg.RETRIEVE_CHUNK_SIZE == 0
