"""
Unit tests for pure utility and safety functions in scripts/env-compare.py.

Import strategy: the file uses argparse only inside parse_args() which is called
only from main(), and main() is guarded by `if __name__ == "__main__"`. So a plain
import is safe — the module-level sys.version check is the only side-effect, and
we are on Python 3.10+ in this environment.
"""
import json
import subprocess as _subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import pytest

import env_compare  # loaded by conftest.py

ensure_safe_command = env_compare.ensure_safe_command
sanitize_token = env_compare.sanitize_token
normalize_api_version = env_compare.normalize_api_version
_storage_key = env_compare._storage_key
_package_key = env_compare._package_key
_package_version = env_compare._package_version
_parse_installed_packages_payload = env_compare._parse_installed_packages_payload


# ---------------------------------------------------------------------------
# ensure_safe_command
# ---------------------------------------------------------------------------

class TestEnsureSafeCommand:
    """Safety blocklist: raises RuntimeError for blocked verbs, ValueError for empty input."""

    # --- empty / degenerate input ---

    def test_empty_list_raises_value_error(self):
        with pytest.raises(ValueError, match="Empty command"):
            ensure_safe_command([])

    # --- forbidden verbs that must be blocked ---

    def test_blocks_git_push(self):
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["git", "push", "origin", "main"])

    def test_blocks_git_commit(self):
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["git", "commit", "-m", "msg"])

    def test_blocks_git_merge(self):
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["git", "merge", "feature"])

    def test_blocks_git_rebase(self):
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["git", "rebase", "main"])

    def test_blocks_sf_deploy_without_dry_run(self):
        """deploy start is validate-only: permitted solely with --dry-run."""
        with pytest.raises(RuntimeError, match="ONLY with --dry-run"):
            ensure_safe_command(["sf", "project", "deploy", "start"])

    def test_allows_sf_deploy_dry_run(self):
        ensure_safe_command(["sf", "project", "deploy", "start", "--dry-run"])

    def test_blocks_sf_delete(self):
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["sf", "data", "delete", "record"])

    def test_blocks_sf_update(self):
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["sf", "data", "update", "record"])

    # --- allowlisted commands that must pass ---

    def test_allows_git_fetch(self):
        # Should not raise
        ensure_safe_command(["git", "fetch", "origin"])

    def test_allows_git_rev_parse(self):
        ensure_safe_command(["git", "rev-parse", "--verify", "main"])

    def test_allows_git_archive(self):
        ensure_safe_command(["git", "archive", "--format=tar", "main", "force-app/main/default"])

    def test_allows_sf_version(self):
        ensure_safe_command(["sf", "--version"])

    def test_allows_sf_project_generate_manifest(self):
        ensure_safe_command(
            ["sf", "project", "generate", "manifest", "--source-dir", "force-app", "--name", "pkg"]
        )

    def test_allows_sf_project_retrieve_start(self):
        ensure_safe_command(
            ["sf", "project", "retrieve", "start", "--manifest", "manifest/pkg.xml", "--target-org", "myorg"]
        )

    def test_allows_sf_package_installed_list(self):
        ensure_safe_command(["sf", "package", "installed", "list", "--target-org", "myorg", "--json"])

    def test_allows_serve_diff_ui(self):
        ensure_safe_command(
            [
                sys.executable,
                str(env_compare.SCRIPT_DIR / "serve-diff-ui.py"),
                "--left",
                "/some/left",
                "--right",
                "/some/right",
            ]
        )

    # --- command not in allowlist (but also not forbidden) ---

    def test_blocks_arbitrary_unknown_command(self):
        """A command with no forbidden token but not in the allowlist is still rejected."""
        with pytest.raises(RuntimeError, match="Command not in allowlist"):
            ensure_safe_command(["ls", "-la"])

    def test_blocks_git_status(self):
        """git status is not in the allowlist."""
        with pytest.raises(RuntimeError, match="Command not in allowlist"):
            ensure_safe_command(["git", "status"])

    def test_blocks_git_log(self):
        with pytest.raises(RuntimeError, match="Command not in allowlist"):
            ensure_safe_command(["git", "log", "--oneline"])

    def test_forbidden_exact_push_token_blocked(self):
        """'push' as a discrete argv token is blocked by the whole-token check."""
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["git", "push"])

    def test_forbidden_deploy_in_arg(self):
        """'deploy' embedded in a flag value is not a whole-token match, so it passes the
        forbidden-verb check but is still rejected by the allowlist check."""
        with pytest.raises(RuntimeError, match="Command not in allowlist"):
            ensure_safe_command(["sf", "some-other-cmd", "--flag=deploy"])

    def test_branch_name_with_update_does_not_block(self):
        """Branch names containing forbidden words as substrings must not be blocked."""
        cmd = ["git", "archive", "--format=tar", "feature/update-readme", "force-app/main/default"]
        ensure_safe_command(cmd)  # Should not raise

    def test_branch_name_with_deploy_does_not_block(self):
        cmd = ["git", "archive", "--format=tar", "deploy-config", "force-app/main/default"]
        ensure_safe_command(cmd)  # Should not raise

    def test_branch_name_with_delete_does_not_block(self):
        cmd = ["git", "archive", "--format=tar", "origin/feature-delete-old-data", "force-app"]
        ensure_safe_command(cmd)  # Should not raise

    def test_case_insensitive_push_blocked(self):
        """Forbidden verbs are matched case-insensitively."""
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["git", "PUSH", "origin", "main"])

    def test_case_insensitive_commit_blocked(self):
        with pytest.raises(RuntimeError, match="Blocked unsafe command"):
            ensure_safe_command(["git", "Commit", "-m", "msg"])


