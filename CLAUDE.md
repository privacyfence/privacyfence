# CLAUDE.md

Notes for Claude Code sessions on PrivacyFence. Everything a human contributor also needs lives in
the ordinary docs, not here:

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branch naming, pull requests, and the plan/ADR lifecycle.
- [`docs/releasing.md`](docs/releasing.md) — versioning, cutting a tag, release notes, gating,
  publishing `privacyfence-sync` to PyPI and the R2 archive.
- [`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md) — code and test
  conventions, and the definition of done (§2.7).

Read those before changing anything they cover. This file covers only what is specific to working
with Claude Code.

## Commands and skills

The workflow commands (`/tld:make-plan`, `/tld:implement`, `/tld:dod`, `/tld:review` and the rest) come
from the tld plugin, enabled in the Claude Project; the plugin's own playbooks say what each one does. The
project facts they read are in `.tld/toolkit.yaml`; [ADR 0313](docs/adr/0313-workflow-commands-come-from-the-tld-plugin.md) says why.

- `.claude/skills/steward/SKILL.md` — PrivacyFence-specific steward policy, on top of the plugin's
  pr-steward policy. The workflows a session dispatches instead of running locally are
  `ci.dispatchable` in `.tld/toolkit.yaml`.
- `/tld:release` prepares and cuts a stable release; the steward skill's "Cutting a release" section adds what this repository requires ([`docs/releasing.md`](docs/releasing.md#cutting-a-release)).
- `/qa-record` records a connector's live QA fixture on the self-hosted runner.
- **Plans.** `/tld:make-plan` writes a plan to `docs/<issue>/plan.md` on its feature branch, and
  `/tld:implement` adds `docs/<issue>/deviations.md` and opens one PR to `main`, where both stay as the
  record of the change ([ADR 0314](docs/adr/0314-plans-stay-on-main-as-the-record-of-their-change.md); the lifecycle is in [`CONTRIBUTING.md`](CONTRIBUTING.md#decisions-plans-and-adrs)).
  Steps only the user can do (third-party console setup, secrets, real-device checks) go only into the
  manifest's `manual_before` and `manual_after`, never between phases.
- **New connectors.** A new connector is two PRs, connect then tools, and `/tld:make-plan` plans them that
  way with the browser connection on the QA server between them as a manual step. The procedure is in
  [`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md#3-adding-a-connector), "Adding a connector".

## Parallel sessions & worktrees

The user regularly runs multiple Claude Code sessions on this repo at once, each on a different
task/branch. To avoid one session's checkout state (branch switches, uncommitted edits) interfering
with another's:

- Start new work in its own `git worktree` under `~/Coding/worktrees/`, not by switching branches
  in whichever checkout happens to be open. Naming convention already in use:
  `~/Coding/worktrees/privacyfence-<short-branch-slug>` (e.g. `privacyfence-fix-tasks-ssl`).
- Don't reuse an existing worktree for an unrelated task — one worktree per active branch/task.
