# Platform and CI hardening plan

Plan 3 of 4 made from the whole-repository review of 2026-10-14
([`docs/repo-2026-10-14-review.md` at c8416d94](https://github.com/privacyfence/privacyfence/blob/c8416d94981ac6eb776db7be8ee72de4af56f12a/docs/repo-2026-10-14-review.md),
reviewed commit `3766d50f`, still `main`'s tip). The other plans are `plan/approval-core-hardening`,
`plan/web-and-org-hardening` and `plan/docs-and-website-accuracy`. Finding ids are the review's;
its evidence is there.

## 1. Goal

Close the privilege-separation, packaging and supply-chain gaps through which the agent's own OS
account or a malicious package can reach the service account, the signing credentials or the live
QA grants, and make the release gates fail when their tests do not run:

- a user-owned `--daemon-exec`, a raced `chmod`, an over-broad pkexec action and a swappable app
  bundle can no longer run or touch anything as root or as the service account
  (SEC-D2, SEC-D3, SEC-D4, SEC-D6);
- the `.mcpb` shim trusts only a control socket and `mcp_url` owned by the service account and
  pointing at this machine (SEC-D5);
- every tool a release or a QA-runner job installs is pinned and hash-checked, the QA runner's jobs
  hold read-only tokens, and only `main`'s two QA workflows can reach that runner
  (SEC-D1, SEC-E1, SEC-E2, SEC-E4);
- the dependency floors `pip install privacyfence` users get carry no known advisories (SEC-E5);
- release tags and the production Worker deploy can only come from their reviewed paths
  (SEC-E6, SEC-E7);
- packaged and browser test layers fail instead of skipping in CI, the coverage ratchet keeps up,
  and the remaining test gaps are closed (TST-C1, B15, C3, C4, C8, C9, C10, C11, C13, A13, B16, B18,
  B19, B21, and TST-A8's `timeout-minutes` half);
- the testing policy and definition of done name everything CI actually requires (DOC-1, TST-C7).

Findings in this plan: **SEC-D1, SEC-D2, SEC-D3, SEC-D4, SEC-D5, SEC-D6, SEC-E1, SEC-E2, SEC-E4,
SEC-E5, SEC-E6, SEC-E7, TST-A8 (workflow half), TST-A13, TST-B15, TST-B16, TST-B18, TST-B19,
TST-B21, TST-C1, TST-C3, TST-C4, TST-C7, TST-C8, TST-C9, TST-C10, TST-C11, TST-C13, DOC-1.**

The org is on GitHub Team (the user confirmed), so organization runner groups with "Selected
workflows" are available for SEC-E1.

## 2. Current state

- **Linux privilege separation** (`scripts/linux_privilege_separation.sh`, 915 lines; installed as
  `/usr/sbin/privacyfence-privilege-separation` by `scripts/build_deb.sh:193-208`).
  `SERVICE_ACCOUNT="privacyfence"`, `SERVICE_GROUP="privacyfence"`, `SYSTEM_ROOT="/var/lib/privacyfence"`,
  `HANDOFF_DIR_MODE=3770` (`:85-107`); `die()` exits 1 (`:139`). Usage (`:143-171`) recommends
  `--daemon-exec .venv/bin/privacyfence-app`. `resolve_executables` (`:233-241`) checks only `-x`.
  `apply_layout` (`:303-326`) runs `find "$SYSTEM_ROOT" ! -type s ! -type l -exec chmod go-rwx {} +`
  (`:317`) and `find ".../handoff" -type f -exec chmod 640 {} +` (`:325`) as root after a
  `chown -h` pass (`:313`). `render_template` (`:366-378`) is a plain `sed` with
  `s|__DAEMON_EXECUTABLE__|${DAEMON_EXECUTABLE}|g`, so `|`, `&` or `\` in a value break it.
  Argument parsing is at `:852-880`; there is no `PKEXEC_UID` handling. The companion calls
  `pkexec <script> daemon start|stop|restart` (`service_control.py:146`),
  `pkexec <script> enable --for-user <user>` (`privilege_separation.py:2188`) and
  `pkexec <script> enable --auto` (`:1702-1709`).
- **Polkit** (`installer/linux/eu.privacyfence.daemon-control.policy`): one action
  `eu.privacyfence.daemon-control`, matched on `exec.path` only, message "start, stop or restart".
- **macOS** (`scripts/macos_privilege_separation.sh`): `apply_layout` `:447-472` has the same two
  `find ... -exec chmod` lines (`:461`, `:471`); `require_trusted_image` (`:234-258`) walks a path to
  `/` requiring root owner and no group/world write (group `wheel` allowed);
  `stage_trusted_image` (`:298-325`) copies a non-bundle executable with `cp -p` and checks it, not
  its interpreter. `REPO_ROOT` is the script's parent's parent and `TEMPLATE_DIR` is
  `${REPO_ROOT}/installer/macos` (`:104-105`), holding `com.privacyfence.daemon.plist.tmpl` and
  `com.privacyfence.companion.plist.tmpl`. `installer/macos/pkg/postinstall` runs
  `/Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh` as
  root (`:24, :58-62, :89`); `scripts/build_pkg.sh:130-131` puts only `postinstall` in the pkg's
  Scripts dir.
- **Shim** (`mcpb/shim`, Node; `npm test` is `node --import tsx --test test/*.test.ts`, no
  coverage threshold). `controlChannel.ts:~129-131` connects to `posixControlSocketPath()` with no
  owner check; `protocol.ts:286-293` `readMcpUrl` only parses the URL;
  `privilegeSeparationRoot(env)` (`:146-155`) is non-null on a separated install. There is no fs
  stat injection seam today; tests use temp dirs and `env` objects. `@anthropic-ai/mcpb` is not a
  dependency; `scripts/build_mcpb.sh:67,71` run `npx --yes @anthropic-ai/mcpb validate|pack`
  (latest from npm) inside the macOS and Windows signing jobs. The daemon,
  `web/control_channel.py:1532-1565` `_start_posix`, refuses a socket owned by another uid
  (`_existing_socket_owner_problem`, `:1426-1461`) and then runs with no control channel.
- **Worker** (`cloudflare/downloads`): vitest with no coverage block.
- **Python installs in CI.** Runtime deps are hash-pinned (`build.yml:84,379,601`), but every
  packaging job then runs `pip install -e ".[dev,test]"` (`:96,389,612`), the SBOM and finalize jobs
  `pip install setuptools_scm`/`cyclonedx-bom`, four R2 steps `pip install boto3` beside the R2 keys,
  `publish-pypi.yml:187` `pip install --upgrade pip build`, and `publish-mcp-registry.yml:147`
  `pip install setuptools_scm`, all unpinned. The QA runner jobs (`connector-live-check.yml:77-81`,
  `qa-record-fixture.yml:98-102`) run `.venv/bin/pip install --upgrade pip` and
  `pip install -e ".[test]"`, with `contents: write` (+ `pull-requests: write` on the live check).
  `requirements/dev.lock.txt` (dev+test+lint, hashed) is never installed by a workflow.
  `scripts/update_dependency_locks.sh` compiles runtime, dev and docs locks with
  `uv pip compile --upgrade --universal --python-version 3.11 --generate-hashes --no-strip-extras`;
  docs is compiled from stdin with only the `docs` extra. `dependency-audit.yml`'s
  `lockfile-freshness` repeats those commands into `/tmp/lockcheck` and diffs, and its `pip-audit`
  job audits only the locks.
- **Floors.** `pyproject.toml:65` `pypdf>=5.0`, `:134` `PyJWT>=2.8,<3.0`, `:62` `requests>=2.31.0`;
  urllib3 has no floor. The lowest-direct resolution has 122 known advisories (SEC-E5).
- **QA runner.** Registered to the repository (`docs/connector-qa.md:414-433`). ADR 0019's
  Decision 2 is enforced only by the two workflow files. `qa-record-fixture.yml` is dispatched
  against a feature branch (`.claude/commands/qa-record.md` step 2) and pushes back to it; its
  refusal (`:76-86`) covers `main` and `releases/*` but not `stable`.
- **Worker deploy.** `deploy-download-worker.yml:68-123` `deploy` has
  `if: github.event_name != 'pull_request'`, no `environment:`, and the
  `CF_DOWNLOADS_WORKER_API_TOKEN`/`_ACCOUNT_ID` repository secrets.
- **Release tags.** No tag ruleset; `docs/releasing.md:19-45` documents a hand `git push origin <tag>`
  as an equal path to `release.yml`. `RELEASE_TAG_TOKEN` is a fine-grained PAT (ADR 0021).
- **Skips.** Packaged steps (`build.yml:242,263,521,627,661`, the graphical workflows) run
  `pytest <module> -v` and pass when everything skips. Browser modules skip on a Chromium launch
  failure (`test_browser_smoke.py:162-172`, four `test_settings_*_browser.py`,
  `test_download_page.py`, `test_releases_page.py`, `test_website_layout.py`,
  `test_website_consent.py`). `tests/conftest.py:128-148` has one hook, `pytest_runtest_makereport`
  (a wrapper). `test_browser_smoke.py` and `test_settings_grist_modal_browser.py` carry no
  `browser` marker; `pyproject.toml` has no `--strict-markers`. `importorskip` guards runtime
  dependencies: `mcp` (11 modules), `cryptography` (2), `openpyxl` (`test_audit_log.py` ×12,
  `abuse/test_abuse_audit_export.py`). `tests.yml` has no `timeout-minutes`.
- **Coverage ratchet.** `scripts/check_coverage_floor.py` (`OVERALL_FLOOR = 95.1`, `MODULE_FLOORS`
  `:68-189`, `main()` `:198-257`) has no test and floors lag actual by up to 11 points.
- **Graphical coverage.** `scripts/check_graphical_session_coverage.py` accepts the latest completed
  run if it is an ancestor; the Linux and Windows workflows' `on.push.paths` omit the Python modules
  they exercise.
- **Other tests.** The Windows named-pipe control channel and `_peer_uid_macos` never run on a PR
  (`tests/unit/web/test_control_channel.py:23` skips on Windows; `tests/platform/` has no
  control-channel test). No per-PR test drives the built shim against a real daemon process
  (`tests/system/test_local_mode_system.py` spawns the daemon; `test_shim_mcp_contract.py` runs the
  shim against an in-process server). `grist/list_documents.json` is never replayed. Two fixtures
  carry a real Gmail Message-ID and Jira tenant UUIDs. `control_channel._mint_nonces` (`:800`) is
  not reset in `tests/conftest.py::_reset`. `systemd-analyze` never checks the shipped unit
  template.
- **Policy docs.** `docs/testing-policy.md` omits the `sync` job and its two required checks, says
  `website-build` is and is not required (`:73-74` vs `:151`), and names the wrong marker map
  (`:29-33`); §2.7, `.claude/toolkit.yaml` and `scripts/pre_release_check.py` have no `sync/**` row.

## 3. Design

### 3.1 Linux image ownership (SEC-D2) and layout race (SEC-D4)

- New functions in `linux_privilege_separation.sh`, placed before `resolve_executables`:

  ```bash
  # The service account runs this file; nothing a non-root account can write may reach it (ADR 0003).
  # A symlink's own mode is always 777 on Linux, so a link is checked for its owner only; the file it
  # resolves to, and every directory above that, get the full owner-and-mode check.
  _check_image_entry() {
    local entry="$1" what="$2" path="$3" link_ok="$4" owner mode
    read -r owner mode < <(stat -c '%u %a' -- "$entry")
    [ "$owner" = "0" ] || die "refusing ${what} ${path}: ${entry} is owned by uid ${owner}, not root -- a separated daemon must run from a root-owned install"
    if [ "$link_ok" = 1 ] && [ -L "$entry" ]; then return 0; fi
    [ $(( 8#$mode & 8#022 )) -eq 0 ] || die "refusing ${what} ${path}: ${entry} is writable by group or others"
  }
  _walk_image_path() {
    local start="$1" what="$2" path="$3" link_ok="$4" candidate="$1"
    while :; do
      _check_image_entry "$candidate" "$what" "$path" "$link_ok"
      [ "$candidate" = "/" ] && break
      candidate="$(dirname -- "$candidate")"
    done
  }
  # Prints the resolved path, which the caller uses from then on, so the unit names what was checked.
  require_root_owned_image() {
    local path="$1" what="$2" literal resolved
    literal="$(realpath -s -- "$path")" || die "refusing ${what} ${path}: cannot resolve it"
    resolved="$(readlink -f -- "$path")" || die "refusing ${what} ${path}: cannot resolve it"
    _walk_image_path "$literal" "$what" "$path" 1
    _walk_image_path "$resolved" "$what" "$path" 0
    printf '%s\n' "$resolved"
  }
  ```

  `realpath -s` makes the path absolute without resolving links, so the walk always ends at `/`
  (a relative `.venv/bin/...` cannot loop). `resolve_executables`, after its `-x` check:
  `DAEMON_EXECUTABLE="$(require_root_owned_image "$DAEMON_EXECUTABLE" "--daemon-exec")"`. When that
  file's first two bytes are `#!`, take the interpreter (the first word after `#!`; if it is
  `/usr/bin/env`, the next word resolved with `command -v`) and run
  `require_root_owned_image "$interpreter" "--daemon-exec interpreter" >/dev/null`. The companion
  executable is not checked (it runs as the signed-in user). The default `/usr/bin/privacyfence-app`
  is a `#!/bin/sh` wrapper (`resources/linux/privacyfence-app-wrapper`); on merged-`/usr` Ubuntu
  `/bin` and `/bin/sh` are root-owned links, which pass.
- Usage text: the `--daemon-exec` example becomes
  `--daemon-exec /opt/privacyfence/venv/bin/privacyfence-app` with the line
  `(a source or venv install can be separated only if root owns it and everything above it)`.
- macOS `stage_trusted_image`: in the per-file (`cp -p`) branch, after copying, if the copied
  daemon executable starts with `#!`, run `require_trusted_image` on its interpreter path.
- `apply_layout`, both scripts. The handoff directory is the only place a group member can write
  (3770). So:
  1. As root, first remove directories inside it:
     `find "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" -mindepth 1 -maxdepth 1 -type d -exec rm -rf -- {} +`
     with `note "removing unexpected directories under ${HANDOFF_DIR_NAME}/"`.
  2. The blanket `chown -h` and `chmod` passes prune handoff, so they never descend into it:
     `find "$SYSTEM_ROOT" -path "${SYSTEM_ROOT}/${HANDOFF_DIR_NAME}" -prune -o ! -type s -exec chown -h ... {} +`
     and the same `-prune -o` form for the `chmod go-rwx` pass. `SYSTEM_ROOT` is 711 and owned by the
     service account, so `handoff/` itself cannot be renamed or replaced.
  3. Handoff's own children are handled with `-mindepth 1 -maxdepth 1` only: one `chown -h` pass
     (root; `-h` never follows the last component and there is no deeper component), and the
     file-mode pass.
  4. Linux runs the two `chmod` passes as the service account, from `/` (pkexec starts in root's
     mode-700 home, where `find` as another user cannot return):
     `( cd / && setpriv --reuid="$SERVICE_ACCOUNT" --regid="$SERVICE_GROUP" --clear-groups find ... ) || warn "could not tighten every mode under ${SYSTEM_ROOT} (a file changed while it ran)"`.
     After the `chown -h` pass every file is the service account's, so it can `chmod` them; a link
     swapped in can only reach files it already owns.
  5. macOS uses `chmod -h` in both passes (BSD `chmod -h` changes a link itself).

### 3.2 pkexec allow-list (SEC-D3) and the unit template (TST-C10)

- The script saves `ORIGINAL_ARGS=("$@")` before any `shift` and defines
  `check_pkexec_arguments()`, called right after the parse loop (`:880`). When `PKEXEC_UID` is set
  it accepts exactly these lists: `daemon start|stop|restart|ensure-running|status`,
  `enable --auto`, and `enable --for-user <name>` where `<name>` equals `$(id -nu "$PKEXEC_UID")`.
  Anything else: `die "not allowed via pkexec: ${ORIGINAL_ARGS[*]}"`; a mismatched `--for-user`:
  `die "not allowed via pkexec: --for-user must name the account that ran pkexec"`. These are the
  only lists the companion and the auto-enable path pass (`service_control.py:146`,
  `privilege_separation.py:1702-1709`, `:2188`); `debian/postinst` runs the script as root without
  pkexec.
- The polkit file gets two actions, selected by the first argument with the
  `org.freedesktop.policykit.exec.argv1` annotation (polkit 0.113 and later; the `.deb` needs
  Ubuntu 24.04+, which ships polkit 124):
  - `eu.privacyfence.daemon-control` (`exec.argv1` = `daemon`): message `Authentication is required
    to start, stop or restart PrivacyFence's background service`.
  - `eu.privacyfence.enable-for-user` (`exec.argv1` = `enable`): message `Authentication is required
    to set up PrivacyFence's background service for your account`.
  Both keep `exec.path` = `/usr/sbin/privacyfence-privilege-separation` and `auth_admin_keep`. The
  argv allow-list stays the guard; the second action makes the prompt say what is being
  authorised. The XML comment says so (ADR 0183).
- `render_template`: escape each value before `sed` with
  `sed_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }` so a path containing `|`, `&` or
  `\` renders literally.

### 3.3 The macOS pkg runs its own copy of the script (SEC-D6)

- `scripts/build_pkg.sh`: besides `postinstall`, copy `scripts/macos_privilege_separation.sh` to
  `${SCRIPTS_DIR}/scripts/macos_privilege_separation.sh` (mode 755) and both
  `installer/macos/*.plist.tmpl` files to `${SCRIPTS_DIR}/installer/macos/`, so the script's own
  `REPO_ROOT` resolves inside the pkg's Scripts directory (Installer unpacks it to a root-only
  temporary directory; `pkgbuild --scripts` keeps subdirectories). The script uses `REPO_ROOT` only
  for the two templates (`:105`, `:529`, `:531`).
- `installer/macos/pkg/postinstall`:
  - `SEPARATION_SCRIPT="$(cd "$(dirname "$0")" && pwd)/scripts/macos_privilege_separation.sh"`;
  - a plain assignment `FIND=/usr/bin/find` (never `${FIND:-...}` in a root script) and a function
    `bundle_is_root_owned() { [ -z "$("$FIND" "$APP_PATH" ! -user root -print -quit 2>/dev/null)" ]; }`;
  - before the first use of the script: `if ! bundle_is_root_owned; then log "PrivacyFenceApp.app is not owned by root throughout -- not setting up the background service"; exit 0; fi`;
  - the lines that print a `sudo ...` command for a person to run keep naming the in-bundle path
    (`${APP_PATH}/Contents/Resources/scripts/macos_privilege_separation.sh`), not the temporary one.
  The postinstall always exits 0, as it does today.
- Rejected: a `codesign` team-id check in `stage_trusted_image` (pre-release and dispatched builds
  may be unsigned, and would then fail to install). Accepted residual risk for ADR 0184:
  `/Applications` is admin-writable, so an admin account could still swap the bundle between the
  ownership check and `stage_trusted_image`'s `ditto`.

### 3.4 Shim ownership and host checks (SEC-D5, TST-C13)

- `protocol.ts`: `export function checkHandoffEntry(entry: string, kind: "socket" | "file", root: string, fsx: { lstatSync: typeof fs.lstatSync; statSync: typeof fs.statSync } = fs): void`
  throws `Error(\`${entry} is not owned by the service account\`)` unless `fsx.lstatSync(entry)` is
  a socket (or a regular file) and its `uid` equals `fsx.statSync(root).uid`.
  `readMcpUrl(mcpUrlFile = MCP_URL_FILE, opts: { root?: string | null; fsx?: ... } = {})`: when
  `opts.root ?? privilegeSeparationRoot()` is non-null, call `checkHandoffEntry(mcpUrlFile, "file", root, opts.fsx)`;
  then require `new URL(text).hostname` to be `127.0.0.1`, `[::1]` or `localhost`, else throw
  `Error(\`mcp_url names ${hostname}, not this computer\`)`.
- `controlChannel.ts`: `MintMcpTokenOptions` gains `fsx?`; `mintMcpToken` passes it to
  `sendControlChannelLine(message, timeoutMs, fsx)`, which, on POSIX when
  `privilegeSeparationRoot()` is non-null, calls `checkHandoffEntry(target, "socket", root, fsx)`
  before `net.createConnection`.
- `package.json` `test`: `node --import tsx --test --experimental-test-coverage --test-coverage-branches=88 --test-coverage-lines=95 test/*.test.ts`.
- Daemon (`control_channel.py`): `_LineProtocolServer.__init__` gains
  `replace_foreign_socket: bool = False`; only `ControlChannelServer` (`:1777`) passes `True`. In
  `_start_posix`, when `_existing_socket_owner_problem` reports a foreign owner and the flag is set,
  try `sock_path.unlink()` and log at warning `"Removed %s: it was owned by uid %s, not this process"`;
  if the unlink raises `OSError`, or the flag is not set, keep today's warning and return.
  `CompanionChannelServer` keeps refusing, as ADR 0027 decision 2 requires for a socket the sticky
  bit does not protect; the daemon's service account owns `handoff/`, so only it can unlink there.
  ADR 0187 records this amendment of 0027.

### 3.5 Pinned build and release tooling (SEC-D1, SEC-E2, SEC-E4, SEC-E5)

- `mcpb/shim/package.json` devDependencies gains `"@anthropic-ai/mcpb": "<exact version>"` (the
  newest release published at least 14 days before the phase runs; `npm view @anthropic-ai/mcpb time --json`).
  `scripts/build_mcpb.sh` runs `"${REPO_ROOT}/mcpb/shim/node_modules/.bin/mcpb" validate ...` and
  `... pack ...` after its `npm ci` (`:48`).
- `pyproject.toml`: floors `pypdf>=6.19`, `PyJWT>=2.15,<3.0`, a new direct `urllib3>=2.8`, and any
  further floor the floor audit below still reports, each raised to the lowest version that clears
  its advisories, with a one-line comment like the starlette one.
- Release tooling is not a package extra (an extra would ship in PyPI metadata as
  `privacyfence[release]`). New `requirements/release.in`: `setuptools>=61.0`, `wheel`,
  `setuptools_scm>=8`, `build>=1.2`, `boto3>=1.34`, `cyclonedx-bom>=4.0`, `uv>=0.4`,
  `pip-audit>=2.7`, one per line with a header comment.
- Locks (`scripts/update_dependency_locks.sh` and `lockfile-freshness`, same flags as today):
  - `requirements/dev.lock.txt` is compiled from `pyproject.toml --extra dev --extra test --extra lint`
    **plus** `requirements/release.in` (both inputs in one `uv pip compile` call), with
    `-c requirements/runtime.lock.txt` so every package it shares with the runtime lock gets the same
    pin. One hashed lock then covers every tool a packaging or QA job needs, including the build
    backend for `--no-build-isolation`, and installing it after the runtime lock changes nothing the
    runtime lock pinned;
  - new `requirements/release.lock.txt` is compiled from `requirements/release.in` alone, for the
    steps that need only the tools.
  `requirements/README.md` describes both.
- `dependency-audit.yml`: new job `floor-audit`: setup-python 3.13,
  `pip install --require-hashes -r requirements/release.lock.txt` (it carries uv and pip-audit),
  `uv pip compile --resolution lowest-direct --python-version 3.11 pyproject.toml -o /tmp/lowest.txt`,
  `pip-audit --strict --no-deps --disable-pip -r /tmp/lowest.txt`. The `pip-audit` job also audits
  `release.lock.txt`, blocking.
- Workflows install from locks; one environment never gets two independently compiled locks:
  - packaging jobs (`build`, `build-windows`, `build-deb`): keep the runtime lock line; replace
    `pip install -e ".[dev,test]"` (and `pyinstaller`) with
    `pip install --require-hashes -r requirements/dev.lock.txt` and
    `pip install --no-deps --no-build-isolation -e .`;
  - `sbom`, `finalize-release`, every R2 step, `publish-pypi.yml` (both builds, `:187` and the
    sync build at `:198`), `publish-mcp-registry.yml`: `pip install --require-hashes -r requirements/release.lock.txt`
    replaces each `setuptools_scm`/`cyclonedx-bom`/`boto3`/`build` install (`--user` where the step
    used it); `python -m build` becomes `python -m build --no-isolation`;
  - QA runner jobs: `.venv/bin/pip install --require-hashes -r requirements/dev.lock.txt` and
    `.venv/bin/pip install --no-deps --no-build-isolation -e .`; no `--upgrade pip`.

### 3.6 QA runner jobs and the Worker deploy (SEC-E1, SEC-E2, SEC-E6, SEC-E7)

- `connector-live-check.yml`: workflow `permissions: contents: read`. Job `live-check` (self-hosted)
  keeps `contents: read`, gains `outputs: drift: ${{ steps.check.outcome == 'failure' }}`, and
  replaces its "Open drift PR" step with two `actions/upload-artifact` steps (only on drift):
  `drift-fixtures` with `path: tests/fixtures/live`, and `drift-report` with
  `path: /tmp/qa-record-report.md`. New job `open-drift-pr` (`runs-on: ubuntu-latest`,
  `needs: live-check`, `if: needs.live-check.outputs.drift == 'true'`,
  `permissions: contents: write, pull-requests: write`): checkout, download `drift-fixtures` with
  `path: tests/fixtures/live` and `drift-report` with `path: /tmp`, then the same pinned
  `peter-evans/create-pull-request` step with the same inputs.
- `qa-record-fixture.yml` is dispatched **on `main`** with a new required input `branch` (the
  branch to record for). The `record` job (self-hosted, `contents: read`):
  - takes the input only through `env: BRANCH: ${{ inputs.branch }}`, never `${{ }}` inside `run:`;
  - refuses unless `github.ref == 'refs/heads/main'` (`"Dispatch this workflow on main and name the branch in the 'branch' input."`);
  - refuses a `BRANCH` that is `main` or `stable`, matches `releases/*`, or starts with `refs/` or
    `pull/`, and one that `git ls-remote --exit-code --heads origin "refs/heads/${BRANCH}"` does not
    find (so no pull-request ref, fork code included, can reach the runner);
  - checks out `ref: refs/heads/${{ inputs.branch }}` (after those checks); records; uploads
    `tests/fixtures/live/${CONNECTOR}` as artifact `recorded-fixture` with that `path`.
  New job `commit-fixture` (`ubuntu-latest`, `needs: record`, `contents: write`) checks out
  `refs/heads/${{ inputs.branch }}`, downloads the artifact with
  `path: tests/fixtures/live/${{ env.CONNECTOR }}`, and commits and pushes it with the existing
  message (skipping when nothing changed). This lets the runner group allow only `main`'s two
  workflow files. `.claude/commands/qa-record.md` step 2 becomes: dispatch with `ref: main` and
  inputs `connector` and `branch` (the current branch).
- `deploy-download-worker.yml` `deploy`: `if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'`
  and `environment: downloads-worker`; fix the comment at `:70-72`.
- `docs/releasing.md`: `release.yml` is the only way to cut a release; the `v*` tag ruleset (ma3)
  refuses a tag pushed by an account without the repository admin role. The account whose
  fine-grained token is `RELEASE_TAG_TOKEN` must have that role; a person with it could still push
  a tag by hand, which the docs say is not the procedure.
- `docs/connector-qa.md` "Runner requirements": the runner is in the organization runner group
  `privacyfence-test` (public repositories allowed, restricted to the two workflows on `main`),
  and fork pull requests from outside collaborators need approval (ma1, ma2); "Recording one
  connector" describes the dispatch-on-main shape.

### 3.7 Skip-means-fail and markers (TST-C1, TST-B15, TST-C8, TST-A13, TST-A8)

- `tests/conftest.py` `pytest_runtest_makereport`: after `rep = yield`, if `rep.skipped` and
  either (`PRIVACYFENCE_REQUIRE_PACKAGED == "1"` and the item has the `packaged` marker) or
  (`PRIVACYFENCE_REQUIRE_BROWSER == "1"`, the item has the `browser` marker, and the skip reason
  matches `(?i)chromium|playwright`), set `rep.outcome = "failed"` and `rep.longrepr` to
  `f"skipped while {name}=1 requires it to run: {reason}"`. A browser test that skips for another
  reason (for example `test_website_layout.py:282`, "/docs/ was not built", in a job without
  zensical) stays a skip.
- New `pytest_collectreport` hook wrapper in `tests/conftest.py`: a skipped module collection fails
  under `PRIVACYFENCE_REQUIRE_PACKAGED == "1"` (each packaged step runs one module, so any
  collection skip there is a failure), and under `PRIVACYFENCE_REQUIRE_BROWSER == "1"` when its
  reason matches `(?i)playwright`.
- `pytest.mark.packaged` joins `test_org_ubuntu_release_smoke.py`'s marks (it is one of the five
  packaged steps, `build.yml:627`; it skips itself at module level, `:200-203`, without its env).
- `pytestmark` gains `pytest.mark.browser` in `test_browser_smoke.py` and
  `test_settings_grist_modal_browser.py`; `pyproject.toml` `addopts = "--strict-markers"`.
- Env: `PRIVACYFENCE_REQUIRE_PACKAGED: "1"` on the five packaged steps in `build.yml` and the
  pytest steps of the three graphical-session workflows; `PRIVACYFENCE_REQUIRE_BROWSER: "1"` on the
  `test` job's pytest step and `website-build.yml`'s guardrail step. `timeout-minutes: 45` on every
  `tests.yml` job (90 for `platform-windows` and `platform-macos`).
- Replace every `pytest.importorskip` of `mcp`, `cryptography` or `openpyxl` (runtime dependencies),
  including the multi-line `pytest.importorskip(\n    "mcp", ...)` form, with a plain import.
  Playwright stays an `importorskip` (it is in the `test` extra only).

### 3.8 Coverage ratchet (TST-C3, TST-C4)

- Floors come from two runs: the full suite with coverage on the phase branch in the container,
  and the `coverage-report` artifact of the latest green `tests.yml` run on `main` (the `test`
  job's `coverage.json`; download it with `gh run download <run id> -n coverage-report` or the
  GitHub MCP actions tools). Each `MODULE_FLOORS` value becomes `math.floor(min(container, ci))`,
  `OVERALL_FLOOR` becomes `math.floor(min(container, ci) * 10) / 10`. Add floors, same rule, for
  `scoped_tokens.py`, `web/mcp_dispatch.py`, `web/step_up_decide.py`, `web/approval_step_up.py`,
  `web/mcp_auth.py`, `policy/scopes.py`, `policy/engine.py`, `policy/store.py`,
  `policy/conditions.py`, `local_files.py`, `upload_staging.py`, `download_staging.py`,
  `principal.py`, `audit_forwarding.py`, `deny_feedback.py`, `web/routes_connect.py`,
  `web/server.py`, `web/routes_org_stepup.py`, `web/csp.py`, `web/state_stream.py`. Fix the stale
  comments (`routes_approvals.py`'s, `control_channel.py`'s "tests/platform/").
- Staleness check in `main()`: a module more than 1.0 point above its floor, or overall more than
  0.5 above, prints `::warning::stale coverage floor: {module} is at {actual:.2f}%, floor {floor:.1f}% -- raise it`
  and does not change the exit code (a warning, so a PR that only adds tests is never blocked by it).

### 3.9 Graphical-session coverage (TST-C9)

- `on.push.paths`: Linux gains `src/privacyfence/privilege_separation.py`,
  `src/privacyfence/companion.py`, `src/privacyfence/oauth_loopback.py`,
  `src/privacyfence/service_control.py`; Windows gains `src/privacyfence/windows_service.py`,
  `src/privacyfence/windows_acl.py`, `src/privacyfence/service_control.py`,
  `src/privacyfence/privilege_separation.py`, `src/privacyfence/companion.py`; macOS gains
  `src/privacyfence/companion.py`, `src/privacyfence/service_control.py`.
- `check_graphical_session_coverage.py`: `watched_paths(workflow_file: Path) -> list[str]` reads the
  `paths:` list under `on.push` line by line (stdlib only: the lines after `    paths:` that start
  with `      - `, quotes stripped, comment lines skipped). `changed_since(run_sha, commit) -> list[str]`
  is `git diff --name-only <run_sha> <commit>`. `evaluate` gains a `stale_files: list[str]`
  argument; when non-empty and the run is otherwise green, it warns
  `f"{workflow}: {len(stale_files)} file(s) it covers changed after its last run ({stale_files[0]}, ...)"`.
  `fnmatch` the changed files against the patterns (`**` and `*` both match across `/` with
  fnmatch). The warning gates stable tags like the others.

### 3.10 Remaining tests (TST-B16, TST-C11, TST-B18, TST-B19, TST-B21)

- `tests/platform/test_control_channel_windows.py` (`pytestmark = [pytest.mark.platform,
  pytest.mark.skipif(sys.platform != "win32", reason="named pipes exist only on Windows")]`).
  `principal_id_for_peer` returns `"local"` unless privilege separation is on, so the test
  monkeypatches `privilege_separation.is_enabled` to return True, `privilege_separation.owner_sid`
  to return `"S-1-5-21-1-2-3-1001"` and `privilege_separation.windows_channel_trustees` to return
  `[]` (read `control_channel.py:578-668` and `:1777-1835` and adjust the names only if they
  differ, keeping the meaning); starts `ControlChannelServer(bootstrap=BootstrapStore())`; sends
  `send_line_windows(server.address, "MINT\n", timeout=5)`; takes the code from the reply;
  `bootstrap.consume(code)` returns `(provenance, principal_id)`; and asserts
  `principal_id == f"os-{sid}"` with the current user's SID read through `win32security`
  (`GetTokenInformation` of the process token). `tests/platform/test_control_channel_macos.py`
  (skip unless darwin): `_peer_uid_macos` on a real `socket.socketpair(socket.AF_UNIX)` returns
  `os.getuid()`.
- `tests/system/test_local_mode_system.py::test_shim_against_real_daemon_process`: reuse
  `_prepare_sandbox`/`_daemon`, spawn `node mcpb/shim/dist/shim.js` with `HOME` (and `USERPROFILE`,
  `LOCALAPPDATA`) set so the shim's data dir is the sandbox (copy `_shim_data_dir` logic from
  `test_shim_mcp_contract.py:192`), drive it with `mcp.client.stdio.stdio_client`, call
  `system_test_send`, expect `approval_pending`. Without node or the built shim it skips, unless
  `PRIVACYFENCE_REQUIRE_NODE=1`, when it fails; `tests.yml`'s `test` job sets that variable.
- `TestLiveFixtureParsing` in `test_grist_client.py` gains `test_list_documents_fixture_still_parses`;
  `test_qa_fixture_recorder.py` gains a meta-test that every `EXPECTED_FIXTURES` file name appears in
  a `self._load("<name>")` or `_load("<name>")` call somewhere under `tests/unit`, and
  `test_committed_fixtures_carry_no_unredacted_identifiers` over `tests/fixtures/live/**/*.json`:
  every email domain is `example.com`; no `@mail.gmail.com`; none of the regexes
  `\bBearer [A-Za-z0-9._-]{10,}`, `\bxox[abpr]-`, `\bya29\.`, `\beyJ[A-Za-z0-9_-]{10,}`,
  `\bsk-[A-Za-z0-9]{20,}`, `\bghp_[A-Za-z0-9]{20,}` matches; no UUID outside an allow-list of the
  placeholder UUIDs the recorder itself writes. `redact_gmail_message` also redacts the
  `Message-ID` header (to `<qa-placeholder-message-id@example.com>`), and the Jira redaction
  replaces `entityId` values with `qa-placeholder-entity-id`; the committed fixtures are rewritten
  by running those functions over them.
- `tests/conftest.py::_reset` gains `control_channel._mint_nonces.clear()`.

### 3.11 Policy docs (DOC-1, TST-C7)

- `docs/testing-policy.md`: a `sync` row in the job table ("`sync` (Python 3.11, 3.13) |
  `privacyfence-sync` alone: `pip install -e "./sync[test]"`, `pytest sync/tests` at 100 % branch
  coverage, `mypy sync/src`, `bandit -r sync/src`"); "the two `Sync client (Python 3.x)` checks" in
  the required-checks sentence; delete "Not in `REQUIRED_STATUS_CHECKS`" from the `website-build`
  paragraph (`:151`); the marker paragraph (`:29-33`) lists the real `browser` modules; the new
  `PRIVACYFENCE_REQUIRE_PACKAGED`/`_BROWSER`/`_NODE` switches; `timeout-minutes`; the floor-audit and
  release-lock jobs; the `qa-record-fixture.yml` dispatch-on-main change.
- `docs/coding-and-testing-guidelines.md` §2.7 "Only if the PR touches these files": a `sync/**`
  row (the three commands; and if `sync/README.md` changed, copy it to
  `plugins/privacyfence-sync/skills/privacyfence-sync-recipes/references/README.md`); §4 names the
  graphical-session workflows as running `packaged` modules too. `.claude/toolkit.yaml`
  `dod_conditional` mirrors the `sync/**` row. `scripts/pre_release_check.py` runs the sync
  commands under a `"sync tests"` key. `scripts/update_branch_protection.py:79`'s comment names
  `3.11`, `3.12` and `3.14`. `tests/unit/test_definition_of_done_drift.py` asserts every
  `REQUIRED_STATUS_CHECKS` job name appears in `docs/testing-policy.md`. `.claude/toolkit.yaml`'s
  `ci.dispatchable` note for `qa-record-fixture.yml` describes the dispatch-on-main shape.

## 4. ADRs

- **0181** — The QA runner sits in an organization runner group that only `main`'s two QA workflows
  can use; `qa-record-fixture.yml` is dispatched on `main` and names the branch; the runner's jobs
  hold read-only tokens and a GitHub-hosted job does the writing. Amends 0019.
- **0182** — Every tool a release or QA-runner job installs comes from a hash-pinned lock or an
  exact npm pin; the `.mcpb` CLI is a shim devDependency; release tooling is pinned through
  `requirements/release.in`, not a package extra.
- **0183** — Under pkexec the privilege-separation script accepts only the daemon actions,
  `enable --auto` and `enable --for-user <the invoking user>`, and polkit shows a separate prompt
  for each of the two kinds.
- **0184** — The macOS pkg runs its own copy of the privilege-separation script, and only over a
  root-owned app bundle.
- **0185** — A release tag is created only by `release.yml`; a tag ruleset refuses `v*` tags from
  accounts without the repository admin role. Amends 0021.
- **0187** — The `.mcpb` shim trusts only handoff entries the service account owns and an `mcp_url`
  on this computer, and the daemon replaces a foreign-owned control socket while the companion
  channel keeps refusing one. Amends 0027.

All six are written in r16. Plans 1, 2 and 4 take 0175-0180 and 0186. Renumber per
`docs/adr/README.md` rule 6 if a number is taken first.

## 5. Manual steps

All after implementation; none is needed before a phase runs. Step-by-step:
[the manual steps page](https://claude.ai/artifact/SoNJWmV9wkrx1m2egX4ApT).

- `ma1-qa-runner-group`: create the organization runner group (public repositories allowed),
  restrict it to the two workflows on `main`, re-register the runner into it, and remove the
  repository-level registration.
- `ma2-fork-approval`: require approval for workflows from all outside collaborators.
- `ma3-tag-ruleset`: add the `v*` tag ruleset, bypass for the repository admin role.
- `ma4-downloads-worker-environment`: create the `downloads-worker` environment limited to `main`
  and add the two Cloudflare secrets to it, before the PR merges (the merge push runs the deploy);
  deleting the repository-level copies is the one step that waits for the merge.
- `ma5-linux-pkexec-dialog`: on a real Ubuntu desktop with the dispatched `.deb`, check both
  password dialogs' wording and that pkexec refuses other argument lists.

## 6. Risks and open questions

- **polkit `exec.argv1`.** If Ubuntu 24.04's polkit does not honour the annotation, r5 stops (its
  stop condition says how to check) rather than shipping two actions where one would win.
- **Node coverage flags.** `--test-coverage-branches` needs Node 22.8 or newer; CI's setup-node
  and the container run 22. If the flag is rejected, r2 stops.
- **Worker coverage provider.** `@cloudflare/vitest-pool-workers` supports Istanbul coverage only;
  if it rejects coverage altogether, r3 stops.
- **Legitimate skips on the release runners.** The REQUIRE switches turn skips into failures; a
  skip that is legitimate on a release runner (an unsigned pkg on a dispatched build,
  `test_macos_pkg_smoke.py:255`; the Linux graphical `_real_home_state` skip) would turn red. r10a
  dispatches `build.yml` and the three graphical-session workflows to find them, and narrows the
  hook to the reasons that mean "the artifact or browser is missing" if needed.
- **Dispatch needs main.** `qa-record-fixture.yml`'s new input only exists for dispatch after this
  PR merges; until then the old shape runs from `main`. Nothing in this plan dispatches it.
- **Cross-plan files.** `tests/integration/test_browser_smoke.py`, `tests/unit/web/test_control_channel.py`,
  `web/control_channel.py`, `tests/unit/test_audit_log.py`, `tests/system/test_local_mode_system.py`
  and `tests/integration/test_org_ubuntu_release_smoke.py` are also edited by plans 1 or 2, in
  different places. The second PR to merge takes a small merge.
- **Coverage floors after the other plans.** r11 raises floors to today's values; a later PR that
  lowers a module's coverage fails the ratchet, which is the ratchet's purpose.

## Implementation manifest

```yaml
plan_slug: platform-and-ci-hardening
feature_branch: fix/platform-and-ci-hardening
max_parallel: 2
manual_steps_artifact: https://claude.ai/artifact/SoNJWmV9wkrx1m2egX4ApT
manual_steps_source: docs/platform-and-ci-hardening-plan-manual-steps.html
manual_before: []
manual_after:
  - id: ma1-qa-runner-group
    title: Put the QA runner in an organization runner group that only main's two QA workflows can use
    why: GitHub settings and the runner host are outside the repository; without this a pull request that adds a workflow can still target the runner that holds live QA grants (SEC-E1)
  - id: ma2-fork-approval
    title: Require approval for workflows from all outside collaborators
    why: A repository setting; with it a first-time or returning outside contributor's workflow never runs unreviewed (SEC-E1)
  - id: ma3-tag-ruleset
    title: Add a ruleset that refuses v* tags from accounts without the repository admin role
    why: A repository setting; the docs change in this PR says release.yml is the only path, and only the ruleset enforces it (SEC-E6)
  - id: ma4-downloads-worker-environment
    title: Create the downloads-worker environment, limited to main, holding the two Cloudflare secrets
    why: The deploy job now names this environment; its branch rule is what stops a dispatch from another branch reaching production (SEC-E7)
  - id: ma5-linux-pkexec-dialog
    title: On a real Ubuntu desktop, check the pkexec dialogs' wording and that other argument lists are refused
    why: The polkit dialog only appears in a real graphical session with a real password prompt (SEC-D3)
verify_after_merge:
  - python3 -m pytest tests/unit/test_privilege_separation.py tests/unit/test_check_graphical_session_coverage.py tests/unit/test_qa_fixture_recorder.py tests/unit/test_grist_client.py tests/unit/test_pre_release_check.py tests/unit/test_definition_of_done_drift.py tests/unit/web/test_control_channel.py -q
  - python3 -m pytest tests/integration/test_org_guide_configs.py tests/system/test_local_mode_system.py -q
  - bash -c "cd mcpb/shim && npm ci --silent && npm test && npm run typecheck"
final_checks:
  - docs/platform-and-ci-hardening-plan.md and docs/platform-and-ci-hardening-plan-manual-steps.html are deleted and nothing links to them
  - ADRs 0181, 0182, 0183, 0184, 0185 and 0187 exist, are Accepted, and are listed in docs/adr/README.md; ADRs 0019, 0021 and 0027 carry a one-line Status pointer to the ADR that amends them
  - CHANGELOG.md has this plan's entries under "## [Unreleased]" and no new version heading
  - build.yml was dispatched against the feature branch head and every job is green, with no packaged test skipped (PRIVACYFENCE_REQUIRE_PACKAGED is on); the run is linked in the PR
  - linux-graphical-session.yml, macos-graphical-session.yml and windows-graphical-session.yml were dispatched against the feature branch head and are green; the runs are linked in the PR
  - the PR's own tests.yml run shows platform-windows running tests/platform/test_control_channel_windows.py and platform-macos running tests/platform/test_control_channel_macos.py, both passed (not skipped), and the test job's test_shim_against_real_daemon_process passed
  - pytest tests/integration -v passed on the feature branch head (mcpb/shim/src and web/control_channel.py changed), with test_shim_mcp_contract.py green
  - requirements/*.lock.txt were regenerated with scripts/update_dependency_locks.sh and dependency-audit.yml is green on the PR, including floor-audit
  - in cloudflare/downloads, npm test, npm run typecheck and npm run dry-run pass
phases:
  - id: r1-mcpb-pin
    title: Pin the .mcpb CLI as a shim devDependency and stop fetching it from npm at build time
    depends_on: []
    complexity: S
    touches:
      - mcpb/shim/package.json
      - mcpb/shim/package-lock.json
      - scripts/build_mcpb.sh
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.5 (first bullet). Finding: SEC-D1.
      1. In mcpb/shim: `npm view @anthropic-ai/mcpb time --json`; choose the newest version
         published at least 14 days ago; `npm install --save-dev --save-exact @anthropic-ai/mcpb@<that version>`.
         Confirm package-lock.json gained `"node_modules/@anthropic-ai/mcpb"` with an `integrity`.
      2. scripts/build_mcpb.sh:67 and :71: call `"${REPO_ROOT}/mcpb/shim/node_modules/.bin/mcpb"`
         (the script has only `REPO_ROOT` and `cd "$REPO_ROOT"` at :37; its `npm ci` is at :48).
         Update the header comment that explains the npx call.
      3. Run `cd mcpb/shim && npm ci && npm run build && cd ../.. && bash scripts/build_mcpb.sh` if
         it runs on Linux and report whether it produced a `.mcpb`; then
         `cd mcpb/shim && npm test && npm run typecheck`.
      4. Do not dispatch build.yml here; r10a and the plan's final checks dispatch it.
      Stop condition: if the newest version older than 14 days has no `validate`/`pack`
      subcommand, stop with status=blocked and quote `mcpb --help`.
    acceptance:
      - "SEC-D1: grep -rn 'npx --yes @anthropic-ai/mcpb' scripts/ returns nothing"
      - "SEC-D1: jq -r '.devDependencies[\"@anthropic-ai/mcpb\"]' mcpb/shim/package.json prints an exact version with no ^ or ~"
      - "SEC-D1: grep -c '\"node_modules/@anthropic-ai/mcpb\"' mcpb/shim/package-lock.json returns 1"
      - bash -c "cd mcpb/shim && npm test && npm run typecheck" passes

  - id: r2-shim-ownership
    title: The shim trusts only service-account-owned handoff entries and a loopback mcp_url
    depends_on: [r1-mcpb-pin]
    complexity: M
    touches:
      - mcpb/shim/src/protocol.ts
      - mcpb/shim/src/controlChannel.ts
      - mcpb/shim/src/index.ts
      - mcpb/shim/test/protocol.test.ts
      - mcpb/shim/test/controlChannel.test.ts
      - mcpb/shim/test/index.test.ts
      - mcpb/shim/package.json
      - src/privacyfence/web/control_channel.py
      - tests/unit/web/test_control_channel.py
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.4; read mcpb/shim/src/index.ts's
      module docstring first (the shim stays small and schema-free). Findings: SEC-D5, TST-C13.
      1. mcpb/shim/test/protocol.test.ts: `readMcpUrl` rejects `http://10.0.0.5:8765/mcp` and
         `https://evil.example/mcp` (`/not this computer/`), accepts `http://localhost:8765/mcp`,
         `http://127.0.0.1:8765/mcp` and `http://[::1]:8765/mcp`; `checkHandoffEntry` with an
         injected `fsx` whose `lstatSync` reports a socket with `uid: 501` and `statSync(root)`
         `uid: 999` throws `/not owned by the service account/`; a regular file where a socket is
         expected throws; matching uids pass; `readMcpUrl` with `{ root, fsx }` reporting a
         foreign owner throws.
      2. mcpb/shim/test/controlChannel.test.ts: with `PRIVACYFENCE_SYSTEM_ROOT` pointing at a
         temp root holding a valid marker (copy how protocol.test.ts builds one) and
         `mintMcpToken({ fsx })` reporting a foreign uid, it rejects with
         `/not owned by the service account/` and never connects.
      3. Implement 3.4 in protocol.ts and controlChannel.ts (`MintMcpTokenOptions.fsx`, passed to
         `sendControlChannelLine(message, timeoutMs, fsx)`); touch index.ts only if `main()` needs
         it for its tests.
      4. package.json `test` script per 3.4. Run `npm test`; if the thresholds fail, add tests for
         the uncovered branches the report names (do not lower the numbers); if Node rejects the
         flags, stop with status=blocked.
      5. src/privacyfence/web/control_channel.py per 3.4 (the constructor flag, set only by
         `ControlChannelServer`). tests/unit/web/test_control_channel.py:
         `test_a_foreign_owned_socket_is_removed_and_the_daemon_channel_starts` (monkeypatch
         `_existing_socket_owner_problem` to report a problem; the path was unlinked and `address`
         is set), `test_an_undeletable_foreign_socket_leaves_the_channel_off` (unlink raising
         PermissionError → today's warning, no channel), and
         `test_the_companion_channel_still_refuses_a_foreign_socket` (`CompanionChannelServer`
         never unlinks).
      6. Prove the host check can fail: remove it in a scratch copy, the matching test fails;
         restore. Report it.
      7. Run `cd mcpb/shim && npm test && npm run typecheck && npm run build`, then
         python3 -m pytest tests/unit/web/test_control_channel.py tests/integration/test_shim_mcp_contract.py -q,
         ruff check ., python3 -m mypy src/privacyfence.
    acceptance:
      - "SEC-D5/TST-C13: bash -c \"cd mcpb/shim && npm test\" passes with the coverage thresholds in package.json's test script"
      - "SEC-D5: grep -n \"not this computer\" mcpb/shim/src/protocol.ts matches"
      - "SEC-D5: python3 -m pytest tests/unit/web/test_control_channel.py -k \"foreign_owned_socket or undeletable_foreign_socket or companion_channel_still_refuses\" -q passes"
      - "TST-C13: the report shows the step 6 mutation failing the host-check test"
      - python3 -m pytest tests/integration/test_shim_mcp_contract.py -q passes

  - id: r3-worker-coverage
    title: Enforce a coverage threshold on the download Worker's tests
    depends_on: []
    complexity: S
    touches:
      - cloudflare/downloads/package.json
      - cloudflare/downloads/package-lock.json
      - cloudflare/downloads/vitest.config.ts
    brief: |
      Finding: TST-C13 (Worker half).
      1. cd cloudflare/downloads; read vitest.config.ts and package.json (vitest version, pool).
         `npm install --save-dev --save-exact @vitest/coverage-istanbul@<the version equal to the installed vitest>`.
      2. vitest.config.ts: `test.coverage = { provider: "istanbul", include: ["src/**"], thresholds: {...} }`.
         Run `npx vitest run --coverage` once; set `lines`, `branches`, `functions` and `statements`
         each to the measured percentage rounded down to an integer.
      3. Make `npm test` enforce it: change the `test` script to pass `--coverage` (keep whatever
         else it passes). Run `npm test`, `npm run typecheck`, `npm run dry-run`, and
         `npm audit --audit-level=high`.
      Stop condition: if the Workers pool refuses coverage, stop with status=blocked and quote the error.
    acceptance:
      - "TST-C13: grep -n \"thresholds\" cloudflare/downloads/vitest.config.ts matches"
      - bash -c "cd cloudflare/downloads && npm test && npm run typecheck && npm run dry-run" passes
      - bash -c "cd cloudflare/downloads && npm audit --audit-level=high" passes

  - id: r4-linux-image-and-layout
    title: Refuse a non-root-owned daemon image, and keep the layout passes out of the handoff directory
    depends_on: []
    complexity: M
    touches:
      - scripts/linux_privilege_separation.sh
      - scripts/macos_privilege_separation.sh
      - tests/unit/test_privilege_separation.py
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.1. Findings: SEC-D2, SEC-D4.
      Test technique: tests/unit/test_privilege_separation.py extracts bash functions with
      `_function` (:1238-1242) and runs them with stub commands first on PATH (see
      TestApplyLayoutLeavesSocketsAlone, :1222-1300).
      1. Tests first, in a new `class TestLinuxDaemonImageMustBeRootOwned` (extract the three
         functions of 3.1 plus `resolve_executables` and `die`):
         - stubbed `stat` (prints what the test chooses, branching on its last argument):
           `test_linux_enable_refuses_user_owned_daemon_exec` (uid 1000 → exit 1 and
           "owned by uid 1000"); `test_group_writable_daemon_exec_is_refused` (`0 775`);
           `test_a_root_owned_link_to_a_user_owned_file_is_refused` (the link reports `0 777`, its
           target uid 1000); `test_a_script_daemon_exec_checks_its_interpreter`
           (`#!/home/u/.venv/bin/python` reports uid 1000);
         - unstubbed: `test_real_system_paths_pass` — `require_root_owned_image /bin/sh x` and
           `require_root_owned_image /usr/bin/python3 x` exit 0 and print the resolved path (skip
           with a reason only when the test process is not root, since CI runners and the
           container differ; the container runs as root);
         - `test_a_relative_path_terminates` — `require_root_owned_image ./x x` for a temp file in
           the cwd exits (non-zero is fine) within 5 s.
      2. Implement 3.1's Linux parts: the three functions, the calls in `resolve_executables`, the
         usage text.
      3. Extend TestApplyLayoutLeavesSocketsAlone with
         `test_handoff_subdirectories_are_removed_and_never_walked` (parametrized over
         POSIX_PLATFORMS): plant `handoff/d/f`; add a `setpriv` stub that logs `setpriv $*` and
         then runs the command after `--clear-groups`; assert `handoff/d` no longer exists, no
         chown or chmod call names `handoff/d/f`, on Linux both chmod `find` passes ran through
         `setpriv --reuid=svc --regid=grp --clear-groups`, and on macOS every chmod call carries
         `-h`. Keep the existing assertions passing (update any that pinned the exact old find lines).
      4. Implement 3.1's `apply_layout` changes (both scripts) and the macOS
         `stage_trusted_image` interpreter check; add
         `test_macos_staging_checks_a_script_images_interpreter` the same way as step 1's
         interpreter test (macOS `stat -f` formats: read `require_trusted_image` for what to stub).
      5. Run python3 -m pytest tests/unit/test_privilege_separation.py -q and
         `bash -n scripts/linux_privilege_separation.sh scripts/macos_privilege_separation.sh`.
      6. Do not dispatch the graphical-session workflows here; r10a and the final checks do.
      Stop condition: if `setpriv` or `realpath -s` is missing on the Ubuntu images the deb supports
      (both are util-linux/coreutils; check debian/control's Depends), stop with status=blocked.
    acceptance:
      - "SEC-D2: python3 -m pytest tests/unit/test_privilege_separation.py -k \"DaemonImageMustBeRootOwned or staging_checks_a_script\" -q passes, with test_real_system_paths_pass run (not skipped) in the container"
      - "SEC-D4: python3 -m pytest tests/unit/test_privilege_separation.py -k handoff_subdirectories -q passes"
      - "SEC-D2: grep -n \"\\.venv/bin/privacyfence-app\" scripts/linux_privilege_separation.sh returns nothing"
      - bash -n scripts/linux_privilege_separation.sh scripts/macos_privilege_separation.sh exits 0

  - id: r5-pkexec-allow-list
    title: Accept only the companion's argument lists under pkexec, prompt per kind, and render the unit safely
    depends_on: [r4-linux-image-and-layout]
    complexity: M
    touches:
      - scripts/linux_privilege_separation.sh
      - installer/linux/eu.privacyfence.daemon-control.policy
      - tests/unit/test_privilege_separation.py
      - tests/integration/test_org_guide_configs.py
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.2. Findings: SEC-D3, TST-C10.
      1. tests/unit/test_privilege_separation.py, new `class TestPkexecAllowList`. Unit-test
         `check_pkexec_arguments` through `_function` extraction, with `ORIGINAL_ARGS` set as a
         bash array, `PKEXEC_UID=1000` and an `id` stub that prints `alice` for `-nu 1000`:
         - refused, exit 1 and "not allowed via pkexec": `enable --daemon-exec /tmp/x`,
           `enable --auto --daemon-exec /tmp/x`, `uninstall --purge`, `uninstall`,
           `enable --user bob`, `enable --machine-only`, `enable --for-user bob`;
         - accepted (exit 0): `daemon status`, `daemon restart`, `daemon ensure-running`,
           `enable --auto`, `enable --for-user alice`;
         - with no `PKEXEC_UID`, `enable --daemon-exec /tmp/x` exits 0.
         Whole-script runs (`["bash", str(INSTALLERS["linux"]), ...]`, as :3065 does) only for
         refused lists, which exit before anything else: one of them, with `PKEXEC_UID` set,
         exits 1 with "not allowed via pkexec".
      2. Implement 3.2: `ORIGINAL_ARGS`, `check_pkexec_arguments` and its call.
      3. Polkit: first check support:
         `strings "$(command -v pkexec)" | grep -c org.freedesktop.policykit.exec.argv1` on an
         Ubuntu 24.04 image (the container's, if it is one; otherwise read polkit 124's source
         `src/programs/pkexec.c` for that string). If it is supported, write the two actions of
         3.2; if not, stop with status=blocked and report what you found.
         tests/unit/test_privilege_separation.py: parse the policy with `xml.etree.ElementTree`
         and assert two `<action>`s, each with `exec.path` `/usr/sbin/privacyfence-privilege-separation`,
         `exec.argv1` `daemon` / `enable`, and its message from 3.2.
      4. tests/integration/test_org_guide_configs.py: `test_rendered_daemon_unit_verifies`:
         extract `render_template` and `sed_escape` from the Linux script with a local regex helper
         (copy `_function` from test_privilege_separation.py:1238-1242), stub `chown`/`chmod` on
         PATH, render `installer/linux/privacyfence-daemon.service.tmpl` with
         `DAEMON_EXECUTABLE=<a tmp stub executable>`, `SERVICE_ACCOUNT=nobody`,
         `SERVICE_GROUP=nogroup`, `SYSTEM_ROOT=<tmp dir>`, then run
         `systemd-analyze verify --man=no <unit>` as `test_systemd_analyze_verifies_the_unit`
         (:191-213) does (skip via `_require` without the binary). Add
         `test_rendered_unit_keeps_special_characters_literal` (an executable path containing `&`
         and `|` appears verbatim in `ExecStart=`); it fails until `sed_escape` exists.
      5. Implement `sed_escape` and use it in `render_template`.
      6. Run python3 -m pytest tests/unit/test_privilege_separation.py tests/integration/test_org_guide_configs.py -q
         (install systemd if `systemd-analyze` is missing: `apt-get install -y systemd` when
         available, else report the skip), and `bash -n scripts/linux_privilege_separation.sh`.
      7. Prove the unit check can fail: insert `Foo=bar` into a scratch copy of the template; the
         verify test must fail; restore. Report it.
    acceptance:
      - "SEC-D3: python3 -m pytest tests/unit/test_privilege_separation.py -k PkexecAllowList -q passes"
      - "SEC-D3: grep -c '<action id=' installer/linux/eu.privacyfence.daemon-control.policy returns 2"
      - "TST-C10: python3 -m pytest -rs tests/integration/test_org_guide_configs.py -k \"rendered_daemon_unit or special_characters_literal\" -q passes, with no skip when systemd-analyze is installed"
      - "TST-C10: the report shows the Foo=bar mutation failing the verify test"

  - id: r6-macos-pkg-scripts
    title: The macOS pkg runs its own copy of the separation script over a root-owned bundle
    depends_on: [r5-pkexec-allow-list]
    complexity: S
    touches:
      - installer/macos/pkg/postinstall
      - scripts/build_pkg.sh
      - tests/integration/test_macos_pkg_smoke.py
      - tests/unit/test_privilege_separation.py
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.3. Finding: SEC-D6.
      1. scripts/build_pkg.sh: copy the separation script and the two plist templates into the
         Scripts directory as 3.3 says (next to the existing postinstall copy at :130-131).
      2. installer/macos/pkg/postinstall: everything 3.3 lists, before the first use of the
         script. Keep `exit 0` on every path.
      3. tests/unit/test_privilege_separation.py (the assertion at :3082 checks
         `enable --auto --app "$APP_PATH"`, which stays true): add
         `test_postinstall_skips_a_bundle_not_owned_by_root` — extract `bundle_is_root_owned` from
         the postinstall, define `FIND` as a stub script that prints a path, and assert the
         function returns non-zero; and the inverse with a stub that prints nothing. Also assert
         the postinstall text computes `SEPARATION_SCRIPT` from `dirname "$0"` and contains
         `FIND=/usr/bin/find` as a plain assignment.
      4. tests/integration/test_macos_pkg_smoke.py:123-156: also require
         `Scripts/scripts/macos_privilege_separation.sh` (executable) and
         `Scripts/installer/macos/com.privacyfence.daemon.plist.tmpl` in the expanded pkg, and the
         postinstall to contain `dirname "$0"`.
      5. Run python3 -m pytest tests/unit/test_privilege_separation.py -q and `bash -n` on both
         changed shell files. The pkg smoke test runs only on macOS; r10a and the final checks
         dispatch build.yml and macos-graphical-session.yml.
    acceptance:
      - "SEC-D6: grep -n 'dirname \"\\$0\"' installer/macos/pkg/postinstall matches the SEPARATION_SCRIPT line"
      - "SEC-D6: grep -n \"macos_privilege_separation.sh\" scripts/build_pkg.sh matches a copy into the Scripts directory"
      - "SEC-D6: python3 -m pytest tests/unit/test_privilege_separation.py -k postinstall -q passes"

  - id: r7-floors-and-release-lock
    title: Raise the vulnerable dependency floors, add the release lock and a floor audit
    depends_on: []
    complexity: M
    touches:
      - pyproject.toml
      - requirements/runtime.lock.txt
      - requirements/dev.lock.txt
      - requirements/docs.lock.txt
      - requirements/release.in
      - requirements/release.lock.txt
      - requirements/README.md
      - scripts/update_dependency_locks.sh
      - .github/workflows/dependency-audit.yml
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.5. Finding: SEC-E5 (and the lock
      work SEC-E2/SEC-E4 need). `uv` is on PATH in the container.
      1. Reproduce: install pip-audit in .venv, then
         `uv pip compile --resolution lowest-direct --python-version 3.11 pyproject.toml -o /tmp/lowest.txt`
         and `pip-audit --strict --no-deps --disable-pip -r /tmp/lowest.txt`; record the count.
      2. pyproject.toml: apply 3.5's floors; rerun step 1 and raise any further floor it still
         reports until it prints "No known vulnerabilities found". Each raised floor gets a one-line
         comment naming why, in the starlette comment's style.
      3. requirements/release.in per 3.5. scripts/update_dependency_locks.sh: dev.lock compiled
         from pyproject (dev, test, lint) plus release.in with `-c requirements/runtime.lock.txt`
         (runtime compiled first); a fourth compile for release.lock.txt from release.in alone.
         dependency-audit.yml: `lockfile-freshness` regenerates and diffs all four the same way;
         `pip-audit` audits `release.lock.txt` blocking; the `floor-audit` job from 3.5.
      4. Run scripts/update_dependency_locks.sh and commit the locks. Check
         `grep -E "^(setuptools|wheel|setuptools-scm|build|boto3|cyclonedx-bom|uv|pip-audit)==" requirements/release.lock.txt`
         lists all eight and the same for dev.lock.txt; if setuptools or wheel is missing, stop
         with status=blocked.
      5. requirements/README.md: describe release.in and release.lock.txt, the dev lock's
         constraint on the runtime lock, and fix "exactly three places" for runtime.lock.txt
         (macos-graphical-session.yml installs it too).
      6. Run python3 -m pytest tests/unit -q -x (a failure caused by a new pypdf/PyJWT version is
         fixed in the test only if the test pinned old behaviour; otherwise stop with
         status=blocked and quote it), then `pip-audit --strict -r requirements/runtime.lock.txt`
         and the same for release.lock.txt.
    acceptance:
      - "SEC-E5: uv pip compile --resolution lowest-direct --python-version 3.11 pyproject.toml -o /tmp/l.txt && pip-audit --strict --no-deps --disable-pip -r /tmp/l.txt prints \"No known vulnerabilities found\""
      - "SEC-E4: test -f requirements/release.lock.txt && grep -c -- \"--hash=sha256:\" requirements/release.lock.txt is greater than 0"
      - "SEC-E5: grep -n \"floor-audit\" .github/workflows/dependency-audit.yml matches"
      - grep -n "release.in" scripts/update_dependency_locks.sh .github/workflows/dependency-audit.yml matches in both
      - "python3 -c \"import tomllib; assert 'release' not in tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']\" exits 0"

  - id: r8a-release-installs-from-locks
    title: Release and packaging jobs install every Python tool from hashed locks
    depends_on: [r7-floors-and-release-lock]
    complexity: M
    touches:
      - .github/workflows/build.yml
      - .github/workflows/publish-pypi.yml
      - .github/workflows/publish-mcp-registry.yml
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.5 (the "Workflows install from
      locks" bullet). Finding: SEC-E4.
      1. Apply 3.5 to every unpinned install the plan's section 2 lists in these three files
         (build.yml :96, :331, :389, :558, :612, :697, :736, :754, :791, :860, :961;
         publish-pypi.yml :187, :216, :365 and the sync build at :198; publish-mcp-registry.yml
         :147). Keep each step's name, its comments (reword those that say the extras are
         installed unpinned) and its `--quiet`/`--user` flags where present.
      2. `python -m build` → `python -m build --no-isolation` everywhere these files call it.
      3. Most of these steps are tag-only, so prove them in the container: in a fresh venv
         `pip install --require-hashes -r requirements/release.lock.txt`,
         `python -m build --no-isolation --outdir /tmp/d`,
         `python -m build --no-isolation --sdist --wheel sync --outdir /tmp/sd`,
         `python -c "import boto3, setuptools_scm"` and `cyclonedx-py --version`; and in another
         fresh venv `pip install --require-hashes -r requirements/runtime.lock.txt`,
         `pip install --require-hashes -r requirements/dev.lock.txt`,
         `pip install --no-deps --no-build-isolation -e .` and `pyinstaller --version`. Report
         each command's exit status.
      4. Validate each file with `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`.
      5. Dispatch build.yml against this phase's branch with the GitHub MCP `actions_run_trigger`
         and wait with `actions_get`; every job must be green. If a job fails on an install line,
         read its log with `get_job_logs`, fix, push and dispatch once more. Report the run URL.
      Stop condition: a second failure on the same install line after a fix is real; stop with
      status=blocked and quote the log.
    acceptance:
      - "SEC-E4: grep -nE 'pip install' .github/workflows/build.yml .github/workflows/publish-pypi.yml .github/workflows/publish-mcp-registry.yml | grep -v -- '--require-hashes' | grep -v -- '--no-deps --no-build-isolation -e \\.' returns nothing"
      - "SEC-E4: every command of step 3 exits 0 in the report"
      - "SEC-E4: the report links a green build.yml run dispatched on this phase's branch"

  - id: r8b-qa-runner-and-deploy-gates
    title: QA-runner jobs read-only and dispatched on main, and the Worker deploy gated to main
    depends_on: [r7-floors-and-release-lock]
    complexity: M
    touches:
      - .github/workflows/connector-live-check.yml
      - .github/workflows/qa-record-fixture.yml
      - .github/workflows/deploy-download-worker.yml
      - .claude/commands/qa-record.md
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2, 3.5 (QA runner bullet) and 3.6.
      Findings: SEC-E1 (repository half), SEC-E2, SEC-E6 (workflow half), SEC-E7.
      1. connector-live-check.yml: the venv step per 3.5; the permission split, the two
         artifacts and the `open-drift-pr` job per 3.6. Keep the concurrency group, the credential
         copy steps and the report artifact upload.
      2. qa-record-fixture.yml: the `branch` input (through `env:` only), the dispatch-on-main
         check, the refusal list including `refs/`, `pull/` and the `git ls-remote` existence
         check, the checkout of `refs/heads/<branch>`, the venv step, the artifact and the
         `commit-fixture` job per 3.6. Update the workflow's header comment.
      3. deploy-download-worker.yml: per 3.6, and the comment at :70-72 says what the `if:` and
         the environment enforce.
      4. .claude/commands/qa-record.md step 2 per 3.6.
      5. Validate each YAML file (`yaml.safe_load`). Do not dispatch these workflows: the QA runner
         holds live credentials and nothing in this plan needs a recording; the deploy is
         production.
    acceptance:
      - "SEC-E2: grep -nE 'pip install' .github/workflows/connector-live-check.yml .github/workflows/qa-record-fixture.yml | grep -v -- '--require-hashes' | grep -v -- '--no-deps --no-build-isolation -e \\.' returns nothing"
      - "SEC-E2: python3 -c \"import yaml; w=yaml.safe_load(open('.github/workflows/connector-live-check.yml')); assert w['jobs']['live-check']['permissions']=={'contents':'read'}; assert w['jobs']['open-drift-pr']['runs-on']=='ubuntu-latest'\" exits 0"
      - "SEC-E2: python3 -c \"import yaml; w=yaml.safe_load(open('.github/workflows/qa-record-fixture.yml')); j=w['jobs']; assert j['record']['permissions']=={'contents':'read'} and j['commit-fixture']['runs-on']=='ubuntu-latest'\" exits 0"
      - "SEC-E6: grep -n \"stable\" .github/workflows/qa-record-fixture.yml shows the refusal, and grep -n 'inputs.branch' .github/workflows/qa-record-fixture.yml shows it only in env: and checkout ref lines"
      - "SEC-E7: grep -n \"environment: downloads-worker\\|github.ref == 'refs/heads/main'\" .github/workflows/deploy-download-worker.yml shows both"

  - id: r10a-require-flags
    title: Packaged and browser tests fail instead of skipping in CI, and CI jobs time out
    depends_on: [r8a-release-installs-from-locks, r6-macos-pkg-scripts]
    complexity: M
    touches:
      - tests/conftest.py
      - tests/unit/test_conftest_require_flags.py
      - tests/integration/test_browser_smoke.py
      - tests/integration/test_settings_grist_modal_browser.py
      - tests/integration/test_org_ubuntu_release_smoke.py
      - .github/workflows/tests.yml
      - .github/workflows/website-build.yml
      - .github/workflows/build.yml
      - .github/workflows/linux-graphical-session.yml
      - .github/workflows/macos-graphical-session.yml
      - .github/workflows/windows-graphical-session.yml
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.7. Findings: TST-C1, TST-B15,
      TST-C8 (markers), TST-A8 (timeout-minutes).
      1. tests/unit/test_conftest_require_flags.py (new; docstring: the switches that turn a
         release-gating skip into a failure). Use `pytester` (`pytest_plugins = ["pytester"]`) and
         a conftest in the pytester dir that does `sys.path.insert(0, <repo root>)` and
         `from tests.conftest import pytest_runtest_makereport, pytest_collectreport`. Cases:
         a `packaged` test calling `pytest.skip("x")` fails with `PRIVACYFENCE_REQUIRE_PACKAGED=1`
         and skips without it; a `browser` test skipping with "Chromium not available" fails with
         `PRIVACYFENCE_REQUIRE_BROWSER=1`; a `browser` test skipping with "/docs/ was not built"
         stays a skip with it; a module with a module-level `pytest.skip(..., allow_module_level=True)`
         fails collection with `PRIVACYFENCE_REQUIRE_PACKAGED=1`; an unmarked skip stays a skip
         with both set.
      2. tests/conftest.py: both hooks per 3.7 (keep the existing diagnostics behaviour).
      3. Markers per 3.7: `pytest.mark.browser` in test_browser_smoke.py's pytestmark (it is
         `pytest.mark.timeout(60)` today; make it a list), a new pytestmark in
         test_settings_grist_modal_browser.py, and `pytest.mark.packaged` added to
         test_org_ubuntu_release_smoke.py's marks.
      4. Workflows per 3.7: the env switches and timeout-minutes.
      5. Prove it locally:
         `PRIVACYFENCE_REQUIRE_BROWSER=1 PRIVACYFENCE_TEST_CHROMIUM=/nonexistent python3 -m pytest tests/integration/test_browser_smoke.py -x -q`
         exits non-zero with a failure; `PRIVACYFENCE_REQUIRE_PACKAGED=1 python3 -m pytest tests/integration/test_deb_packaged_lifecycle.py -q`
         exits non-zero with no dist/ present; and
         `PRIVACYFENCE_REQUIRE_BROWSER=1 python3 -m pytest -m browser -q` (with the session's
         PRIVACYFENCE_TEST_CHROMIUM and no zensical installed) passes.
      6. Dispatch build.yml and the three graphical-session workflows against this phase's branch
         (all in ci.dispatchable) and wait. A packaged test that now fails because it skipped for
         a legitimate reason on that runner (for example `test_macos_pkg_smoke.py:255` on an
         unsigned dispatched pkg) is handled by narrowing the hook to reasons that mean the
         artifact is missing, written as a reason regex next to the browser one, never by
         removing the env var. Report the four run URLs.
    acceptance:
      - "TST-C1/TST-B15: python3 -m pytest tests/unit/test_conftest_require_flags.py -q passes with at least 6 tests"
      - "TST-B15: PRIVACYFENCE_REQUIRE_BROWSER=1 PRIVACYFENCE_TEST_CHROMIUM=/nonexistent python3 -m pytest tests/integration/test_browser_smoke.py -x -q exits non-zero"
      - "TST-C1: PRIVACYFENCE_REQUIRE_PACKAGED=1 python3 -m pytest tests/integration/test_deb_packaged_lifecycle.py -q exits non-zero"
      - "TST-C8: python3 -m pytest tests/integration/test_browser_smoke.py tests/integration/test_settings_grist_modal_browser.py -m \"not browser\" --co -q collects 0 tests"
      - "TST-C1: grep -c \"PRIVACYFENCE_REQUIRE_PACKAGED\" .github/workflows/build.yml returns 5"
      - "TST-A8: grep -c \"timeout-minutes\" .github/workflows/tests.yml is at least 7"
      - "TST-C1/TST-B15: the report links green build.yml and graphical-session runs on this phase's branch"

  - id: r10b-strict-markers-and-imports
    title: Strict markers, and plain imports for runtime dependencies in tests
    depends_on: [r10a-require-flags]
    complexity: S
    touches:
      - pyproject.toml
      - tests/integration/test_deferred_approval_round_trip.py
      - tests/integration/test_pending_approval_releases_nothing.py
      - tests/integration/test_mcp_daemon_contract.py
      - tests/integration/test_shim_mcp_contract.py
      - tests/integration/test_deb_packaged_lifecycle.py
      - tests/integration/test_org_ubuntu_release_smoke.py
      - tests/integration/test_macos_packaged_smoke.py
      - tests/integration/test_windows_packaged_smoke.py
      - tests/integration/test_linux_graphical_session_autostart.py
      - tests/integration/test_windows_graphical_session_autostart.py
      - tests/system/test_local_mode_system.py
      - tests/unit/test_org_bundle_signing.py
      - tests/unit/test_build_org_bundle.py
      - tests/unit/abuse/test_abuse_audit_export.py
      - tests/unit/test_audit_log.py
    brief: |
      Read docs/platform-and-ci-hardening-plan.md section 3.7. Findings: TST-C8 (strict markers),
      TST-A13. About 50 mechanical test lines and 3 lines of pyproject.toml.
      1. pyproject.toml `[tool.pytest.ini_options]`: `addopts = "--strict-markers"`; fix the
         markers comment so it says strict markers make a typo an error.
      2. Replace every `pytest.importorskip` of `mcp`, `cryptography` or `openpyxl` under tests/,
         one-line and multi-line forms, with a plain import of the same name (keep the variable
         name where the result was assigned, e.g. `import mcp as mcp_client`; for
         `test_audit_log.py`'s in-function calls, import at module top once). Leave playwright's.
      3. Run `python3 -m pytest --co -q` (must succeed) and python3 -m pytest
         tests/unit/test_audit_log.py tests/unit/test_org_bundle_signing.py
         tests/unit/test_build_org_bundle.py tests/unit/abuse tests/integration/test_deferred_approval_round_trip.py
         tests/integration/test_pending_approval_releases_nothing.py tests/integration/test_mcp_daemon_contract.py
         tests/system/test_local_mode_system.py -q, then ruff check .
    acceptance:
      - "TST-A13: grep -rn -A1 importorskip tests | grep -E '\"(mcp|cryptography|openpyxl)\"' returns nothing"
      - "TST-C8: grep -n 'addopts = \"--strict-markers\"' pyproject.toml matches"
      - python3 -m pytest --co -q exits 0

  - id: r11-coverage-ratchet
    title: Raise the coverage floors, add the missing ones, warn on stale floors, and test the script
    depends_on: [r2-shim-ownership, r10b-strict-markers-and-imports, r13-platform-and-e2e-tests, r14-fixture-hygiene]
    complexity: S
    touches:
      - scripts/check_coverage_floor.py
      - tests/unit/test_check_coverage_floor.py
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.8. Findings: TST-C3, TST-C4.
      1. tests/unit/test_check_coverage_floor.py (new; docstring: the ratchet gates every merge,
         so it must be able to fail). Load the script as tests/unit/test_tag_release.py:17-26
         does. Write a small `coverage.json` per case into tmp_path. Cases: overall below
         `OVERALL_FLOOR` → 1; one module below its floor → 1 and its name in stdout; a
         `MODULE_FLOORS` entry missing from `files` → 1 and "[MISSING]"; a missing file → 2;
         everything at its floor → 0 and "Coverage ratchet passed"; a module 2 points above its
         floor → 0 and a `::warning::stale coverage floor` line naming it.
      2. Get both coverage reports of 3.8: the full suite on this branch
         (`python3 -m pytest -q --cov=src/privacyfence --cov-branch --cov-report=json:/tmp/cov-local.json`,
         with PRIVACYFENCE_TEST_CHROMIUM set as the session hook says), and the `coverage-report`
         artifact of the latest green tests.yml run on main.
      3. scripts/check_coverage_floor.py: floors and new entries per 3.8 from the minimum of the
         two; fix the two stale comments; add the staleness warning to `main()`.
      4. Run `python3 scripts/check_coverage_floor.py /tmp/cov-local.json` (passes) and
         python3 -m pytest tests/unit/test_check_coverage_floor.py -q. Prove one case can fail: in a
         scratch copy change `ok = actual >= floor` to `ok = True`; the below-floor test fails.
      Do not commit any coverage.json.
    acceptance:
      - "TST-C4: python3 -m pytest tests/unit/test_check_coverage_floor.py -q passes with at least 6 tests"
      - "TST-C4: the report shows the ok = True mutation failing the below-floor test"
      - "TST-C3: python3 scripts/check_coverage_floor.py /tmp/cov-local.json prints \"Coverage ratchet passed.\""
      - "TST-C3: grep -c '\"src/privacyfence/web/mcp_dispatch.py\"' scripts/check_coverage_floor.py returns 1"

  - id: r12-graphical-coverage-paths
    title: Graphical-session workflows watch the modules they exercise, and the gate reports later changes
    depends_on: [r10a-require-flags]
    complexity: M
    touches:
      - .github/workflows/linux-graphical-session.yml
      - .github/workflows/windows-graphical-session.yml
      - .github/workflows/macos-graphical-session.yml
      - scripts/check_graphical_session_coverage.py
      - tests/unit/test_check_graphical_session_coverage.py
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.9. Finding: TST-C9.
      `watched_paths` stops at the first non-blank line under `paths:` that is neither a comment
      nor a `      - ` item.
      1. Tests first in tests/unit/test_check_graphical_session_coverage.py (follow its patterns:
         the module is loaded by path, `TestEvaluate`, `TestCheckAll` with monkeypatched helpers):
         `TestWatchedPaths` reads a temp workflow file with quoted and unquoted entries and a
         comment line; `TestEvaluate::test_a_green_run_older_than_a_watched_change_is_a_gap`;
         `TestCheckAll::test_stale_files_are_reported` (monkeypatch `changed_since`); a real-git
         `TestChangedSince` like `TestIsAncestor`.
      2. Implement 3.9 in the script; `check_all` calls `watched_paths` on
         `REPO_ROOT / ".github/workflows" / workflow` and `changed_since(run["head_sha"], commit)`
         only when the run exists and is an ancestor.
      3. Add the paths per 3.9 to the three workflows.
      4. Run python3 -m pytest tests/unit/test_check_graphical_session_coverage.py -q.
    acceptance:
      - "TST-C9: python3 -m pytest tests/unit/test_check_graphical_session_coverage.py -k \"WatchedPaths or older_than_a_watched_change or stale_files or ChangedSince\" -q passes"
      - "TST-C9: python3 -c \"import yaml; p=yaml.safe_load(open('.github/workflows/windows-graphical-session.yml'))[True]['push']['paths']; assert 'src/privacyfence/windows_service.py' in p\" exits 0"

  - id: r13-platform-and-e2e-tests
    title: Windows and macOS control-channel peer tests, and the built shim against a real daemon
    depends_on: [r10b-strict-markers-and-imports]
    complexity: M
    touches:
      - tests/platform/test_control_channel_windows.py
      - tests/platform/test_control_channel_macos.py
      - tests/system/test_local_mode_system.py
      - .github/workflows/tests.yml
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 2 and 3.10. Findings: TST-B16, TST-C11.
      1. tests/platform/test_control_channel_windows.py and test_control_channel_macos.py exactly
         as 3.10 spells them out.
      2. tests/system/test_local_mode_system.py: `test_shim_against_real_daemon_process` per 3.10.
      3. .github/workflows/tests.yml `test` job pytest step: `PRIVACYFENCE_REQUIRE_NODE: "1"`.
      4. Locally: python3 -m pytest tests/system/test_local_mode_system.py tests/platform -q (Node
         22 and `cd mcpb/shim && npm ci && npm run build` are available in the container). Prove
         the shim test can fail: change the `MINT MCP` command string in a scratch copy of
         mcpb/shim/src/controlChannel.ts, rebuild, the test fails; restore and rebuild. Report it.
      5. Do not dispatch tests.yml (the steward skill: cross-platform pytest runs on every PR).
         The two platform modules are checked on the PR's own run (final_checks). Write them so
         a Windows-only API is imported inside the test, after the skipif, as
         tests/platform/test_windows_acls.py does.
    acceptance:
      - "TST-C11: python3 -m pytest tests/system/test_local_mode_system.py -k shim_against_real_daemon_process -q passes"
      - "TST-C11: the report shows the MINT MCP mutation failing the test"
      - "TST-B16: python3 -m pytest tests/platform/test_control_channel_windows.py tests/platform/test_control_channel_macos.py -q collects both modules and skips them on Linux with their platform reasons"

  - id: r14-fixture-hygiene
    title: Replay every committed fixture, scan fixtures for identifiers, and reset the mint nonces
    depends_on: [r10a-require-flags]
    complexity: S
    touches:
      - tests/conftest.py
      - tests/unit/test_grist_client.py
      - tests/unit/test_qa_fixture_recorder.py
      - scripts/qa_fixture_recorder.py
      - tests/fixtures/live/gmail/get_message.json
      - tests/fixtures/live/jira/list_projects.json
      - tests/fixtures/live/jira/get_issue.json
    brief: |
      Read docs/platform-and-ci-hardening-plan.md section 3.10 (last three bullets). Findings:
      TST-B18, TST-B19, TST-B21.
      1. tests/unit/test_grist_client.py::TestLiveFixtureParsing (:655):
         `test_list_documents_fixture_still_parses` (`docs = client.list_documents()` through
         `_client_returning(self._load("list_documents.json"))`; assert
         `docs and all(d.id and d.name for d in docs)`).
      2. tests/unit/test_qa_fixture_recorder.py: the meta-test and the identifier scan per 3.10.
         Run the scan: it must fail on gmail/get_message.json and jira/list_projects.json.
      3. scripts/qa_fixture_recorder.py: extend `redact_gmail_message` (:315) and the Jira
         redaction (`redact_jira_issue`, :414, and whatever function list_projects uses) per 3.10.
         Then rewrite the committed fixtures by loading each JSON, applying the matching redact
         function, and writing it back with the formatting the recorder uses (read its writer
         for indent and key order). Read the resulting diff: only the Message-ID and entityId
         values may change.
      4. tests/conftest.py `_reset`: `control_channel._mint_nonces.clear()` (import
         `privacyfence.web.control_channel` the way the other resets import their modules).
      5. Run python3 -m pytest tests/unit/test_grist_client.py tests/unit/test_qa_fixture_recorder.py
         tests/unit/web/test_control_channel.py -q and ruff check .
    acceptance:
      - "TST-B18: python3 -m pytest -k \"TestLiveFixtureParsing or fixture_still_parses\" --co -q tests/unit collects at least 21 tests"
      - "TST-B19: python3 -m pytest tests/unit/test_qa_fixture_recorder.py -k \"unredacted_identifiers or every_expected_fixture\" -q passes"
      - "TST-B19: grep -rn \"@mail.gmail.com\" tests/fixtures/live returns nothing"
      - "TST-B21: grep -n \"_mint_nonces\" tests/conftest.py matches"

  - id: r15-policy-docs
    title: Testing policy, definition of done and runner docs match what CI requires
    depends_on: [r3-worker-coverage, r8b-qa-runner-and-deploy-gates, r11-coverage-ratchet, r12-graphical-coverage-paths]
    complexity: M
    touches:
      - docs/testing-policy.md
      - docs/coding-and-testing-guidelines.md
      - .claude/toolkit.yaml
      - scripts/pre_release_check.py
      - tests/unit/test_pre_release_check.py
      - tests/unit/test_definition_of_done_drift.py
      - scripts/update_branch_protection.py
      - docs/connector-qa.md
      - docs/releasing.md
    brief: |
      Read docs/platform-and-ci-hardening-plan.md sections 3.6, 3.7, 3.10 and 3.11. Findings:
      DOC-1, TST-C7, and the doc halves of SEC-E1, SEC-E6, TST-B15, TST-C1, TST-C8.
      1. tests/unit/test_definition_of_done_drift.py: add
         `test_every_required_check_is_named_in_the_testing_policy` (import
         `REQUIRED_STATUS_CHECKS` from scripts/update_branch_protection.py the way the file already
         loads scripts; for a matrix check like "Sync client (Python 3.11)" assert the stem before
         " (" appears). It fails today on the sync checks.
         tests/unit/test_pre_release_check.py: assert a `"sync tests"` key.
      2. Make them pass: docs/testing-policy.md, docs/coding-and-testing-guidelines.md (§2.7 row,
         §4), .claude/toolkit.yaml (`dod_conditional` gains `when: ["sync/**"]` with the three
         commands and the README-copy note; and the `ci.dispatchable` note for
         `qa-record-fixture.yml` now says it is dispatched on `main` with inputs `connector` and
         `branch` and commits to that branch), scripts/pre_release_check.py, and the comment in
         scripts/update_branch_protection.py:79 — all per 3.11. The toolkit.yaml header says
         `verify.*` mirrors §2.7: keep them in step.
      3. docs/testing-policy.md also gets: the three REQUIRE switches (3.7, 3.10) where it says
         browser tests skip without Chromium; `timeout-minutes`; the floor-audit job, release.in
         and release.lock.txt in the dependency-audit paragraph; the qa-record-fixture
         dispatch-on-main change in layer 5; the marker paragraph.
      4. docs/connector-qa.md "Runner requirements" (:414-433) and "Recording one connector":
         per 3.6. docs/releasing.md "Cutting a release" (:19-45): per 3.6's releasing bullet.
      5. Run python3 -m pytest tests/unit/test_definition_of_done_drift.py tests/unit/test_pre_release_check.py
         tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py -q, and validate
         .claude/toolkit.yaml with `python3 <devflow root>/scripts/validate_profile.py .claude/toolkit.yaml`
         (find the root as the project-profile skill says).
    acceptance:
      - "DOC-1: grep -n \"Sync client\" docs/testing-policy.md matches at least one line"
      - "DOC-1: grep -n 'sync/\\*\\*' docs/coding-and-testing-guidelines.md .claude/toolkit.yaml matches a line in each"
      - "TST-C7: grep -c \"Not in \\`REQUIRED_STATUS_CHECKS\\`\" docs/testing-policy.md returns 1"
      - "TST-C7: grep -n \"'3.11', '3.12', '3.14'\" scripts/update_branch_protection.py matches"
      - "DOC-1: python3 -m pytest tests/unit/test_definition_of_done_drift.py tests/unit/test_pre_release_check.py -q passes"
      - "SEC-E1: grep -n \"Selected workflows\" docs/connector-qa.md matches"
      - "SEC-E6: grep -n \"git push origin <tag>\" docs/releasing.md returns nothing"

  - id: r16-retire
    title: ADRs 0181-0185 and 0187, changelog, and delete the plan
    depends_on: [r15-policy-docs, r13-platform-and-e2e-tests]
    complexity: M
    touches:
      - docs/adr/0181-*.md
      - docs/adr/0182-*.md
      - docs/adr/0183-*.md
      - docs/adr/0184-*.md
      - docs/adr/0185-*.md
      - docs/adr/0187-*.md
      - docs/adr/0019-live-connector-credentials-only-on-a-self-hosted-runner.md
      - docs/adr/0021-release-tag-push-never-uses-github-token.md
      - docs/adr/0027-a-group-member-cannot-take-over-another-members-companion-socket.md
      - docs/adr/README.md
      - docs/security-and-compliance.md
      - CHANGELOG.md
      - docs/platform-and-ci-hardening-plan.md
      - docs/platform-and-ci-hardening-plan-manual-steps.html
    brief: |
      Read docs/platform-and-ci-hardening-plan.md (whole) and docs/adr/README.md.
      1. Six ADRs with the template, Status "Accepted — <today>.", from section 4 and the design
         sections they cite: 0181 (3.6; "Amends 0019"; GitHub Team runner groups now enforce
         Decision 2, and qa-record-fixture.yml is dispatched on main), 0182 (3.5; it extends ADR
         0085's lockfile rule from AI-client CLIs to every build and release tool; release.in
         rejected as an extra because it would ship in PyPI metadata), 0183 (3.2; the argv
         allow-list is the guard, the two polkit actions make the prompt honest), 0184 (3.3; the
         rejected codesign check and the accepted admin-swap race), 0185 (3.6 releasing bullet;
         "Amends 0021"; the bypass is the repository admin role, and an admin could still push a
         tag by hand), 0187 (3.4; "Amends 0027"; only the daemon's control channel replaces a
         foreign socket, the companion channel keeps refusing).
      2. ADRs 0019, 0021 and 0027: one Status line each in the existing format,
         "Amended by [ADR 0181](0181-<file>.md)." and so on. docs/adr/README.md: six index rows;
         the 0019, 0021 and 0027 rows' status cells name the amending ADR.
      3. docs/security-and-compliance.md: in the supply-chain paragraph (around :422), one
         sentence that release and QA-runner jobs install every build tool from hash-pinned
         locks; in the privilege-separation material, one sentence each that the Linux script
         refuses a daemon image a non-root account can write and accepts only the companion's own
         requests under pkexec.
      4. CHANGELOG.md under "## [Unreleased]" (never a version heading), Security unless noted:
         - "Linux: separating an install refuses a daemon program that a non-root account can
           modify, and the password prompt now names what it is for and only ever starts, stops
           or restarts the service or sets it up for your account."
         - "macOS: the installer runs its own copy of the setup script and skips setup when the
           app bundle is not owned by root."
         - "Claude Desktop extension: it now connects only to a PrivacyFence on this computer
           whose files belong to PrivacyFence's own service account."
         - "Raised minimum versions of pypdf, PyJWT and urllib3 for `pip install privacyfence`, to
           releases without known advisories."
         Internal-only CI changes need no entry.
      5. git rm docs/platform-and-ci-hardening-plan.md docs/platform-and-ci-hardening-plan-manual-steps.html;
         grep -rn "platform-and-ci-hardening-plan" . --include=*.md --include=*.html --include=*.py
         --include=*.yml returns nothing.
      6. Run python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py
         tests/unit/test_docs_links.py -q and ruff check .
    acceptance:
      - ls docs/adr/0181-*.md docs/adr/0182-*.md docs/adr/0183-*.md docs/adr/0184-*.md docs/adr/0185-*.md docs/adr/0187-*.md lists six files, each containing "Accepted"
      - grep -n "Amended by \[ADR 0181\]" docs/adr/0019-*.md, grep -n "Amended by \[ADR 0185\]" docs/adr/0021-*.md and grep -n "Amended by \[ADR 0187\]" docs/adr/0027-a-group-member-cannot-take-over-another-members-companion-socket.md all match
      - test ! -e docs/platform-and-ci-hardening-plan.md && test ! -e docs/platform-and-ci-hardening-plan-manual-steps.html
      - python3 -m pytest tests/unit/test_code_no_history.py tests/unit/test_docs_no_history.py tests/unit/test_docs_links.py -q passes
```
