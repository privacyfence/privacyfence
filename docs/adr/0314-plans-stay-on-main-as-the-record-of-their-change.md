# ADR 0314: plans stay on `main` as the record of their change

## Status

Accepted — 2026-11-15. Implemented: `CONTRIBUTING.md`, `docs/adr/README.md` (rules 4 and 5), `CLAUDE.md`.
Amends the plan lifecycle in `docs/adr/README.md`, which until now said a plan is deleted when its work lands.

## Context

Plans used to live on `plan/` branches or as `docs/*-plan.md` files and were deleted when their work landed,
after their decisions were extracted into ADRs. The tld plugin's `/tld:implement` writes
`docs/<issue>/deviations.md` beside the plan and opens one pull request, and no phase of it may touch the plan
file, so it cannot delete the plan. Keeping the plan also keeps the record of what was planned and what the
implementation did differently.

## Decision

1. **The lifecycle.** A plan is a GitHub issue, or `docs/<issue>/plan.md` with `docs/<issue>/deviations.md`
   written on the feature branch. It reaches `main` with the feature PR and stays there as the record of why
   the change was made. Nobody edits it after that. Its decisions become ADRs before it merges.
2. **The gate moves.** The PR that brings a plan to `main` adds or amends the ADRs for every decision the
   plan contained, or says in its description that the plan contained none (ADR README rule 5).
3. **Citing a plan.** An ADR may cite a plan under `docs/<issue>/` as the record of a change, but states its
   decision in full and never relies on the plan to explain it (rule 4).
4. **Plan records are history.** The docs tests skip plan records entirely: they are not held to the link and
   reference checks (`tests/unit/test_docs_links.py` and `tests/unit/test_docs_references_exist.py` already
   skip them).

## Alternatives considered

- **Keep deleting plans** — `/tld:implement` cannot, and the record of the deviations would go with the plan.
- **Move plans to `docs/plans/<n>/`** — the owner declined; the plugin's `plan_dir` stays `docs/`.
- **Hold plan records to the reference checks** — a later rename of a file would force editing a historical
  record, which this decision forbids.

## Consequences

- ADR README rules 4 and 5, `CONTRIBUTING.md`, the pull request template and the definition of done
  (`docs/coding-and-testing-guidelines.md` §2.7) say "brings a plan to `main`" instead of "deletes a plan".
- `scripts/build_site.py` publishes only top-level `docs/*.md`, so plans under `docs/<issue>/` never reach
  the website.
- Old top-level `docs/*-plan.md` files were deleted when their work landed; they are cited by the commit that
  deleted them.
- A plan's text can disagree with the code a year later. That is accepted; it records what was intended then.

## Verification

- `tests/unit/test_docs_links.py` and `tests/unit/test_docs_references_exist.py` skip plan records.
- `.tld/toolkit.yaml` `dod_manual`, the row that asks for the extraction of a plan's decisions.

## Related

[`CONTRIBUTING.md`](../../CONTRIBUTING.md#decisions-plans-and-adrs), [ADR README](README.md).
