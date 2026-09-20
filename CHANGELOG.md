# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Managed-package classification: when the compared org has an installed-packages snapshot, files present on only one side under an installed namespace (for example `LLC_BI__*`) are classified as `managed:<ns>` ignored entries instead of active drift, in the CLI diff, compare matrix, and diff UI. The same namespaces are excluded from deploy/destroy exports and reverse-sync retrieval.
- Profile and PermissionSet normalization now drops grants that reference installed managed-package components (`ns__` prefixes in tab, field, object, class, and similar references), and grants whose verdicts are all at their platform default (`visibility` = `DefaultOn`, all-false permissions) — both are semantically identical to the grant being absent.
- `<profile>` / `<profiles>` element text is compared case-insensitively; Salesforce treats these API-name references case-insensitively and retrieves round-trip them in varying case.
- With `strip_retrieve_defaults` enabled, `*Settings` documents treat an absent leaf element as equal to an explicit `false` — the Metadata API emits newer elements at their default while source committed under an older API omits them.
- The pipx install instructions now warn that reinstalling a pre-externalization build deletes its in-venv snapshot store, and direct users to run `mct migrate` first.

### Fixed

- Chunked retrieves no longer hollow out Profiles and PermissionSets or overwrite retrieved objects with empty stubs. Each request now retrieves into its own directory and is merged stub-aware, and profile-like types are sent in one dedicated request that carries the manifest's scope-defining members. Observed on a 19k-file org: 75 profiles and 93 objects were reported as drift when the org matched the branch.

### Changed

- `--api-version` now defaults to the DX project's `sfdx-project.json` `sourceApiVersion` (falling back to 66.0) instead of always 66.0, avoiding "type is unknown" retrieve warnings on projects authored against newer API versions. The web UI omits the flag when the workspace leaves it blank so the same resolution applies; exported manifests are stamped with the resolved version.
- `--wait-seconds` default raised from 120 to 600 for retrieve commands — a 4000-member manifest chunk can exceed two minutes on large orgs. The orchestrator's per-task timeout now scales with `wait_seconds` so chunked retrieves are not killed mid-run.

## [0.1.0] - 2026-09-19

Initial public release.

### Added

- `mct` command line and local web interface for comparing Salesforce metadata across Git branches, authenticated orgs, and installed-package snapshots.
- Normalized XML and JSON diffs with active, accepted, ignored, and stale-acceptance classifications.
- Offline HTML reports, Markdown exports, deployment and destructive-change manifests, and source ZIP bundles.
- Check-only deployment validation via `validate-deploy` (`sf project deploy start --dry-run` only).
- `--include-type` / `--exclude-type` metadata scope filters with scoped summary metrics.
- Per-user data and configuration directories with a `migrate` command for legacy stores.
- `--repo-root` defaults to the current directory; commands that need a DX project stop with a clear message when `sfdx-project.json` is missing.
- Cross-platform GitHub Actions CI (Linux, macOS, Windows; Python 3.10 and 3.13).

### Security

- Metadata reads are confined to the trusted comparison root; symlinks, ancestor links, traversal, and out-of-root paths are rejected.
- Archive extraction rejects links, special files, absolute paths, and traversal.
- Baseline fingerprints (`v2`) distinguish binary, missing, empty, and unreadable content; stale acceptances resurface instead of being silently honored.
- Retrieval warnings, skipped types, and API-version mismatches are surfaced in the UI and all report formats.
- `validate-deploy` fails closed when target-only deletions are present rather than silently skipping them.

[Unreleased]: https://github.com/billv5w/salesforce-metadata-compare/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/billv5w/salesforce-metadata-compare/releases/tag/v0.1.0
