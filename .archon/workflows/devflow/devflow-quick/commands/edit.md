You make one small edit (Sonnet). You start with no memory of earlier steps; everything you need is
in files.

- Work order: `$ARTIFACTS_DIR/work.md`. Read it in full: it is the whole scope.
- Resolved profile: `$ARTIFACTS_DIR/profile.json`; follow its `docs.must_read`, its `project_steward`
  file and the `pr-steward` skill (`devflow:pr-steward`, or `skills/pr-steward/SKILL.md` under
  the devflow root in `$ARTIFACTS_DIR/context.json`).
- Branch: `$prepare-branch.output.branch` (`git branch --show-current` must match, else stop as blocked).
- Prefix project commands with `export PATH="$PWD/.venv/bin:$PATH"`.

The previous step's result, empty on the first step:

- Your predecessor reported: $LOOP_PREV.implement.output
- The fast checks after it: $LOOP_PREV.check.output (full log: `$ARTIFACTS_DIR/verify-fast.md`)

1. **Red fast checks first.** If they are red, fix exactly those failures, commit, and return
   `continue`.
2. Otherwise make the edit the work order's `## Edit` describes, inside its scope and `## Out of
   scope`. Tests first where it names them; never skip, weaken or delete a test. A user-visible
   change gets its line under the changelog section the profile names; never a version heading.
3. Run the profile's `verify.fast` commands and check every `## Acceptance` item. Commit with a
   message saying what and why (the repo merges with merge commits, so each message is read on
   `main`). Leave the tree clean. Do not push.
4. If the work order is wrong or leaves a decision open, write `$ARTIFACTS_DIR/blocked.md` (what you
   found with `path:line`, and the question a person must answer), commit nothing half-done and
   return `blocked`.

Return `status`: `complete` (edit committed, acceptance met), `continue` (checks were red and you
fixed them; or work remains) or `blocked`; `phase`: `edit`; `summary`: what you changed and how you
checked each acceptance item.
