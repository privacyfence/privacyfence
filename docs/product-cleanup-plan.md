# Product cleanup plan: audit fixes and legacy-code removal

**Active plan.** This file is temporary, per [`README.md`](README.md)'s documentation rules and
`CLAUDE.md` "Decisions, plans and ADRs": it lives here while the phases below are open and is
deleted by the PR that lands the last phase, after every decision it records has an ADR (see
[ADRs this plan creates](#adrs-this-plan-creates)).

It holds the **code** changes that the 2026-09-24 source-code audit of the documentation turned
up. The documentation and website work that audit also produced stays in
[`website-plan.md`](website-plan.md), which depends on this plan: its Wave 1 documents the code
*after* these phases, so every phase here should land before (or with a clear note in) the doc
PR that describes the same behavior.

## Contents

- [How to run this plan](#how-to-run-this-plan) — for the orchestrating session
- [Decisions](#decisions)
- [Status](#status)
- [Dependency graph](#dependency-graph)
- [Phase briefs](#phase-briefs) — one per session / PR
- [ADRs this plan creates](#adrs-this-plan-creates)
- [Found during implementation, not fixed](#found-during-implementation-not-fixed)
- [Inputs needed from the maintainer](#inputs-needed-from-the-maintainer)
- [Retiring this plan](#retiring-this-plan)

## How to run this plan

This plan is written to be executed by asking Claude to *"implement \<URL of this file\>"*. The
session that receives that request is the **orchestrator**; it does not implement phases itself
unless it has no way to delegate.

**Orchestrator loop:**

1. **Establish state from GitHub, not from memory.** For each phase ID in [Status](#status), search
   the repo's pull requests for the tag `[cleanup <ID>]` in the title. Merged → done. Open → in
   progress (follow it, don't start a second one). Neither → not started. The Status table in
   this file is a convenience that phase PRs update; GitHub wins if they disagree.
2. **Pick every phase whose dependencies are all merged** (see [Dependency graph](#dependency-graph))
   and which is not blocked on a [maintainer input](#inputs-needed-from-the-maintainer). If a
   phase is blocked on an input, ask the maintainer for it once, with the exact question from
   that section, and carry on with other phases.
3. **Start one worker per ready phase**, each in its own session and branch, with the phase's
   brief as its task. Preferred: a new Claude Code session per phase (`create_session` against
   this repo at `main`), prompted with *"Implement phase \<ID\> of \<URL of this file\>. Follow the
   brief and the Worker rules exactly; open a PR titled `[cleanup <ID>] <summary>` and drive it to
   green."* If sessions can't be created, run phases one at a time in the orchestrator's own
   session, each on its own branch from an up-to-date `main`. Parallel phases must be in
   different lanes (the graph marks them); never run two phases of the same lane at once.
4. **Report** to the maintainer after each round: which PRs are open, which are green and waiting
   on review, which are blocked and on what. Merging is the maintainer's decision — never merge a
   phase PR, never approve one.
5. **Repeat** when PRs merge. After phase **X2** merges, the plan is finished.

**Worker rules** (every phase):

- Read `CLAUDE.md`, `.claude/skills/steward/SKILL.md` and
  [`coding-and-testing-guidelines.md`](coding-and-testing-guidelines.md) §2.7 first. The steward
  skill says which checks must be dispatched to a GitHub Actions runner rather than run locally
  (packaged installers, graphical-session autostart, live connectors) — each brief names which ones
  apply.
- Branch from an up-to-date `main`; one phase = one branch = one PR. PR title
  `[cleanup <ID>] <summary>` (put the `fix:`/`chore:` type in the summary if the branch name can't
  carry it). PR body: link to this file's brief, what changed, what was verified and how (dispatched
  run links included).
- Stay inside the brief's scope. Anything else found along the way goes in the PR description under
  "Found, not fixed" — not into the diff.
- Every behavior change gets a test and a `CHANGELOG.md` line under `## [Unreleased]` (never a
  version heading). Removals go under "Removed".
- If this file is on `main`, update its [Status](#status) row for the phase in the same PR (→
  `in review`, with the PR link), and correct the brief in the same PR if it turned out wrong.
  If it is not on `main` yet (it was written on the `claude/documentation-refactoring` branch),
  leave it alone: PR titles are the state, and brief corrections go in the PR description for the
  orchestrator to fold back in.
- Do not edit docs that [`website-plan.md`](website-plan.md) Wave 1/2 will rewrite, beyond fixing a
  link or sentence your change makes false. Do fix every code comment, docstring, error message and
  test your change makes false.
- Run the `/dod` gate before asking for review; the PR is ready when §2.7 is green and every
  dispatch named in the brief has a green run linked.

## Decisions

Carried over from `website-plan.md` (question IDs kept so the two plans stay cross-referenceable).

| # | Decision |
|---|---|
| G1 | **There are no existing users.** Nothing needs an upgrade path from an earlier layout, format or tool name. |
| G3 | **Legacy and migration code is removed** (approved 2026-09-24). Uninstall rule: removing the package stops PrivacyFence and **leaves data in the system root**; purging deletes it. Nothing moves data back into a home directory. |
| G4 | **Telegram ships in the PyPI build** too (approved 2026-09-24), accepting that the app's `api_id`/`api_hash` become readable in the published sdist/wheel — as they already are, with more effort, inside the DMG, installer and `.deb`. |
| H1 | **Anything a current fresh install uses stays.** `enforce_separation` for a packaged install that somehow isn't separated stays unless a phase shows it unreachable. |
| H2 | **An alpha tag gates the next stable release.** Installer changes are exercised only by `build.yml`'s packaged smoke tests and the graphical-session workflows, so after the L phases an alpha is cut and must be green before any stable release ships them. |

## Status

| ID | Phase | Lane | Depends on | Status |
|---|---|---|---|---|
| P1 | Apps Script connectable from Settings | A | — | merged ([#662](https://github.com/privacyfence/privacyfence/pull/662)) |
| P2 | Recovery-code route hardening | B | — | merged ([#663](https://github.com/privacyfence/privacyfence/pull/663)) |
| P3 | Org bundle: bind `127.0.0.1` by default, `agent_links` flag | C | — | merged ([#661](https://github.com/privacyfence/privacyfence/pull/661)) |
| P4 | Telegram credentials in the PyPI build | D | — (input resolved) | merged ([#664](https://github.com/privacyfence/privacyfence/pull/664)) |
| P5 | Declare minimum OS versions | E | — | merged ([#665](https://github.com/privacyfence/privacyfence/pull/665)) |
| L1 | Remove policy v1 settings conversion (+ ADR) | A | P1 | merged ([#670](https://github.com/privacyfence/privacyfence/pull/670)) |
| L2 | Remove legacy file-location moves (Python + shim) | B | L1, P2 | merged ([#671](https://github.com/privacyfence/privacyfence/pull/671)) |
| L3 | Linux: installer cleanup and remove/purge semantics | E | L2, P5 | merged ([#673](https://github.com/privacyfence/privacyfence/pull/673)) |
| L4 | macOS: installer cleanup and uninstall semantics | E | L3 | merged ([#672](https://github.com/privacyfence/privacyfence/pull/672)) |
| L5 | Windows: installer cleanup and uninstall semantics | E | L4 | merged ([#674](https://github.com/privacyfence/privacyfence/pull/674)) |
| X1 | Pre-flight and alpha tag | — | all above | not started |
| X2 | Retire this plan | — | X1 | not started |

## Dependency graph

```
Lane A  P1 ───────────► L1 ─┐
Lane B  P2 ─────────────────┴─► L2 ─┐
Lane C  P3                          │
Lane D  P4 (needs secrets)          │
Lane E  P5 ─────────────────────────┴─► L3 (Linux) ─► L4 (macOS) ─► L5 (Windows) ─► X1 ─► X2
```

- **Round 1** can run P1, P2, P3, P4, P5 in parallel — they touch disjoint files.
- **L1 → L2 → L3 → L4 → L5 are sequential** because they share `settings_controller.py`,
  `daemon_main.py`, `paths.py` and `privilege_separation.py`; running them in parallel only buys
  merge conflicts in security-relevant code. L1 waits for P1 (both edit `settings_controller.py`),
  L2 for P2 (both edit the web/auth layer), L3 for P5 (both edit `debian/`).
- **X1** waits for every phase including P3 and P4, so the alpha exercises everything.

## Phase briefs

Evidence line numbers were re-checked against `main` at `d89250d2` (2026-09-24, after the
policy-surface consolidation PSC-2…6, agent attribution AGT-1…6 and the 4.3.0/4.4.0 release PRs);
re-locate by symbol name if they have moved since.

**What those merges already changed for this plan:**
- The deprecated MCP tools `privacyfence_list_auto_accept_rules` and
  `privacyfence_propose_auto_accept_rule_change` and `gate.propose_rule_change` were deleted by
  #633 (PSC-3). L1 is smaller accordingly.
- There is now one approval route module and one settings dispatcher for both modes
  (`routes_approvals.py`, `routes_settings.py`; ADR 0033), and sensitive settings writes need
  step-up in both modes (ADR 0034). P1 adds Apps Script once, not per mode.
- `maybe_auto_enable_macos` is now called from `privilege_separation.enforce_separation`, not from
  `companion.py` — see L4.
- ADRs are numbered up to 0038; the release pre-flight is ADR 0030 (`/cut-release`).

### P1 — Apps Script connectable from Settings

**Problem.** Apps Script is the one Google connector that cannot be authenticated from the Settings
page, locally or in organization mode. Its only path is `privacyfence-app --apps-script-oauth`,
which no user doc a new user would find explains.

**Evidence.** `settings_controller.py` `ALL_CONNECTORS` (≈l.234) and `GOOGLE_CONNECTORS` (≈l.247)
omit it; `web/routes_connect.py` `GOOGLE_SCOPES`/`OAUTH_SERVICES` (≈l.94–100) have no Apps Script
entry; `apps_script_client.py`'s error messages and `daemon_main.py` still point at
`--apps-script-oauth`.

**Do.**
- Add Apps Script to the Settings connector list and the Google OAuth connect flow, with its own
  scopes, so Authenticate/Reconnect and the status pill work like Gmail's. Since ADR 0033 there is
  one settings dispatcher and one renderer for both modes, so this is one change, not two.
- In org mode, make sure the org bundle's Google section covers it and the redirect URI
  (`/oauth/callback/<service>`) is registered by the same code path as the other five.
- Update `apps_script_client.py`'s "re-authorize" messages to point at Settings. Keep
  `--apps-script-oauth` only if another current flow uses it; otherwise remove it (G1).
- Tests: controller/route unit tests mirroring the existing Gmail ones; browser smoke test if one
  covers the connector list.

**Verify.** `/dod`. `scripts/qa_web_smoke.py` locally (Chromium is in the container). No live-
connector dispatch needed unless the fixture for Apps Script changes.

**Done when.** A fresh local install can connect Apps Script from Settings; org mode shows it with
the other Google connectors; no user-facing string mentions `--apps-script-oauth` unless it still
exists.

**As landed ([#662](https://github.com/privacyfence/privacyfence/pull/662)) — corrections to this brief:**

- `daemon_main.py` never told users to run `--apps-script-oauth`; it only defines the flag next to its five Google siblings. The flag stays: `scripts/qa_authenticate_connectors.py` uses it.
- The org bundle's single `google` section already covered Apps Script; no bundle or script change was needed. Org admins must register `https://<server>/oauth/callback/apps_script` (CHANGELOG says so).

### P2 — Recovery-code route hardening

**Problem.** The step-up recovery-code route (`POST /security/recover`) audits only a successful
use — its docstring says it records every attempt — accepts any signed-in session including an
unattested one, and has no attempt rate limit. Low severity — the code is 64 random bits — but the
missing audit and the unattested-session gap are real. Still true on `main` after the PSC merges.

**Evidence.** `web/routes_security.py` `recover_credential` (≈l.576–610): returns 401 on a bad code
before `_audit` runs, and never calls `session_auth.is_human_session`; `webauthn_stepup.py`
`consume_recovery_code`.

**Do.**
- Write an audit event for every failed and successful recovery attempt, in the same format as
  other step-up events.
- Require a human-attested session: `session_auth.is_human_session` /
  `human_session_required_json`, the same check the other step-up management routes use. Check
  how org mode resolves the principal here (org sessions come from the IdP, ADR 0033's auth
  adapter) and apply the equivalent there rather than refusing every org user.
- Rate-limit attempts per session and globally (reuse any existing limiter; otherwise a small
  in-memory one with a stated window), returning a clear error when exceeded.
- Tests for each of the three, including the audit record contents.

**Verify.** `/dod`; `bandit` clean. This is a trust-boundary change: say so in the PR and ask for
the maintainer's review explicitly.

**Done when.** Failed attempts appear in the audit log; an unattested session is refused; the
N+1th attempt in the window is refused.

**As landed ([#663](https://github.com/privacyfence/privacyfence/pull/663)) — corrections to this brief:**

- On an unseparated local install the route still accepts a non-attested session on purpose — no human session can exist there (the existing `require_human_session` rule). On a separated install it is refused.
- There was no existing limiter in `src/`; a new in-memory one was added.

### P3 — Org bundle: bind `127.0.0.1` by default, `agent_links` flag

**Problem.** `build_org_bundle.py --server-bind-host` defaults to `0.0.0.0` while the org guide says
never to expose the listener. `agent_links` can only be set by hand-editing the bundle.

**Evidence.** `scripts/build_org_bundle.py` ≈l.204; `org_mode.py` ≈l.165 (`agent_links: bool =
True`), ≈l.211.

**Do.**
- Default `--server-bind-host` to `127.0.0.1`; update its help text. Binding elsewhere stays
  possible by passing the flag explicitly.
- Add `--agent-links/--no-agent-links` (default unchanged: on) writing the bundle's `agent_links`.
- Check `org_mode.py`'s own default for a bundle *without* a bind host, and align it with the
  script's if they differ.
- Tests for both flags and the defaults; update any test that assumed `0.0.0.0`.

**Verify.** `/dod`.

**Done when.** A bundle built with no flags binds loopback; `agent_links` is settable from the CLI.

### P4 — Telegram credentials in the PyPI build

**Blocked on** the maintainer confirming the `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` secrets are
available to `publish-pypi.yml` (see [inputs](#inputs-needed-from-the-maintainer)).

**Problem.** Telegram doesn't work on a PyPI install: the DMG, installer and `.deb` builds generate
`_telegram_credentials.py` from secrets (`build.yml` ≈l.195, 392, 536); `publish-pypi.yml` does not.

**Do.**
- In `publish-pypi.yml`'s build job, pass the two secrets and generate `_telegram_credentials.py`
  exactly as `build_dmg.sh`/`build_deb.sh` do (reuse their generator, don't copy it).
- The file is git-ignored and `setuptools_scm` packages only tracked files: add an explicit include
  (package data / `MANIFEST.in`) so it lands in both sdist and wheel when present.
- Add a build-job step that inspects the built wheel and sdist and **fails on a stable tag** if the
  file is missing; warns otherwise. Local/dev builds without the secrets must keep working.
- CHANGELOG: Telegram works on PyPI installs.
- Record G4 in an ADR (see [ADRs](#adrs-this-plan-creates)).

**Verify.** `/dod`. `python -m build` locally with and without the env vars and check the archive
contents both ways. `publish-pypi.yml` itself only runs fully on a tag — its first real exercise is
X1's alpha; say so in the PR.

**Done when.** A wheel built with the secrets contains the credentials module; one built without
them builds cleanly and doesn't.

**As landed ([#664](https://github.com/privacyfence/privacyfence/pull/664)) — corrections to this brief:**

- There was no shared generator to reuse: each build script (including `build_installer.ps1`) had its own heredoc. P4 added one shared generator and switched all of them to it, which is why `build.yml` was dispatched.
- ADR is **0040**.

### P5 — Declare minimum OS versions

**Problem.** Minimum OS versions aren't declared in the installers, so an unsupported system gets
an install that fails at runtime instead of a clear refusal.

**Do.**
- `installer/privacyfence.iss`: `MinVersion` for the lowest supported Windows (confirm from
  `docs/platform-support.md` and the CI runner images; ask if they disagree).
- `debian/control`: express the Debian/Ubuntu floor through dependency versions (e.g. the Python
  and systemd versions the package actually needs) rather than a distro name check.
- macOS: the app bundle already declares `LSMinimumSystemVersion` 13.0 (`PrivacyFenceApp.spec`
  ≈l.213); the `.pkg` has no OS check of its own (`scripts/build_pkg.sh`,
  `installer/macos/pkg/`) — add one so the installer refuses instead of installing an app that
  won't launch.
- Unit tests that read the three declarations, so they can't silently drift from
  `platform-support.md`'s matrix.

**Verify.** `/dod`; dispatch `build.yml` against the branch (steward table) — all three packaged
jobs must be green.

**Done when.** All three installers state a floor that matches the support matrix, enforced by test.

**As landed ([#665](https://github.com/privacyfence/privacyfence/pull/665)) — corrections to this brief:**

- `platform-support.md` had no floors to confirm from; P5 added the Minimum OS column.
- The `.deb`'s real floor is glibc, set by the build runner, so P5 added a build-time check to stop it drifting on runner-image bumps.
- The macOS floor includes the CPU architecture (arm64 only), at the maintainer's request.
- P5 needed an ADR of its own (distribution path): **0039**.

### L1 — Remove policy v1 settings conversion

**Already done on `main`:** the two deprecated MCP tools and `gate.propose_rule_change` (#633,
PSC-3, closing ADR 0004 decision 3). What remains is the v1 → v2 *settings* conversion, which
ADR 0004 decisions 4–5 deliberately kept as the one remaining caller of `policy/compat.py` and
`policy/resource_registry.py`.

**Removes.**
- `policy/compat.py` (whole module); `daemon_main._migrate_settings_to_policy_v2` and its two call
  sites; `settings_controller.policy_v2_migration_notice_html` and whatever renders it;
  `settings_controller.RULES_BY_OPERATION` / `GRANT_RESOURCE_TYPES` (v1 predicate tables —
  verify no current caller first); `auto_accept.migrate_telegram_search_operation_key`; the
  `replaces=(…)` aliases for old condition names in `policy/conditions.py`; any v1 handling in
  `policy/store.py`; the migration-only parts of `policy/resource_registry.py` (its name-resolution
  callbacks stay — `resource_names.py` still uses them); leftover comments in `gate.py` and
  `mcp_dispatch.py` that describe the deleted v1 tools.

**Do.** Remove function by function, deleting or rewriting the tests that only exercised the
removed paths (never skipping them). A settings file in v1 format should now fail with a clear
error naming the problem, not be silently converted — add a test for that error.

**Also in this PR:** the ADR *"Only the current install layout is supported; no upgrade path from
earlier layouts"* recording G1, G3 and H1 for all of Wave L (L2–L5 cite it). It **partly
supersedes ADR 0004** (decisions 4–5: the one-time migration as a kept caller) — per
`docs/adr/README.md`, write `Supersedes 0004 (in part)` in the new ADR and add one forward-pointing
line to ADR 0004's Status plus its row in the ADR index. CHANGELOG "Removed" entry for the v1 policy settings format.

**Verify.** `/dod`; coverage floor must hold (removed code removes its tests too — the ratchet is
on percentage). `scripts/qa_web_smoke.py` locally.

**Done when.** `policy/compat.py` is gone, nothing in `src/` calls a `migrate_*` policy function,
and a v1-format `settings.yaml` is refused with a clear message.

**As landed ([#670](https://github.com/privacyfence/privacyfence/pull/670)) — corrections to this brief:**

- `settings_controller.GRANT_RESOURCE_TYPES` is not a v1 table — it is `resource_registry`'s manifest used for name resolution — and stays. The v1 tables removed were `RULES_BY_OPERATION`, `RULES_LIST_VALUE`, `RULES_INT_VALUE` and `OPERATION_LABELS`.
- The shipped `resources/settings.yaml.example` was itself in v1 format and had to be converted, or every fresh install would have been refused.
- ADR is **0041**.

### L2 — Remove legacy file-location moves (Python and shim)

**Removes.** `paths._migrate_path`, `_migrate_legacy_authority_files`,
`_migrate_legacy_audit_log_dir`, `_LEGACY_AUTHORITY_PATHS`; `web/mcp_auth.delete_legacy_shared_mcp_token`;
`web/server._clear_legacy_bootstrap_url_files`; the shim's legacy shared `mcp_token` file
fallback (`protocol.ts` `readMcpToken()`, referenced from `index.ts` and `controlChannel.ts`) and
the `%LOCALAPPDATA%\Programs` candidate in `mcpb/shim/src/daemon.ts` `windowsDefaultAppPaths`
(its comment says it stays only for installs an older lowest-privilege release made — G1); phase/issue history in the
comments of the code touched (e.g. `paths.py`).

**Keep** the matching cleanup lines in the three `scripts/*_privilege_separation.*` scripts for L3–L5
(they are installer code and are exercised only there); note each one in the PR so the platform
phase finds it.

**Verify.** `/dod`, including the shim's `npm test` and `npm run typecheck` in `mcpb/shim/`.
Dispatch `build.yml` against the branch — the shim change ships in every `.mcpb`.

**Done when.** No Python or TypeScript code reads or moves a pre-current-layout path.

**As landed ([#671](https://github.com/privacyfence/privacyfence/pull/671)) — corrections to this brief:**

- Also removed: `paths._LEGACY_AUDIT_LOG_RELATIVE`, the `_authority_migration_attempted`/`_audit_log_migration_attempted` memos, and `authority_root()`'s `migrate_audit_log` kwarg.
- Two test seeds wrote the pre-`authority/` layout and depended on the migration; they were rewritten.
- The legacy lines left for L3–L5 included `mcp_token` in each script's `HANDOFF_FILE_NAMES`.

### L3 — Linux: installer cleanup and remove/purge semantics

**Removes.** The `.deb`'s `/etc/xdg/autostart/privacyfence.desktop`;
`linux_privilege_separation.sh` `stop_legacy_autostart`, `LEGACY_USER_UNIT`, `migrate_data` (the
per-user → system-root move), `move_handoff_files_in/out` where Linux-only, and the legacy-bootstrap
cleanup left by L2.

**Changes (G3).** Today `debian/prerm remove` runs `privacyfence-privilege-separation disable`,
which moves `/var/lib/privacyfence` into the owner's home. After this phase:
- `apt remove`: stops and disables the service, leaves `/var/lib/privacyfence` (and the config under
  the system root) in place.
- `apt purge`: `postrm purge` deletes the data, the service user and group.
- `disable`: **decide here, for all three platforms** — either remove it, or make it exactly the
  stop-and-leave step `remove` needs. Pick whichever keeps the three uninstall paths simplest,
  write the choice into the PR description and into L4's and L5's briefs in this file (same PR),
  and implement Linux accordingly. `privilege_separation.py`'s shared `disable` path changes here.

**Verify.** `/dod`; dispatch `build.yml` (the `build-deb` job runs `test_deb_packaged_lifecycle.py`
— extend it to assert remove keeps data and purge deletes it) and `linux-graphical-session.yml`
(update `test_linux_graphical_session_autostart.py` for the removed XDG path).

**Done when.** Install → remove → reinstall keeps data; purge leaves nothing; no XDG autostart file
ships.

**As landed ([#673](https://github.com/privacyfence/privacyfence/pull/673)) — corrections to this brief:**

- **`disable` decision (maintainer, option C): `disable` is replaced by `uninstall [--purge]` on all three platforms — [ADR 0042](https://github.com/privacyfence/privacyfence/blob/main/docs/adr/0042-uninstall-replaces-disable.md).** `uninstall` stops and unregisters the service and keeps the data, marker and service account; `--purge` also deletes them.
- `postrm purge` deletes the data and account itself: by then dpkg has removed the tool. A test keeps postrm's paths in step with the script.
- `move_handoff_files_in/out` and `HANDOFF_FILE_NAMES`/`HANDOFF_FILE_GLOB` all went (only migration and `disable` reached them); `migrate_data` also carried ADR 0008's non-owner copy, gone too.
- With the XDG entry gone the `.deb` has no conffiles; `build_deb.sh`'s `DEBIAN/conffiles` step went.
- `privilege_separation.py` had no shared `disable` code path, only comments.

### L4 — macOS: installer cleanup and uninstall semantics

**Removes.** `macos_privilege_separation.sh` `stop_legacy_agent`, `migrate_data`, the remaining
`move_handoff_files_in/out`; the matching parts of `installer/macos/pkg/postinstall`, `build_pkg.sh`
and `build_dmg.sh`. #638 fixed two bugs in this script and #649 two macOS startup races — keep
their fixes and tests.

**Decide, don't assume: `privilege_separation.maybe_auto_enable_macos`.** The audit called it an
in-place-upgrade fallback, but on `main` it is now what `enforce_separation` runs on macOS for a
packaged install that isn't separated yet (`privilege_separation.py` ≈l.1735, reached from
`daemon_main.py` ≈l.2234). If the `.pkg` postinstall always leaves a fresh install separated, it
is unreachable on a fresh install and goes, with `enforce_separation` then refusing to serve
instead; if any fresh-install path relies on it, it stays under H1. Show which in the PR.

**Changes (G3).** Uninstall follows ADR 0042 (`uninstall [--purge]`, the `disable` decision recorded by L3). macOS has no package
manager purge: provide the documented equivalent (the uninstall command the user runs, with a
"delete data" option) and make sure no step moves data back into `~/Library`.

**Verify.** `/dod`; dispatch `build.yml` (`build` job: `test_macos_packaged_smoke.py`,
`test_macos_pkg_smoke.py`) and `macos-graphical-session.yml`
(`test_macos_graphical_session_autostart.py`, `test_macos_pkg_install.py`).

**Done when.** The `.pkg` installs the current layout only; uninstall keeps data unless asked to
delete it; both dispatched runs are green.

**As landed ([#672](https://github.com/privacyfence/privacyfence/pull/672)) — corrections to this brief:**

- Uninstall follows ADR 0042: `sudo …/macos_privilege_separation.sh uninstall [--purge]` is the documented macOS uninstall; the `.pkg` welcome screen names it.
- Also removed: `HANDOFF_FILE_NAMES`/`HANDOFF_FILE_GLOB` and `drop_stale_sockets` (#638's socket fix lived inside `migrate_data`). `build_dmg.sh` had no migration step.
- `maybe_auto_enable_macos` **stays under H1**: the `.pkg` postinstall never fails, so an install can finish with no marker; the shim then spawns the packaged daemon, and `enforce_separation()` → `maybe_auto_enable_macos()` is the only remaining path to finish separation.
- L4 also took the darwin halves of the shared POSIX tests L3 narrowed.

### L5 — Windows: installer cleanup and uninstall semantics

**Removes.** `installer/privacyfence.iss` `RegisterAutostartTask` and `Disable-DaemonTask` steps;
`installer/privacyfence-task.xml.tmpl` and `tests/unit/test_windows_autostart_task_template.py`
(if the template is only used by the removed step — verify); `windows_privilege_separation.ps1`
`Move-Data`, `Get-LegacyDataDir`, `Restore-LegacyDataDir` and the legacy-bootstrap cleanup left by L2.

**Changes (G3).** Uninstall follows ADR 0042 (`uninstall [--purge]`, the `disable` decision recorded by L3): the uninstaller stops
the service and leaves data under the system root; a documented option (uninstaller checkbox or
command) deletes it. Nothing restores data to `%LOCALAPPDATA%`.

**Verify.** `/dod`; dispatch `build.yml` (`build-windows` job: `test_windows_packaged_smoke.py`)
and `windows-graphical-session.yml` (`test_windows_graphical_session_autostart.py`).

**Done when.** The installer registers nothing it later disables; uninstall keeps data unless asked;
both dispatched runs are green.

**As landed ([#674](https://github.com/privacyfence/privacyfence/pull/674)) — corrections to this brief:**

- Uninstall follows ADR 0042: `uninstall [-Purge]` (PowerShell spelling). The uninstaller runs it and offers a **Delete PrivacyFence data** checkbox, unchecked by default; a silent uninstall never purges.
- `installer/privacyfence-task.xml.tmpl` was used only by the removed step and went, with its test; its lessons moved into the companion template's header.
- New unit test: no Inno `[Code]` line may start with `[` or `#` (it broke the first dispatched build).
- Also removed: `Disable-DaemonTask`/`Enable-DaemonTask`, `Move-HandoffFilesIn/Out`, `$HandoffFileNames` and the `*_url` pattern, `status`'s `STILL AUTOSTARTS` check, `privilege_separation.WINDOWS_DAEMON_TASK_NAME`, and the daemon-task contract in `tests/windows_task_contract.py`. `enable` now creates `%ProgramData%\PrivacyFence` instead of migrating into it.
- `test_windows_graphical_session_autostart.py` was **re-scoped**: the old test undid separation so the daemon task could redo it, and neither exists now. It checks the companion task against its contract and that `PrivacyFenceCompanion.exe` runs as the signed-in user, and keeps the service crash-restart test.
- **Manual check owed:** the uninstaller's "Delete PrivacyFence data" checkbox has only been compiled in CI, never clicked. It is in `release-testing.md`'s manual Windows checks; do it before the next stable release.

### X1 — Pre-flight and alpha tag

Not a code phase; the orchestrator does it (or asks the maintainer to).

- Dispatch `build.yml` against `main`'s tip (the pre-flight in `CLAUDE.md`, ADR 0030) and the
  three graphical-session workflows; all green.
- Dispatch `release.yml` with `dry_run: true` for the next alpha (`/cut-release` does both) and
  report the result. **Cutting the alpha for real is the maintainer's decision** (steward skill).
- After the real alpha: confirm `publish-pypi.yml`'s build job produced a wheel that passed P4's
  check (pre-release tags build but don't publish to PyPI), and that R2 holds every artifact.

**Done when.** The alpha's `build.yml` and `publish-pypi.yml` runs are green. Record the run links in
this file's Status table.

### X2 — Retire this plan

- Confirm every ADR in [ADRs this plan creates](#adrs-this-plan-creates) exists.
- Delete this file; remove its entry from `docs/README.md`'s list of open plans and the dependency note in
  `website-plan.md` (its Wave 1 then documents the post-cleanup code directly).
- If `website-plan.md` Wave 1 has already landed, check that its docs describe the post-cleanup
  behavior (uninstall, Apps Script, org bind default, Telegram on PyPI) and list anything that
  doesn't in the PR description.

## ADRs this plan creates

All landed; numbers as merged.

| ADR | Phase |
|---|---|
| [0039](https://github.com/privacyfence/privacyfence/blob/main/docs/adr/0039-installers-refuse-an-os-below-the-support-matrix.md) Installers refuse an OS below the support matrix | P5 (not in the original list; added by the phase) |
| [0040](https://github.com/privacyfence/privacyfence/blob/main/docs/adr/0040-telegram-app-credentials-ship-in-every-distribution.md) Telegram app credentials ship in every distribution, including PyPI (G4) | P4 |
| [0041](https://github.com/privacyfence/privacyfence/blob/main/docs/adr/0041-only-the-current-install-layout-is-supported.md) Only the current install layout is supported; no upgrade path from earlier layouts (G1, G3, H1) | L1 |
| [0042](https://github.com/privacyfence/privacyfence/blob/main/docs/adr/0042-uninstall-replaces-disable.md) `uninstall [--purge]` replaces `disable` (option C; rejects A and B) | L3 (macOS/Windows status lines added by L4/L5) |

## Found during implementation, not fixed

Recorded in the phase PRs' "Found, not fixed" sections; none is in this plan's scope. Doc items
belong to [`website-plan.md`](website-plan.md) Wave 1; code items need their own issue or PR.

- **Code: `enable --for-user` overwrites the marker's owner** (L4, [#672](https://github.com/privacyfence/privacyfence/pull/672)):
  `cmd_enable_for_user` rewrites `owner_user` with the `--for-user` account even when that account
  is a second ADR 0008 principal, not the recorded owner. Pre-existing, on all three platforms; the recorded owner maps to
  the local principal, so this is a trust-boundary fix. Being fixed before X1 (branch
  `fix/for-user-keeps-recorded-owner`).
- **Code: org registered-clients format conversion** (L2, [#671](https://github.com/privacyfence/privacyfence/pull/671)):
  `web/oauth_provider.py` (≈l.286) still reads the pre-SEC-16 top-level format of org mode's
  registered-clients file, a format conversion ADR 0041 would also retire.
- **Docs: `security-and-compliance.md`** (P2, [#663](https://github.com/privacyfence/privacyfence/pull/663)): says a recovery code,
  "correctly or not, never grants a second attempt"; a wrong code does not consume the stored one.
- **Docs: `org-mode-setup-guide.md` §4.2** (P1, [#662](https://github.com/privacyfence/privacyfence/pull/662)): gives one
  `/oauth/callback/google` redirect URI; the code builds one per service (`/oauth/callback/gmail`, …, `/apps_script`).
- **Docs: `README.md` ≈l.337** (L2): points Windows users at `%LOCALAPPDATA%\Programs\PrivacyFence\`; the installer is admin-only.
- **Docs: `TECHNICAL_REFERENCE.md`** (L5, [#674](https://github.com/privacyfence/privacyfence/pull/674)): says the Start Menu entry opens the
  web settings UI; since ADR 0031 it launches the companion.
- **Docs: leftover macOS/Linux `disable` mentions** (L5; still present on `main` after L3/L4):
  `platform-support.md` ≈l.127–148, `release-testing.md` ≈l.26/34, `security-and-compliance.md` ≈l.58.
- **Packaging: `debian/control`** (P5, [#665](https://github.com/privacyfence/privacyfence/pull/665)): lists `Architecture: amd64 arm64`; only amd64 is built.
- **Comments: `audit_log.py` / `connector_host.py`** (L1): historical mentions of the deleted v1
  tools, deliberately left (history, not false present-tense claims).

## Inputs needed from the maintainer

- ~~**Telegram secrets (P4)**~~: resolved 2026-09-24. They are repository secrets, the same ones
  `build.yml` reads.
- ~~**`disable` (L3)**~~: decided 2026-09-24, option C; see ADR 0042.
- **Reviews:** P2 (trust boundary) and L3 (uninstall semantics, the `disable` decision) need an
  explicit read, not a skim.
- **Alpha (X1):** the go-ahead to cut the alpha after the dry run.

## Retiring this plan

Phase X2 deletes this file.
