# Testing policy

What runs where, and when. How to *write* a test is in
[`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md); the checks a human runs
before a release are in [`release-testing.md`](release-testing.md).

## Principle

**Do not test the full Cartesian product** (`OS × connector × operation × gate × browser × package
type`). Each dimension is proven on its own, in one layer below — gate logic in layers 1–2, OS
portability in layer 3, browser behavior in layer 4, providers in layer 5, packages in layer 6 —
and only a few high-value end-to-end tests cross a boundary.

## The seven layers

Every check is exactly one layer. `pyproject.toml`'s `[tool.pytest.ini_options]` registers the markers
below; a new test module carries the marker for its layer.

| # | Layer | What it proves | Marker | Where it runs |
|---|---|---|---|---|
| 1 | Unit | Python logic in isolation, offline | `unit` | `tests.yml`, every PR |
| 2 | Integration | Real internal stack (sockets, servers, the daemon process), no external network | `integration` | `tests.yml`, every PR |
| 3 | Cross-platform system | OS path/process/locking/daemon behavior; the full daemon → MCP → approval → audit scenario | `system`, `platform` | `tests.yml`, every PR, on Linux, Windows and macOS |
| 4 | Browser system | JS, CSP and rendering in real Chromium | `browser` | `tests.yml`, every PR; `qa_web_smoke.py` by hand |
| 5 | Live connector | Provider API drift | `live` | `connector-live-check.yml`, self-hosted runner, weekly |
| 6 | Packaged artifact | Installer/package correctness, autostart, upgrade, uninstall | `packaged` | `build.yml` and the graphical-session workflows |
| 7 | Manual | Subjective judgment, first-time consent, OS-native presentation | — | [`release-testing.md`](release-testing.md) |

Markers are applied to new modules, not backfilled across old ones. Today `system` is on
`tests/system/test_local_mode_system.py` and `test_org_ubuntu_release_smoke.py`; `platform` on every
module under `tests/platform/`; `packaged` on every module in layer 6; `browser` on
`test_download_page.py` (not yet on `test_browser_smoke.py`); `live` on nothing, since layer 5 is a
script, not a pytest collection. When a `packaged`- or `system`-marked test fails,
`tests/conftest.py` writes its diagnostics (environment, installed-file manifest, daemon and audit
logs) under `test-results/`, which CI uploads as an artifact of the failed job.

| Failure type | Owning layer |
|---|---|
| Wrong logic or malformed output from a pure function | 1 |
| Broken wiring between daemon, web routes, gate and audit | 2 |
| Works on Linux, breaks on Windows/macOS (paths, locking, process spawning, discovery) | 3 |
| Script order, a CSP directive that silently blocks something, rendering at a viewport or color scheme | 4 |
| A provider renamed a field, removed an endpoint, changed a response shape | 5 |
| Files in the wrong place, autostart missing, uninstall leaves files, upgrade loses state | 6 |
| Contrast/spacing "looks wrong"; a provider's consent screen behaves unexpectedly | 7 |

## Layers 1–4: every PR

`.github/workflows/tests.yml` runs on every pull request, every push to `main` or `releases/**`,
and on dispatch. A 100% pass rate is required to merge.

| Job | Runs |
|---|---|
| `test` (Ubuntu, Python 3.13) | `npm test` and `npm run typecheck` in `mcpb/shim/`; the full `pytest` suite with branch coverage; `scripts/check_coverage_floor.py coverage.json`; uploads `coverage-report` |
| `platform-windows`, `platform-macos` | The full `pytest` suite on `windows-latest`/`macos-latest` |
| `test-python-compat` | The suite on Python 3.11, 3.12 and 3.14, without Node (`--ignore`s `test_shim_mcp_contract.py`); one check per version |
| `org-mode-smoke` | `test_org_ubuntu_release_smoke.py`, with `PRIVACYFENCE_RUN_RELEASE_SMOKE_TESTS=1` (the module skips itself without it) |
| `static-analysis` | `ruff check .`, `bandit -c pyproject.toml -r src`, `scripts/mypy_strict_modules.py` (all blocking); whole-tree `mypy src/privacyfence` (`continue-on-error`, informational) |

`pip install -e ".[test]"` installs everything the suite needs, Playwright included; the `test` job
also runs `playwright install --with-deps chromium`. Browser tests skip when Playwright or Chromium
is missing, and layer-6 tests skip when no built artifact is under `dist/`.

**Coverage is a ratchet.** `check_coverage_floor.py` fails if overall coverage, or any module on its
security-critical list (`MODULE_FLOORS`), drops below its recorded floor. Raise a floor in the PR
that raises its coverage; lowering one is a regression, not a config edit.

**Required checks.** `REQUIRED_STATUS_CHECKS` in `scripts/update_branch_protection.py` is the
reviewed list: `test`, `platform-windows`, `platform-macos`, the three `Test (Python 3.x, core
suite)` checks, `static-analysis`, `org-mode-smoke`, and `website-build` (`website-build.yml`'s
job, which also runs on every PR). Update it in the PR that adds, renames or removes one of those
jobs. It is applied to the live repository ruleset by hand (`... apply`, and
`--branch "releases/**" apply` for the release branches) — no CI job does it; `... show` compares
intent with what is live.

Key modules in the suite:

- `tests/integration/test_mcp_daemon_contract.py` — a real `WebServer` on a loopback socket, driven
  by the official `mcp` client over TCP.
- `tests/integration/test_shim_mcp_contract.py` — the built `mcpb/shim/dist/shim.js` against that
  server, over MCP-over-stdio. Skips without Node.
- `tests/system/test_local_mode_system.py` — `python -m privacyfence.daemon_main` as a separate
  process: discovery, bootstrap into `/approvals` and `/settings`, a gated call allowed and denied
  through the HTTP decide route, the audit log read back, and "Quit PrivacyFence". Runs in every
  full-suite job, so on all three OSes.
- `tests/platform/` — atomic-write concurrency, cross-process single-instance locking, the browser
  launch default, a spawned-daemon lifecycle, and real `icacls` behavior on Windows.
- `tests/integration/test_browser_smoke.py` — real headless Chromium: login, decisions (including
  "Always allow", live SSE refresh, idempotency), PDF preview, CSP, org-mode WebAuthn, PII banners,
  three viewports, light/dark structure. `test_download_page.py` covers `website/download/` with
  the Worker's API stubbed.
- Each connector's `TestLiveFixtureParsing` replays a committed fixture from
  `tests/fixtures/live/<connector>/` through the real parser; it skips if no fixture is recorded.

**What a green `org-mode-smoke` does not prove.** It proves org mode deploys and authenticates:
fail-closed startup, reverse-proxy Host handling, per-principal sessions, MCP OAuth discovery, an
approval audited under the right principal, state surviving a restart. Its `org_config.json` has no
connectors, so it cannot see a principal's tool list or auto-accept rules; those are proven by
`tests/unit/web/test_routes_mcp.py::TestListTools` and
`tests/unit/test_daemon_main.py::TestLoadPrincipalSettings`.

### Other workflows that gate a PR

- **`dependency-audit.yml`** — on a PR or a push to `main`/`releases/**` that touches
  `pyproject.toml`, `requirements/*.lock.txt` or either `package.json`/`package-lock.json`, weekly
  (Monday 07:00 UTC), and on dispatch. `lockfile-freshness` fails if a lock file is out of step with
  `pyproject.toml`; `pip-audit --strict` blocks on any finding in `requirements/runtime.lock.txt`
  and `docs.lock.txt` (the website's docs generator) and reports `dev.lock.txt` informationally; `npm-audit` blocks on high/critical in the shim's
  runtime dependencies (`--omit=dev`); `npm-audit-worker` blocks on high/critical across the whole
  `cloudflare/downloads/` tree, dev dependencies included, because `wrangler` deploys with a
  production token.
- **`deploy-download-worker.yml`'s `verify` job** — on a PR, or a push to `main`, that touches
  `cloudflare/downloads/**`: `npm ci`, `npm run typecheck`, `npm test` (local Miniflare, no
  Cloudflare credentials), `npm run dry-run`. Its `deploy` job needs `verify` and runs only on
  `main` or dispatch. Neither job is in `REQUIRED_STATUS_CHECKS`.
- **`website-build.yml`** — on every PR and every push to `main`/`releases/**`: builds
  privacyfence.eu with `scripts/build_site.py` three ways (the newest stable tag, as `pages.yml`
  would deploy it; `v4.5.0`, which must hit the stale-tag guard; a throwaway local tag on the PR's
  commit, which renders its `/docs/`), then runs the website tests (`tests/unit/test_website_*.py`,
  `test_build_site.py`, and the browser tests `test_website_layout.py`, `test_website_consent.py`
  and `test_download_page.py`) against that last build, `/docs/` pages included. Uploads the built
  sites and the layout screenshots. Not in `REQUIRED_STATUS_CHECKS`.

### `qa_web_smoke.py` (layer 4, by hand)

`scripts/qa_web_smoke.py` drives the real embedded server in real Chromium for what a pytest
assertion cannot judge: a click that really round-trips, script-order bugs, the CSP letting
`resources/sw.js` register, and how it looks in light and dark at phone width. It never runs in CI.
Run it when a PR touches `web_shell.py`, `approval_list_html.py`, the JS emitted by
`web/routes_approvals.py`/`web/routes_settings.py`, `resources/sw.js`, or the CSP in
`web/server.py`:

```bash
.venv/bin/python scripts/qa_web_smoke.py [--chromium-path <binary>] [--report-file <path>]
```

Paste the report into the PR description under `## Web smoke check`.

## Layer 5: live connector

Live provider credentials exist only on the project's self-hosted runner (label
`privacyfence-test`), as local files — never as GitHub Actions secrets, never on a GitHub-hosted
runner, never reachable from `pull_request` ([ADR
0019](adr/0019-live-connector-credentials-only-on-a-self-hosted-runner.md)). Runner and QA-account
setup: [`connector-qa.md`](connector-qa.md).

- **`connector-live-check.yml`** — weekly (Monday 06:00 UTC) and on dispatch. Runs
  `qa_fixture_recorder.py --check` over every connector in `CONNECTOR_CHECKS`; on drift, `--record`
  and a PR on `chore/connector-live-fixture-drift` with the redacted diff, reviewed like any other
  PR. Then `--lifecycle` (create, read, update, delete a tagged QA object in `calendar`,
  `confluence`, `jira`, `tasks`); a lifecycle failure fails the job.
- **`qa-record-fixture.yml`** — dispatch only, input `connector`. Records one connector's fixture
  and commits it to the branch it was dispatched against; refuses a protected branch. Use it for a
  connector with no fixture yet, which the drift-only record step never records. It shares the
  `connector-live-check` concurrency group, so it queues behind a running check.
- **`/qa-record <connector>`** (`.claude/commands/qa-record.md`) — dispatches `qa-record-fixture.yml`
  for the current branch, then pulls and reviews the fixture diff. A connector name missing from
  `CONNECTOR_CHECKS` makes the recorder exit 0 having recorded nothing, so the command checks first.

A PR touching `src/privacyfence/*_client.py` or `src/privacyfence/connectors/**` owes a `--check`
report, scoped to that connector, under a `## Local QA check` heading. Run it on a machine with QA
credentials, or dispatch `connector-live-check.yml`:

```bash
.venv/bin/python scripts/qa_fixture_recorder.py --check confluence    # never writes
.venv/bin/python scripts/qa_fixture_recorder.py --record confluence   # writes tests/fixtures/live/confluence/
```

`--check` reports each fixture's age: under 60 days healthy, 60–90 warning, over 90 refresh
required. After `--record`, read the diff: identity fields must already be placeholders; a real
email, name or account id means the redaction needs fixing before the commit.

## Layer 6: packaged artifact

| Test module | Workflow, job | Covers |
|---|---|---|
| `test_macos_packaged_smoke.py` | `build.yml`, `build` | DMG contents, the app from its `.pkg`, MCP approval round trip, `codesign`/`spctl`, upgrade keeps state |
| `test_macos_pkg_smoke.py` | `build.yml`, `build` | `.pkg` structure, postinstall, OS/arch refusal, signature — no install |
| `test_windows_packaged_smoke.py` | `build.yml`, `build-windows` | Silent install separates, upgrade over a running install, uninstall keeps data, purge, refusal of a user-writable install dir |
| `test_deb_packaged_lifecycle.py` | `build.yml`, `build-deb` | Install, remove, reinstall, purge, upgrade, unattended install, a failing machine half failing the install |
| `test_linux_graphical_session_autostart.py` | `linux-graphical-session.yml` | XDG autostart under a real `systemd --user` session; the OAuth loopback opening a browser under Xvfb |
| `test_windows_graphical_session_autostart.py` | `windows-graphical-session.yml` | Task Scheduler's stored companion task against `tests/windows_task_contract.py`; the companion started as the signed-in user; SCM restart of a killed service |
| `test_macos_graphical_session_autostart.py`, `test_macos_pkg_install.py` | `macos-graphical-session.yml` | `enable` and a real `installer -pkg` bringing up the LaunchDaemon as `_privacyfence` and the companion LaunchAgent |

`build.yml` runs on a `v*` tag push and on dispatch. Each packaged test runs right after its job
builds the artifact and before any upload; `build-deb` also runs `test_org_ubuntu_release_smoke.py`.
Every upload and publish step is gated on a tag ref, so a dispatched run builds and tests everything
and publishes nothing — which is the pre-flight in [`release-testing.md`](release-testing.md).
`publish-pypi.yml`'s `wait_for_build` job blocks every publish until `build.yml`'s run for the same
commit succeeds.

The three graphical-session workflows run on a push to `main` or `releases/**` that touches their
packaging paths, weekly (Monday 07:00, 08:00, 09:00 UTC for Linux, Windows, macOS), and on dispatch —
never on `pull_request`, never as part of `build.yml`. `build.yml`'s `finalize-release` runs
`scripts/check_graphical_session_coverage.py`, which reads the latest completed run of each on the
release branch: a run that is missing, not an ancestor of the tagged commit, or red warns on a
pre-release tag and fails a stable one. It never waits for a new run. They run on GitHub-hosted
runners with one secret, `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`, the shared app identity every
release build already carries ([ADR 0040](adr/0040-telegram-app-credentials-ship-in-every-distribution.md)),
not a per-account credential.

## Layer 7: manual

Manual release checks, and the rule for what may stay manual, are in
[`release-testing.md`](release-testing.md).

## Running the gate yourself

- **`/dod`** (`.claude/commands/dod.md`) — runs the blocking gate of
  [`coding-and-testing-guidelines.md` §2.7](coding-and-testing-guidelines.md#27-definition-of-done-for-a-pr-touching-this-repo)
  (`pytest` with coverage, the coverage floor, `ruff`/`bandit`/`mypy_strict_modules.py`, the shim's
  `npm test`/`typecheck`), then checks the diff for the conditional items (live `--check` report,
  contract tests, lock files, `CHANGELOG.md`) and reports a pass/fail table. Given a path, it narrows
  `pytest` and skips the coverage floor.
- **`scripts/pre_release_check.py`** — the same blocking commands as one script, exiting non-zero on
  any failure.