# ---------------------------------------------------------------------------
# sanitize_token
# ---------------------------------------------------------------------------

class TestSanitizeToken:
    """sanitize_token produces filesystem-safe tokens."""

    def test_preserves_alphanumeric(self):
        assert sanitize_token("abc123") == "abc123"

    def test_preserves_dots_and_hyphens(self):
        assert sanitize_token("feature-branch.1") == "feature-branch.1"

    def test_preserves_underscore(self):
        # underscore is in the allowed set [A-Za-z0-9._-], so it is preserved
        result = sanitize_token("my_branch")
        assert result == "my_branch"

    def test_replaces_forward_slash(self):
        result = sanitize_token("feature/my-branch")
        assert "/" not in result
        assert result == "feature-my-branch"

    def test_replaces_backslash(self):
        result = sanitize_token("feature\\my-branch")
        assert "\\" not in result
        assert result == "feature-my-branch"

    def test_collapses_consecutive_hyphens(self):
        result = sanitize_token("a//b")
        assert "--" not in result
        assert result == "a-b"

    def test_strips_leading_trailing_hyphens(self):
        result = sanitize_token("/leading")
        assert not result.startswith("-")

    def test_strips_whitespace(self):
        result = sanitize_token("  abc  ")
        assert result == "abc"

    def test_empty_string_returns_hash_prefix(self):
        # Falls back to a short hash to avoid "unknown" collisions
        result = sanitize_token("")
        assert result.startswith("x") and len(result) == 9

    def test_whitespace_only_returns_hash_prefix(self):
        result = sanitize_token("   ")
        assert result.startswith("x") and len(result) == 9

    def test_special_chars_only_returns_unique_hash(self):
        # Different all-special-char inputs must not collide
        result1 = sanitize_token("@@@")
        result2 = sanitize_token("!!!")
        assert result1.startswith("x") and result2.startswith("x")
        assert result1 != result2

    def test_spaces_replaced(self):
        result = sanitize_token("hello world")
        assert " " not in result

    def test_org_alias_with_at_sign(self):
        result = sanitize_token("myorg@sandbox")
        assert "@" not in result

    def test_org_alias_with_colon(self):
        result = sanitize_token("prod:main")
        assert ":" not in result

    def test_version_string_preserved(self):
        # dots and digits are safe
        assert sanitize_token("66.0") == "66.0"

    def test_branch_with_slash_and_numbers(self):
        result = sanitize_token("release/2024-01-01")
        assert "/" not in result
        assert result == "release-2024-01-01"


# ---------------------------------------------------------------------------
# normalize_api_version
# ---------------------------------------------------------------------------

