# ADR 0315: rulesets and required checks live in the admin policy

## Status

Accepted — 2026-11-15. Implemented: `.tld/admin-policy.yaml`, `tests/unit/test_required_checks.py`,
`docs/testing-policy.md`. The live rulesets are repository settings the owner applies after the merge.

## Context

The required checks were listed in `scripts/update_branch_protection.py`, which the owner ran with a token
that can administer rulesets, and the live repository also held hand-made rulesets (`main`, `default-branch`,
`release tags`, `releases/**`) that overlapped it and had gone stale (the `releases/**` one required
eight checks). `.tld/admin-policy.yaml` already described rulesets for the tld plugin, so the same
list existed in two places.

## Decision

1. **One list.** `.tld/admin-policy.yaml` is the one place that holds the required checks and the
   merge-method settings. `rulesets.default_branch.required_status_checks` names ten checks: `test`, the three
   `Test (Python 3.x, core suite)` legs, `static-analysis`, `org-mode-smoke`, the two
   `Sync client (Python 3.x)` legs, `website-build` and `n8n-node`.
2. **One writer.** `tld ops repo-admin apply` writes two rulesets by name, `tld-main` and `tld-tags`, and
   leaves every other ruleset alone. `tld ops repo-admin audit` shows what differs.
3. **The script is retired.** `scripts/update_branch_protection.py` and its tests are deleted.
   `tests/unit/test_required_checks.py` keeps the list equal to what the workflows report, so a listed check
   that never reports cannot block every merge unnoticed.
4. **Merge methods.** `repo_settings` allows merge commits only (`allow_merge_commit: true`, squash and
   rebase false), because `tld-main` has no merge-method rule and pull requests merge with a real merge commit.
5. **Cleanup.** The owner applies the policy first, then deletes the rulesets the policy does not own.
   Deleting first would leave the default branch unprotected in between.

## Alternatives considered

- **Keep the script for `main`** — two sources of truth for one list.
- **Keep the script read-only** — it needs an owner token and compares against a ruleset the policy no
  longer writes.
- **A CI job that applies the policy** — its token would need ruleset administration rights.

## Consequences

- `tld-main` is looser or different than the live rulesets, and this is accepted: one approval is required,
  stale approvals are dismissed and the last push must be approved by someone else; the strict up-to-date
  rule is off; the code-owner review requirement goes (`.github/CODEOWNERS` stays and still requests the
  owner's review); the admin role can bypass through a pull request.
- The policy's `admin_bypass` takes effect only once the old `main` ruleset is deleted.
- Merge methods are restricted by repository settings, not by a ruleset.
- A PR that adds, renames or removes a required job changes the policy in the same PR.

## Verification

- `tests/unit/test_required_checks.py` (duplicates, plain jobs, both Python matrices, `website-build`).
- `tests/unit/test_definition_of_done_drift.py` reads the same list.
- `tld ops repo-admin audit` against the live repository.

## Related

[`docs/testing-policy.md`](../testing-policy.md), [ADR 0312](0312-a-release-tag-is-created-only-by-tld-release-yml.md).
