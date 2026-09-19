"""Snapshot naming uniqueness: ts_local() resolves to the second, so rapid
successive retrieves must still mint distinct snapshot ids, output dirs,
manifest names, and package-list filenames."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import mct.snapshot as snap

REPO = Path(__file__).resolve().parents[2]


class TestUniqueStamp:
    def test_same_second_calls_never_repeat(self, monkeypatch):
        """Every call landing in the same second must produce a distinct
        stamp — the old code returned the identical string twice."""
        monkeypatch.setattr(
            snap._cfg, "ts_local", lambda: "20260919-120000-0700"
        )
        monkeypatch.setattr(snap, "_last_stamp", "")
        stamps = {snap._unique_stamp() for _ in range(5)}
        assert len(stamps) == 5

    def test_new_second_resets_sequence(self, monkeypatch):
        seq = itertools.chain(["20260919-120000-0700"], ["20260919-120001-0700"] * 3)
        monkeypatch.setattr(snap._cfg, "ts_local", lambda: next(seq))
        monkeypatch.setattr(snap, "_last_stamp", "")
        first = snap._unique_stamp()
        assert first == "20260919-120000-0700"
        later = {snap._unique_stamp() for _ in range(3)}
        assert len(later) == 3
        assert all(s.startswith("20260919-120001-0700") for s in later)

    def test_concurrent_calls_unique(self, monkeypatch):
        monkeypatch.setattr(
            snap._cfg, "ts_local", lambda: "20260919-120000-0700"
        )
        monkeypatch.setattr(snap, "_last_stamp", "")
        stamps = []
        threads = [
            threading.Thread(target=lambda: stamps.append(snap._unique_stamp()))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(stamps)) == 10


class TestUniqueName:
    def test_first_name_unchanged(self, tmp_path):
        assert snap._unique_name(tmp_path, "retrieved-x-stamp") == "retrieved-x-stamp"

    def test_collision_appends_counter(self, tmp_path):
        (tmp_path / "retrieved-x-stamp").mkdir()
        assert snap._unique_name(tmp_path, "retrieved-x-stamp") == "retrieved-x-stamp-2"
        (tmp_path / "retrieved-x-stamp-2").mkdir()
        assert snap._unique_name(tmp_path, "retrieved-x-stamp") == "retrieved-x-stamp-3"

    def test_extension_preserved(self, tmp_path):
        (tmp_path / "packages-o-stamp.json").write_text("{}")
        assert (
            snap._unique_name(tmp_path, "packages-o-stamp.json")
            == "packages-o-stamp-2.json"
        )

    def test_snap_id_derives_from_unique_dir_name(self, tmp_path, monkeypatch):
        """The snapshot id must track the on-disk name — including any
        collision suffix — so index records stay resolvable."""
        monkeypatch.setattr(
            snap._cfg, "ts_local", lambda: "20260919-120000-0700"
        )
        monkeypatch.setattr(snap, "_last_stamp", "")
        monkeypatch.setattr(snap._cfg, "STORAGE_ROOT", tmp_path)
        stamp = snap._unique_stamp()
        name = snap._unique_name(tmp_path, f"retrieved-delta-org-{stamp}")
        assert name.removeprefix("retrieved-") == f"delta-org-{stamp}"


class TestAtomicClaims:
    """S3: _claim_dir/_claim_file allocate atomically — check-and-create is
    one operation, so simultaneous processes cannot receive the same name."""

    def test_claim_dir_creates_and_skips_existing(self, tmp_path):
        first = snap._claim_dir(tmp_path, "retrieved-x-stamp")
        assert first == tmp_path / "retrieved-x-stamp"
        second = snap._claim_dir(tmp_path, "retrieved-x-stamp")
        assert second == tmp_path / "retrieved-x-stamp-2"
        assert first.is_dir() and second.is_dir()

    def test_claim_file_reserves_and_skips_existing(self, tmp_path):
        first = snap._claim_file(tmp_path, "packages-o-stamp.json")
        assert first.name == "packages-o-stamp.json"
        assert first.exists()  # reservation is a real (empty) file
        second = snap._claim_file(tmp_path, "packages-o-stamp.json")
        assert second.name == "packages-o-stamp-2.json"

    def test_manifest_name_separates_processes(self):
        a = snap._manifest_name("package-o-manifest-org", "STAMP")
        assert a == f"package-o-manifest-org-STAMP-p{os.getpid()}"
        assert a.endswith(f"p{os.getpid()}")

    def test_two_processes_same_second_get_distinct_snapshots(self, tmp_path):
        """Two real interpreters, frozen clock, barrier after the atomic
        claim: mkdir/O_EXCL is the reservation, so both must succeed with
        distinct ids and paths."""
        script = r'''
import json, os, sys, time
from pathlib import Path
from mct import config, snapshot
root, worker = Path(sys.argv[1]), sys.argv[2]
os.environ['MCT_DATA_DIR'] = str(root / 'data')
config.apply_repo_root(str(root / 'project'))
config.ts_local = lambda: '20260919-120000+1200'
snapshot.branch_exists = lambda branch: True

def extract(ref, subdir, destination):
    path = destination / subdir
    path.mkdir(parents=True)
    (path / 'Fixture.cls').write_text('public class Fixture {}')

snapshot._extract_archive_to = extract
original_claim = snapshot._claim_dir

def synchronized_claim(directory, base):
    claimed = original_claim(directory, base)
    (root / ('ready-' + worker)).touch()
    deadline = time.monotonic() + 5
    while len(list(root.glob('ready-*'))) < 2:
        if time.monotonic() > deadline:
            raise TimeoutError('barrier timed out')
        time.sleep(0.01)
    return claimed

snapshot._claim_dir = synchronized_claim
try:
    result = snapshot.materialize_branch_snapshot('main', 'src', False)
    output = {'ok': True, 'id': result.snapshot_id, 'path': result.path}
except Exception as exc:
    output = {'ok': False, 'error': type(exc).__name__, 'message': str(exc)}
(root / ('result-' + worker + '.json')).write_text(json.dumps(output))
'''
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path), str(i)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(2)
        ]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                assert process.returncode == 0, stdout + stderr
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        results = [
            json.loads((tmp_path / f"result-{i}.json").read_text())
            for i in range(2)
        ]
        assert all(result["ok"] for result in results), results
        assert len({result["id"] for result in results}) == 2
        assert len({result["path"] for result in results}) == 2

    def test_simultaneous_retrieves_keep_distinct_staging_and_content(
        self, tmp_path, monkeypatch
    ):
        """Two in-flight retrieves sharing a base name: distinct staging
        dirs, distinct final dirs, each with its own content, staging
        cleaned up."""
        from concurrent.futures import ThreadPoolExecutor

        monkeypatch.setattr(snap._cfg, "PROJECT_ROOT", tmp_path / "project")
        monkeypatch.setattr(snap._cfg, "STORAGE_ROOT", tmp_path / "store")
        monkeypatch.setattr(snap._cfg, "RETRIEVE_CHUNK_SIZE", 0)
        manifest = tmp_path / "package.xml"
        manifest.write_text(
            '<Package xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<types><members>Fixture</members><name>ApexClass</name></types>"
            "<version>66.0</version></Package>",
            encoding="utf-8",
        )
        barrier = threading.Barrier(2)
        staging_paths = []

        def fake_retrieve(manifest, org_alias, staging_dir, timeout, api_version):
            staging_paths.append(staging_dir)
            staging_dir.mkdir(parents=True)
            (staging_dir / "marker.txt").write_text(org_alias, encoding="utf-8")
            barrier.wait(timeout=5)
            return [], '{"result":{"success":true}}'

        monkeypatch.setattr(snap, "_retrieve_request", fake_retrieve)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda alias: snap.retrieve_with_manifest(
                    manifest, alias, "same-base", 10, "66.0"
                ),
                ["fixture-a", "fixture-b"],
            ))

        assert len(set(staging_paths)) == 2
        assert len(set(results)) == 2
        assert sorted(p.name for p in results) == ["same-base", "same-base-2"]
        # Each worker's content followed its own claimed dir.
        assert {
            (p / "marker.txt").read_text() for p in results
        } == {"fixture-a", "fixture-b"}
        assert all(not p.exists() for p in staging_paths)
