# ADR 0316: the `releases/*` integration-branch convention is retired

## Status

Accepted — 2026-11-15. Implemented: `CONTRIBUTING.md`, `CLAUDE.md`, `.claude/skills/steward/SKILL.md`,
`docs/testing-policy.md`, the triggers of `tests.yml`, `website-build.yml` and `dependency-audit.yml`.
Relates to [ADR 0181](0181-the-qa-runner-is-reachable-only-from-mains-two-qa-workflows.md).

## Context

`CONTRIBUTING.md` described long-lived `releases/*` integration branches, protected like `main`, and several
CI triggers and docs carried the pattern. No such branch exists in this repository's history that anyone
relies on. The live `releases/**` ruleset was stale (eight required checks), and the second ruleset was the
main reason the old branch-protection script took a `--branch` argument.

## Decision

1. **One integration branch.** There are no long-lived `releases/*` branches. `main` is the only integration
   branch.
2. **No pattern left.** No CI trigger, ruleset or contributor document keeps the `releases/*` pattern:
   `tests.yml`, `website-build.yml` and `dependency-audit.yml` run on pushes to `main` only, and the
   contributor and steward docs no longer describe the convention.
3. **One harmless exception.** The QA recording workflow keeps refusing `releases/*` branches. It is a
   deny-list entry, ADR 0181 names the refusal and is unchanged, and refusing a branch nobody uses costs
   nothing.

## Alternatives considered

- **Keep the convention with a hand-made `releases/**` ruleset when a branch is cut** — a second ruleset
  outside the admin policy (ADR 0315) that would go stale again.
- **Keep a script just for `releases/**`** — it would carry a branch argument for a branch that does not exist.

## Consequences

- A batch of work that must wait is a feature branch or a feature flag.
- `v*` tags are cut from `main` only; `tag_release.py` already refuses any other branch (ADR 0312).
- A future team that wants a release branch writes a new ADR and a ruleset in the admin policy first.
- Documents that describe the QA workflow's refusal list still mention `releases/*`, on purpose.

## Verification

- `grep -rn 'releases/\*' .github/workflows` finds only the refusal in `qa-record-fixture.yml`.
- `scripts/tag_release.py` refuses to tag from any branch but `main`.

## Related

[ADR 0181](0181-the-qa-runner-is-reachable-only-from-mains-two-qa-workflows.md),
[ADR 0315](0315-rulesets-and-required-checks-live-in-the-admin-policy.md).
