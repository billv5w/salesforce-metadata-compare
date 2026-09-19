# Testing and release validation

Use test-first changes for behavior corrections: reproduce the defect with a failing assertion, implement the correction, and rerun the affected tests before the full gates.

## Local gates

```sh
python -m pytest tests/unit tests/acceptance tests/review -q
python -m mypy scripts/ mct/ --ignore-missing-imports --strict-optional
npm run lint
npm run test:e2e
python scripts/verify_dist.py
```

`tests/review` preserves additional synthetic regression cases alongside the permanent unit and acceptance suites. These tests do not require Salesforce credentials. Local HTTP tests require permission to bind loopback ports.

The distribution verifier builds wheel and source archives, checks their contents, installs the wheel into a temporary environment, exercises the installed CLI/local interface, and rebuilds the wheel from the source archive.

## Continuous integration

GitHub Actions runs Python 3.10 and 3.13 on macOS, Linux, and Windows, plus packaging, browser, and JavaScript lint jobs. Inspect the actual matrix results before making a cross-platform release; the existence of workflow configuration alone is not passing evidence.

## Live Salesforce checks

Use explicitly designated test orgs and a small shared metadata scope. Verify retrieve warnings, expected file counts, manifest members, comparisons, and exported component contents. Test large or chunked retrievals separately if those workloads are part of the release claim. Mocked Salesforce responses do not establish live compatibility.

Validate export fixtures with Salesforce's local source converter and inspect the converted manifest and payloads. Exit 0 alone is insufficient: incomplete source can convert to an empty package.

Keep authentication, org metadata, generated manifests, and reports outside public source control. Preserve documented limitations around namespaced metadata, partial retrievals, and Profile/PermissionSet request scope. Check-only deployment requires an explicitly selected target org and remains separate from offline tests.
