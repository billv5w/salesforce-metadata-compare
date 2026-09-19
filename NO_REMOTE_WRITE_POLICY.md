# Application operation policy

This policy describes commands run by the application. It does not prevent repository maintainers from committing or publishing the application's source code.

## External operations

`mct/safety.py` enforces a command allowlist. The application uses read-only Git operations (`rev-parse`, `fetch`, `archive`) and Salesforce metadata discovery, retrieval, installed-package listing, and read-only SOQL queries. It does not push or commit source changes, delete orgs, or perform persisted deployments.

The explicit validation exception permits `sf project deploy start` only with `--dry-run`. Quick/resumed and persisted deployments are rejected. The application does not change this restriction when it exports deployment manifests or source ZIPs.

An optional `--notify-webhook` argument sends a drift summary to the endpoint the caller supplies. Treat that summary and all generated metadata reports as information belonging to the compared orgs.

## Local writes

- Snapshots, indexes, baselines, and history are stored under the per-user data root, keyed by DX project.
- Workspace presets are stored under the per-user configuration root.
- Generated manifests and temporary retrieval staging use the selected DX project.
- Validation uses a temporary local DX project; exports and reports use the requested output locations.
- Snapshot-deletion commands affect local snapshot storage only.

See [the README](README.md#state-and-migration) for default paths and overrides. Authentication is provided by the existing Salesforce CLI session; this source repository contains no org credentials or metadata snapshots.
