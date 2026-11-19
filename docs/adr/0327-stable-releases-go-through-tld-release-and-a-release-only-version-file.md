# ADR 0327: stable releases go through `/tld:release` and a release-only version file

## Status

Accepted — 2026-11-18. Implemented: `.tld/release-version`, `scripts/release_check.py`, the `release:` block of
`.tld/toolkit.yaml`, `.claude/skills/steward/SKILL.md`.
Amends [ADR 0312](0312-a-release-tag-is-created-only-by-tld-release-yml.md) (its rejected `/tld:release`
alternative is adopted) and [ADR 0030](0030-preflight-dispatches-build-yml-before-tagging.md) (the pre-flight
moves from `/cut-release` to the steward skill).
Relates to [ADR 0022](0022-one-release-tag-per-commit.md) and
[ADR 0023](0023-changelog-is-the-only-source-of-release-notes.md).

## Context

`/tld:release` could not run here: the profile had no `release:` block, and the block needs a file that holds
the version. This repository deliberately has none, because `setuptools_scm` derives the version from git
tags (ADR 0312 rejected the block for that reason). `/cut-release` covered the same ground by hand: the
`build.yml` pre-flight, then a dispatch of `tld-release.yml`. With `version` empty the workflow raises the
newest stable tag by a patch, which is wrong for a minor or major release.

## Decision

1. **The version file.** `.tld/release-version` holds one line, the last stable version, no `v`, ending in a
   newline. Only `tld ops release-prepare` and `scripts/release_check.py` read it. Nothing builds from it;
   `setuptools_scm` stays the only version source.
2. **The profile block.** `.tld/toolkit.yaml` gets `release: {workflow: tld-release.yml, version_files:
   [{path: .tld/release-version, pattern: '^(\d+\.\d+\.\d+)$'}]}`.
3. **The check.** `scripts/release_check.py` runs `check_release_version` right after the channel step, for the
   stable channel only, and fails the run when the file does not hold exactly the version being released. A
   pre-release skips it.
4. **Dispatch.** The steward skill tells a session to pass `version: <X.Y.Z>` besides `dry_run`; the check is
   the backstop, so a wrong version fails.
5. **Pre-flight.** The `build.yml` pre-flight moves from `/cut-release` into the steward skill and runs before
   the playbook's start question. `/cut-release` is removed; pre-releases (`aN`, `bN`, `rcN`) are cut by hand
   from the Actions tab.
6. **This ADR** records the change.

## Alternatives considered

- **Keep `/cut-release` beside `/tld:release`** — two procedures for one tag, and the pre-flight can be skipped
  from either.
- **A `version_files` entry on `pyproject.toml`** — it has no version string (`dynamic = ["version"]`).
- **Only the check, no `version` in the dispatch** — minor and major releases fail until tld passes `version`
  itself.
- **Edit `tld-release.yml` to read the version file** — it is rendered by `tld ops repo-admin`, so every audit
  would report drift.

## Consequences

- A stable release needs the release pull request first; a stable `version` dispatched by hand without it
  fails `release_check.py`.
- The changelog link definitions are a second commit on the release branch, a deliberate departure from the
  playbook's "edit nothing by hand" (`release-prepare --check --base` allows any change to the changelog
  file). `release-prepare` writes the heading with a hyphen where older headings use an em dash, which
  `changelog_section.py` accepts.
- A dispatch without `version` fails `release_check.py`, and the maintainer dispatches again by hand with
  `version`.
- When the tld release playbook passes `version` itself, step 3 of the steward section can lose its `version`
  sentence.

## Verification

- `tests/unit/test_release_check.py`.

## Related

[ADR 0312](0312-a-release-tag-is-created-only-by-tld-release-yml.md),
[ADR 0030](0030-preflight-dispatches-build-yml-before-tagging.md),
[ADR 0022](0022-one-release-tag-per-commit.md),
[ADR 0023](0023-changelog-is-the-only-source-of-release-notes.md),
[`docs/releasing.md`](../releasing.md#cutting-a-release).
