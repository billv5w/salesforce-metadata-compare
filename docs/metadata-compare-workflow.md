# Comparison workflows

Use `--repo-root` to identify the Salesforce DX project whose Git branches and org snapshots you want to compare. Authenticate org aliases with the Salesforce CLI before running retrieval commands. All examples use placeholders rather than real org names.

## Branch to org

```sh
mct --repo-root /path/to/dx-project snapshot-org-from-source \
  --branch main --org my-sandbox --wait-seconds 300
mct --repo-root /path/to/dx-project list --json
```

This snapshots the branch without checking it out, generates a manifest from that source, and retrieves matching metadata. It cannot discover org-only components outside the source manifest.

For drift in both directions, use a union manifest:

```sh
mct --repo-root /path/to/dx-project snapshot-org-bidirectional \
  --branch main --org my-sandbox --wait-seconds 600
```

The union includes source members and org members of source-tracked types. Review skipped-type and retrieval warnings; this is not an unrestricted full-org comparison.

## Org to org

```sh
mct --repo-root /path/to/dx-project snapshot-org-from-org \
  --org source-sandbox --wait-seconds 600
mct --repo-root /path/to/dx-project snapshot-org-from-org \
  --org target-sandbox --wait-seconds 600
mct --repo-root /path/to/dx-project list --json
```

Compare the two resulting org snapshot IDs. The manifests are generated from each org independently, so check scope, permissions, warnings, and available types before interpreting differences.

## Inspect and compare

```sh
mct --repo-root /path/to/dx-project verify-snapshot --id <snapshot-id>
mct --repo-root /path/to/dx-project diff \
  --left <source-snapshot-id> --right <target-snapshot-id> --json --fail-on-diff
mct --repo-root /path/to/dx-project ui \
  --left <source-snapshot-id> --right <target-snapshot-id>
```

Left is the desired source; right is the comparison target. Left-only files suggest additions to the target, while right-only files may suggest deletions only when both snapshots are complete for the intended scope. A nonzero snapshot file count confirms files exist, not retrieval completeness.

`diff --fail-on-diff` returns 1 for active differences. Matrix comparisons use exit 0 for clean results, 1 for drift when `--fail-on-any-diff` is set, and 2 for operational/pair errors:

```sh
mct --repo-root /path/to/dx-project compare-matrix \
  --pair <source-id>:<target-id> --pair <another-source-id>:<another-target-id> \
  --json --fail-on-any-diff
```

## Baselines and exports

Accept known differences from the diff interface or configure ignore rules with `mct baseline --help`. Exports and validation use active drift. A selected file can expand to the complete Salesforce component when its payload or descriptor is required; unrelated records must remain outside that component's scope.

The diff page exports standalone HTML, Markdown, manifests, and ZIP source bundles. Review the warning text, source scope, and destructive manifest before using a bundle. No export performs a deployment.

`validate-deploy` optionally performs a check-only Salesforce validation:

```sh
mct --repo-root /path/to/dx-project validate-deploy \
  --left <source-snapshot-id> --right <target-snapshot-id> --org target-sandbox
```

Exit 0 means valid as authored (or no active drift), 1 means validation failed — including a delta containing target-only deletions, which are outside this application's supported validation scope (a check-only deploy exercises what `package.xml` deploys; it can never cover `destructiveChanges.xml`), so the delta is refused and reported rather than partially validated — and 2 means validation succeeded only after retry exclusions. Treat exit 2 as requiring review of the exclusion report.

## Installed packages

```sh
mct --repo-root /path/to/dx-project snapshot-packages --org source-sandbox
mct --repo-root /path/to/dx-project snapshot-packages --org target-sandbox
mct --repo-root /path/to/dx-project compare-packages \
  --left <source-package-snapshot-id> --right <target-package-snapshot-id> \
  --json --fail-on-diff
```

Package comparison is separate from metadata-file comparison. See [the README](../README.md#comparison-limits) for normalization and retrieval limitations and [CI recipes](ci-drift-monitoring.md) for scheduled monitoring.
