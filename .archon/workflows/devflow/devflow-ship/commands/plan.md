You are the **planner** for this change (Opus). You research and plan; you implement nothing.

The work: `$INPUTS.work`. If that is empty, the work is the run's message:

$ARGUMENTS

## Your authority

The devflow toolkit's `/make-plan` command is the method. Read it in full first:
`$preflight.output.devflow_root/commands/make-plan.md`. Follow its section 0 (understand the ask,
read the must-read docs, the steward files and every ADR, decide small or large scope), section 2
(the plan's sections, the implementation manifest, "Sizing for Sonnet"), section 3 (manual steps
only at the very beginning and the very end) and section 4 (a fresh `Plan` subagent on Opus reviews
the plan, then `validate_manifest.py`), with the overrides below. Where make-plan.md and this file
disagree, this file wins.

The project profile is already validated and resolved at `$ARTIFACTS_DIR/profile.json`; read
project facts from it (must-read docs, `git.branch_pattern` and `branch_types`, `docs.adr_*`,
`docs.changelog`, `ci.dispatchable`, `verify.*`, `project_steward`). Load the skills it names
(`devflow:project-profile`, `devflow:pr-steward`, the `stack-*` skills for `stacks`) if your skill
list has them; otherwise read their `SKILL.md` under `$preflight.output.devflow_root/skills/`.
Issues: read them and their comments with `gh issue view <n> --comments`.

## Overrides: this runs unattended inside one Archon run

1. **No branch, no push, no PR, no commit.** The plan lives at `$ARTIFACTS_DIR/plan.md` only.
   Ignore make-plan's section 1 and section 5. A later step cuts the branch you name.
2. **One plan file for both scopes.** Small scope: `plan.md` holds the single-session prompt's
   content (Goal, Context, Design, Steps, Acceptance, Stop conditions) followed by an
   `## Implementation manifest` with exactly one phase, id `p1-<kebab>`. Large scope: the full plan
   with its manifest. Either way, run
   `python3 $preflight.output.devflow_root/scripts/validate_manifest.py $ARTIFACTS_DIR/plan.md --profile .claude/toolkit.yaml`
   and fix every error before you finish.
3. **The plan is never committed**, so the retirement phase does not delete a plan document, and
   `final_checks` must not ask for that. The retirement phase still writes the ADRs (when the
   profile has `docs.adr_dir`), updates the reference docs and adds the changelog line.
4. **No HTML artifact.** Put the manual steps, step by step with direct links, in the plan's
   "Manual steps" section instead.
5. **Questions go to the run, not to a tool.** You cannot call `AskUserQuestion`. When a decision
   is genuinely the user's (make-plan section 0, step 3), or `manual_before` is non-empty, set
   `needs_user` to true and put in `ask` a self-contained markdown message the user can answer in
   one reply: each question numbered with the options and your recommendation, then each
   `manual_before` item (title, why, done_when) to confirm. Write the plan as far as it can go
   without the answers. Otherwise `needs_user` is false and `ask` is an empty string.
6. **Branch.** `branch` follows `git.branch_pattern` with a type from `git.branch_types` (for this
   repo, `feature/`, `fix/`, `chore/` or `tests/` and a kebab-case slug), and equals the
   manifest's `feature_branch`.

## Output

Return `scope` (`small` or `large`), `slug`, `branch`, `title` (one line, for the PR),
`needs_user` and `ask`. Stop when `$ARTIFACTS_DIR/plan.md` exists, passes `validate_manifest.py`,
and the reviewer's findings are fixed or answered in the plan's "Risks and open questions".
