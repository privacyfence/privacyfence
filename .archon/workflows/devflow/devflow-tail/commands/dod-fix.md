Run facts are in `$ARTIFACTS_DIR/context.json`: `main_branch` (written `<main>` below), `devflow_root`, `branch`
(`<branch>`), `slug`, `title`. The resolved profile is `$ARTIFACTS_DIR/profile.json`. The work order is
`$ARTIFACTS_DIR/plan.md` when it exists (with phases and a manifest), otherwise `$ARTIFACTS_DIR/work.md`
(an outcome and acceptance, no phases); `<work order>` below means whichever exists. When the work order
says the work is confidential security work, nothing public (PR title and body, comments, commit
messages) describes the vulnerability: name the class of fix only, never an exploit step.

The project's definition of done (`verify.dod` in `.claude/toolkit.yaml`) is red on this branch.
Make it green (Sonnet).

- The last run's result: $LOOP_PREV.dod.output
- The full log, one section per command with its output: `$ARTIFACTS_DIR/verify-dod.md`.
- Work order: <work order>.
- You are on branch `<branch>`. Prefix project commands with
  `export PATH="$PWD/.venv/bin:$PATH"`.

Fix only the `FAIL` rows. `UNAVAILABLE` rows are commands this host cannot run (CI runs them on
the PR); leave them.

Follow the `pr-steward` skill's "A red check is real until proven otherwise" and the project's
`project_steward` file: a coverage or floor failure means the new code needs tests, never a lower
floor; a static-analysis false positive is suppressed at the call site with a reason (for bandit,
`# nosec BXXX  # <reason>`), never in the project configuration; never skip, weaken or delete a
test. Re-run each failing command until it passes. Commit with messages naming what was red. Leave
the tree clean. Do not push.
