# ADR 0132: Claude Code workflow commands come from the devflow toolkit plugins in the Claude Project

## Status

Accepted — 2026-10-09. Implemented: `.claude/toolkit.yaml`.

## Context

`/make-plan`, `/implement` and `/dod` lived in `.claude/commands/` and mixed a generic plan →
implement workflow with PrivacyFence facts. The generic part now lives in the devflow toolkit
(`andras-tkcs/claude-toolkit`) so other repositories can use it, with each project's facts in a
profile, `.claude/toolkit.yaml`. A cloud session does not install plugins that a repository enables
in `.claude/settings.json`, so the toolkit needs a delivery route. Two exist: Claude Project
plugins, or a SessionStart hook that installs a tagged release.

## Decision

The commands, and the toolkit's `project-profile`, `pr-steward`, `review-checklist`,
`secure-code-review` and stack skills, come from the `devflow` and `stack-python` plugins enabled in
the Claude Project. PrivacyFence's own facts (must-read docs, branch rules, the §2.7 gate commands
and rows, dispatchable workflows) are in `.claude/toolkit.yaml`, which has no `toolkit.ref` because
nothing in this repository installs the toolkit. `.claude/skills/steward/SKILL.md` keeps only
PrivacyFence-specific policy. Only one delivery route is on: this repository has no toolkit
SessionStart hook.

## Alternatives considered

- **A SessionStart hook pinned to a toolkit tag.** Rejected: the maintainer uses the toolkit in
  several projects, and enabling the plugins once in the Project serves all of them, while a hook
  would have to be added, and its tag bumped, in each repository. The hook's advantage, a version
  pinned in this repository, is given up.
- **Keep the commands in this repository.** Rejected: they would drift from the toolkit, and every
  fix would have to be made twice.
- **Both routes at once.** Rejected: the commands would appear twice (`/dod` and `devflow:dod`),
  possibly at different versions.

## Consequences

There is no pin in the repository, so a plan's run is reproducible only as far as the Project's
plugin version is, and a change to the workflow does not show in this repository's history. A
session outside the Project, or one where the plugins are off, has none of the three commands.
`/dod`'s rows now live in two places, `.claude/toolkit.yaml` and §2.7, which must be changed
together; `tests/unit/test_definition_of_done_drift.py` fails if a §2.7 command is missing from the
profile. Upgrading the toolkit is a plugin update in the Project.

## Verification

The toolkit's `validate_profile.py` accepts `.claude/toolkit.yaml`; the drift test above keeps
`verify` and §2.7 in step.

## Related

- The toolkit's migration guide for this repository, `docs/migrating-privacyfence.md`, and its
  `docs/delivery.md`, in `andras-tkcs/claude-toolkit` (full GitHub URLs).
- [ADR 0113](0113-process-docs-live-in-docs-not-in-claude-md.md), which moved the process docs out of `CLAUDE.md`.
