# ADR 0023: `CHANGELOG.md` is the only source of stable release notes, and rendering fails loudly

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-14 in `4192ade2`, "Build
the stable GitHub Release body from CHANGELOG.md", merged in
[#389](https://github.com/privacyfence/privacyfence/pull/389)). Hardened by `b2745acb` (duplicate
section, [#430](https://github.com/privacyfence/privacyfence/pull/430)) and `30ccdaba`
(populated `[Unreleased]`, [#485](https://github.com/privacyfence/privacyfence/pull/485)).

## Context

A stable GitHub Release's body used to be whatever GitHub's "generate release notes" produced: a
list of every merged pull request, which for 4.0.0 would have been about 127 lines nobody reads.
The project already keeps a curated `CHANGELOG.md` (Keep a Changelog), so the notes existed; they
were just not what shipped.

The mechanism that attaches the body, `softprops/action-gh-release`, has a trap: when `body_path`
cannot be read it silently keeps the release's existing (auto-generated) body. Any rendering
failure that produces no file, or the wrong file, still ships a green release.

Two such failures were found against the real file after the first version landed:

- **Duplicated section.** 4.0.0's heading was opened early, so the "rename `[Unreleased]`" step
  produced a second `## [4.0.0]`. The renderer took the first and exited 0: 87 lines of beta fixes,
  with the 4.0.0 entry itself (including the "Upgrading from 3.x" instructions) dropped.
- **Populated `[Unreleased]`.** Doing only the "correct its date" half of the merge left one
  correct heading and the whole cycle stranded above it. Measured on 2026-09-17: a 142-line body
  missing the eSigner, `%LOCALAPPDATA%`, `privacyfence_status` and `SameSite` entries, at exit 0.

## Decision

A stable release's GitHub Release body is exactly `CHANGELOG.md`'s section for that version,
rendered by `scripts/changelog_section.py <version>`. The render refuses (exits non-zero) when:

- there is no section for the version;
- there is more than one heading for it;
- the section is empty;
- `## [Unreleased]` still has entries (unless `--allow-unreleased` is passed, which is for reading
  a section by hand mid-cycle; no workflow passes it).

Rendering is its own step, before the upload, so a refusal fails the job instead of falling
through to the auto-generated body. The notes are therefore a pull-request deliverable: the PR that
cuts a release leaves exactly one `## [X.Y.Z] — YYYY-MM-DD` heading, an empty `## [Unreleased]`
above it, and updated link definitions. Feature branches only add under `[Unreleased]`.

Pre-release tags get no section of their own (they fold into the version they lead to), so only
the stable channel renders a body. The dependency runs one way: the changelog never determines a
version; `setuptools_scm` does.

## Alternatives considered

- **GitHub's auto-generated notes** — rejected: an unread wall of PR titles, and not the text
  reviewers approved.
- **Match the first heading and stop at the next `##`** (the original renderer) — rejected in
  `b2745acb`: silently ships half the notes when a section is duplicated.
- **Treat a populated `[Unreleased]` as housekeeping** — rejected in `30ccdaba`: the duplicate
  guard cannot see it, and it drops a whole cycle's entries.

## Consequences

- Tag day involves no writing; the notes are reviewed in an ordinary PR.
- A release can be blocked by changelog hygiene alone. That is intended.
- The release workflow can preview the exact body: `release.yml` renders it into the job summary
  before a stable tag exists, and its dry run is the only way to read it without shipping.
- Pre-release entries keep whatever body GitHub generates.

## Verification

- `scripts/changelog_section.py` (`section()`, `unreleased_body()`, `--allow-unreleased`).
  Tests: `tests/unit/test_changelog_section.py`, including `TestRealChangelog`, which checks this
  repository's own file for duplicate and empty sections.
- `.github/workflows/build.yml`, `finalize-release` job (`needs: [build, build-windows, build-deb,
  sbom]`): "Render release notes from CHANGELOG.md", then one `action-gh-release` call with
  `body_path: release-notes.md`, both stable-only.
- `.github/workflows/release.yml`: "Render release notes" (stable only), before the tag is made.

## Related

- [#389](https://github.com/privacyfence/privacyfence/pull/389),
  [#430](https://github.com/privacyfence/privacyfence/pull/430),
  [#485](https://github.com/privacyfence/privacyfence/pull/485).
- [#373](https://github.com/privacyfence/privacyfence/issues/373): moved attaching (and so the
  single render) into `finalize-release`.
- [ADR 0022](0022-one-release-tag-per-commit.md): the other pre-tag release guard.
