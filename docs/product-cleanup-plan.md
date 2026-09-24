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
- In the same PR, update this file's [Status](#status) row for the phase (→ `in review`, with the
  PR link). If a brief turned out wrong, correct the brief in the same PR and say so.
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
| P1 | Apps Script connectable from Settings | A | — | not started |
| P2 | Recovery-code route hardening | B | — | not started |
| P3 | Org bundle: bind `127.0.0.1` by default, `agent_links` flag | C | — | not started |
| P4 | Telegram credentials in the PyPI build | D | maintainer input | not started |
| P5 | Declare minimum OS versions | E | — | not started |
| L1 | Remove policy v1 and deprecated MCP tools (+ ADR) | A | P1 | not started |
| L2 | Remove legacy file-location moves (Python + shim) | B | L1, P2 | not started |
| L3 | Linux: installer cleanup and remove/purge semantics | E | L2, P5 | not started |
| L4 | macOS: installer cleanup and uninstall semantics | E | L3 | not started |
| L5 | Windows: installer cleanup and uninstall semantics | E | L4 | not started |
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

Evidence line numbers are from the audit (2026-09-24); re-locate by symbol name if they have moved.

### P1 — Apps Script connectable from Settings

**Problem.** Apps Script is the one Google connector that cannot be authenticated from the Settings
page, locally or in organization mode. Its only path is `privacyfence-app --apps-script-oauth`,
which no user doc a new user would find explains.

**Evidence.** `settings_controller.py` `ALL_CONNECTORS` and `GOOGLE_CONNECTORS` omit it (≈l.233–246);
`web/routes_connect.py` `GOOGLE_SCOPES`/`OAUTH_SERVICES` (≈l.94–97) have no Apps Script entry;
`apps_script_client.py`'s error messages point at `--apps-script-oauth`.

**Do.**
- Add Apps Script to the Settings connector list and the Google OAuth connect flow in both modes,
  with its own scopes, so Authenticate/Reconnect and the status pill work like Gmail's.
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

### P2 — Recovery-code route hardening

**Problem.** The step-up recovery-code route doesn't audit failed attempts (its own docstring says
it does), accepts an unattested session, and has no attempt rate limit. Low severity — the code is
64 random bits — but the missing audit and the unattested-session gap are real.

**Evidence.** `web/routes_security.py` ≈l.590–611; `webauthn_stepup.py` ≈l.611–631.

**Do.**
- Write an audit event for every failed and successful recovery attempt, in the same format as
  other step-up events.
- Require a human-attested session (the same check the other step-up management routes use).
- Rate-limit attempts per session and globally (reuse any existing limiter; otherwise a small
  in-memory one with a stated window), returning a clear error when exceeded.
- Tests for each of the three, including the audit record contents.

**Verify.** `/dod`; `bandit` clean. This is a trust-boundary change: say so in the PR and ask for
the maintainer's review explicitly.

**Done when.** Failed attempts appear in the audit log; an unattested session is refused; the
N+1th attempt in the window is refused.

### P3 — Org bundle: bind `127.0.0.1` by default, `agent_links` flag

**Problem.** `build_org_bundle.py --server-bind-host` defaults to `0.0.0.0` while the org guide says
never to expose the listener. `agent_links` can only be set by hand-editing the bundle.

