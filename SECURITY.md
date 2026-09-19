# Security policy

## Scope

This tool reads Salesforce metadata from authenticated orgs and local Git checkouts and writes snapshots, reports, and manifests to the local machine. Issues of particular interest include:

- Reading files outside the selected repository or snapshot root (path traversal, symlink following, archive extraction).
- Any code path that could push to a Git remote, deploy to an org, or otherwise bypass the allowlist in `mct/safety.py`.
- Leaking org metadata or credentials into logs, reports, or webhook payloads beyond what the user explicitly requested.
- Local web interface issues (the servers under `scripts/` bind to loopback only and are not designed for network exposure).

Vulnerabilities in the Salesforce CLI, Git, or third-party dependencies should be reported upstream.

## Reporting a vulnerability

Please do not open a public issue for security problems.

Use [GitHub private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository. Include a description, affected version or commit, and a reproduction that uses only synthetic metadata. Do not include real org identifiers, credentials, or retrieved metadata.

You should receive an acknowledgement within a few days. Fixes are released as a new version with a changelog entry; credit is given unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes.
