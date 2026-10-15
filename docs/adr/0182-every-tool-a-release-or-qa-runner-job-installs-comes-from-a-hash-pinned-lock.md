# ADR 0182: every tool a release or QA-runner job installs comes from a hash-pinned lock or an exact npm pin

## Status

Accepted — 2026-10-15. Implemented.
Extends the rule of [ADR 0085](0085-ci-installs-ai-client-clis-from-a-committed-lockfile.md) from
AI-client CLIs to every build and release tool.

## Context

The runtime dependencies were already installed from a hashed lock, but the jobs that hold signing
and storage credentials then ran `pip install -e ".[dev,test]"`, `pip install boto3`,
`pip install --upgrade pip build` and `npx --yes @anthropic-ai/mcpb`, all resolving the newest
release at run time. The QA-runner jobs, which sit next to live grants, did the same. A malicious
release of any of those packages would have run with the release or QA credentials.
[ADR 0085](0085-ci-installs-ai-client-clis-from-a-committed-lockfile.md) had closed this for the
AI-client CLIs only.

## Decision

1. **The `.mcpb` CLI is an exact-version devDependency of `mcpb/shim`**, installed by `npm ci`
   and run from `node_modules/.bin`; `scripts/build_mcpb.sh` no longer calls `npx`.
2. **Release tooling is listed in `requirements/release.in`**, not in a package extra, and locked
   with hashes in `requirements/release.lock.txt`. `requirements/dev.lock.txt` is compiled from
   the `dev`, `test` and `lint` extras plus `release.in`, constrained by
   `requirements/runtime.lock.txt`, so one hashed lock covers everything a packaging or QA job needs.
3. **Workflows install only with `--require-hashes` from those locks**, then install the project
   itself with `--no-deps --no-build-isolation`. The SBOM, finalize, R2 and publish steps use
   `release.lock.txt`; packaging and QA-runner jobs use `dev.lock.txt`. A job never installs from
   two independently compiled locks.
4. **`dependency-audit.yml` audits both**, and a `floor-audit` job resolves `pyproject.toml` at
   its lowest allowed versions and audits that, so a floor that admits a vulnerable release fails.

## Alternatives considered

- **A `release` extra in `pyproject.toml`.** Rejected: an extra ships in the PyPI metadata as
  `privacyfence[release]`, publishing build tooling as if it were a product feature.
- **Pin each tool inline (`pip install boto3==x`).** Rejected: it pins no transitive dependency
  and no hash.
- **Keep `npx @anthropic-ai/mcpb`.** Rejected: it fetches the newest package inside the signing
  jobs.

## Consequences

- A tool update is a reviewed change to a lock, made by `scripts/update_dependency_locks.sh` and
  checked by `lockfile-freshness`.
- Adding a tool to a job means adding it to `release.in` (or the shim's `package.json`) first.

## Verification

- `requirements/release.in`, `requirements/release.lock.txt`, `requirements/dev.lock.txt`,
  `scripts/update_dependency_locks.sh`, `mcpb/shim/package.json`.
- `.github/workflows/build.yml`, `publish-pypi.yml`, `publish-mcp-registry.yml`,
  `connector-live-check.yml`, `qa-record-fixture.yml` and `dependency-audit.yml` (`floor-audit`,
  `pip-audit`, `lockfile-freshness`).

## Related

- [ADR 0085](0085-ci-installs-ai-client-clis-from-a-committed-lockfile.md)
- [ADR 0046](0046-release-ci-pins-codesigntool-by-version-and-sha256.md)