**Evidence.** `scripts/build_org_bundle.py` ≈l.203–205; `org_mode.py` ≈l.165 (`agent_links: bool =
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

### P5 — Declare minimum OS versions

**Problem.** Minimum OS versions aren't declared in the installers, so an unsupported system gets
an install that fails at runtime instead of a clear refusal.

**Do.**
- `installer/privacyfence.iss`: `MinVersion` for the lowest supported Windows (confirm from
  `docs/platform-support.md` and the CI runner images; ask if they disagree).
- `debian/control`: express the Debian/Ubuntu floor through dependency versions (e.g. the Python
  and systemd versions the package actually needs) rather than a distro name check.
- macOS: confirm the app bundle's `LSMinimumSystemVersion` and the `.pkg`'s install check match
  macOS 13; fix whichever doesn't.
- Unit tests that read the three declarations, so they can't silently drift from
  `platform-support.md`'s matrix.

**Verify.** `/dod`; dispatch `build.yml` against the branch (steward table) — all three packaged
jobs must be green.

**Done when.** All three installers state a floor that matches the support matrix, enforced by test.

### L1 — Remove policy v1 and deprecated MCP tools

**Removes.**
- `policy/compat.py` (whole module); `daemon_main._migrate_settings_to_policy_v2` and its two call
  sites; `settings_controller.policy_v2_migration_notice_html` and whatever renders it;
  `settings_controller.RULES_BY_OPERATION` / `GRANT_RESOURCE_TYPES` (v1 predicate tables —
  verify no current caller first); `auto_accept.migrate_telegram_search_operation_key`; the
  `replaces=(…)` aliases for old condition names in `policy/conditions.py`; any v1 handling in
  `policy/store.py`.
- MCP tools `privacyfence_list_auto_accept_rules` and `privacyfence_propose_auto_accept_rule_change`
  (`web/mcp_tools.py`, `mcp_dispatch.py`), `gate.propose_rule_change` and its v1
  `target: rule|grant` translation. Their v2 replacements stay.

**Do.** Remove function by function, deleting or rewriting the tests that only exercised the
removed paths (never skipping them). A settings file in v1 format should now fail with a clear
error naming the problem, not be silently converted — add a test for that error.

**Also in this PR:** the ADR *"Only the current install layout is supported; no upgrade path from
earlier layouts"* recording G1, G3 and H1 for all of Wave L (L2–L5 cite it), and CHANGELOG "Removed"
entries for the two MCP tools and the v1 policy format.

**Verify.** `/dod`; coverage floor must hold (removed code removes its tests too — the ratchet is
on percentage). `scripts/qa_web_smoke.py` locally.

**Done when.** `grep -ri "v1\|compat\|auto_accept_rule" src/privacyfence/policy src/privacyfence/web/mcp_tools.py`
finds nothing legacy; the MCP tool list has no deprecated names.

### L2 — Remove legacy file-location moves (Python and shim)

**Removes.** `paths._migrate_path`, `_migrate_legacy_authority_files`,
`_migrate_legacy_audit_log_dir`, `_LEGACY_AUTHORITY_PATHS`; `web/mcp_auth.delete_legacy_shared_mcp_token`;
`web/server._clear_legacy_bootstrap_url_files`; the shim's legacy `mcp_token` file fallback and the
withdrawn `%LOCALAPPDATA%\Programs` path in `mcpb/shim/src/daemon.ts`; phase/issue history in the
comments of the code touched (e.g. `paths.py`).

**Keep** the matching cleanup lines in the three `scripts/*_privilege_separation.*` scripts for L3–L5
(they are installer code and are exercised only there); note each one in the PR so the platform
phase finds it.

**Verify.** `/dod`, including the shim's `npm test` and `npm run typecheck` in `mcpb/shim/`.
Dispatch `build.yml` against the branch — the shim change ships in every `.mcpb`.

**Done when.** No Python or TypeScript code reads or moves a pre-current-layout path.

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

### L4 — macOS: installer cleanup and uninstall semantics

**Removes.** `macos_privilege_separation.sh` `stop_legacy_agent`, `migrate_data`, the remaining
`move_handoff_files_in/out`; `privilege_separation.maybe_auto_enable_macos` (in-place-upgrade
fallback) and its caller in `companion.py`; the matching parts of `installer/macos/pkg/postinstall`,
`build_pkg.sh` and `build_dmg.sh`.

**Changes (G3).** Uninstall follows the `disable` decision recorded by L3. macOS has no package
manager purge: provide the documented equivalent (the uninstall command the user runs, with a
"delete data" option) and make sure no step moves data back into `~/Library`.

**Verify.** `/dod`; dispatch `build.yml` (`build` job: `test_macos_packaged_smoke.py`,
`test_macos_pkg_smoke.py`) and `macos-graphical-session.yml`
(`test_macos_graphical_session_autostart.py`, `test_macos_pkg_install.py`).

**Done when.** The `.pkg` installs the current layout only; uninstall keeps data unless asked to
delete it; both dispatched runs are green.

### L5 — Windows: installer cleanup and uninstall semantics

**Removes.** `installer/privacyfence.iss` `RegisterAutostartTask` and `Disable-DaemonTask` steps;
`installer/privacyfence-task.xml.tmpl` and `tests/unit/test_windows_autostart_task_template.py`
(if the template is only used by the removed step — verify); `windows_privilege_separation.ps1`
`Move-Data`, `Get-LegacyDataDir`, `Restore-LegacyDataDir` and the legacy-bootstrap cleanup left by L2.

**Changes (G3).** Uninstall follows the `disable` decision recorded by L3: the uninstaller stops
the service and leaves data under the system root; a documented option (uninstaller checkbox or
command) deletes it. Nothing restores data to `%LOCALAPPDATA%`.

**Verify.** `/dod`; dispatch `build.yml` (`build-windows` job: `test_windows_packaged_smoke.py`)
and `windows-graphical-session.yml` (`test_windows_graphical_session_autostart.py`).

**Done when.** The installer registers nothing it later disables; uninstall keeps data unless asked;
both dispatched runs are green.

### X1 — Pre-flight and alpha tag

Not a code phase; the orchestrator does it (or asks the maintainer to).

- Dispatch `build.yml` against `main`'s tip and the three graphical-session workflows; all green.
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

Next free number when each PR lands.

| ADR | Phase |
|---|---|
| Only the current install layout is supported; no upgrade path from earlier layouts (G1, G3, H1) | L1 |
| Telegram app credentials ship in every distribution, including PyPI (G4) | P4 |

If L3's `disable` decision rejects an alternative for a non-obvious reason, L3 adds its own ADR;
otherwise the L1 ADR covers it.

## Inputs needed from the maintainer

- **Telegram secrets (P4):** *"Are `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` available to
  `publish-pypi.yml` — as repository secrets, or in the `pypi`/`testpypi` environments?"*
- **Reviews:** P2 (trust boundary) and L3 (uninstall semantics, the `disable` decision) need an
  explicit read, not a skim.
- **Alpha (X1):** the go-ahead to cut the alpha after the dry run.

## Retiring this plan

Phase X2 deletes this file.
