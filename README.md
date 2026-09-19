# Salesforce Metadata Compare

Compare Salesforce metadata across Git branches, authenticated orgs, and installed-package snapshots. Use the local web interface or the `mct` command to review drift, accept known differences, and export reports and deployment bundles.

The application does not deploy changes or push to Git remotes. Its optional `validate-deploy` command performs Salesforce check-only validation with `--dry-run`. See [the operation policy](NO_REMOTE_WRITE_POLICY.md).

## Requirements

- Python 3.10 or newer.
- Git.
- [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli), authenticated to the orgs you want to compare.
- Node.js for development and browser tests only.

CI targets macOS, Linux, and Windows. Local verification has been performed on macOS; check the GitHub Actions matrix for the published revision before relying on another platform.

## Install

From a checkout of this repository:

```sh
cd salesforce-metadata-compare
pipx install .
```

Use `pipx install --force .` after updating the checkout. The Python package is named `metadata-compare-tool`; the installed command is `mct`.

## Start the web interface

```sh
mct start
```

The application opens a local browser page. Set the repository folder to your **Salesforce DX project**, then choose a branch, org alias, API version, and retrieval mode. This tool's checkout is separate from your DX project.

Create snapshots, choose the left/source and right/target snapshots, and open the comparison. The diff page supports:

- Active, accepted, ignored, and stale-acceptance classifications.
- Metadata-type filters, file selection, and normalized XML/JSON diffs.
- Offline HTML reports and Markdown exports.
- Deployment and destructive-change manifests.
- ZIP bundles containing the selected components and their required source files.

Destructive manifest generation fails if Salesforce component names cannot be resolved accurately. Deploy-side resolution can fall back to a warning-marked heuristic; review warnings before using an export. Running any deployment commands from an exported bundle is a separate action performed by you.

## Command-line examples

```sh
# Snapshot a Git branch and retrieve the corresponding metadata from an org.
mct --repo-root /path/to/dx-project snapshot-org-from-source \
  --branch main --org my-sandbox --wait-seconds 300

# List the resulting snapshot IDs.
mct --repo-root /path/to/dx-project list --json

# Compare two saved snapshots, with a nonzero exit when active drift exists.
mct --repo-root /path/to/dx-project diff \
  --left <source-snapshot-id> --right <target-snapshot-id> --fail-on-diff

# Open the same comparison in the local diff interface.
mct --repo-root /path/to/dx-project ui \
  --left <source-snapshot-id> --right <target-snapshot-id>
```

Run `mct --help` or `mct <command> --help` for options. See the [comparison workflows](docs/metadata-compare-workflow.md) and [CI drift-monitoring recipes](docs/ci-drift-monitoring.md).

## State and migration

Snapshots, baselines, and comparison history live outside the installed package, keyed by DX project:

| Platform | Default data directory | Default configuration directory |
| --- | --- | --- |
| macOS | `~/Library/Application Support/mct` | `~/Library/Application Support/mct` |
| Linux | `$XDG_DATA_HOME/mct` or `~/.local/share/mct` | `$XDG_CONFIG_HOME/mct` or `~/.config/mct` |
| Windows | `%LOCALAPPDATA%\mct` | `%LOCALAPPDATA%\mct` |

Set `MCT_DATA_DIR` and `MCT_CONFIG_DIR` to override these locations. Generated manifests are written under your DX project's `manifest/` directory. Saved workspaces live in the configuration directory.

For data from an older installation, run `mct migrate --legacy-root /path/to/old/.metadata-compare/snapshot-store`. To let the original checkout locate its own legacy store, run `python3 scripts/env-compare.py migrate` from that checkout instead. Migration copies and verifies data, preserves the originals, and reports existing destination conflicts. It does not overwrite an existing workspace registry.

## Comparison limits

- Comparison covers retrieved files. Missing or failed retrievals are not proof that a component was deleted from an org; inspect retrieval warnings and scope.
- Namespaced managed-package round-trips are only partially supported. Source-manifest generation can drop a namespace prefix, causing a follow-on retrieve to omit that component.
- Profile and PermissionSet contents depend on the request's metadata scope. Chunked retrievals record a scope warning for these types.
- XML above the normalizer size cap is compared byte-for-byte and flagged as `normalization_skipped`. Order-sensitive structures are preserved; supported unordered XML collections and JSON object keys are normalized.
- Accepting a difference is a comparison baseline decision, not a change to Salesforce metadata. Accepted entries can become stale when the compared content changes.
- A nonempty snapshot or passing small-sample comparison does not establish full-org completeness or large-org performance.

## Development

```sh
python3 -m venv .venv
# Activate .venv using your platform's shell, then:
python -m pip install -e ".[dev]"
npm ci
npx playwright install chromium

python -m pytest tests/unit tests/acceptance tests/review -q
python -m mypy scripts/ mct/ --ignore-missing-imports --strict-optional
npm run lint
npm run test:e2e
python scripts/verify_dist.py
```

The browser suite starts its local server automatically. Salesforce calls in the automated suites use synthetic fixtures or mocks; live-org verification is separate. See [testing and release validation](docs/testing.md).

```text
mct/                Python application and CLI
scripts/            Local servers, web assets, and distribution verifier
tests/unit/         Unit and integration-boundary tests
tests/acceptance/   Release acceptance tests
tests/review/       Additional regression tests
tests/e2e/          Playwright browser tests
docs/               User and maintainer documentation
.github/workflows/ Cross-platform CI
```

The UI assets are vendored under `scripts/ui/kit/`; no sibling repository is required to run or develop this project.

## License

[MIT](LICENSE). Vendored dependency attribution is recorded in [scripts/ui/kit/vendor/README.md](scripts/ui/kit/vendor/README.md).
