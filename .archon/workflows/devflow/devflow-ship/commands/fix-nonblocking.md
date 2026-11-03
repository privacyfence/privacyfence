You handle the **non-blocking** findings of the final code review (Sonnet). The review has no
blocking findings left.

- Review report: `$ARTIFACTS_DIR/review.md`.
- Plan: `$ARTIFACTS_DIR/plan.md`; resolved profile: `$ARTIFACTS_DIR/profile.json`. Follow its
  must-read docs, its `project_steward` file and the `pr-steward` skill.
- You are on branch `$prepare-branch.output.branch`. Prefix project commands with
  `export PATH="$PWD/.venv/bin:$PATH"`.

For each non-blocking finding, decide:

- **Fix it** when the finding is correct and the fix stays inside the plan's scope (the files the
  plan touches, the behaviour it designs). One commit per finding, its message naming the finding.
- **Decline it** when it is wrong, when the fix would expand the change beyond the plan (new
  behaviour, unrelated files, a refactor the plan did not ask for), or when it is a follow-up
  worth its own issue. Say which, in one sentence.

Never weaken a test or a check. Run the profile's `verify.fast` commands after your last fix; if a
fix turns them red and you cannot make it green within that finding's scope, revert that commit
(`git revert`) and decline the finding, saying why.

Write `$ARTIFACTS_DIR/non-blocking.md`: a table with one row per finding: number, `path:line`,
`fixed` with the commit SHA or `declined` with the reason. The PR body quotes it. Leave the tree
clean. Do not push.
