# Contributing

Thanks for your interest in improving Salesforce Metadata Compare.

## Before you start

- Check existing issues before opening a new one. For bugs, include the `mct` version, platform, Python version, and a minimal reproduction using synthetic metadata. Never attach real org metadata, org IDs, usernames, or retrieval output.
- For anything beyond a small fix, open an issue first so the approach can be discussed.
- Security issues: see [SECURITY.md](SECURITY.md). Do not open a public issue.

## Development setup

```sh
python3 -m venv .venv
# Activate .venv using your platform's shell, then:
python -m pip install -e ".[dev]"
npm ci
npx playwright install chromium
```

## Making changes

1. Write a failing test that reproduces the bug or specifies the new behavior. Tests live in `tests/unit`, `tests/acceptance`, `tests/review`, and `tests/e2e`; see [docs/testing.md](docs/testing.md).
2. Implement the change. Match the surrounding code style; the JavaScript under `scripts/ui/` is formatted with Prettier and linted with ESLint.
3. Run the full local gates before opening a pull request:

   ```sh
   python -m pytest tests/unit tests/acceptance tests/review -q
   python -m mypy scripts/ mct/ --ignore-missing-imports --strict-optional
   npm run lint
   npm run test:e2e
   python scripts/verify_dist.py
   ```

4. Automated tests must not contact Salesforce or require credentials. Use synthetic fixtures or mocks.
5. Update `CHANGELOG.md` under **Unreleased** and any affected documentation.

## Operation policy

The application must remain read-only toward Git remotes and Salesforce orgs, with the single documented exception of `sf project deploy start --dry-run`. Any change that adds an external command must go through the allowlist in `mct/safety.py` and be covered by tests. See [NO_REMOTE_WRITE_POLICY.md](NO_REMOTE_WRITE_POLICY.md).

## Pull requests

- Keep pull requests focused on one change.
- Describe what changed and why, and how you verified it.
- CI runs on Linux, macOS, and Windows; all jobs must pass.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
