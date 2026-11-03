The project's definition of done (`verify.dod` in `.claude/toolkit.yaml`) is red on this branch.
Make it green (Sonnet).

- The last run's result: $LOOP_PREV.dod.output
- The full log, one section per command with its output: `$ARTIFACTS_DIR/verify-dod.md`.
- Plan: `$ARTIFACTS_DIR/plan.md`; resolved profile: `$ARTIFACTS_DIR/profile.json`.
- You are on branch `$prepare-branch.output.branch`. Prefix project commands with
  `export PATH="$PWD/.venv/bin:$PATH"`.

Fix only the `FAIL` rows. `UNAVAILABLE` rows are commands this host cannot run (CI runs them on
the PR); leave them.

Follow the `pr-steward` skill's "A red check is real until proven otherwise" and the project's
`project_steward` file: a coverage or floor failure means the new code needs tests, never a lower
floor; a static-analysis false positive is suppressed at the call site with a reason (for bandit,
`# nosec BXXX  # <reason>`), never in the project configuration; never skip, weaken or delete a
test. Re-run each failing command until it passes. Commit with messages naming what was red. Leave
the tree clean. Do not push.
