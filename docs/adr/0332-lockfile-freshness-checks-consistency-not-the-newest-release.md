# ADR 0332: lockfile-freshness checks that the locks match their inputs, not that they are the newest release

## Status

Accepted — 2026-11-19. Implemented when the protected workflow PR merges.
Amends [ADR 0182](0182-every-tool-a-release-or-qa-runner-job-installs-comes-from-a-hash-pinned-lock.md).

## Context

The `lockfile-freshness` job of `dependency-audit.yml` resolved all four locks from scratch and diffed
them with `requirements/*.lock.txt`. Every upstream release of a locked package turned the job, and
`main`, red until someone reran `scripts/update_dependency_locks.sh` (issue 1164: seven releases, red on
`main` and two open PRs).

## Decision

1. The job seeds its scratch directory with the committed locks, hashes removed. `uv pip compile` reads
   an existing output file back as preferences, so a pin that still satisfies `pyproject.toml`,
   `requirements/release.in` or the `docs` extra is kept; only a pin that no longer does, or a dependency
   added or removed, is re-resolved. Every hash is fetched again and diffed, so a wrong hash still fails.
2. Newer releases arrive through Dependabot, `/tld:deps` and a manual run of the script, which keeps
   `--upgrade`. `pip-audit` still catches a vulnerable locked version on every PR and every Monday.

## Alternatives considered

- **Keep the from-scratch diff, refresh the locks on every release.** `main` is red after each upstream
  release and each costs a refresh commit, as the history of `requirements/` shows.
- **Drop the job.** A lock could drift from `pyproject.toml` unnoticed, defeating the hash-pinned install.
- **Make the job non-blocking.** Nobody reads a red informational job.

## Consequences

- A lock can lag the newest release until Dependabot, `/tld:deps` or the script bumps it.
- The audit, not this job, guards against a vulnerable pin.
- A contributor who changes `pyproject.toml` still has to rerun the script.
- A bound the committed runtime lock cannot satisfy is a resolver error in the dev compile: still red.
- The job can still fail with no input change when PyPI adds a file to a pinned version; run the script.

## Verification

`.github/workflows/dependency-audit.yml`, `scripts/update_dependency_locks.sh`, `requirements/README.md`.

## Related

Issue 1164; [ADR 0182](0182-every-tool-a-release-or-qa-runner-job-installs-comes-from-a-hash-pinned-lock.md).
