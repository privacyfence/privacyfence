---
description: Pre-flight and cut a release tag through tld-release.yml
argument-hint: "<version>  e.g. 4.2.0, or 4.2.0a1 / 4.2.0b2 / 4.2.0rc1"
---

Cut the release: **$ARGUMENTS**

If no version was given above, stop and ask. Never infer the next version from `CHANGELOG.md` —
`setuptools_scm` and the git tags are the only version source in this repo, and
`scripts/changelog_section.py`'s own docstring forbids reading a version out of the changelog.

This container cannot push `refs/tags/*`, so the tag is cut by `.github/workflows/tld-release.yml`.
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
`tld-release.yml`'s own dry run never installs the package or builds anything, so it cannot catch a
regression in the org image build — that runs only inside `build.yml`'s `build-org-image` jobs, which
otherwise trigger only on a real tag push. That gap is exactly how `v4.2.0` reached a pushed tag
broken (see `CHANGELOG.md`'s `[4.2.1]` entry and
[ADR 0030](../../docs/adr/0030-preflight-dispatches-build-yml-before-tagging.md)).

Dispatch `.github/workflows/build.yml` (`workflow_dispatch`, it takes no inputs) against
`origin/main`'s exact commit from the step above, and wait for the `build-org-image (amd64)`, `build-org-image (arm64)`, and `sbom` jobs to all finish. This is safe off an untagged commit: every
upload/publish/release step in `build.yml` — including `finalize-release` — is gated
`if: startsWith(github.ref, 'refs/tags/')`, so this run builds and tests every artifact but
publishes nothing anywhere. Report the run URL and its outcome.

If any of those jobs fail, including either `build-org-image` architecture, or if one never started: stop. Treat it as an ordinary CI failure on `main` — diagnose, fix,
push, and re-run this same pre-flight against the fixed commit — before touching `tld-release.yml` at
all. Do not proceed to the dry run on a red pre-flight.

**Also check the OAuth relay is up** (`docs/oauth-relay.md`): `curl -fsSI https://oauth.privacyfence.eu/health`
must return 200. Report the result. A relay that is down does not block the release, but say so,
because organization servers using the built-in apps cannot sign in through it until it is back.

The pre-flight must have finished, not merely started: `tld-release.yml`'s green check refuses a
commit with a running or failed check run.

**Dry run.** Only once the `build.yml` pre-flight is green, dispatch `tld-release.yml` on `main` with
`version: <version>` and `dry_run: "true"`. Never leave `version` empty and never pass `skip_checks`.
The owner approves the `tld-release` environment in GitHub before the job starts. Report the run URL.
For a stable version, report the notes from the "Run the project's release check" step's summary; the
"Show the release notes" step lists GitHub's generated notes, which do not ship. If "Check that the
commit is green" fails, list the check runs it names and stop: a red or queued run on `main`'s tip is
fixed or re-run, never skipped. If `tag_release.py` says local `main` is not `origin/main`, `main`
moved while the run waited for approval: report it and dispatch the dry run again.

**Real run.** Only after the owner's yes on the tld board, which tld's pr-steward policy requires for
any real release dispatch from a session. Ask with `tld board ask` (the session's tld runtime says how
to post and wait): one `choice` question, id `cut`, titled `Release <version>: cut for real?`, its
text the dry-run URL and the channel, options `real` (recommended) and `stop`. A yes typed in chat
alone is not enough. On `real`: run `tld guard allow-release tld-release.yml`, dispatch with
`dry_run: "false"`, then always run `tld guard end-allow`, also when the dispatch failed. The owner
approves the environment again. On `stop`: report that nothing was cut.

**After a real cut.** List `build.yml` and `publish-pypi.yml` runs. A `push` run on the tagged commit
must appear for each within 5 minutes. If none does, tell the maintainer at once: the tag exists and
the release did not start. Never dispatch again for the same version. Report the tag and both run URLs.
