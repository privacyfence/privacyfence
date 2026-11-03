Write the changelog section for release `$INPUTS.version` and open the pull request that lands it
(Sonnet). The release cannot be cut until `scripts/changelog_section.py $INPUTS.version` succeeds on
`main`, and a feature branch must never open a version heading, so this PR is the one place it
happens.

The facts, read at origin/main's tip: the section is missing (`$facts.output.changelog_error`).
You are on a detached `origin/main`. Read first: the how-to-use comment at the top of
`CHANGELOG.md` (it is the authority on this edit), `docs/releasing.md` ("Release notes come from
CHANGELOG.md"), and the project's `CLAUDE.md`.

1. `git switch -c chore/release-<version with dots replaced by dashes>-changelog origin/main`.
2. Edit `CHANGELOG.md` exactly as its how-to-use comment says: `## [Unreleased]` stays, empty,
   above a new `## [$INPUTS.version] -- <today, YYYY-MM-DD>` heading holding the entries that were
   under `[Unreleased]`; update the two link definitions at the bottom. If a section for this
   version already exists, merge `[Unreleased]`'s entries into it and fix its date instead of
   renaming (a second heading breaks the renderer). Pre-release tags get no section of their own.
   Change nothing else in the file: do not reword, reorder or add entries. If `[Unreleased]` is
   empty and there is no section for the version, stop: write nothing and say so in your reply.
3. `python3 scripts/changelog_section.py $INPUTS.version` must succeed; read the rendered notes
   once, they are the literal GitHub Release body. Run the profile's `verify.fast` commands from
   `.claude/toolkit.yaml` that apply to a docs-only change (`ruff check .`; the guards under
   `tests/unit` that read `CHANGELOG.md`, found with `grep -l CHANGELOG tests/unit/*.py`).
4. Commit ("Release notes for $INPUTS.version"), `git push -u origin <branch>`, and
   `gh pr create --base main` with a body that says it only moves the entries and why, and
   pastes the rendered notes. Not a draft. Do not merge it: a person does.

Return `number` and `url` of the PR.