class TestNormalizeApiVersion:
    """normalize_api_version ensures versions have a decimal component."""

    def test_integer_string_gets_dot_zero(self):
        assert normalize_api_version("66") == "66.0"

    def test_already_float_unchanged(self):
        assert normalize_api_version("66.0") == "66.0"

    def test_float_with_nonzero_decimal_unchanged(self):
        assert normalize_api_version("59.1") == "59.1"

    def test_strips_whitespace(self):
        assert normalize_api_version("  66  ") == "66.0"

    def test_strips_whitespace_float(self):
        assert normalize_api_version("  66.0  ") == "66.0"

    def test_empty_string_returns_default(self):
        result = normalize_api_version("")
        assert result == env_compare.DEFAULT_API_VERSION

    def test_whitespace_only_returns_default(self):
        result = normalize_api_version("   ")
        assert result == env_compare.DEFAULT_API_VERSION

    def test_none_equivalent_empty_returns_default(self):
        # The implementation does: s = (v or "").strip() or DEFAULT_API_VERSION
        # so passing None would fail at the .strip() call; but empty string is handled
        result = normalize_api_version("")
        assert "." in result

    def test_older_version_integer(self):
        assert normalize_api_version("55") == "55.0"

    def test_large_version_integer(self):
        assert normalize_api_version("100") == "100.0"

    def test_version_with_multiple_dots_unchanged(self):
        # Already has a dot; function just checks "." in s
        result = normalize_api_version("1.2.3")
        assert result == "1.2.3"


# ---------------------------------------------------------------------------
# _storage_key
# ---------------------------------------------------------------------------

class TestStorageKey:
    """_storage_key returns a 16-char hex digest deterministically."""

    def test_returns_16_chars(self):
        key = _storage_key(Path("/some/path"))
        assert len(key) == 16

    def test_hex_chars_only(self):
        key = _storage_key(Path("/some/path"))
        assert all(c in "0123456789abcdef" for c in key)

    def test_deterministic(self):
        p = Path("/some/fixed/path")
        assert _storage_key(p) == _storage_key(p)

    def test_different_paths_produce_different_keys(self):
        assert _storage_key(Path("/path/a")) != _storage_key(Path("/path/b"))


# ---------------------------------------------------------------------------
# _package_key
# ---------------------------------------------------------------------------

class TestPackageKey:
    """_package_key builds a namespace::name composite."""

    def test_namespace_and_name(self):
        row = {"SubscriberPackageNamespace": "ns", "SubscriberPackageName": "MyPkg"}
        assert _package_key(row) == "ns::MyPkg"

    def test_name_only_no_namespace(self):
        row = {"SubscriberPackageName": "MyPkg"}
        assert _package_key(row) == "MyPkg"

    def test_empty_namespace_uses_name_only(self):
        row = {"SubscriberPackageNamespace": "", "SubscriberPackageName": "MyPkg"}
        assert _package_key(row) == "MyPkg"

    def test_fallback_to_subscriber_package_id(self):
        row = {"SubscriberPackageId": "04tXXXXXXXXXXXXX"}
        assert _package_key(row) == "04tXXXXXXXXXXXXX"

    def test_unknown_when_no_name_fields(self):
        assert _package_key({}) == "(unknown)"

    def test_strips_whitespace_from_namespace_and_name(self):
        row = {"SubscriberPackageNamespace": " ns ", "SubscriberPackageName": " MyPkg "}
        assert _package_key(row) == "ns::MyPkg"


# ---------------------------------------------------------------------------
# _package_version
# ---------------------------------------------------------------------------

class TestPackageVersion:
    """_package_version extracts the version id string."""

    def test_subscriber_package_version_id(self):
        row = {"SubscriberPackageVersionId": "04tABC"}
        assert _package_version(row) == "04tABC"

    def test_camel_case_fallback(self):
        row = {"subscriberPackageVersionId": "04tDEF"}
        assert _package_version(row) == "04tDEF"

    def test_empty_row_returns_empty_string(self):
        assert _package_version({}) == ""

    def test_strips_whitespace(self):
        row = {"SubscriberPackageVersionId": "  04tABC  "}
        assert _package_version(row) == "04tABC"


# ---------------------------------------------------------------------------
# _parse_installed_packages_payload
# ---------------------------------------------------------------------------

