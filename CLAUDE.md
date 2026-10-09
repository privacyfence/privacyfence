# CLAUDE.md

Notes for Claude Code sessions on PrivacyFence. Everything a human contributor also needs lives in
the ordinary docs, not here:

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branch naming, pull requests, `releases/*` integration
  branches, and the plan/ADR lifecycle.
- [`docs/releasing.md`](docs/releasing.md) — versioning, cutting a tag, release notes, gating,
  PyPI and the R2 archive.
- [`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md) — code and test
  conventions, and the definition of done (§2.7).

Read those before changing anything they cover. This file covers only what is specific to working
with Claude Code.

## Commands and skills

`/make-plan`, `/implement` and `/dod` come from the devflow toolkit (`andras-tkcs/claude-toolkit`,
enabled as plugins in the Claude Project); the behaviour described here is unchanged. The project
facts they use are in `.claude/toolkit.yaml`;
[ADR 0132](docs/adr/0132-workflow-commands-come-from-the-devflow-toolkit-plugins.md) says why.

- `.claude/skills/steward/SKILL.md` — PrivacyFence-specific steward policy, on top of the toolkit's
  `pr-steward` skill. The workflows a session dispatches instead of running locally are
  `ci.dispatchable` in `.claude/toolkit.yaml`.
- `/cut-release` (`.claude/commands/cut-release.md`) runs the `build.yml` pre-flight that
  [`docs/releasing.md`](docs/releasing.md#cutting-a-release) requires, then dispatches
  `release.yml`.
- `/make-plan <prompt or issue>` (runs on Opus) researches the
  change first. For a small scope (one session's worth, such as a single bug-fix issue) it writes
  no plan: it hands back a self-contained prompt to paste into a new session, and says whether
  that session should run on Sonnet (the default) or Opus. For a large scope it writes a
  `docs/<slug>-plan.md` with an `## Implementation manifest` on its own `plan/<slug>` branch, cut
  from `main`. A `plan/` branch is never PR'd: `/implement` cuts the feature branch from it, and
  the plan's last phase deletes the plan document, so it reaches `main` only as that deletion in
  the feature PR. Delete the `plan/` branch once the feature PR merges. Steps only the user can do
  (third-party console setup, secrets, real-device checks) go only into the manifest's
  `manual_before` and `manual_after`, never between phases, and get a step-by-step HTML artifact.
- A plan with an `## Implementation manifest` can be run with `/implement <plan URL>`.
  The orchestrator session (Sonnet) waits for the user to
  confirm `manual_before`, builds the manifest's `feature/<name>` branch out of
  `feature/<name>--<phase id>` branches, one Sonnet child session each, merged with `--no-ff` and
  a `Plan-Phase:` trailer, has one Opus session review the whole branch against the plan, and
  opens a single PR to `main` at the end, with `manual_after` as unchecked items. Phase branches
  are never PR'd on their own.
- `/dod` runs the §2.7 definition-of-done gate from `verify` in `.claude/toolkit.yaml`;
  `/qa-record` records a connector's live QA fixture on the self-hosted runner.

## Parallel sessions & worktrees

The user regularly runs multiple Claude Code sessions on this repo at once, each on a different
task/branch. To avoid one session's checkout state (branch switches, uncommitted edits) interfering
with another's:

- Start new work in its own `git worktree` under `~/Coding/worktrees/`, not by switching branches
  in whichever checkout happens to be open. Naming convention already in use:
  `~/Coding/worktrees/privacyfence-<short-branch-slug>` (e.g. `privacyfence-fix-tasks-ssl`).
- Don't reuse an existing worktree for an unrelated task — one worktree per active branch/task.
