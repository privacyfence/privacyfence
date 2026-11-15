# ADR 0313: workflow commands come from the tld plugin

## Status

Accepted — 2026-11-15. Implemented: `.tld/toolkit.yaml`, `CLAUDE.md`, `.claude/skills/steward/SKILL.md`.
Supersedes [ADR 0132](0132-workflow-commands-come-from-the-devflow-toolkit-plugins.md).

## Context

ADR 0132 took the Claude Code workflow commands from the devflow toolkit plugins enabled in the Claude
Project. The toolkit has since become the tld plugin. Its commands, guard hooks and status board replace
devflow's, and this repository's own skills and docs still named devflow.

## Decision

1. **The plugin supplies the workflow.** The `/tld:*` commands (`/tld:make-plan`, `/tld:implement`,
   `/tld:dod`, `/tld:review` and the rest), the guard hooks (`protect-paths`, `no-git-tag`, `plan-gate`,
   `board-gate`) and the status board come from the tld plugin enabled in the Claude Project.
2. **The project supplies facts.** What the plugin reads about this repository is `.tld/toolkit.yaml`,
   validated by `tld profile validate`.
3. **Steward policy stays small.** `.claude/skills/steward/SKILL.md` keeps only PrivacyFence-specific policy,
   on top of the plugin's pr-steward policy.
4. **No install hook.** No SessionStart hook installs the plugin; it comes from the Project's plugins.

## Alternatives considered

- **Stay on devflow** — it is no longer where the workflow commands and guards are maintained.
- **Install tld from a SessionStart hook pinned to a tag** — ADR 0132 rejected this for devflow for the same
  reason: it adds a network step to every session start and a pin to maintain.

## Consequences

- Protected files (`.github/workflows/**`, `.tld/toolkit.yaml`) need an allow list on `main`
  (`hooks.protect_paths_allow`) before a session may edit them.
- A real release dispatch from a session needs `tld guard allow-release`, after the owner's yes on the board.
- The plugin version is not pinned in this repository; a plugin change can change a command's behaviour
  without a commit here.
- Docs and skills say "the tld plugin" and `/tld:<command>`; the old devflow command names are gone.

## Verification

- `tld profile validate` on `.tld/toolkit.yaml`.
- `.tld/toolkit.yaml` `ci.dispatchable` and `verify` rows, which the plugin's commands read.

## Related

[ADR 0132](0132-workflow-commands-come-from-the-devflow-toolkit-plugins.md),
[`CLAUDE.md`](../../CLAUDE.md).