class TestParseInstalledPackagesPayload:
    """_parse_installed_packages_payload normalises various CLI response shapes."""

    def test_none_returns_empty(self):
        assert _parse_installed_packages_payload(None) == []

    def test_plain_list_of_dicts(self):
        data = [{"name": "A"}, {"name": "B"}]
        result = _parse_installed_packages_payload(data)
        assert len(result) == 2

    def test_list_filters_non_dicts(self):
        data = [{"name": "A"}, "string", 42]
        result = _parse_installed_packages_payload(data)
        assert len(result) == 1

    def test_dict_with_result_list(self):
        data = {"result": [{"name": "A"}, {"name": "B"}]}
        result = _parse_installed_packages_payload(data)
        assert len(result) == 2

    def test_dict_with_result_dict_records(self):
        data = {"result": {"records": [{"name": "A"}]}}
        result = _parse_installed_packages_payload(data)
        assert len(result) == 1

    def test_dict_with_top_level_records(self):
        data = {"records": [{"name": "A"}, {"name": "B"}]}
        result = _parse_installed_packages_payload(data)
        assert len(result) == 2

    def test_non_dict_non_list_returns_empty(self):
        assert _parse_installed_packages_payload("bad") == []
        assert _parse_installed_packages_payload(42) == []

    def test_empty_list(self):
        assert _parse_installed_packages_payload([]) == []

    def test_empty_dict(self):
        assert _parse_installed_packages_payload({}) == []


# ---------------------------------------------------------------------------
# read_package_dirs_from_branch  (uses a real temp git repo)
# ---------------------------------------------------------------------------

read_package_dirs_from_branch = env_compare.read_package_dirs_from_branch
_merge_package_dirs = env_compare._merge_package_dirs


def _git(repo, *args):
    _subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        capture_output=True,
    )


def _make_git_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo, files: dict):
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            target.write_text(json.dumps(content), encoding="utf-8")
        else:
            target.write_text(str(content), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "test commit")


