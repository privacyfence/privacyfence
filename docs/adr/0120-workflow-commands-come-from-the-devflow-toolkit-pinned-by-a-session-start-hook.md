# ADR 0120: Claude Code workflow commands come from the devflow toolkit, pinned by a SessionStart hook

## Status

Accepted — 2026-10-08. Implemented: `.claude/toolkit.yaml`, `.claude/settings.json`.

## Context

`/make-plan`, `/implement` and `/dod` lived in `.claude/commands/` and mixed a generic plan →
implement workflow with PrivacyFence facts. The generic part now lives in the devflow toolkit
(`andras-tkcs/claude-toolkit`) so other repositories can use it, with each project's facts in a
profile, `.claude/toolkit.yaml`. A cloud session does not install plugins that a repository enables
in `.claude/settings.json`, so the toolkit needs a delivery route. Two exist: Claude Project
plugins, or a SessionStart hook that installs a tagged release.

## Decision

The commands, and the toolkit's `project-profile`, `pr-steward`, `review-checklist`,
`secure-code-review` and stack skills, come from the toolkit at the tag in `toolkit.ref`. They are
installed under the container's `~/.claude/` by a SessionStart hook that runs the installer script
from the same tag. `toolkit.ref` and the hook URL change together. PrivacyFence's own facts (must-read
docs, branch rules, the §2.7 gate commands and rows, dispatchable workflows) are in
`.claude/toolkit.yaml`. `.claude/skills/steward/SKILL.md` keeps only PrivacyFence-specific policy.
Only one delivery route is on: the Claude Project's devflow plugins are off.

## Alternatives considered

- **Claude Project plugins.** Rejected: nothing in the repository pins the version, so a plan's run
  is reproducible only as far as the Project's plugin version is, and a change to the workflow
  would not show up in this repository's history.
- **Keep the commands in this repository.** Rejected: they would drift from the toolkit, and every
  fix would have to be made twice.
- **Vendor the installer script into `.claude/hooks/`.** Not needed: it saves only the script
  fetch, and the install still fetches the toolkit from `github.com`.
- **Both routes at once.** Rejected: the commands would appear twice (`/dod` and `devflow:dod`),
  possibly at different versions.

## Consequences

A session start needs `raw.githubusercontent.com` (the installer) and `github.com` (the toolkit).
If either fails, the hook prints curl's error or the installer's `WARNING`, exits 0, and the
session runs without the three commands. The session itself still works.
`/dod`'s rows now live in two places, `.claude/toolkit.yaml` and §2.7, which must be changed
together; `tests/unit/test_definition_of_done_drift.py` fails if a §2.7 command is missing from the
profile. Upgrading the toolkit is a PR that bumps the tag in both places.

## Verification

The toolkit's `validate_profile.py` accepts `.claude/toolkit.yaml`; the drift test above keeps
`verify` and §2.7 in step; the second hook in `.claude/settings.json` installs the pinned tag.

## Related

- The toolkit's migration guide for this repository, `docs/migrating-privacyfence.md` at tag `v0.1.0`
  of `andras-tkcs/claude-toolkit`, and its `docs/delivery.md` at the same tag (full GitHub URLs).
- [ADR 0113](0113-process-docs-live-in-docs-not-in-claude-md.md), which moved the process docs out of `CLAUDE.md`.
