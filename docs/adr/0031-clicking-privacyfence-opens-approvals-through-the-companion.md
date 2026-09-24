# ADR 0031: clicking PrivacyFence opens Approvals through the companion

## Status

Accepted — 2026-09-24.
Amends [0002](0002-local-mode-trust-boundary-and-companion-app.md) (the bundle's entry point, and
who may reach the companion channel on a separated install).

## Context

On macOS the bundle's main executable (`CFBundleExecutable`) was the daemon, as PyInstaller's
default had made it. On a `.pkg` install the daemon already runs as a LaunchDaemon under its
service account, started from the staged copy in `/Library/PrivacyFence/image`. So a double-click
on `/Applications/PrivacyFenceApp.app` started a *second* daemon as the logged-in user.
`privilege_separation.check_runtime_identity()` refuses that (`daemon_main.py`), and with
`LSUIElement` set and no console, nobody saw it happen: the click did nothing.
`docs/getting-started.md` documented this rather than fixing it ("double-clicking the application
does not open one").

The two other platforms each had a visible entry point, and they did different things. Windows'
Start Menu entry opened the bare `http://localhost:8765/settings` URL, which only works for a
browser that already has a session cookie. Linux's Applications-menu entry ran the companion's
`--action open-approvals`.

That Linux entry had a gap of its own. A one-shot `--action` process exits before the daemon can
call it back, so it asks the running companion to open the page instead (`SHOW <path>`). That
command's gate is a confirmation dialog, because the request arrives from another process running
as the same OS user, which the agent could equally be (`control_channel._show_page`). But on a
separated POSIX install the companion channel refused every peer that was not the daemon's service
account (#428 B10, `_verify_companion_peer`). B10 predates `SHOW` (`e251b0f` vs. `57eaa5a`). So
every Linux menu click on a separated install fell back to an unattested link that can view
Approvals but not approve anything. Windows' pipe ACL already admitted the user, so Windows was
unaffected.

## Decision

1. **Clicking PrivacyFence runs the companion's `--launch` on macOS and Windows.** It opens
   Approvals on every platform:
   - **macOS:** the bundle's `CFBundleExecutable` is a new launcher executable,
     `Contents/MacOS/PrivacyFence` (`src/_launcher_entry.py`, which runs the companion's
     `--launch`). The daemon keeps its path, `Contents/MacOS/PrivacyFenceApp`, and launchd keeps
     starting both jobs by the explicit paths `macos_privilege_separation.sh` renders. Nothing
     about the service changes.
   - **Windows:** the main Start Menu entry runs `PrivacyFenceCompanion.exe --launch`.
   - **Linux:** the Applications-menu entry keeps `--action open-approvals`. With no tray to
     start, that is `--launch`'s behavior there anyway.
2. **What `--launch` does** (`companion._launch()`):
   - If a companion is running, it asks that companion to open Approvals (`SHOW /approvals`, with
     its dialog).
   - If a companion answered but did not open the page (Deny, or a dialog nobody answered), it
     stops there. It never starts a second tray beside a companion that just answered.
   - If no companion is running, it becomes the tray itself and opens Approvals from it. That
     needs no dialog, because this process now owns the channel the daemon calls back, exactly as
     for a click on the tray's own Open Approvals.

   `control_channel.show_via_companion()` distinguishes the three outcomes. "No companion" means
   only a failure to connect.
3. **On a separated POSIX install, the companion channel also accepts the companion's own uid,
   for `SHOW /approvals` and `SHOW /settings` only.** Nothing else is admitted from that uid:
   `SHOW RECOVERY`, `OPEN` and `CONFIRM` stay daemon-only, and another service-group member's uid
   is refused for every command ([0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md)).
   The check now reads the request line before deciding (`_verify_companion_peer(conn, line)`).

## Alternatives considered

- **Keep the daemon as the bundle's main executable, and have it hand off when started by a
  human.** Rejected. The daemon's start-up path is where the runtime-identity and separation
  checks live, and teaching it to tell a Finder launch from a launchd one adds a branch to the
  most security-sensitive entry point for a UI convenience.
- **`launchctl kickstart` the companion LaunchAgent instead of becoming the tray.** Rejected. The
  result is the same tray, but it needs a separated install's agent to exist, and it still needs
  a `SHOW` (and so a dialog) to open the page. Becoming the tray works on every macOS and Windows
  install and opens Approvals attested with no dialog. The one cost is a companion that launchd
  did not start, which is also what the documented by-hand recovery (`docs/getting-started.md`)
  already runs.
- **Leave the peer check alone, and fall back to a view-only link when a companion is already
  running.** Rejected. The click then works only when it is least needed, and Linux's menu entry
  stays silently degraded on separated installs.
- **Admit the companion's own uid for every command.** Rejected. `SHOW RECOVERY` puts a recovery
  code on screen, and `OPEN`/`CONFIRM` are the daemon's own call-backs. None of them has a dialog
  that would make a same-user caller safe.
- **Open Settings rather than Approvals.** Rejected. Approvals is where a human most often needs
  to go, and it is what Linux already opened. Settings is one click away in the tray menu.

## Consequences

- A double-click on the macOS app, or a click on the Windows Start Menu entry, now always does
  something visible. It either opens Approvals, or starts the menu-bar/tray icon and then opens
  Approvals.
- When the companion is already running, a click costs one extra Allow dialog. That dialog is
  `SHOW`'s gate and cannot be skipped. Clicking the tray icon's own Open Approvals avoids it.
- Accepted risk: anything running as the logged-in user, the agent included, can now make a
  separated POSIX companion put the `SHOW` dialog on screen. Windows was already in this position,
  and so was every unseparated install. It gains nothing without a human clicking Allow, and it is
  the same threat `_show_page` already names.
- The bundle carries a third executable, which adds to its size. It is still one `.app`, one
  signature and one notarization ([0002](0002-local-mode-trust-boundary-and-companion-app.md)
  decision 4).
- A companion started by a click is not supervised by launchd or the Scheduled Task. If it exits,
  the next login's autostart brings the supervised one back.

## Verification

- `PrivacyFenceApp.spec` (`"CFBundleExecutable": "PrivacyFence"`, the launcher `EXE`) and
  `src/_launcher_entry.py`.
- `tests/unit/test_privilege_separation.py`'s `TestMacosBundleMainExecutable` and
  `test_the_main_start_menu_entry_launches_through_the_companion`.
- `tests/integration/test_macos_pkg_smoke.py`, which reads the built payload's `Info.plist`
  (`pytest.mark.packaged`, `build.yml` only).
- `companion._launch()` and `tests/unit/test_companion.py`'s `TestLaunch`.
- `control_channel._verify_companion_peer()` and `tests/unit/web/test_control_channel.py`'s
  `TestCompanionChannelPeerVerification`.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) (companion app);
  [ADR 0003](0003-separated-installs-only.md) (separated installs only);
  [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md).
- #428 B10 (`e251b0f`, the companion channel's peer check), `57eaa5a` (`SHOW`).