class TestReadPackageDirsFromBranch:
    def _with_repo(self, repo):
        """Context manager: patch PROJECT_ROOT to use repo as git cwd."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            original = env_compare.PROJECT_ROOT
            env_compare.PROJECT_ROOT = repo
            try:
                yield
            finally:
                env_compare.PROJECT_ROOT = original

        return _ctx()

    def test_reads_multiple_package_dirs(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {
            "sfdx-project.json": {
                "packageDirectories": [
                    {"path": "force-app", "default": True},
                    {"path": "omnistudio"},
                    {"path": "datacloud"},
                    {"path": "jsp"},
                ]
            }
        })
        with self._with_repo(repo):
            result = read_package_dirs_from_branch("HEAD")
        assert result == ["force-app", "omnistudio", "datacloud", "jsp"]

    def test_single_package_dir(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {
            "sfdx-project.json": {"packageDirectories": [{"path": "force-app", "default": True}]}
        })
        with self._with_repo(repo):
            result = read_package_dirs_from_branch("HEAD")
        assert result == ["force-app"]

    def test_falls_back_when_no_sfdx_project_json(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {"README.md": "hello"})
        with self._with_repo(repo):
            result = read_package_dirs_from_branch("HEAD")
        assert result == ["force-app"]

    def test_falls_back_when_package_directories_empty(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {"sfdx-project.json": {"packageDirectories": []}})
        with self._with_repo(repo):
            result = read_package_dirs_from_branch("HEAD")
        assert result == ["force-app"]

    def test_strips_leading_trailing_slashes_from_paths(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {
            "sfdx-project.json": {
                "packageDirectories": [{"path": "/force-app/"}, {"path": " omnistudio "}]
            }
        })
        with self._with_repo(repo):
            result = read_package_dirs_from_branch("HEAD")
        assert result == ["force-app", "omnistudio"]


# ---------------------------------------------------------------------------
# _merge_package_dirs  (uses a real temp git repo)
# ---------------------------------------------------------------------------

class TestMergePackageDirs:
    def _with_repo(self, repo, storage_root):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            orig_pr = env_compare.PROJECT_ROOT
            orig_sr = env_compare.STORAGE_ROOT
            env_compare.PROJECT_ROOT = repo
            env_compare.STORAGE_ROOT = storage_root
            try:
                yield
            finally:
                env_compare.PROJECT_ROOT = orig_pr
                env_compare.STORAGE_ROOT = orig_sr

        return _ctx()

    def test_merges_two_packages(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {
            "force-app/main/default/classes/MyClass.cls": "public class MyClass {}",
            "omnistudio/main/default/omniScripts/MyScript.osp-meta.xml": "<OmniScript/>",
        })
        storage = tmp_path / "storage"
        storage.mkdir()
        out_dir = storage / "snapshot"
        out_dir.mkdir()

        with self._with_repo(repo, storage):
            merged, collisions = _merge_package_dirs("HEAD", ["force-app", "omnistudio"], out_dir)

        assert merged == out_dir / "main" / "default"
        assert collisions == []
        assert (merged / "classes" / "MyClass.cls").is_file()
        assert (merged / "omniScripts" / "MyScript.osp-meta.xml").is_file()

    def test_skips_missing_package_gracefully(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {
            "force-app/main/default/classes/A.cls": "class A {}",
        })
        storage = tmp_path / "storage"
        storage.mkdir()
        out_dir = storage / "snapshot"
        out_dir.mkdir()

        with self._with_repo(repo, storage):
            # "nonexistent" package silently skipped
            merged, _collisions = _merge_package_dirs("HEAD", ["force-app", "nonexistent"], out_dir)

        assert (merged / "classes" / "A.cls").is_file()

    def test_later_package_wins_on_collision(self, tmp_path):
        repo = _make_git_repo(tmp_path)
        _commit(repo, {
            "pkg-a/main/default/classes/Shared.cls": "// from pkg-a",
            "pkg-b/main/default/classes/Shared.cls": "// from pkg-b",
        })
        storage = tmp_path / "storage"
        storage.mkdir()
        out_dir = storage / "snapshot"
        out_dir.mkdir()

        with self._with_repo(repo, storage):
            merged, collisions = _merge_package_dirs("HEAD", ["pkg-a", "pkg-b"], out_dir)

        content = (merged / "classes" / "Shared.cls").read_text()
        assert content == "// from pkg-b"
        assert collisions == ["classes/Shared.cls"]


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    """CLI parser no longer exposes the legacy 'compare' subcommand."""

    def test_compare_subcommand_is_not_supported(self, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            ["env-compare.py", "compare", "--left", "any-left", "--right", "any-right"],
        )
        with pytest.raises(SystemExit) as exc:
            env_compare.parse_args()
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# TestRunUiRecordsHistory
# ---------------------------------------------------------------------------



class TestRunUiRecordsHistory(unittest.TestCase):
    """Verify that run_ui() records comparison history before launching subprocess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.left_dir = self.tmp_path / "left"
        self.right_dir = self.tmp_path / "right"
        self.left_dir.mkdir()
        self.right_dir.mkdir()
        (self.left_dir / "file.txt").write_text("hello")
        (self.right_dir / "file.txt").write_text("hello")
        self._ctx = env_compare.repo_context(str(self.tmp_path))
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        # Override specific paths for this test's layout
        env_compare.STATE_DIR = self.tmp_path / ".mct"
        env_compare.INDEX_PATH = env_compare.STATE_DIR / "snapshots.json"
        env_compare.COMPARISON_INDEX_PATH = env_compare.STATE_DIR / "comparisons.json"
        env_compare.STATE_DIR.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_run_ui_records_history(self):
        """run_ui() calls record_comparison before launching the subprocess."""
        recorded = []

        def fake_record(left_rel, right_rel, result):
            recorded.append((left_rel, right_rel))
            return None  # best-effort, None is fine

        with mock.patch.object(env_compare, 'record_comparison', side_effect=fake_record):
            with mock.patch('subprocess.run', return_value=type('P', (), {'returncode': 0})()):
                try:
                    env_compare.run_ui(
                        left=str(self.left_dir),
                        right=str(self.right_dir),
                        port=0,
                        no_open=True,
                    )
                except Exception:
                    pass  # subprocess.run mock may not be complete, that's OK

        # The important thing: record_comparison was called
        self.assertGreater(len(recorded), 0, "record_comparison should have been called by run_ui")
