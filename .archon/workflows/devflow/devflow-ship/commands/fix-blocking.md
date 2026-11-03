You fix the **blocking** findings of a code review (Sonnet), and nothing else.

- Review report: `$ARTIFACTS_DIR/review.md`. Its blocking findings are your whole scope; leave
  the non-blocking ones alone (a later step handles them).
- Plan: `$ARTIFACTS_DIR/plan.md`; resolved profile: `$ARTIFACTS_DIR/profile.json`. Follow its
  must-read docs, its `project_steward` file and the `pr-steward` skill.
- You are on branch `$prepare-branch.output.branch`. Prefix project commands with
  `export PATH="$PWD/.venv/bin:$PATH"`.

For each blocking finding: make the fix the finding names, or a better one that meets the same
bar, with a test that fails without it where the finding is about behaviour. Never skip, weaken or
delete a test, and never loosen a threshold or a linter configuration to get green.

When you are sure a finding is wrong (the code already handles it, or the fix would contradict
the plan's Design or an accepted ADR), do not change the code for it. Append to
`$ARTIFACTS_DIR/review-responses.md` the finding number and your evidence (`path:line`, a test
name, a command and its output). The next review reads it.

Run the profile's `verify.fast` commands before committing. Commit each fix (or a coherent group)
with a message naming the finding it fixes; the repo merges with merge commits, so each message is
read on `main`. Leave the tree clean. Do not push.

Reply with one line per finding: fixed (commit SHA) or answered (where).
