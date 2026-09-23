# ADR 0021: one release tag per commit; a stuck release is fixed forward

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-09-18 in `52260857`, "Fail a
release build when the tag doesn't name the version it resolved", merged in
[#544](https://github.com/privacyfence/privacyfence/pull/544)).

## Context

There is no version string in the source tree. `setuptools_scm` derives the version from git tags
through `git describe`, and every build job reads it back from there.

`git describe` reports *a* tag on the commit being built, not specifically the tag whose push
started the run. When a commit carries two release tags, describe picks one of them (for two
lightweight tags of the same age, the alphabetically earlier), and the build stamps every artifact
with a version nobody asked to release.

This happened. `v4.1.0a7` was tagged onto the same commit as a stuck `v4.1.0a6`. The run
resolved `4.1.0a6`, rebuilt and re-signed the whole artifact set, and then tried to republish a
version R2 already held under different bytes. `scripts/r2_release.py upload`'s immutability
guard refused it in `build-deb`, `build-windows` and `sbom`: correct, but seven minutes and one
full signing pass after the mistake, with no release produced
([the run](https://github.com/privacyfence/privacyfence/actions/runs/35388772087)).

## Decision

1. **A commit carries at most one release tag.** `scripts/tag_release.py` refuses to create a tag
   on a commit that already has one.
2. **Every release job checks that the version it resolved is the one the pushed tag names,
   before building.** `scripts/r2_release.py check-tag --tag "$GITHUB_REF_NAME" --version
   "$VERSION"` compares parsed identities, not strings, so a long-form tag such as
   `v4.1.0-alpha1` still matches `4.1.0a1`.
3. **A stuck or wrongly tagged release is fixed forward.** Move the release onto a new commit, or
   delete the unwanted tag and re-run from the wanted one. A version that has already published
   artifacts stays published: R2 artifacts are immutable, so cut the next version rather than
   replacing one.

## Alternatives considered

- **Rely on the R2 immutability guard alone** — rejected: it did catch the incident, but only
  after a full build and signing pass. The point of `check-tag` is to fail in the first seconds.
- **Match the tag and version as strings** — rejected in `52260857`: the release tooling accepts
  long-form tag spellings, and a guard that rejected them would fail releases it has no business
  failing.
- **Overwrite an already-published version with corrected bytes** — ruled out by the immutability
  guard: someone may already have downloaded the original under that version number.

## Consequences

- A mistyped or duplicate tag now costs a few seconds of CI instead of a signed artifact set.
- Re-releasing "the same" code under a new pre-release number requires a new commit (or deleting
  the old tag first); tagging the existing commit a second time is refused locally and fails in
  CI.
- A version number, once artifacts exist for it, is spent. Recovery is always the next number.

## Verification

- `scripts/r2_release.py`: `assert_tag_matches_version()` and the `check-tag` subcommand;
  `upload()`'s immutability guard. Tests: `tests/unit/test_r2_release.py`.
- `.github/workflows/build.yml`: `check-tag` in the version-resolution step of `build`,
  `build-windows`, `build-deb`, `sbom` and `finalize-release`.
- `.github/workflows/publish-pypi.yml`: `check-tag` in the `build` job's version-resolution step.
  This one runs after the sdist/wheel build step in that job, not before it; nothing is published
  before it runs.
- `scripts/tag_release.py` (the one-tag-on-HEAD check), also run by `.github/workflows/
  release.yml`. Test: `tests/unit/test_tag_release.py::test_rejects_when_head_already_tagged`.

## Related

- [#544](https://github.com/privacyfence/privacyfence/pull/544) (`52260857`): added `check-tag`
  and wired it into every version-resolving job.
- Incident run: https://github.com/privacyfence/privacyfence/actions/runs/35388772087
- `d929510`, "Revert version bump — will release together with other pending CRs": the earlier,
  unrelated collision (two branches claiming one version) that moved versioning onto tags in the
  first place.
- [ADR 0020](0020-release-tag-push-never-uses-github-token.md): the workflow that runs
  `tag_release.py` on a runner.
