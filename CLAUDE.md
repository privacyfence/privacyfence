# CLAUDE.md

Process notes for working on PrivacyFence with Claude Code. For code/test conventions see
[`docs/coding-and-testing-guidelines.md`](docs/coding-and-testing-guidelines.md); for contribution
process (forking, issues, license) see [`CONTRIBUTING.md`](CONTRIBUTING.md). This file covers the
parts of the workflow that live only in git history, not in a doc — release mechanics and branch
hygiene.

## Releasing

There is no version string in the source tree and no version-bump commit. `pyproject.toml` declares
`dynamic = ["version"]`; the real version is derived from git tags by `setuptools_scm`
(`[tool.setuptools_scm]` in `pyproject.toml`), and `src/privacyfence/__init__.py` reads it back at
import time via `importlib.metadata.version("privacyfence")`. This replaced the old two-file
hand-bumped scheme (`pyproject.toml`'s `project.version` + `__init__.py`'s `__version__`, kept in
sync by a dedicated `Bump to vX.Y.Z` commit) specifically to avoid that scheme's failure mode:
parallel branches (see worktrees below) both claiming the same next version, one bump commit landing
after another release already took that number (see `d929510`, "Revert version bump — will release
together with other pending CRs", from back when that was still how it worked).

**Cutting a release is a tag, not a commit.** Once `main` is at the commit you want to release, tag
it and push the tag:

```
git tag v4.0.0            # stable
git tag v4.0.0a13          # pre-release: a=alpha, b=beta, rc=release-candidate (PEP 440 short form)
git push origin <tag>
```

`scripts/tag_release.py <version> [--push]` does the same thing with the checks this step otherwise
has no gate for (clean tree, at `origin/main`'s tip, no second tag on the commit, PEP 440 short
form, sequential with no gaps) — see its own docstring.

**Or cut it from the Actions tab.** `.github/workflows/release.yml` (`workflow_dispatch`, inputs
`version` and `dry_run`) runs `r2_release.py channel`, then `changelog_section.py` on the stable
channel, then `tag_release.py`, against `main`'s tip on a runner. `dry_run` **defaults to true**:
the default dispatch runs every check and creates the tag on the runner without pushing it, which
is also the only way to read the release notes that would ship without shipping them. It exists
because a release should not depend on which machine you are sitting at — in particular, a Claude
Code on the web container can push branches but not `refs/tags/*`.

It needs one secret, `RELEASE_TAG_TOKEN`, and **that secret may not be the `GITHUB_TOKEN`**:
GitHub does not start a workflow run for an event raised by the `GITHUB_TOKEN`, so a tag pushed
with it creates the tag, starts neither `build.yml` nor `publish-pypi.yml`, and reports success.
No artifacts, no R2 upload, no GitHub Release, no error. Use a fine-grained PAT with **Contents:
write** on this repo. A GitHub App installation token minted per run would be the account-owned
alternative — the same reasoning the R2 credentials get below, a user token stops working when
that user's access changes — but `release.yml` reads only `secrets.RELEASE_TAG_TOKEN` and has no
step that mints one, so switching to an App is a workflow change, not a secret swap. The workflow
refuses to start without the secret, and after pushing a tag it polls for the `build.yml` run on
that commit and fails loudly if none appears, so the silent-failure mode above cannot pass for a
successful release. See [ADR 0021](docs/adr/0021-release-tag-push-never-uses-github-token.md).

**Pre-flight `build.yml` itself before tagging — `release.yml`'s dry run does not cover this.**
`release.yml` never installs the package or resolves a version through `setuptools_scm`, so its
`dry_run` only proves the tag/changelog bookkeeping (`r2_release.py channel`,
`changelog_section.py`, `tag_release.py`'s sequencing guards) — it touches no packaged artifact.
Every `pytest.mark.packaged` test (the macOS DMG/`.pkg`, Windows installer, and `.deb` lifecycle
smoke tests — see `docs/testing-policy.md`'s layer 6) runs only inside `build.yml`'s
`build`/`build-windows`/`build-deb` jobs, which otherwise only trigger on an actual tag push. That
gap is exactly what let `v4.2.0` reach a real tag broken: a `principal_id` regression that only
`pytest.mark.packaged` could catch sat on `main` for a full cycle, and only surfaced once
`build.yml` ran for real against the pushed tag — see `CHANGELOG.md`'s `[4.2.1]` entry and
[ADR 0030](docs/adr/0030-preflight-dispatches-build-yml-before-tagging.md).

Before dispatching `release.yml` — dry run or real — dispatch `build.yml` itself
(`workflow_dispatch`, no inputs) against the exact commit you intend to tag, and confirm `build`,
`build-windows`, `build-deb`, and `sbom` all succeed. This is safe to run against an untagged
commit: every upload/publish/release step in `build.yml` is gated
`if: startsWith(github.ref, 'refs/tags/')`, so the run builds and tests every artifact but uploads
nothing to R2, GitHub Releases, or PyPI — see that job's "Determine version and release channel"
step, which resolves `channel="n/a (not a tag push)"` off a tag. A failure here is fixed on `main`
like any other CI failure, then the pre-flight is re-run, before `release.yml` is dispatched at all.
The `.claude/commands/cut-release.md` command runs this pre-flight automatically.

**One release tag per commit.** `setuptools_scm` resolves the version through `git describe`,
which reports *a* tag on the commit being built rather than specifically the one whose push started
the run — so a commit carrying two release tags builds as whichever one `describe` prefers (the
alphabetically earlier, for two lightweight tags of the same age), not as the tag just pushed. That
is not hypothetical: `v4.1.0a7` was tagged onto the same commit as a stuck `v4.1.0a6` and the whole
run built, signed and tried to publish `4.1.0a6` a second time, which R2's immutability guard
refused ([the run](https://github.com/privacyfence/privacyfence/actions/runs/35388772087)). Every job that resolves a version now runs
`scripts/r2_release.py check-tag` before publishing anything — as the first step in each of
`build.yml`'s jobs, so this fails in the first few seconds instead of after a full artifact set has
been built; `publish-pypi.yml`'s `build` job runs it after building the sdist/wheel. The fix is
still to move the release forward onto a new commit, or to delete the unwanted tag before
retagging. A version that has already published artifacts stays published; cut the next one. See
[ADR 0022](docs/adr/0022-one-release-tag-per-commit.md).

**macOS ships one file.** `scripts/build_dmg.sh` builds the app bundle, the `.mcpb` and (by
calling `scripts/build_pkg.sh`) the `.pkg`, then puts the `.pkg` and the `.mcpb` on the DMG and
nothing else — no app bundle, no `/Applications` symlink. The `.pkg` is never uploaded or attached
on its own; releasing the DMG releases all three. See `scripts/build_dmg.sh`'s own header and
`docs/platform-support.md`.

That tag push is what `.github/workflows/build.yml` **and** `.github/workflows/publish-pypi.yml`
both trigger on (`on: push: tags: ['v*']`) — the former builds and signs the DMG (which carries the
macOS `.pkg` installer and the `.mcpb`; `scripts/build_dmg.sh` builds all three), the Windows
installer and the `.deb` (running each one's own packaged-artifact smoke test, see "Packaged-artifact
release gating" below) and generates the SBOMs, the latter builds the sdist/wheel from the same tag. The two
workflows trigger independently but no longer publish independently: `publish-pypi.yml`'s
`wait_for_build` job blocks every one of its own publish steps on `build.yml`'s run for that same
commit actually succeeding — see "Packaged-artifact release gating" below. Every one of those
artifacts always uploads to the private Cloudflare R2 release archive (see "Cloudflare R2 release
archive" below);
whether it *also* reaches a public GitHub Release / PyPI/TestPyPI depends on the tag's channel
(`a`/`b`/`rc` suffix, or none for stable — same PEP 440 short-form scheme `update_checker.py`'s
beta channel already ranks by): only a stable tag's DMG/SBOMs get attached to a public GitHub
Release and only a stable tag's sdist/wheel reach PyPI/TestPyPI; a pre-release tag still gets a
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
installs this package. `scripts/build_dmg.sh` and `scripts/build_mcpb.sh` both read the resolved
version back via `importlib.metadata.version("privacyfence")`, so they require the package to
already be `pip install -e .`d (both scripts' own prerequisites say so) — same as
`PrivacyFenceApp.spec`'s `VERSION` and `src/privacyfence/__init__.py`'s `__version__` itself.

`mcpb/shim/package.json`'s `version` field is **not** tied to any of this — leave it as
`0.0.0-dev`. The shim carries no protocol version of its own to keep in sync with the daemon's (it
has no tool-schema knowledge at all — see `mcpb/shim/src/index.ts`'s module docstring), so unlike
the original bridge it replaced, there's nothing here for the real version to be injected into at build time. `scripts/
build_mcpb.sh` reads the real version only to stamp the `.mcpb` manifest itself
(`mcpb/manifest.json.tmpl`'s `__VERSION__`), not anything inside the bundled `shim.js`.

### Release notes come from CHANGELOG.md

A stable tag's GitHub Release body is `CHANGELOG.md`'s section for that version, not GitHub's
"generate release notes" button. `build.yml`'s `finalize-release` job — the same job that attaches
every build's artifacts to the release, see "Packaged-artifact release gating" below — runs
`scripts/changelog_section.py <version>` once, after `needs:` has already proven `build`,
`build-windows`, `build-deb`, and `sbom` all succeeded, and hands the result to
`softprops/action-gh-release` as `body_path:` in that same single call that attaches the files.

That makes the notes a pull-request deliverable rather than a tag-day one, and it puts one
requirement on the PR that cuts a release: **`CHANGELOG.md` must end up with exactly one
`## [X.Y.Z] — YYYY-MM-DD` heading for the version being tagged, a fresh empty `## [Unreleased]`
above it, and the two link definitions at the bottom updated — before tagging.**

Usually that means renaming `## [Unreleased]`. **Check first whether a section for that version
already exists**, because renaming on top of one produces a *second* `## [X.Y.Z]` rather than the
first: 4.0.0's section was opened early, while the changelog was being written, so its release PR
must merge `[Unreleased]`'s entries into the existing `## [4.0.0]` section and correct its date
instead of renaming anything.

A stable tag with no matching section fails the release build at the render step, which is
deliberate: `action-gh-release` silently keeps the release's existing body when `body_path` can't be
read, so failing loudly is the only way not to ship the auto-generated pull-request wall by
accident. A *duplicated* section fails the same way and for the same reason — before that guard
existed, `changelog_section.py` matched the first heading and stopped at the next `##`, emitting
whichever half came first, dropping the other, and exiting 0.

**A still-populated `## [Unreleased]` fails the render too**, and that is the half the duplicate
guard does not catch: do only the "correct its date" part of the step above and you are left with
exactly one, correct `## [4.0.0]` heading and every entry from the cycle stranded above it, which
the duplicate check is right not to complain about. Measured against the real file on 2026-09-17,
that shipped a 142-line body containing none of the eSigner, `%LOCALAPPDATA%`,
`privacyfence_status` or `SameSite` entries, green, at exit 0. Merging `[Unreleased]` is therefore
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

### Packaged-artifact release gating

Phase 6.4 of the CI test-suite buildout (see [`docs/testing-policy.md`](docs/testing-policy.md)):
every published DMG/installer/`.deb` is started and exercised, automatically, before it (or
anything else from the same tag) actually ships.

Within `build.yml`, this needs no cross-workflow trickery — each of the `build` (macOS),
`build-windows`, and `build-deb` jobs runs its own packaged-artifact test
(`tests/integration/test_macos_packaged_smoke.py` and, for the `.pkg` inside that same DMG,
`test_macos_pkg_smoke.py`; `test_windows_packaged_smoke.py`;
`test_deb_packaged_lifecycle.py` — all `pytest.mark.packaged`) as an ordinary step, right after
that job builds its own artifact and before that same job's own R2-upload and
workflow-artifact-upload (`actions/upload-artifact`) steps. An ordinary failed step stops the job
there, so a broken DMG (or the `.pkg` inside it)/installer/`.deb` never reaches its own upload steps — no `needs:`
needed for this part, since it's all sequencing within one job.

The GitHub Release attachment is a separate guarantee, and it *does* need `needs:` (privacyfence/
privacyfence#373): `build`/`build-windows`/`build-deb`/`sbom` each only upload their own artifact
as a workflow artifact now, never straight to the release, so a job failing after a sibling has
already succeeded can no longer leave the public GitHub Release with only some of a stable
release's files. `finalize-release` — gated by `needs: [build, build-windows, build-deb, sbom]`,
the same job that promotes R2's `latest.json` — downloads every workflow artifact and makes the
one `softprops/action-gh-release` call itself, after all four jobs have already succeeded. The
GitHub Release ends up exactly as atomic as the R2 promotion: either it gets the complete file set,
or it doesn't get touched at all.

Getting the same guarantee into `publish-pypi.yml` is the part that actually needs wiring: that
workflow's sdist/wheel has no packaged-artifact test of its own to gate on, but a broken macOS/
Windows/Linux build should still block *its* publish too — a release isn't good just because the
one artifact this workflow happens to build is fine. GitHub Actions has no `needs:` across separate
workflow files, so `publish-pypi.yml`'s `wait_for_build` job (its own first job, gating
`publish-testpypi`/`publish-pypi`/`publish-r2`) polls the REST API for `build.yml`'s own run against
the exact same commit and fails loudly, without publishing anything, unless that run completed
successfully. See that job's own comment for the reasoning, including how a `workflow_dispatch`
rerun is gated the same way (keyed on commit SHA, not run recency, so a rerun after a fixed and
re-run `build.yml` finds the new result immediately).

### Publishing to PyPI

`publish-pypi.yml` builds the sdist/wheel and publishes to **TestPyPI first, then PyPI**, gated in
that order (`publish-pypi` job's `needs: publish-testpypi`) — a broken publish never reaches the
real index. It authenticates with neither project via a stored API token: both use PyPI's OIDC
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
   - **PyPI Project Name**: `privacyfence`
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

**Only a stable tag reaches PyPI/TestPyPI.** `publish-testpypi` (and, transitively, `publish-pypi`,
which depends on it) is gated on `needs.build.outputs.channel == 'stable'` — a pre-release tag
(`a`/`b`/`rc` suffix) still builds the sdist/wheel, but the `build` job's `publish-r2` sibling is
the only place it's published; see below.

### Cloudflare R2 release archive

Every tag push — stable and pre-release alike — additionally uploads that release's artifacts to
Cloudflare R2 (bucket `privacyfence-releases`), laid out as `releases/<channel>/<version>/...`:
the DMG, and the two org-config build scripts (`build.yml`'s `build` job), both SBOMs (`build.yml`'s
`sbom` job), and the sdist/wheel (`publish-pypi.yml`'s `publish-r2` job). `<version>` is the
resolved `major.minor.patch[a|b|rc<n>]` string (never the `v`-prefixed tag itself); `<channel>` is
`stable`, `alpha`, `beta`, or `rc`, derived from that suffix — see `scripts/r2_release.py`, which
every one of those upload steps calls (`channel` subcommand to resolve the directory, `upload` to
actually push files). That script is also where the R2 credential wiring lives now; it replaced
`.github/workflows/r2-smoke-test.yml`, a one-off workflow that proved the GitHub Actions → R2
credentials/endpoint plumbing worked and was deleted once `r2_release.py` existed to reuse it.

R2 is the one archive that has *everything*, regardless of what's also public elsewhere — stable
artifacts land here too, even though they're also on PyPI/GitHub Releases. The bucket itself is
left at Cloudflare R2's default (private — no public bucket policy or custom domain configured by
anything in this repo). **Be exact about what that privacy buys, because this section used to claim
more than it delivers:**

- **It does** make the download Worker the only public path to any artifact. Nothing can enumerate
  the bucket, hotlink an object, or fetch a release without passing through the route that counts
  it — which is what makes the download KPI a full count rather than a partial sample.
- **It does not** restrict *who* may download a pre-release. `cloudflare/downloads/src/index.ts`
  serves `/download/<channel>/<artifact>` for every channel it knows, unauthenticated, and
  `/api/releases` lists them all. A private bucket behind a public Worker is a private bucket with
  a public door.

Concretely, this means:

- **Stable**: reaches PyPI/TestPyPI (see above) and gets a public GitHub Release with the DMG,
  org-config scripts, and SBOMs attached, exactly as before — R2 is an additional private
  mirror, not stable's only distribution point.
- **Alpha / beta / rc**: never reach PyPI/TestPyPI, and their GitHub Release entry (still created,
  marked prerelease, so `update_checker.py`'s beta channel — which reads exactly that flag off the
  releases list — keeps working) carries no file attachments. The actual DMG/SBOMs/sdist/wheel
  are stored only in R2 — reachable through the Worker's own download routes, but listed on no public
  index other than `privacyfence.eu/download/` itself.

Required secrets/vars (Settings → Secrets and variables → Actions), named for what they're for —
the release archive's R2 credentials, distinct from the download Worker's own deploy credentials
(see [`docs/downloads-and-release-kpi.md`](docs/downloads-and-release-kpi.md)'s Credentials
section for those):

- `CF_RELEASES_R2_ACCESS_KEY_ID` / `CF_RELEASES_R2_SECRET_ACCESS_KEY` (secrets) — an R2 API token
  scoped to the `privacyfence-releases` bucket. Mint it as an **account-owned** token (Manage
  Account → Account API Tokens), not from a personal profile: this credential publishes every
  release, and a user token stops working when that user's access changes. Same rule as the
  downloads Worker's own token — see
  [`docs/downloads-and-release-kpi.md`](docs/downloads-and-release-kpi.md)'s Credentials section.
- `CF_RELEASES_R2_ENDPOINT` (repo/environment **variable**, not a secret — `build.yml` and
  `publish-pypi.yml` read it as `vars.`) — the bucket's S3-compatible endpoint URL.

### Who can download a pre-release

Anyone, through the Worker — decided 2026-09-13, and deliberately kept. A tester needs no
credential, just the link or the "Want to test the next version?" section on
`privacyfence.eu/download/`. Every pre-release download still goes through the Worker, so it is
counted, and R2 stays unreachable except through it. The reasoning, and the exact steps to reverse
it (Worker routes and website together, never one without the other), are in
[ADR 0024](docs/adr/0024-pre-releases-are-publicly-downloadable.md).

## Decisions, plans and ADRs

Three kinds of document, three lifecycles — the full rules, template and index are in
[`docs/adr/README.md`](docs/adr/README.md):

- **Plans** (what we are about to do) are temporary: a GitHub issue, or a `docs/*-plan.md` while its
  work is open. They are deleted when the work lands.
- **ADRs** (`docs/adr/NNNN-*.md` — why it is this way, what was rejected) are permanent and frozen
  once accepted. Change your mind with a new ADR that supersedes the old one; never rewrite an
  accepted ADR's body, and never put implementation progress in one.
- **Reference docs** (`docs/*.md`, this file) describe today's behavior and link to ADRs for the
  *why* rather than retelling it.

**Retiring a plan requires extracting its decisions first.** The PR that deletes a plan document
adds or amends an ADR for every decision the plan made — anything hard to reverse, touching a trust
boundary or the release/distribution path, or rejecting an alternative for a non-obvious reason —
or says in its description that the plan made none. Before this rule, a dozen plans were deleted
with their rejected alternatives in them; ADRs 0009–0025 are the backfill.

The same applies when a decision is made somewhere else — a PR thread, an issue, a section of this
file: if it meets the bar above, it gets an ADR in the same PR. ADRs link to issues, PRs, commits
and source files, never to a plan document, which will not outlive it.

## Branching & PRs

- Branch names are `<type>/<kebab-case-description>`. Standard types: `feature/` for new
  functionality, `fix/` for bug fixes, `chore/` for non-functional maintenance, `tests/` for
  test-only changes. Use `feature/`, not `feat/` — a few early branches used `feat/` before this
  was settled; that prefix is retired, don't reintroduce it on new branches.
- `main` is protected — all changes land via PR (`CONTRIBUTING.md`). PRs merge with a real merge
  commit (`Merge pull request #N from <fork>/<branch>`), not squash — keep that in mind when writing
  commit messages on a feature branch, since they survive into `main`'s history individually.
- User-visible changes get a line under `CHANGELOG.md`'s `## [Unreleased]` heading in the same PR.
  Never open a concrete `## [X.Y.Z]` heading on a feature branch — see "Release notes come from
  CHANGELOG.md" above.
- Definition of done for a PR is the checklist in
  [`docs/coding-and-testing-guidelines.md` §2.7](docs/coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo).
- `releases/*` (e.g. `releases/4.1-dev`) is a long-lived, cross-cycle integration branch, cut from
  `main` when a batch of work for the next release needs to accumulate somewhere other than `main`
  while `main` stays frozen for a prior release's remaining blockers (renamed from the original,
  one-off `4.1-dev` for exactly this reuse — the pattern, not just that one branch, is what's
  protected now). It's a deliberate exception to "feature branches go straight to `main`", not a
  new standing convention: branches still fork from and PR into whichever `releases/*` branch is
  current, using the normal `<type>/<kebab-case-description>` naming, and the `releases/*` branch
  itself is deleted once it merges back into `main` in one PR.
- `releases/*` is protected the same way `main` is — same ruleset (PR required, no force-push/
  deletion, the same required status checks `scripts/update_branch_protection.py` manages for
  `main`; run it with `--branch "releases/**"` to sync that ruleset too) and the same CI: every
  `.github/workflows/*.yml` push trigger scoped to `branches: [main]` for a test/audit/lint job
  also lists `"releases/**"`. This deliberately does **not** extend to the production-deploy
  triggers (`pages.yml`'s website deploy, `deploy-download-worker.yml`'s Worker deploy + live D1
  migrations) — those stay `main`-only, so merging into a `releases/*` branch never ships to
  production ahead of that branch's eventual merge into `main`.

## Parallel sessions & worktrees

The user regularly runs multiple Claude Code sessions on this repo at once, each on a different
task/branch. To avoid one session's checkout state (branch switches, uncommitted edits) interfering
with another's:

- Start new work in its own `git worktree` under `~/Coding/worktrees/`, not by switching branches
  in whichever checkout happens to be open. Naming convention already in use:
  `~/Coding/worktrees/privacyfence-<short-branch-slug>` (e.g. `privacyfence-fix-tasks-ssl`).
- Don't reuse an existing worktree for an unrelated task — one worktree per active branch/task.
