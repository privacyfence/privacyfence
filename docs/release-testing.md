# Release testing

The one home for everything a human checks before a release: the gates in the order they run,
then the manual checks per platform. What CI proves automatically, and where, is in
[`testing-policy.md`](testing-policy.md). How a tag is cut is in `CLAUDE.md`'s "Releasing" section
and `/cut-release` (`.claude/commands/cut-release.md`).

## What stays manual

A check stays manual only when automation cannot reliably decide pass or fail. That leaves:

- visual and subjective judgment — contrast, spacing, light vs. dark, phone width;
- a first-time OAuth/consent screen, which the provider renders and which differs per account;
- one real MCP client (Claude Desktop with the `.mcpb`) against the packaged application;
- OS-native presentation — Gatekeeper, the macOS Installer and its password prompt, SmartScreen,
  UAC, the Windows uninstaller's own dialog;
- a real interactive sign-in, which no hosted runner can produce (the Windows `LogonTrigger`);
- privilege separation between two real OS accounts on a real machine, which needs root, a real
  system account and a real login session;
- review of provider drift the live check detected (the drift PR, see
  [`testing-policy.md`](testing-policy.md#layer-5-live-connector)).

Nothing is added to this list unless it meets that bar, and nothing on it is repeated across every
OS × connector combination. Do not rerun the automated suite by hand because a release is being cut.

## Gates, in order

1. **CI green on the commit to tag.** `origin/main`'s tip (or the `releases/*` branch being merged)
   with every required check passing. For a release that touched connectors, the latest
   `connector-live-check.yml` run is green and no `chore/connector-live-fixture-drift` PR is open
   unreviewed.
2. **`scripts/pre_release_check.py`**, locally from the repo root with `mcpb/shim/` dependencies
   installed. It runs CI's blocking commands — `pytest` with branch coverage,
   `check_coverage_floor.py`, the shim's `npm test` and `npm run typecheck`, `ruff check .`,
   `mypy_strict_modules.py`, `bandit` — reports PASS/FAIL for each, and exits non-zero on any
   failure. It checks no packaged artifact.
3. **Graphical-session coverage.** The latest completed runs of `linux-graphical-session.yml`,
   `windows-graphical-session.yml` and `macos-graphical-session.yml` on the release branch are green
   and their commits are ancestors of the one to tag. `finalize-release` checks exactly this with
   `scripts/check_graphical_session_coverage.py` and fails a **stable** tag on a gap (a pre-release
   only warns), after the artifacts are already built. If a run is stale or red, dispatch that
   workflow now.
4. **`build.yml` pre-flight.** Dispatch `build.yml` (no inputs) against the exact commit to tag and
   confirm `build`, `build-windows`, `build-deb` and `sbom` succeed. This is the only run of the
   packaged-artifact tests before a tag; every publish step is gated on a tag ref, so it publishes
   nothing ([ADR 0030](adr/0030-preflight-dispatches-build-yml-before-tagging.md)). A failure is
   fixed on `main` and the pre-flight re-run.
5. **Human checks** below, against this run's workflow artifacts (`PrivacyFence-dmg`,
   `PrivacyFence-windows-installer`, `PrivacyFence-deb`) — signed builds of the commit that will be
   tagged, carrying a dev version.
6. **`release.yml` dry run.** Dispatch with `version` and `dry_run: true` (the default). It resolves
   the channel, renders the stable release notes with `changelog_section.py`, and runs
   `tag_release.py`'s checks, creating the tag on the runner without pushing it. It builds nothing.
7. **Cut.** Re-dispatch with `dry_run: false`. The workflow fails if `build.yml` does not start for
   the tagged commit.

## Human checks

Run on a clean machine or VM per platform — the purge steps below delete PrivacyFence's data, so
never on a machine whose data you need.

### Every platform

1. **Fresh install**, every stable release: follow [`getting-started.md`](getting-started.md)
   verbatim for the platform, through "Finish setup" and "Check the install is working". Any step
   where the guide and the product disagree is a release blocker for the guide or the product.
2. **UI**, when approval, settings, notification or web-shell code changed: the approval list and
   cards, settings pages, notifications, and responsive layout in light and dark.
3. **First-time consent**, when authentication setup changed or a provider's consent behavior is in
   question: a real OAuth flow for the affected connector, from a new account.
4. **Connector QA**, for a new connector, a major connector rewrite, a broad change to `gate.py`,
   `auto_accept.py`, `policy/resource_registry.py` or the approval UI, or an unexplained provider
   regression: [`connector-qa.md`](connector-qa.md).

### macOS

1. **Installer**, every stable release: double-click `PrivacyFence.pkg` from the mounted DMG.
   Gatekeeper opens it without a block; Installer.app asks for an administrator password and
   completes. CI only runs `installer -pkg` from the command line, which shows neither.
2. **Separation**, when a release touches privilege separation (`privilege_separation.py`, the
   data-directory resolution in `paths.py`, the control channel, the companion, or the shim's
   discovery), and on every stable release until the privilege-separation item in
   [What stays manual](#what-stays-manual) is closed. After a logout and
   login:
   - `sudo /Applications/PrivacyFenceApp.app/Contents/Resources/scripts/macos_privilege_separation.sh status`
     reports a clean layout;
   - the daemon runs as `_privacyfence` (`ps -o user= -p <pid>`);
   - your own account cannot read `/Library/Application Support/PrivacyFence/authority`.
3. **Client**: the menu-bar companion opens `/approvals`; Claude Desktop with `PrivacyFence.mcpb`
   reaches `/mcp`; one gated call is approved end to end.
4. **Uninstall** (with step 2): `sudo … macos_privilege_separation.sh uninstall`. The daemon and
   companion are gone, `/Library/Application Support/PrivacyFence` is kept, and nothing appeared in
   your home directory ([ADR 0042](adr/0042-uninstall-replaces-disable.md)).

### Windows

1. **Installer**, every stable release: run the installer on a clean Windows VM. SmartScreen's
   Authenticode presentation is not an outright block, UAC prompts, and Setup separates the install
   itself — no manual `enable`.
2. **Sign-in**, every stable release: sign out and back in. The `PrivacyFenceCompanion` task's logon
   trigger starts the tray icon. This is the only coverage of the trigger firing.
3. **Separation**, on the same condition as macOS step 2:
   - `powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" status`
     reports a clean layout;
   - `sc.exe qc PrivacyFence` shows `SERVICE_START_NAME: NT SERVICE\PrivacyFence`, the service is
     running rather than failed with error 1053, and Task Manager's Details tab shows
     `PrivacyFenceApp.exe` under that account — only the SCM ever starts the service host
     (`windows_service.py`);
   - `type` a file in `%ProgramData%\PrivacyFence\authority` from your own account: access denied.
4. **Client**: the tray companion opens `/approvals`; installing the `.mcpb` into Claude Desktop from
   the installer's last page reaches `/mcp`; a connector OAuth flow (Slack, Salesforce or Atlassian)
   opens the default browser and completes while separated — a service in session 0 cannot reach a
   desktop, so the companion has to.
5. **Upgrade over a running install**, every stable release
   ([ADR 0045](adr/0045-the-windows-installer-ends-its-own-processes-and-force-closes-the-rest.md)):
   with the previous release installed and its service and tray companion running, run the new
   installer interactively, with `/LOG="%TEMP%\pf-setup.log"`. Setup's `PrepareToInstall` stops the
   service and ends `PrivacyFenceApp.exe`, `PrivacyFenceCompanion.exe` and `privacyfence-app.exe`
   itself, polling for up to 30 s; Restart Manager (`CloseApplications=force`) force-closes anything
   still holding a PrivacyFence file. Check:
   - no "applications are using files" page, Abort/Retry/Ignore box or `DeleteFile failed` box;
   - the log has `PrepareToInstall: no PrivacyFence process is still running`, not
     `still running after 30s:`;
   - the log names no non-PrivacyFence process being closed — if one does, report it: that is the
     condition under which the ADR's filter decision is revisited;
   - afterwards the service and companion are running again and connectors are still signed in.
6. **Uninstall**, every stable release. The "Delete PrivacyFence data" dialog is compiled in CI but
   never clicked: a silent uninstall never purges, and CI drives the purge through the script.
   - From Add/Remove Programs, uninstall. The dialog names `%ProgramData%\PrivacyFence`, the
     checkbox is unchecked, and OK is the only button. Leave it unchecked. Program files, the
     `PrivacyFence` service and the `PrivacyFenceCompanion` task are gone;
     `%ProgramData%\PrivacyFence` and the `PrivacyFenceUsers` group remain.
   - Reinstall: the connector is still signed in.
   - Uninstall again with the box ticked: `%ProgramData%\PrivacyFence` is gone and
     `Get-LocalGroup PrivacyFenceUsers` finds nothing.

### Debian/Ubuntu

On a real desktop Ubuntu, not a server or container.

1. **Install**, every stable release: `sudo apt install ./privacyfence_<version>_amd64.deb`, then log
   out and back in. `debian/postinst` separates the install itself.
2. **Separation**, on the same condition as macOS step 2:
   - `sudo privacyfence-privilege-separation status` reports a clean layout, not `PENDING USER`;
   - the daemon runs as `privacyfence` (`systemctl show -p User privacyfence-daemon.service`,
     `ps -o user= -p <pid>`);
   - your own account cannot read `/var/lib/privacyfence/authority`.
3. **Client**: the Applications-menu entry opens `/approvals`; a real MCP client reaches `/mcp`.
4. **OAuth while separated**: complete a real connector OAuth flow (Slack, Salesforce or Atlassian).
   The daemon has no desktop session, so the flow goes through the `privacyfence-companion --serve`
   process the XDG autostart entry starts; if that entry did not take, the daemon looks healthy while
   authentication has no way to reach you. CI covers the daemon's own autostart, not this one.
5. **Passkey enrollment**, every stable release, and whenever `webauthn_stepup.py` or the
   `/security` page changed: on the Passkeys page the companion opens after login, add a passkey in
   **Chrome** and again in **Firefox**, each time once with a USB security key and once with a
   phone over the browser's QR-code (hybrid) flow. Each enrollment completes, asks for the key's PIN
   or the phone's unlock, and the new passkey then approves a gated write. Most Linux desktops have
   no built-in authenticator, so this is the only proof a Linux user can enroll the passkey that
   step-up asks for by default
   ([ADR 0055](adr/0055-step-up-passkey-enrollment-accepts-any-authenticator.md)).
6. **Remove and purge**: `sudo apt remove privacyfence`, reinstall, and the connector is still signed
   in (`/var/lib/privacyfence` is kept); `sudo apt purge privacyfence`, and `/var/lib/privacyfence`
   and the `privacyfence` account are gone.

## Platform artifacts

Test the artifact that will ship — the pre-flight run's, from the commit being tagged — never a
source checkout standing in for it. [`platform-support.md`](platform-support.md) describes each
platform's packaging and startup.

## Evidence

Record in the release PR or discussion, not in this document:

1. the commit, the `build.yml` pre-flight run and the `release.yml` dry-run URL;
2. which human checks applied to this release's changes, and their results;
3. any provider drift, signing, packaging, sign-in or autostart issue seen.
