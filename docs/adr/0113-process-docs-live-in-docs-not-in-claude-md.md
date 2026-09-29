# ADR 0113: Project process documentation lives in `docs/` and `CONTRIBUTING.md`, not in `CLAUDE.md`

## Status

Accepted — 2026-09-29. Implemented: `docs/releasing.md`, the branching and ADR sections of
`CONTRIBUTING.md`, and a `CLAUDE.md` reduced to Claude Code-specific notes.

## Context

`CLAUDE.md` had grown into the reference for the whole release process (versioning, tagging,
release notes, packaged-artifact gating, PyPI, the R2 archive, who can download a pre-release),
for branch naming and `releases/*` protection, and for the plan/ADR lifecycle. Workflows, build
scripts, tests, other docs and the pull request template cited its sections as the authority.

That content is what any contributor needs to cut a release or open a correct pull request, but it
sat in a file named after one AI tool, and it was not in the contributor half of `docs/README.md`'s
index. It also tied the project's own process documentation to the continued presence of
tool-specific configuration in the repository.

## Decision

1. The release process is `docs/releasing.md`, a contributor doc (in `CONTRIBUTOR_DOCS`, never
   published on privacyfence.eu, per [ADR 0051](0051-privacyfence-eu-publishes-the-user-and-operator-docs-only.md)).
   Its section headings are the ones `CLAUDE.md` used, so a citation of a section by name keeps
   its meaning with only the file name changed.
2. Branch naming, the changelog rule for feature branches, `releases/*` integration branches and
   the plan/ADR lifecycle summary are in `CONTRIBUTING.md`.
3. `CLAUDE.md` holds only what is specific to Claude Code sessions (its commands and skills, and
   the worktree convention for parallel sessions) and points to the documents above for
   everything else. Nothing outside `.claude/` cites `CLAUDE.md` as the authority for a process.
4. Accepted ADRs that cite `CLAUDE.md`'s "Releasing" section (or one of its subsections) are left
   as written, per the ADR rules; read those citations as the same-named section of
   `docs/releasing.md`.

## Alternatives considered

- **Keep `CLAUDE.md` as the reference and link to it from `docs/README.md`.** Contributors would
  find it, but the project's process would still depend on a tool-specific file, and moving
  Claude Code configuration elsewhere later would take the release documentation with it.
- **Move `CLAUDE.md` wholesale into a separate private repository.** Considered as the end state
  for the Claude Code-specific files; doing it with the process content still inside would remove
  the release documentation from the public repository and break every citation of it.

## Consequences

- A change to the release process edits `docs/releasing.md` (and `CONTRIBUTING.md` for branch
  rules) in the same PR as the behavior, like any reference doc.
- `CLAUDE.md` and `.claude/` can later be moved or removed without touching release documentation
  or the code that cites it.
- `docs/releasing.md` is subject to the contributor-doc checks (`tests/unit/test_docs_no_history.py`,
  `tests/unit/test_website_docs_allowlist.py`).

## Related

- [ADR 0051](0051-privacyfence-eu-publishes-the-user-and-operator-docs-only.md): which docs are
  published.
- [ADR 0023](0023-changelog-is-the-only-source-of-release-notes.md): the changelog rules now
  described in `docs/releasing.md`.
