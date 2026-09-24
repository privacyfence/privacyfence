# ADR 0030: cutting a release first dispatches `build.yml` on the untagged commit

## Status

Accepted — 2026-09-24.

## Context

`v4.2.0` was tagged (`09d48f22`), and its release build failed: `build.yml`'s `build` (macOS) and
`build-deb` jobs both hit `tests/integration/test_macos_packaged_smoke.py` /
`test_deb_packaged_lifecycle.py`'s `pytest.mark.packaged` assertion — "attested control channel
mint failed" — because ADR 0008's per-peer `principal_id` (resolved from the real connecting uid,
landed on `main` via `5ca27e57`) resolved the smoke-test helpers' root-based stand-in companion to
a different principal than the one the daemon's mint call addressed. `finalize-release` never ran,
so nothing public shipped, but the tag existed and had to be superseded by `v4.2.1` once the
helpers were fixed (`462f663a`) — see `CHANGELOG.md`'s `[4.2.1]` entry for the full account,
including a second, unrelated bug (ADR 0029) the fix cycle's own re-runs of `build.yml` turned up
along the way.

The regression that broke `4.2.0` sat on `main` for the entire cycle undetected, because nothing
in ordinary PR CI could have caught it: `docs/testing-policy.md`'s layer-6 taxonomy runs every
`pytest.mark.packaged` test (the macOS DMG/`.pkg`, Windows installer, and `.deb` lifecycle smoke
tests) exclusively inside `build.yml`'s `build`/`build-windows`/`build-deb` jobs, deliberately kept
off the per-PR path since each needs a real signed installer built on its own OS runner. `build.yml`
itself only ever ran automatically on a tag push (`on: push: tags: ['v*']`) — before this decision,
that meant the first time `main`'s tip actually went through those jobs was the moment a release
was already being cut.

`release.yml`'s own `dry_run` does not fill that gap: by design it never installs the package or
resolves a version through `setuptools_scm` at all (`af193025`'s commit message: "None of the three
[scripts] needs PrivacyFence installed... there is no way for this job to resolve a version through
setuptools_scm"). It only proves the tag/changelog mechanics (`r2_release.py channel`,
`changelog_section.py`, `tag_release.py`'s sequencing guards) — none of which touches a single
packaged artifact.

## Decision

`.claude/commands/cut-release.md`'s pre-flight now dispatches `.github/workflows/build.yml` itself
(`workflow_dispatch`, no `version`/`dry_run` inputs — it takes none) against the exact commit
`release.yml` would tag, and waits for the `build`, `build-windows`, `build-deb`, and `sbom` jobs to
all succeed, **before** dispatching `release.yml` even in dry-run mode. A failure here is treated
exactly like a failed `release.yml` dry run: fix it on `main` first, re-run the `build.yml`
pre-flight, and only then proceed.

This costs nothing beyond runner time: every upload/publish/release step in `build.yml` and its own
`finalize-release` job is gated `if: startsWith(github.ref, 'refs/tags/')`, so a `workflow_dispatch`
run off an untagged commit builds and tests every artifact but touches no R2 bucket, no GitHub
Release, and no PyPI index — see that job's own "Determine version and release channel" step, which
resolves `channel="n/a (not a tag push)"` for exactly this case.

## Alternatives considered

- **Run `pytest.mark.packaged` on every PR.** Rejected for the same reason `docs/testing-policy.md`
  already keeps it off that path: each test needs a real installer built and installed on its own
  OS runner (DMG mount + codesign/notarization chain on macOS, a real `dpkg -i`/`.deb` lifecycle,
  a real Windows installer run), which is too slow and too expensive to run on every push. This
  decision adds one gate at release time instead of changing that per-PR trade-off.
- **Widen `release.yml`'s own dry run to also build and test artifacts.** Rejected: `release.yml`
  is deliberately cheap and dependency-free (no `pip install`, no `setuptools_scm` resolution) so
  that the one thing that must never be wrong — the tag/version/changelog bookkeeping — has nothing
  else that can fail alongside it. Duplicating `build.yml`'s multi-platform build logic into
  `release.yml` would mean maintaining two copies of it.
- **Accept the risk and fix forward**, i.e. what actually happened for `4.2.0`. Rejected as the
  standing default: it costs a full extra release cycle (a stranded, unpublished tag, a second
  version number, a `CHANGELOG.md` entry explaining the supersession) every time a `packaged`-only
  regression lands between releases, which — per the taxonomy above — is the only place such a
  regression can be caught at all.

## Consequences

- Cutting a release now takes as long as a full `build.yml` run (macOS, Windows, and Linux builds,
  each with its own packaged-artifact smoke test) in addition to `release.yml`'s own dry run, before
  the real tag is ever pushed.
- A `packaged`-marked regression is caught against the commit that would be tagged, not against the
  tag itself — no more stranded, unpublished release tags for this failure mode.
- This does not change layer 6's own scope or scheduling (`docs/testing-policy.md`) — the
  graphical-session/autostart workflows stay off the critical path, scheduled weekly, exactly as
  before; this decision only adds a manual pre-flight dispatch of `build.yml` itself to the release
  runbook.

## Verification

- `.claude/commands/cut-release.md`'s pre-flight step.
- `CLAUDE.md`'s "Releasing" section.

## Related

- `CHANGELOG.md`'s `[4.2.1]` entry (the incident this ADR responds to).
- ADR 0008 (the `principal_id` change that exposed the gap), ADR 0029 (the second bug the fix
  cycle's own re-runs of `build.yml` found).
- `docs/testing-policy.md`'s layer-6 (packaged-artifact) taxonomy.
- Commits `09d48f22` (the failed `v4.2.0` tag), `462f663a` (the fix), `af193025` (`release.yml`
  itself and its dry-run scope).
