# ADR 0148: `ruff format` is not enforced

## Status

Accepted — 2026-10-10. Implemented in the PR that adds this ADR.

## Context

`ruff format --check .` would rewrite 361 of 588 files
([#865](https://github.com/privacyfence/privacyfence/issues/865)). The tree has never been
formatted by it, and a formatting-only change would conflict with every open branch.

## Decision

`ruff format` is not run in CI and is not required. `ruff check` with the rules in
`[tool.ruff.lint]` ([`pyproject.toml`](../../pyproject.toml)) stays the style gate, and
contributors match the surrounding code.

## Alternatives considered

- **One formatting-only PR plus `ruff format --check .` in CI** — it touches 361 of 588 files,
  conflicts with every open branch and with `feature/plugin-framework-parked`, and buys no
  correctness.

## Consequences

Formatting is not mechanically uniform, and reviewers may still ask for a change to match nearby
code. No churn lands on open branches.

Revisit when no long-lived branch is open.

## Verification

`ruff check .` runs in CI; no workflow runs `ruff format`.

## Related

- [#865](https://github.com/privacyfence/privacyfence/issues/865)
