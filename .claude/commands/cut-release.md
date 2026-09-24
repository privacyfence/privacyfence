---
description: Pre-flight and cut a release tag through the release workflow
argument-hint: "<version>  e.g. 4.2.0, or 4.2.0a1 / 4.2.0b2 / 4.2.0rc1"
---

Cut the release: **$ARGUMENTS**

If no version was given above, stop and ask. Never infer the next version from `CHANGELOG.md` —
`setuptools_scm` and the git tags are the only version source in this repo, and
`scripts/changelog_section.py`'s own docstring forbids reading a version out of the changelog.

This container cannot push `refs/tags/*`, so the tag is cut by `.github/workflows/release.yml`.
Do not try to `git tag && git push` from here; it will fail on the push and may leave a local tag
behind that makes later checks lie.

**Before dispatching anything**, report the state so I can sanity-check it:

- `git fetch origin main` and confirm what commit `origin/main` is at, and what it is
  (`git log -1 --oneline origin/main`). That commit is what gets tagged.
- `python3 scripts/r2_release.py channel <version>` — the channel this version resolves to.
- `git tag --list` — the most recent existing tags, so the sequence is visible. A pre-release must be
  exactly +1 on its stage; a stable must be an unskipped bump.
- For a **stable** version only: `python3 scripts/changelog_section.py <version>` must succeed, and
  show me the rendered notes. That is the literal GitHub Release body. Never pass
  `--allow-unreleased` — it exists for reading a section by hand mid-cycle and a release must not
  use it. If this fails because `[Unreleased]` is still populated, the fix is a PR that merges those
  entries into the version's section, not a flag.

**Then pre-flight `build.yml` itself against that same commit — do not skip this.**
`release.yml`'s own dry run never installs the package or builds anything, so it cannot catch a
regression in the `pytest.mark.packaged` smoke tests (macOS DMG/`.pkg`, Windows installer, `.deb`
lifecycle) — those run only inside `build.yml`'s `build`/`build-windows`/`build-deb` jobs, which
otherwise trigger only on a real tag push. That gap is exactly how `v4.2.0` reached a pushed tag
broken (see `CHANGELOG.md`'s `[4.2.1]` entry and
[ADR 0030](../../docs/adr/0030-preflight-dispatches-build-yml-before-tagging.md)).

Dispatch `.github/workflows/build.yml` (`workflow_dispatch`, it takes no inputs) against
`origin/main`'s exact commit from the step above, and wait for the `build`, `build-windows`,
`build-deb`, and `sbom` jobs to all finish. This is safe off an untagged commit: every
upload/publish/release step in `build.yml` — including `finalize-release` — is gated
`if: startsWith(github.ref, 'refs/tags/')`, so this run builds and tests every artifact but
publishes nothing anywhere. Report the run URL and its outcome.

If any of those four jobs fail: stop. Treat it as an ordinary CI failure on `main` — diagnose, fix,
push, and re-run this same pre-flight against the fixed commit — before touching `release.yml` at
all. Do not proceed to the dry run on a red pre-flight.

**Only once the `build.yml` pre-flight is green, dispatch `release.yml` against `main` with
`dry_run: true`** and report the result. The dry run performs every check and creates the tag on
the runner without pushing it, so a failure costs nothing.

**Only cut for real once I have seen the dry run and said to.** Then re-dispatch with
`dry_run: false`, and watch it: the workflow verifies that `build.yml` actually started for the
tagged commit, and a failure there means the tag exists but the release did not start — tell me
immediately if that happens, it needs the tag deleted and re-pushed, not a retry.

After a real cut, report the tag, the `build.yml` run URL, and the `publish-pypi.yml` run URL.
