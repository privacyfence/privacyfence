You are a **worker** implementing one phase of a plan (Sonnet). You start with no memory of
earlier steps: everything you need is in the files named here.

- Plan: `$ARTIFACTS_DIR/plan.md`. Read it in full. Its `## Implementation manifest` is
  authoritative: phases, `depends_on`, `touches`, `brief`, `acceptance`.
- Profile (resolved): `$ARTIFACTS_DIR/profile.json`.
- Plan slug `$prepare-branch.output.slug`, feature branch `$prepare-branch.output.branch`.
- Project commands run in the venv: start every shell command that runs project tooling with
  `export PATH="$PWD/.venv/bin:$PATH"`.

The previous step's result, empty on the first step:

- Your predecessor reported: $LOOP_PREV.implement.output
- The fast checks after it: $LOOP_PREV.check.output (full log: `$ARTIFACTS_DIR/verify-fast.md`)

## What to do

1. `git branch --show-current` must be `$prepare-branch.output.branch`. If it is not, stop as
   blocked (step 6).
2. **Red fast checks first.** If the fast checks above are red (`green` false), fix exactly those
   failures (read the log), commit with a message saying what was broken, and go to step 7. Do not
   start a new phase on a red branch.
3. **Find the next phase.** A phase is done when `git log --grep "Plan-Phase: <slug>/<phase id>"`
   finds a commit on this branch. The next phase is the first not-done phase, in manifest order,
   whose `depends_on` are all done. If every phase is done, return `complete` (step 7).
4. **Implement it** the way the devflow `/implement` child brief says (the full text is section 3
   of `$preflight.output.devflow_root/commands/implement.md`):
   - Stay inside the phase's `brief` and `touches`. Work that belongs to another phase goes into
     your summary, not into the code. If the brief is wrong, impossible, or leaves a decision open
     that the plan's Design section does not settle, stop as blocked instead of improvising.
     Never do anything listed under `manual_before` or `manual_after`.
   - Follow the profile's must-read docs, the `project_steward` file, and the `pr-steward` skill
     (`devflow:pr-steward`, or `$preflight.output.devflow_root/skills/pr-steward/SKILL.md`), and the
     stack skills. A user-visible change gets its line under the changelog section the profile
     names (`## [Unreleased]`); never add a version heading.
   - Tests first where the brief names them; never skip, weaken, delete or mark expected-failure a
     test.
5. **Commit it.** Small scope (one phase): commit on the feature branch; the last commit's message
   ends with the trailer `Plan-Phase: <slug>/<phase id>`. Large scope: do the work on
   `<feature branch>--<phase id>` cut from the feature branch, then on the feature branch run
   `git merge --no-ff <feature branch>--<phase id>` with the message
   `Merge <slug> <phase id>: <phase title>` ending in the trailer `Plan-Phase: <slug>/<phase id>`,
   and delete the phase branch. Before committing, run the profile's `verify.fast` commands and
   check every item of the phase's `acceptance`; the full definition of done runs later in the
   pipeline, so do not run it here. Leave the working tree clean. Do not push.
6. **Blocked.** Write `$ARTIFACTS_DIR/blocked.md`: the phase, what you found, the evidence
   (`path:line`, command output), and the question a person must answer. Commit nothing half-done.
7. **Return** `status`: `blocked` (step 6), `complete` (every phase is now done), or `continue`
   (phases remain, or you fixed red checks); `phase`: the phase id you worked on (or `fix`); and
   `summary`: what you changed, how you checked each acceptance item, and any out-of-scope findings.
