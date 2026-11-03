Open the one pull request for this change (Sonnet). Branch `$prepare-branch.output.branch`, base
`$preflight.output.main_branch`, title from the plan: "$prepare-branch.output.title".

1. `git status --porcelain` must be empty. Push with `git push -u origin $prepare-branch.output.branch`.
   Never force-push. If the push is refused, stop and say why.
2. If `gh pr list --head $prepare-branch.output.branch --state open` already finds a PR (a resumed
   run), update its body with `gh pr edit` instead of opening another.
3. Otherwise `gh pr create --base $preflight.output.main_branch --head $prepare-branch.output.branch`,
   ready for review (not a draft), with the body below. Prefix the title with its type when the
   repo's convention asks for it.

The body follows `.github/pull_request_template.md` when the repo has one: fill in every section,
tick what was run and passed, and strike through a row that does not apply with a one-line reason.
Build it from the run's files, never from memory:

- **What this changes**: the plan's Goal, and the issue it closes (`Closes #<n>`) if the work
  named one. Read `$ARTIFACTS_DIR/plan.md`.
- **Phases**: each phase with its commit (`git log --grep "Plan-Phase:"`), and the combined
  acceptance checklist.
- **Definition of done**: from `$ARTIFACTS_DIR/verify-dod.md`. A row marked `UNAVAILABLE` was not
  run on this host: say so and that CI runs it; never tick it. The `dod_conditional` and
  `dod_manual` tables from `$ARTIFACTS_DIR/review.md`; a conditional row that is owed (a dispatch,
  a credential) stays unticked with what is owed.
- **Review**: the final verdict and how many rounds it took, from `$ARTIFACTS_DIR/review*.md`.
- **Non-blocking findings**: the table from `$ARTIFACTS_DIR/non-blocking.md`.
- **Decisions**: the ADRs added, or, if the plan's Design made no ADR-worthy decision, a line
  saying so.
- **Manual verification**: each `manual_after` item from the plan's manifest as an unchecked box,
  with its steps from the plan's "Manual steps" section. Omit the section if there are none.

Read the PR back with `gh pr view <number> --json number,url,headRefOid,baseRefName` and check that
its head is your pushed `HEAD` and its base is `$preflight.output.main_branch`. Return `number`
and `url`.
