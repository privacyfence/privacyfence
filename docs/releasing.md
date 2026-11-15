# Releasing

How a PrivacyFence release is versioned, cut, built, gated and published. For the
organization image, see [`org-mode-docker.md`](org-mode-docker.md#how-the-image-is-built); for the release gates and the manual checks
every release still needs, see [`release-testing.md`](release-testing.md).

## Versioning

There is no version string in the source tree and no version-bump commit. `pyproject.toml` declares
`dynamic = ["version"]`; the real version is derived from git tags by `setuptools_scm`
(`[tool.setuptools_scm]` in `pyproject.toml`), and `src/privacyfence/__init__.py` reads it back at
import time via `importlib.metadata.version("privacyfence")`. This replaced the old two-file
hand-bumped scheme (`pyproject.toml`'s `project.version` + `__init__.py`'s `__version__`, kept in
sync by a dedicated `Bump to vX.Y.Z` commit) specifically to avoid that scheme's failure mode:
parallel branches both claiming the same next version, one bump commit landing
after another release already took that number (see `d929510`, "Revert version bump — will release
together with other pending CRs", from back when that was still how it worked).

## Cutting a release
**Cutting a release is a tag, not a commit.** Once `main` is at the commit you want to release,
dispatch `tld-release.yml` on `main` (below) with `version` and `dry_run`. Versions are PEP 440 short
form: `v4.0.0` stable, `v4.0.0a13` pre-release (a=alpha, b=beta, rc=release-candidate). Always pass
`version` explicitly, and never pass `skip_checks`. `prerelease` has no effect here: the workflow
creates no GitHub Release (`create_release` is false), and `build.yml` decides the channel from the tag.

`tld-release.yml` is the only way to cut a release ([ADR 0312](adr/0312-a-release-tag-is-created-only-by-tld-release-yml.md)). It runs in the `tld-release`
environment, which admits only `main`, and the owner approves every run. It creates an annotated tag
through the REST API with `TLD_RELEASE_TOKEN`, an owner token stored as that environment's secret.
The `tld-tags` ruleset (`rulesets.tags` in `.tld/admin-policy.yaml`) lets no workflow token create a
tag. A person with the admin role could still push a tag by hand; that is not the procedure, because
it skips every check below. `scripts/tag_release.py <version>` holds the tag checks the workflow runs
(clean tree, at `origin/main`'s tip, no second tag on the commit, PEP 440 short form, sequential with
no gaps) — see its own docstring.

**Cut it from the Actions tab.** `.github/workflows/tld-release.yml` (`workflow_dispatch`, inputs
`version` and `dry_run`) takes these steps against `main`'s tip, in order: decide the tag; check that
the commit is green (every check run on `main`'s tip must be finished and green, so a red, queued or
still-running run blocks — including a `build.yml` pre-flight that is still running, a scheduled run,
and the self-hosted `deploy-qa-server` job when that runner is offline); run
`scripts/release_check.py`, which runs `r2_release.py channel`, `changelog_section.py` (stable
channel only, with the body that will ship written to the run summary) and
`tag_release.py --no-fetch --check-only`; show GitHub's generated notes, which do not ship; and, on a
real run, create the tag. `dry_run` **defaults to true**: the default dispatch runs every check and
creates nothing, which is also the only way to read the release notes that would ship without
shipping them. It exists because a release should not depend on which machine you are sitting at — in
particular, a sandboxed cloud container (such as Claude Code on the web) can push branches but not
`refs/tags/*`. If `main` moves while the run waits for approval, `tag_release.py` refuses (local
`main` is not `origin/main`); dispatch again.

`TLD_RELEASE_TOKEN` **may not be the `GITHUB_TOKEN`**: GitHub does not start a workflow run for an
event raised by the `GITHUB_TOKEN`, so a tag created with it exists, starts neither `build.yml` nor
the publish workflows, and reports success. No image, no R2 upload, no GitHub Release, no error. It
is an owner token, so the tag starts both workflows. The workflow does not check afterwards that
`build.yml` started, so whoever cuts confirms a `push` run of `build.yml` and of `publish-pypi.yml`
for the tagged commit within a few minutes. If neither appears, the tag exists and nothing started:
the owner deletes the tag and cuts again. See
[ADR 0021](adr/0021-release-tag-push-never-uses-github-token.md).

**Pre-flight `build.yml` itself before tagging — the `tld-release.yml` dry run does not cover this.**
`tld-release.yml` never installs the package or resolves a version through `setuptools_scm`, so its
dry run only proves the tag/changelog bookkeeping (`r2_release.py channel`,
`changelog_section.py`, `tag_release.py`'s sequencing guards) — it touches no packaged artifact.
Every `pytest.mark.packaged` test (the Docker image test and the Ubuntu release smoke test — see
[`testing-policy.md`'s layer 6](testing-policy.md#layer-6-packaged-artifact)) runs inside `build.yml`'s
`build-org-image` job, which otherwise only triggers on an actual tag push, so a
regression only a packaged test catches would otherwise first surface on a real tag — see
[ADR 0030](adr/0030-preflight-dispatches-build-yml-before-tagging.md).

Before dispatching `tld-release.yml` — dry run or real — dispatch `build.yml` itself
(`workflow_dispatch`, no inputs) against the exact commit you intend to tag, and confirm
`build-org-image` (both architectures) and `sbom` all succeed. This is safe to run against an untagged
commit: every upload/publish/release step in `build.yml` is gated
`if: startsWith(github.ref, 'refs/tags/')`, so the run builds and tests every artifact but uploads
nothing to R2 or GitHub Releases — see that job's "Determine version and release channel"
step, which resolves `channel="n/a (not a tag push)"` off a tag. A failure here is fixed on `main`
like any other CI failure, then the pre-flight is re-run, before `tld-release.yml` is dispatched at all.
The green check refuses while the pre-flight is running or red, but cannot notice a pre-flight that
was never dispatched.

**Check the OAuth relay before tagging.** Organization servers that use the built-in OAuth apps
sign in through the relay at `oauth.privacyfence.eu`, which ships on its own track and not with
a release tag ([`oauth-relay.md`](oauth-relay.md)). Run `curl -fsSI https://oauth.privacyfence.eu/health`
before dispatching `tld-release.yml`; it must return 200. A relay that is down does not block a
release, but a release published while it is down cannot be tested end to end.

**One release tag per commit.** `setuptools_scm` resolves the version through `git describe`,
which reports *a* tag on the commit being built rather than specifically the one whose push started
the run — so a commit carrying two release tags builds as whichever one `describe` prefers (the
alphabetically earlier, for two lightweight tags of the same age), not as the tag just pushed. Every job that resolves a version runs
`scripts/r2_release.py check-tag` before publishing anything — as the first step in each of
`build.yml`'s jobs, so this fails in the first few seconds instead of after the image has
been built; `publish-pypi.yml`'s `build` job runs it after building `privacyfence-sync`. The fix is
to move the release forward onto a new commit, or to delete the unwanted tag before
retagging. A version that has already published artifacts stays published; cut the next one. See
[ADR 0022](adr/0022-one-release-tag-per-commit.md).

A stable GitHub Release also attaches `privacyfence-sync-plugin.zip`, the recipe-writing skill as a Claude
plugin: `finalize-release` runs `scripts/build_sync_plugin.py --version <version>`, which stamps the release
version into the plugin's `plugin.json` and refuses a stale copy of `sync/README.md`
(ADR 0163).

**The organization Docker image is published for every release, pre-releases included.**
`.github/workflows/publish-org-image.yml` triggers on the same tag push, waits for `build.yml` to
succeed on that commit (the same `wait_for_build` gate as `publish-pypi.yml`), downloads the two images
`build.yml`'s `build-org-image` jobs built and tested (`org-image-amd64`, `org-image-arm64`),
pushes them to `ghcr.io/privacyfence/privacyfence-org`, combines them into one multi-architecture
manifest, applies the tags `scripts/org_image_tags.py` chooses and attaches a GitHub
build-provenance attestation. A stable release gets the version, `X.Y`, `X` and `latest` (the
floating three only when no higher stable release is already tagged in that scope); a pre-release
gets only its exact version, so the floating tags, and with them unattended upgrades, never reach a
pre-release, and its run summary says no floating tag moved. It authenticates with the workflow's
own `GITHUB_TOKEN`, so there is no secret to set up. It rebuilds nothing, so what ships is what the
image test ran against. A failed publish is rerun with `workflow_dispatch` from the tag, within 30
days (how long the tested images are kept as workflow artifacts). It is a workflow of its own so a
GHCR outage can never hold back the `privacyfence-sync` publish, whereas `build-org-image` inside `build.yml` does gate
the `privacyfence-sync` publish and the GitHub Release on every tag (see "Packaged-artifact release gating"
below). See ADRs 0209, 0210, 0215 and 0268 (which amends 0208's stable-only rule).

If the repository restricts allowed Actions, `publish-org-image.yml` needs both
`actions/attest-build-provenance@*` and `actions/attest@*` on the allowlist (the former calls the
latter). Without `actions/attest` the job fails at setup, before pushing anything; add it and
re-run the workflow from the release's tag.

**First release only:** a new GHCR package starts private. After the first release pushes
it, switch `privacyfence-org` to public once (the organization's Packages settings may first need
public packages allowed), then pull it signed out and run
`gh attestation verify oci://ghcr.io/privacyfence/privacyfence-org:X.Y.Z --owner privacyfence`.

That tag push is what `.github/workflows/build.yml`, `.github/workflows/publish-pypi.yml` **and**
`.github/workflows/publish-n8n-node.yml` all trigger on (`on: push: tags: ['v*']`) — the first builds
and tests the organization image and generates the SBOM (see "Packaged-artifact release gating" below),
the second builds `privacyfence-sync` from the same tag, and the third publishes the n8n node to npm
(stable tags only, see "Publishing the n8n node to npm"). The
workflows trigger independently but no longer publish independently: `publish-pypi.yml`'s and `publish-n8n-node.yml`'s
`wait_for_build` jobs block every one of their own publish steps on `build.yml`'s run for that same
commit actually succeeding — see "Packaged-artifact release gating" below. Every artifact `build.yml`
produces uploads to the private Cloudflare R2 release archive (see "Cloudflare R2 release
archive" below); whether it *also* reaches a public GitHub Release / PyPI/TestPyPI depends on the tag's
channel (`a`/`b`/`rc` suffix, or none for stable — same PEP 440 short-form scheme `update_checker.py`'s
beta channel already ranks by): only a stable tag's SBOMs and sync plugin get attached to a public GitHub
Release and only a stable tag's `privacyfence-sync` reaches PyPI/TestPyPI; a pre-release tag still gets a
GitHub Release entry (marked prerelease, so `update_checker.py`'s beta channel — which reads
exactly that flag — keeps working), just with no files attached to it. The one thing that does need to be
committed first is the release notes — see "Release notes come from CHANGELOG.md" below. Nothing
else anywhere needs editing or committing. Between tags,
`__version__` is a `setuptools_scm`-synthesized dev version (`<next-version>.dev<n>+g<sha>`, e.g.
`4.0.1.dev3+gabc1234`) — see `update_checker.py`'s module docstring for exactly how that's compared
against real release tags.

A checkout needs its full tag history for this to resolve correctly — a shallow clone (or a tarball
with no `.git/` at all) falls back to `[tool.setuptools_scm]`'s `fallback_version`, a placeholder
that's never a real shipped version. `.github/workflows/tests.yml` and `build.yml` both pass
`fetch-depth: 0` to `actions/checkout` for exactly this reason; do the same in any new workflow that
installs this package. The wheel that goes into the image is built inside `build-org-image` and is never published, attached to a
release or uploaded to R2: the image is the only distribution of the `privacyfence` package
([ADR 0309](adr/0309-the-organization-image-is-the-only-distribution.md)).

## Release notes come from CHANGELOG.md

A stable tag's GitHub Release body is `CHANGELOG.md`'s section for that version, not GitHub's
"generate release notes" button. `build.yml`'s `finalize-release` job — the same job that attaches
every build's artifacts to the release, see "Packaged-artifact release gating" below — runs
`scripts/changelog_section.py <version>` once, after `needs:` has already proven `build-org-image`
and `sbom` succeeded, and hands the result to
`softprops/action-gh-release` as `body_path:` in that same single call that attaches the files.

That makes the notes a pull-request deliverable rather than a tag-day one, and it puts one
requirement on the PR that cuts a release: **`CHANGELOG.md` must end up with exactly one
`## [X.Y.Z] — YYYY-MM-DD` heading for the version being tagged, a fresh empty `## [Unreleased]`
above it, and the two link definitions at the bottom updated — before tagging.**

Usually that means renaming `## [Unreleased]`. **Check first whether a section for that version
already exists**, because renaming on top of one produces a *second* `## [X.Y.Z]` rather than the
first. If a section was opened early, the release PR merges `[Unreleased]`'s entries into it and
corrects its date instead of renaming anything.

A stable tag with no matching section fails the release build at the render step, which is
deliberate: `action-gh-release` silently keeps the release's existing body when `body_path` can't be
read, so failing loudly is the only way not to ship the auto-generated pull-request wall by
accident. A *duplicated* section fails the same way and for the same reason: otherwise only one half of it
would ship.

**A still-populated `## [Unreleased]` fails the render too**, and that is the half the duplicate
guard does not catch: do only the "correct its date" part of the step above and you are left with
exactly one, correct `## [X.Y.Z]` heading and every entry from the cycle stranded above it, which
would ship green with none of those entries in the notes (see
[ADR 0023](adr/0023-changelog-is-the-only-source-of-release-notes.md)). Merging `[Unreleased]` is therefore
not housekeeping to do eventually — it is what makes the release notes the release notes.
`changelog_section.py --allow-unreleased` renders anyway, for reading a section by hand mid-cycle;
nothing in `.github/workflows/` passes it, and a release build must not.

Feature branches add under `## [Unreleased]` and never open a concrete version heading — that is
the same `d929510` failure mode described above, in a different file.

Pre-release tags (`aN`/`bN`/`rcN`) get no section of their own: per Keep a Changelog they fold into
the version they lead to, which is why `finalize-release` only renders a body on the stable
channel. Their release entries keep whatever body GitHub generated.

`changelog_section.py` reads a version *out of* the changelog and never determines one —
`setuptools_scm` remains the only version source, and nothing may parse `CHANGELOG.md` to find out
what is being built.

The wheel also carries `CHANGELOG.md` (`setup.py` copies it into the built package; see
[ADR 0237](adr/0237-the-wheel-bundles-the-changelog-through-a-setup-py-build-hook.md)), so the
section a release PR writes is what organization users read on the What's new page after upgrading
([ADR 0238](adr/0238-org-mode-shows-each-person-the-changes-since-the-version-they-last-saw.md)).
A pre-release shows its `[Unreleased]` entries there.

## What CI builds

`build.yml` runs on a `v*` tag push and on `workflow_dispatch` (every upload, publish and release
step is gated on a tag ref, so a dispatch against an untagged commit is the pre-flight described
above). A release builds three things: the organization image, the SBOM, and, from
`publish-pypi.yml`, `privacyfence-sync` ([ADR 0306](adr/0306-releases-build-the-org-image-privacyfence-sync-and-the-sbom-only.md)). Each job first runs
`scripts/r2_release.py check-tag` ([ADR 0022](adr/0022-one-release-tag-per-commit.md)).

| Job | Runner | Builds | Tests |
|---|---|---|---|
| `build-org-image` | `ubuntu-24.04`, `ubuntu-24.04-arm` | the organization image per architecture, saved as a workflow artifact | the image's own smoke tests, before anything is uploaded |
| `sbom` | `ubuntu-latest` | CycloneDX SBOM for the Python lock file | — |
| `finalize-release` | `ubuntu-latest` | needs both; attaches files, renders `CHANGELOG.md` notes, uploads the org admin tools to R2 | — |

`publish-org-image.yml` pushes the tested image bytes on a stable tag, and `publish-pypi.yml`
publishes `privacyfence-sync`. All tests live in `tests/integration/`. Layer definitions are in
[`testing-policy.md`](testing-policy.md#the-seven-layers).

## The Glama listing

Glama starts `python -m privacyfence.catalog_server` to read PrivacyFence's tool list. That module
is a catalog, not a way to run PrivacyFence: it lists every tool from the same definitions the
daemon's `/mcp` endpoint serves, holds no credentials, does no I/O beyond stdio, and answers every
tool call with an error that says to install PrivacyFence
([ADR 0114](adr/0114-the-glama-listing-runs-a-tool-catalog-not-a-hosted-privacyfence.md)). No
console script is installed for it.

The listing's build spec is set in the listing's own settings on Glama, not in this repository. It
installs the catalog server from this repository's tag, not from PyPI, so a change to the catalog
reaches Glama only when the listing is rebuilt from a new tag. The command arguments are
`["mcp-proxy", "--", "/opt/privacyfence/bin/python", "-m", "privacyfence.catalog_server"]`, with no
environment variables. `uv` and `mcp-proxy` come from Glama's build image, not from this repository.

## Packaged-artifact release gating

The organization image is started and exercised, automatically, before it (or
anything else from the same tag) ships. Which test covers it is
[`testing-policy.md`'s layer 6](testing-policy.md#layer-6-packaged-artifact); the
process guarantees are:

- **Within a build job:** `build-org-image` runs its `pytest.mark.packaged` tests (the image test and
  the Ubuntu release smoke test) as ordinary steps, right after building the image and before its
  own R2 and workflow-artifact uploads. A failed step stops the job there; no `needs:` involved.
- **The GitHub Release is all-or-nothing:** `build-org-image` and `sbom` only upload workflow
  artifacts. `finalize-release` (`needs: [sbom, build-org-image]`) downloads them and makes the one
  `softprops/action-gh-release` call, so a release gets the complete file set or is not touched at all.
- **One run per tag push:** GitHub occasionally starts a workflow twice for one push, and two
  `build.yml` runs for one tag cannot both succeed. Each workflow's first job, `dedupe`, cancels
  the newer of two runs for the same push (`scripts/release_run_guard.py`, see
  [ADR 0111](adr/0111-a-duplicate-release-run-cancels-itself.md)).
- **The sync publish waits for `build.yml`:** GitHub Actions has no `needs:` across workflow files, so
  `publish-pypi.yml`'s `wait_for_build` job polls for `build.yml`'s run on the same commit
  and fails, publishing nothing, unless it succeeded. It is keyed on commit SHA, not run recency,
  so a `workflow_dispatch` rerun after a fixed `build.yml` finds the new result immediately (see
  that job's own comment).

## Publishing privacyfence-sync to PyPI

`publish-pypi.yml` builds `privacyfence-sync` (`python -m build --sdist --wheel sync`, with the
same version as the root tag) and publishes it to **TestPyPI first, then PyPI**, gated in that order
(`publish-pypi-sync`'s `needs: publish-testpypi-sync`) — a broken publish never reaches the real
index. The `privacyfence` package itself is not published: its wheel is built only inside the image
build and never leaves it ([ADR 0309](adr/0309-the-organization-image-is-the-only-distribution.md)).
The workflow keeps its name because the Trusted Publisher entries below are bound to the workflow
file name.

It authenticates with neither index via a stored API token: both use PyPI's OIDC
**Trusted Publisher** mechanism (`pypa/gh-action-pypi-publish`, `permissions: id-token: write`),
so GitHub mints a short-lived token for the job and PyPI/TestPyPI trade it for a one-shot upload
credential themselves. There is no long-lived secret in this repo for either index.

Before the workflow can publish for the first time, register it as a Trusted Publisher on **both**
services — the project need not already exist there; both accept a "pending" publisher for a name
that isn't claimed yet, and claim it on the first successful publish. Do this once per service:

1. Sign in and go to `test.pypi.org/manage/account/publishing/` (repeat later, separately, on
   `pypi.org/manage/account/publishing/` — the two are unrelated accounts/registrations even if you
   use the same login for both).
2. Add a pending publisher with:
   - **PyPI Project Name**: `privacyfence-sync`
   - **Owner**: `privacyfence`
   - **Repository name**: `privacyfence`
   - **Workflow name**: `publish-pypi.yml`
   - **Environment name**: `testpypi` (on TestPyPI) / `pypi` (on PyPI) — matches the `environment:`
     each job in `publish-pypi.yml` declares. Scoping the publisher to an environment means the
     minted OIDC token is only ever valid for that job, not any other job in this repo.

Optionally, also create matching GitHub Environments (repo **Settings → Environments**) named
`testpypi` and `pypi`. This isn't required for the OIDC exchange itself, but it's where you'd add a
**required reviewer** on the `pypi` environment if you want a manual go/no-go checkpoint between the
TestPyPI publish succeeding and the real PyPI publish running — the "test first" step the workflow
already enforces via job ordering, made into an explicit approval gate rather than just a rerun-only
safety net.

`workflow_dispatch` exists for rerunning by hand (e.g. after a transient failure) — point it at a
tagged commit. Run from an untagged commit and `setuptools_scm` produces a dev version with a local
segment (`+g<sha>`), which both indexes reject as an upload.

**Only a stable tag reaches PyPI/TestPyPI.** `publish-testpypi-sync` (and, transitively,
`publish-pypi-sync`, which depends on it) is gated on the stable channel — a pre-release tag
(`a`/`b`/`rc` suffix) builds `privacyfence-sync` but publishes it nowhere.

## Publishing the n8n node to npm

`publish-n8n-node.yml` publishes the `n8n/` package (`n8n-nodes-privacyfence`) to npm with a
provenance statement. It starts on the same `v*` tag push as `build.yml` and `publish-pypi.yml`,
and its `wait_for_build` job blocks the publish on `build.yml`'s run for that commit succeeding.
Only a **stable** tag publishes: the `publish` job runs `scripts/r2_release.py channel` on the
version and stops unless it prints `stable`, so `a`/`b`/`rc` tags publish nothing. Every stable
tag publishes a new package version, also when `n8n/` did not change.

The job runs in the GitHub environment `npm` (with `id-token: write`). It installs npm 11.21.2,
runs `npm ci`, sets the package version from the tag, runs `npm run lint` and `npm run build`,
and then runs `npm publish --provenance --access public`. A rerun skips the publish when
`npm view n8n-nodes-privacyfence@<version> version` already prints that version, so the workflow
can be run again after a partial failure. `workflow_dispatch` takes one input, `dry_run`
(default `true`), which adds `--dry-run` to the publish; dispatch it on a tag ref. The workflow
never uses `n8n-node release`: that command bumps the version, commits and tags.

**Authentication.** A package must exist on npm before a trusted publisher can be added to it, so
the first publish uses a token: an automation token from npm, saved as the secret `NPM_TOKEN` of
the `npm` environment. When `NPM_TOKEN` is set, the workflow writes it to the npm config; when it
is unset, npm authenticates with GitHub's OIDC token (trusted publishing).

**First release of the n8n node.** After the first stable tag that publishes it, open
https://www.npmjs.com/package/n8n-nodes-privacyfence and check the version and the Provenance
badge. Then, on the package's Settings page, add the trusted publisher (owner `privacyfence`,
repository `privacyfence`, workflow `publish-n8n-node.yml`, environment `npm`), and delete the
`NPM_TOKEN` environment secret and the token on npm.

## Cloudflare R2 release archive

Every tag push — stable and pre-release alike — also uploads that release's artifacts to the
private Cloudflare R2 bucket `privacyfence-releases`, under `releases/<channel>/<version>/`. R2 keeps
the uploads and no manifest or `latest` pointer; it is the one archive that has *everything*, and the
GitHub Release carries a stable tag's files only.
The facts live in [`release-archive.md`](release-archive.md):

- layout, channels, which job uploads what, and what the private bucket does and does not buy —
  [Release archive (R2)](release-archive.md#release-archive-r2);
- `scripts/r2_release.py`'s subcommands (`channel`, `check-tag`, `upload`), which every upload step
  calls — same section;
- the secrets and variables (`CF_RELEASES_R2_ACCESS_KEY_ID`, `CF_RELEASES_R2_SECRET_ACCESS_KEY`,
  and the `CF_RELEASES_R2_ENDPOINT` variable) and the rule that every Cloudflare token is
  **account-owned**, never minted from a personal profile —
  [Credentials](release-archive.md#credentials).

The process rule that belongs here: a pre-release tag's GitHub Release entry (still created, marked
prerelease, because `update_checker.py`'s beta channel reads exactly that flag) carries no files;
its artifacts are only in the archive, which is private
([ADR 0305](adr/0305-the-download-page-and-worker-are-retired.md)).
