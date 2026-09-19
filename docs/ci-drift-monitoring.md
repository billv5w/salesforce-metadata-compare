# Scheduled drift monitoring (CI / cron)

Recipes for running `mct` unattended. The tool stays read-only; the only
requirement beyond a normal install is an authenticated `sf` CLI on the machine
running the job (`sf org login web` interactively once, or `sf org login jwt`
for headless CI).

All examples run from inside the DX project. Pass `--repo-root PATH` instead if
the job runs elsewhere.

## The core loop

```bash
# 1. Snapshot both sides (bidirectional mode sees drift in both directions)
mct snapshot-org-bidirectional --branch main --org prod --wait-seconds 600

# 2. Grab the two newest snapshot ids
LEFT=$(mct list --json | jq -r '[.snapshots[] | select(.type=="branch")] | last | .id')
RIGHT=$(mct list --json | jq -r '[.snapshots[] | select(.type=="org_retrieve")] | last | .id')

# 3. Diff with the baseline: exit 1 only on NEW (non-accepted, non-ignored) drift
mct diff --left "$LEFT" --right "$RIGHT" \
  --fail-on-diff \
  --output-file drift-report.json \
  --notify-webhook "$SLACK_WEBHOOK_URL"
```

Key properties:

- **`--fail-on-diff` counts only active drift.** Baseline-ignored and
  accepted diffs never trip it, so the job goes red exactly when something
  _new_ drifts — the difference between an alert and alert fatigue.
- **`--output-file` writes the full JSON payload** (file lists, per-type
  counts, ignored/accepted breakdown) — archive it as a build artifact.
- **`--notify-webhook`** POSTs a JSON summary (includes a human-readable
  `text` field, so a Slack "incoming webhook" renders it directly) only when
  drift is found. Best-effort: notify failures never change the exit code.

### Package drift in the same loop

Installed-package version drift (a package upgraded in production but not in a
sandbox, or missing from one org) is environment drift too. Snapshot each org's
packages and compare with the same CI affordances as `diff`:

```bash
mct snapshot-packages --org prod
PKG=$(mct list --json | jq -r '[.snapshots[] | select(.type=="installed_packages")] | last | .id')

# Compare against a known-good snapshot id (or JSON file kept as a baseline)
mct compare-packages --left "$PKG_BASELINE" --right "$PKG" \
  --json --fail-on-diff --notify-webhook "$SLACK_WEBHOOK_URL"
```

Exit 1 means a package was added, removed, or changed version since the
baseline snapshot.

### Validate-deploy exit codes in CI

`validate-deploy` distinguishes these outcomes: **0** valid as authored
(or no active drift), **1** failed — including a delta containing
target-only deletions, which `--dry-run` validation cannot cover and is
therefore refused before any deploy check runs (see
`unvalidated_deletions` in the report JSON) — and **2** valid only after
`--clean-retries` excluded components (NOT deployable as authored — read
`excluded_files` in the report JSON). Treat 2 as a soft failure, not
success.

## GitHub Actions (nightly)

```yaml
name: metadata-drift
on:
  schedule:
    - cron: "0 5 * * *" # 05:00 UTC nightly
  workflow_dispatch: {}

jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4 # your DX repo
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: npm install -g @salesforce/cli
      - name: Install mct
        run: pip install "git+https://github.com/billv5w/salesforce-metadata-compare.git@v0.1.0"
      - name: Authenticate (JWT, headless)
        run: |
          echo "${{ secrets.SF_JWT_KEY }}" > server.key
          sf org login jwt --client-id "${{ secrets.SF_CLIENT_ID }}" \
            --jwt-key-file server.key --username "${{ secrets.SF_USERNAME }}" \
            --alias prod --instance-url https://login.salesforce.com
      - name: Snapshot + diff
        env:
          MCT_DATA_DIR: ${{ github.workspace }}/.mct-data
        run: |
          mct snapshot-org-bidirectional --branch main --org prod --wait-seconds 600
          LEFT=$(mct list --json | jq -r '[.snapshots[] | select(.type=="branch")] | last | .id')
          RIGHT=$(mct list --json | jq -r '[.snapshots[] | select(.type=="org_retrieve")] | last | .id')
          mct diff --left "$LEFT" --right "$RIGHT" --fail-on-diff \
            --output-file drift-report.json \
            --notify-webhook "${{ secrets.SLACK_WEBHOOK_URL }}"
      - uses: actions/upload-artifact@v4
        if: always()
        with: { name: drift-report, path: drift-report.json }
```

Note: the baseline lives under the per-user data root at
`<data-root>/snapshot-store/<key>/baseline.json` (see the README section
[State and migration](../README.md#state-and-migration)). CI runners start
empty, so either point `MCT_DATA_DIR` at a directory restored by
`actions/cache`, or commit a copy of `baseline.json` and copy it into place
before the diff step — otherwise CI has no baseline and reports all drift as
active.

## Local cron (macOS/Linux)

```cron
# nightly at 02:15 — assumes `sf org login web` was done interactively once
15 2 * * * mct --repo-root "$HOME/work/dx-project" \
  snapshot-org-bidirectional --branch main --org prod --wait-seconds 600 \
  >> "$HOME/.mct-cron.log" 2>&1
```

Then review with `history --trend`:

```
$ mct --repo-root ~/work/dx-project history --trend
WHEN                     DIFF  ONLY-L  ONLY-R  TOTAL  PAIR
──────────────────────────────────────────────────────────
20260701-021500+1200        3       1       0      4  branch-main… vs retrieved-org…
20260708-021500+1200        3       1       2      6 ↑  branch-main… vs retrieved-org…
20260711-021500+1200        1       0       0      1 ↓  branch-main… vs retrieved-org…
```

Retention is 50 comparison records per project (`comparisons.json`).

## Slack webhook payload

`--notify-webhook` POSTs:

```json
{
  "source": "metadata-compare-tool",
  "left": "...",
  "right": "...",
  "different_count": 3,
  "only_left_count": 1,
  "only_right_count": 0,
  "ignored_count": 2,
  "accepted_count": 5,
  "accepted_stale_count": 0,
  "text": "Metadata drift: … vs … — 3 different, 1 only-left, 0 only-right"
}
```

Slack incoming webhooks render the `text` field as the message. For other
receivers (Teams, PagerDuty, a custom endpoint), adapt on the receiving side —
the payload is stable.
