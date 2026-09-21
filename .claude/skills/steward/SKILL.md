---
name: steward
description: PrivacyFence repo policy for an agent session — which work has to be dispatched to a GitHub Actions runner instead of run locally, which pull requests to follow, and how to treat a red check. Read this before acting on a CI failure or a review comment.
---

# Stewarding a PrivacyFence pull request

This is repo-specific policy. It sits alongside `CLAUDE.md` (release mechanics and branch
hygiene), `docs/coding-and-testing-guidelines.md` (code and test conventions, and §2.7's
definition of done) and `docs/testing-policy.md` (which tier runs where) — none of which it
replaces. What it adds is the part none of them covers: what a session should do when the box it
is running on cannot do the thing being asked.

## Work that has to leave this machine

A Claude Code on the web container has no macOS, no Windows, no code-signing identity, and — by
deliberate policy, not by oversight — no live connector credentials. `docs/testing-policy.md` is
explicit that the real QA OAuth grants never reach a GitHub-hosted runner or a
`pull_request`-triggered workflow, and a cloud session is neither trusted nor persistent enough to
be an exception.

None of that is a reason to skip a checklist row. Every one of those jobs already exists as a
workflow with `workflow_dispatch` declared, and a session can dispatch it **against its own
branch** and read the result back.

| You need | Dispatch | Notes |
|---|---|---|
| A fixture for a newly added connector | `qa-record-fixture.yml` | Takes a `connector` input naming one of `CONNECTOR_CHECKS` in `scripts/qa_fixture_recorder.py`. Runs on the self-hosted runner and **commits the recorded fixture back to the branch it was dispatched against** — pull before you continue working. |
| `qa_fixture_recorder.py --check` for §2.7's QA row | `connector-live-check.yml` | No inputs. Records only as drift remediation, and runs `--lifecycle` for write-capable providers. The report lands as the `connector-live-check-report` artifact; link the run in the PR rather than pasting nothing. |
| Windows autostart / Task Scheduler behaviour | `windows-graphical-session.yml` | No inputs. |
| Linux systemd `--user` / XDG autostart behaviour | `linux-graphical-session.yml` | No inputs. |
| macOS `LaunchAgent` autostart, `.pkg` install | `macos-graphical-session.yml` | No inputs. |
| A real DMG, `.pkg`, `.mcpb`, Windows installer or `.deb` | `build.yml` | No inputs. Each platform job runs its own packaged-artifact smoke test as an ordinary step before any upload, so a green job means the artifact actually starts. |
| Cross-platform `pytest` | — | Don't dispatch. `tests.yml` already runs `platform-windows` and `platform-macos` on every PR; read the failing job's log instead of guessing. |
| To cut a release tag | `release.yml` | Takes `version` and `dry_run`. **`dry_run` defaults to true** — dispatch it that way first; it runs every check and creates the tag on the runner without pushing. Only cut for real when the maintainer has seen the dry run and said to. See `/cut-release`. |

Three things about dispatching, so they are not rediscovered at runtime:

- **The workflow must already be on `main`.** GitHub only offers `workflow_dispatch` for workflows
  present on the default branch; the `ref` you pass selects which checkout runs. A workflow added
  on a feature branch is not dispatchable until that branch merges —
  `.github/workflows/qa-record-fixture.yml`'s own header documents hitting exactly this.
- **`qa-record-fixture.yml` and `connector-live-check.yml` share a concurrency group**
  (`group: connector-live-check`, `cancel-in-progress: false`). They copy the runner's single
  persistent credential store into their checkout and copy refreshed tokens back out, so
  overlapping runs could write a stale token over a fresh one. A queued run is correct behaviour,
  not a hang. Wait; do not re-dispatch.
- **A recorded fixture is still a diff to read.** `docs/qa-environment-setup.md` says to review
  fixture diffs before committing them regardless of how they were produced. A fixture that
  arrived by dispatch gets the same read: no real account identifier, tenant URL, token or private
  content may enter the repository.

Two things that look local-only but may not be, in this container specifically:

- `scripts/qa_web_smoke.py` needs a real browser, which is why `docs/testing-policy.md` §2.2 lists
  it as local-only. The web container ships Chromium and Playwright already (see the
  `PLAYWRIGHT_BROWSERS_PATH` note in `.claude/hooks/session-start.sh`), so try it before declaring
  it impossible.
- Pushing a release tag directly is **not** possible here — the container can push branches but not
  `refs/tags/*`. Don't try; a failed push can leave a local tag behind that makes
  `tag_release.py`'s later checks lie. Dispatch `release.yml` instead (table above), and note that
  cutting a release for real is the maintainer's decision, never a session's: dispatch the dry run,
  report it, and wait to be told.

## Which pull requests to follow

- **A PR this session opened is this session's to drive to green.** Subscribe to it, and keep
  working it until CI passes and it is mergeable. That is the default and this file does not
  soften it.
- **Do not follow Dependabot PRs or the fixture-drift PR** that `connector-live-check.yml` opens,
  unless the maintainer asks. Those exist to be looked at by a person: a dependency bump is a
  supply-chain decision, and a fixture diff is a privacy review.
- **Stop at merge or close.** Nothing further is owed.

## A red check is real until proven otherwise

`scripts/check_graphical_session_coverage.py` already states the rule this repo runs on, for the
release gate: *"A second red run on the same commit is real and must not be re-run away."* Apply
it to every check, not just that one.

In particular:

- Never skip, disable, `xfail` or quarantine a test to get to green. The suite is a 100%-pass,
  ratcheted-coverage gate (`scripts/check_coverage_floor.py`) and a hole in it does not show up
  again until it matters.
- Never push an empty commit, or close and reopen a PR, to kick CI.
- A coverage-floor failure means the new code needs tests, not that the floor needs lowering.
- A `bandit` finding that is genuinely a false positive gets `# nosec BXXX  # <reason>` at the
  call site — never a suppression in `pyproject.toml` (§2.7).
- A missing QA credential is **not** a test failure. It is the dispatch table above.

## Conventions worth restating because they are easy to get wrong

- **Never open a concrete `## [X.Y.Z]` heading in `CHANGELOG.md` on a feature branch.** Entries go
  under `## [Unreleased]`. `CLAUDE.md` traces this to a real incident (`d929510`) and the release
  build fails loudly on a duplicated or still-populated section.
- **PRs merge with a real merge commit, not a squash.** Every commit message on the branch
  survives into `main`'s history individually, so write each one for that audience.
- **Branch names.** `CLAUDE.md` specifies `<type>/<kebab-case-description>` (`feature/`, `fix/`,
  `chore/`, `tests/` — never `feat/`). A Claude Code on the web session is assigned a
  `claude/<generated-name>` branch it cannot rename or push around, so that convention cannot be
  met from here; use the assigned branch and put the `<type>` in the PR title instead. Apply the
  convention normally anywhere it can be followed.
- **`releases/*` is protected like `main`.** If work is targeting one, branch from it and PR back
  into it — not into `main`.
