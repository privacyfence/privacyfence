---
name: steward
description: PrivacyFence-specific policy for an agent session — container facts that matter here, which pull requests not to follow, and the repo conventions that are easy to get wrong. The generic policy (dispatching to CI instead of skipping, how to treat a red check) is the devflow `pr-steward` skill; the table of dispatchable workflows is `ci.dispatchable` in `.claude/toolkit.yaml`. Read before acting on a CI failure or a review comment.
---

# Stewarding a PrivacyFence pull request

This is repo-specific policy and sits on top of the `pr-steward` skill. It sits alongside
`CONTRIBUTING.md` and `docs/releasing.md` (branch hygiene and release mechanics),
`docs/coding-and-testing-guidelines.md` (code and test conventions, and §2.7's definition of done) and
`docs/testing-policy.md` (which tier runs where) — none of which it replaces.

## Why live connector checks leave this machine

`docs/testing-policy.md` is explicit that the real QA OAuth grants never reach a GitHub-hosted runner
or a `pull_request`-triggered workflow, and a cloud session is neither trusted nor persistent enough to
be an exception. Hence `qa-record-fixture.yml` and `connector-live-check.yml` in `ci.dispatchable`.

- **`qa-record-fixture.yml` and `connector-live-check.yml` share a concurrency group**
  (`group: connector-live-check`, `cancel-in-progress: false`). They copy the runner's single
  persistent credential store into their checkout and copy refreshed tokens back out, so overlapping
  runs could write a stale token over a fresh one. A queued run is correct behaviour, not a hang.
- **A recorded fixture is still a diff to read.** `docs/connector-qa.md` ("Reviewing recorded
  fixtures") says to review fixture diffs before committing them regardless of how they were produced:
  no real account identifier, tenant URL, token or private content may enter the repository.
- **`.github/workflows/qa-record-fixture.yml`'s own header** documents the "workflow must already be on
  `main`" limit.
- Cross-platform `pytest` is not dispatched: `tests.yml` already runs `platform-windows` and
  `platform-macos` on every PR; read the failing job's log instead of guessing.
- To cut a release tag, see `/cut-release`; cutting for real is the maintainer's decision.

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
- `scripts/check_graphical_session_coverage.py` states the rule: *"A second red run on the same commit
  is real and must not be re-run away."*

## Conventions worth restating because they are easy to get wrong

- **Never open a concrete `## [X.Y.Z]` heading in `CHANGELOG.md` on a feature branch.** Entries go under
  `## [Unreleased]`. `docs/releasing.md` traces this to a real incident (`d929510`) and the release
  build fails loudly on a duplicated or still-populated section.
- **PRs merge with a real merge commit, not a squash.** Every commit message on the branch survives into
  `main`'s history individually.
- **Branch names.** `CONTRIBUTING.md` specifies `<type>/<kebab-case-description>` (`feature/`, `fix/`,
  `chore/`, `tests/` — never `feat/`). A Claude Code on the web session is assigned a
  `claude/<generated-name>` branch it cannot rename; use it and put the `<type>` in the PR title.
- **`releases/*` is protected like `main`.** If work is targeting one, branch from it and PR back into it.
- **Pushing a release tag directly is not possible here** — the container can push branches but not
  `refs/tags/*`. A failed push can leave a local tag behind that makes `tag_release.py`'s later checks
  lie. Dispatch `release.yml` (dry run first) instead.
