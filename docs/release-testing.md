# Release testing

This document contains the release checks that still require human judgment. Automated correctness belongs in CI and is documented in [`testing-policy.md`](testing-policy.md). There is no active plan document tracking remaining automation work: a new testing gap belongs in `testing-policy.md` if it changes current policy, or in [`platform-support.md`](platform-support.md)'s "Known open items" section if it's a standing open item.

## Before release

Confirm the release commit is on a reviewed branch/PR and all required CI checks are green. Also confirm the relevant platform build workflow succeeds for the artifact being published.

For a release that touches connectors or provider behavior, verify the scheduled live-connector workflow is healthy and review any open fixture-drift pull request before publishing.

## Human checks

Run only checks that automation cannot judge reliably:

- inspect the approval list, approval cards, settings pages, notifications, responsive layout, and light/dark presentation when UI code changed;
- complete a real first-time OAuth/consent flow when authentication setup changed or provider consent behavior is in question — on Windows specifically when a Windows-affecting change lands, confirm the OAuth loopback flow opens the default browser and completes a real connector auth through the installed app;
- exercise one real MCP client against the packaged application when MCP discovery, the shim, packaging, or daemon startup changed — on Windows specifically, install the `.mcpb` into a real Claude Desktop on the same machine and confirm the shim finds and launches the installed daemon end to end;
- verify OS-native installation/security presentation when installer, signing, notarization, SmartScreen/Gatekeeper/UAC, autostart, or login-session behavior changed — before a signed Windows release ships specifically: a real installer run on a clean Windows VM confirming SmartScreen/Authenticode presentation isn't an outright block, **a real sign-out/sign-in confirming the Task Scheduler `ONLOGON` trigger itself starts the daemon** (the one part of Windows autostart no CI job can cover — a hosted runner cannot produce the Terminal Services session logon the trigger subscribes to, so this human check is the only coverage it has; see `platform-support.md`'s "Known open items"), and an Add/Remove Programs uninstall confirming the program files and scheduled task are gone while `%USERPROFILE%\.privacyfence\` is untouched;
- on macOS specifically, when a release changes privilege separation or anything it touches
  (`privilege_separation.py`, `paths.py`'s data-directory resolution, the control channel, the
  companion, or the MCPB shim's discovery): on a real Mac, run
  `sudo scripts/macos_privilege_separation.sh enable`, confirm `… status` reports a clean layout
  after a logout/login, confirm the daemon is running under `_privacyfence` (`ps -o user= -p …`) and
  that your own account genuinely cannot read `/Library/Application Support/PrivacyFence/authority`,
  confirm the menu-bar companion opens `/approvals` and a real MCP client still reaches `/mcp`, then
  run `… disable` and confirm the previous layout is back with connector tokens intact. No CI job can
  cover any of this — it needs root, a real system account and a real login session — so this check
  is its only coverage; see `platform-support.md`'s "Known open items";
- on Linux specifically, the same check against the same set of changes, in that platform's own
  terms: on a real desktop Ubuntu, run `sudo privacyfence-privilege-separation enable`, confirm
  `… status` reports a clean layout after a logout/login, confirm the daemon is running under
  `privacyfence` (`systemctl show -p User privacyfence-daemon.service`, `ps -o user= -p …`) and that
  your own account genuinely cannot read `/var/lib/privacyfence/authority`, confirm the Applications
  menu entry opens `/approvals` and a real MCP client still reaches `/mcp`, then run `… disable` and
  confirm the previous layout is back with connector tokens intact. **Plus the one thing macOS's
  check doesn't have to cover: complete a real connector OAuth flow (Slack, Salesforce or
  Atlassian) while separated.** A daemon with no desktop session cannot open a browser itself, so
  that flow goes through the `--serve` companion the XDG autostart entry starts — and if that entry
  didn't take, the daemon comes up looking perfectly healthy while connector authentication
  silently has no way to reach you;
- on Windows specifically, the same check against the same set of changes, in that platform's own
  terms — and this is the one with the most that only a human can see. On a real Windows machine
  with the **per-machine** install (the elevated one; the per-user path cannot be separated, and
  `enable` will say so), from an elevated PowerShell run
  `powershell -ExecutionPolicy Bypass -File "$env:ProgramFiles\PrivacyFence\privilege-separation.ps1" enable`.
  Then, after a sign-out/sign-in: confirm `… status` reports a clean layout, confirm the daemon is
  running as the virtual account (`sc.exe qc PrivacyFence` shows `SERVICE_START_NAME:
  NT SERVICE\PrivacyFence`, and Task Manager's Details tab shows `PrivacyFenceApp.exe` under that
  user name) rather than having died at start with **error 1053** — the service host
  (`windows_service.py`) is the single genuinely new moving part on this platform and the SCM is the
  only thing that exercises it; confirm your own account genuinely cannot read
  `%ProgramData%\PrivacyFence\authority` (`type` a file in it and expect access denied, rather than
  trusting `icacls` output alone); confirm the tray companion opens `/approvals`, that a real MCP
  client still reaches `/mcp`, and — as on Linux, and for the same reason with a sharper edge, since
  a service in session 0 can reach no desktop at all — **complete a real connector OAuth flow
  (Slack, Salesforce or Atlassian) while separated**. Then run `… disable` and confirm the previous
  layout is back at `%LOCALAPPDATA%\PrivacyFence` with connector tokens intact and the original
  Scheduled Task re-enabled. Worth doing once in the other direction too: try `enable` against a
  per-user install and confirm it refuses rather than producing a service running an executable you
  can rewrite;
- perform focused exploratory connector QA using [`connector-qa-testing.md`](connector-qa-testing.md) for a new connector, a major connector rewrite, or an unexplained provider regression.

A signed Windows release specifically must not ship without the Windows-specific bullet above having actually been run against that release build. Whether that manual real-machine verification has been done yet is tracked in [`platform-support.md`](platform-support.md)'s "Known open items" section, not here.

Do not repeat the full automated unit/integration/browser/provider matrix manually just because a release is being cut.

## Platform artifacts

Use [`platform-support.md`](platform-support.md) to identify the packaging path and startup mechanism for each platform. Test the exact artifact intended for release, not a source checkout standing in for the package.

Preserve user state across normal upgrade/uninstall flows according to the platform packaging contract. Do not delete a real user's `~/.privacyfence` (or equivalent home-scoped state) as part of routine release testing.

## Evidence

A release review should be able to answer:

1. Which commit/tag produced the artifact?
2. Which automated checks ran and passed?
3. Which human-only checks were relevant to the changes?
4. Were any provider drift, signing, packaging, or login/autostart issues observed?

Keep that evidence in the release/PR discussion rather than turning this document into a chronological release log.
