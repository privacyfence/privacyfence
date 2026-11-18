---
name: steward
description: PrivacyFence-specific policy for an agent session — container facts that matter here, which pull requests not to follow, and the repo conventions that are easy to get wrong. The generic policy (dispatching to CI instead of skipping, how to treat a red check) is the tld plugin's pr-steward policy; the table of dispatchable workflows is `ci.dispatchable` in `.tld/toolkit.yaml`. Read before acting on a CI failure or a review comment.
---

# Stewarding a PrivacyFence pull request

This is repo-specific policy and sits on top of the tld plugin's pr-steward policy. It sits alongside
`CONTRIBUTING.md` and `docs/releasing.md` (branch hygiene and release mechanics),
`docs/coding-and-testing-guidelines.md` (code and test conventions, and §2.7's definition of done) and
`docs/testing-policy.md` (which tier runs where) — none of which it replaces.

## Why live connector checks leave this machine

`docs/testing-policy.md` is explicit that the real QA OAuth grants never reach a GitHub-hosted runner
or a `pull_request`-triggered workflow, and a cloud session is neither trusted nor persistent enough to
be an exception. Hence `qa-record-fixture.yml` and `connector-live-check.yml` in `ci.dispatchable`.

- **`qa-record-fixture.yml` and `connector-live-check.yml` share a concurrency group**
  (`group: connector-live-check`, `cancel-in-progress: false`). They copy the QA principal's
  tokens from the QA server's data directory into their checkout and write refreshed ones back, so
  overlapping runs could write a stale token over a fresh one. A queued run is correct behaviour, not a hang.
- **A recorded fixture is still a diff to read.** `docs/connector-qa.md` ("Reviewing recorded
  fixtures") says to review fixture diffs before committing them regardless of how they were produced:
  no real account identifier, tenant URL, token or private content may enter the repository.
- **`.github/workflows/qa-record-fixture.yml`'s own header** documents the "workflow must already be on
  `main`" limit.
- To cut a release, see "Cutting a release" below; cutting for real is the maintainer's decision.

## Two things that look local-only but may not be, in this container

- `scripts/qa_web_smoke.py` needs a real browser, which is why `docs/testing-policy.md`
  ("`qa_web_smoke.py` (layer 4, by hand)") lists it as local-only. The web container ships Chromium and
  Playwright already (see the `PLAYWRIGHT_BROWSERS_PATH` note in `.claude/hooks/session-start.sh`), so
  try it before declaring it impossible — with `--chromium-path "$PRIVACYFENCE_TEST_CHROMIUM"` when the
  hook exported that variable, because the container's Chromium is not the build the locked
  `playwright` expects.
- The browser tests themselves (`test_browser_smoke.py`, the website tests) run here too, through the
  same `PRIVACYFENCE_TEST_CHROMIUM` the hook exports. Read the hook's `==>` Chromium line: a `WARNING`
  there means every browser test will report SKIPPED, and a green run is then not a pass.

## Which pull requests not to follow

Do not follow Dependabot PRs or the fixture-drift PR that `connector-live-check.yml` opens, unless the
maintainer asks. A dependency bump is a supply-chain decision, and a fixture diff is a privacy review.

## Red checks, specifically

- The suite is a 100%-pass, ratcheted-coverage gate (`scripts/check_coverage_floor.py`); a hole in it
  does not show up again until it matters. A coverage-floor failure means the new code needs tests, not
  that the floor needs lowering.
- A `bandit` finding that is genuinely a false positive gets `# nosec BXXX  # <reason>` at the call
  site — never a suppression in `pyproject.toml` (§2.7).
- A second red run on the same commit is real and must not be re-run away.

## Conventions worth restating because they are easy to get wrong

- **Never open a concrete `## [X.Y.Z]` heading in `CHANGELOG.md` on a feature branch.** Entries go under
  `## [Unreleased]`. `docs/releasing.md` traces this to a real incident (`d929510`) and the release
  build fails loudly on a duplicated or still-populated section.
- **PRs merge with a real merge commit, not a squash.** Every commit message on the branch survives into
  `main`'s history individually.
- **Branch names.** `CONTRIBUTING.md` specifies `<type>/<kebab-case-description>` (`feature/`, `fix/`,
  `chore/`, `tests/` — never `feat/`). A Claude Code on the web session is assigned a
  `claude/<generated-name>` branch it cannot rename; use it and put the `<type>` in the PR title.
- **Pushing a release tag directly is not possible here** — the container can push branches but not
  `refs/tags/*`. A failed push can leave a local tag behind that makes `tag_release.py`'s later checks
  lie. Cut releases as "Cutting a release" below says.

## Cutting a release

Stable releases (`X.Y.Z`) go through `/tld:release`; pre-releases (`X.Y.ZaN`, `bN`, `rcN`) do not, because `tld ops release-prepare` only knows `X.Y.Z`.

**Stable.**
1. Run `/tld:release`. It opens `chore/release-X-Y-Z` (the `## [X.Y.Z]` heading in `CHANGELOG.md` and `.tld/release-version`). `release-prepare` does not touch the link definitions at the bottom of `CHANGELOG.md`: in a second commit on that branch point `[Unreleased]` at `compare/vX.Y.Z...HEAD` and add `[X.Y.Z]: .../compare/v<previous stable>...vX.Y.Z`. Never edit the version files by hand.
2. The maintainer merges it. Before the playbook's `start` question, run the pre-flight: fetch `origin/main`, dispatch `build.yml` (no inputs) on that exact commit, wait for `build-org-image (amd64)`, `build-org-image (arm64)` and `sbom` to finish, and report the run URL. Any red or missing job stops the release (fix it as an ordinary CI failure on `main`, then run the pre-flight again). `curl -fsSI https://oauth.privacyfence.eu/health` must return 200; a failure does not block the release, but say so. Never skip the pre-flight; `tld-release.yml`'s own dry run installs and builds nothing.
3. Dispatch `tld-release.yml` on `main` with `version: X.Y.Z` and `dry_run`. Always pass `version`: with it empty the workflow raises the newest stable tag by a patch, which is the wrong version for a minor or major release. Never pass `skip_checks`. The owner approves the `tld-release` environment for every run. `release_check.py` fails the run when `.tld/release-version` differs from `version`; if a run was dispatched without `version` and failed that way, the playbook says never re-run, so tell the maintainer to dispatch again by hand with `version` set. `release-prepare` writes the changelog heading as `## [X.Y.Z] - <date>` with a hyphen; that is expected.
4. After a real run, list the `build.yml` and `publish-pypi.yml` runs: a `push` run on the tagged commit must appear for each within 5 minutes. If none does, tell the maintainer at once (the tag exists and the release did not start) and never dispatch again for the same version. Report the tag and both run URLs.

**Pre-release.** Run the pre-flight of step 2, then dispatch `tld-release.yml` on `main` with `version: <the pre-release>` and `dry_run: "true"` and report the run URL. The maintainer starts the real run in GitHub (Actions, tld-release, Run workflow). `.tld/release-version` and `CHANGELOG.md` stay as they are.
