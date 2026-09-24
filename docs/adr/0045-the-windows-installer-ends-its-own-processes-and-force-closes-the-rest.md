# ADR 0045: The Windows installer ends its own processes, and RestartManager force-closes whatever is left

## Status

Accepted — 2026-09-24. Implemented in `installer/privacyfence.iss` (`CloseApplications=force`,
and `PrepareToInstall`'s wait).

## Context

On an upgrade, Inno Setup has to overwrite files that PrivacyFence's own processes have open: the
daemon, which on a separated install is the `PrivacyFence` service, the companion tray app
(`PrivacyFenceCompanion.exe`) and the daemon's alias (`privacyfence-app.exe`). By default
(`CloseApplications=yes`) Setup asks Windows Restart Manager which processes use the files it is
about to replace, and asks them to close.

Restart Manager's request is not answered by any of those processes. The daemon has no window and
the companion only a tray icon, so `RmShutdown` returns `ERROR_FAIL_SHUTDOWN`, Setup logs "Some
applications could not be shut down", and shows an Abort/Retry/Ignore box. Under
`/SUPPRESSMSGBOXES` that box answers Abort: this is `ShutdownApplications` in Inno Setup's
`Setup.Install.HelperFunc.pas`, which calls `AbortRetryIgnoreTaskDialogMsgBox` with a suppressed
default of `IDABORT` (checked against the `is-6_7_3` tag; `build.yml` installs Inno Setup
unpinned from Chocolatey). Setup exits 5 and rolls back. Interactively the user sees the same box
and has to close the processes themselves before Retry does anything.

This aborted Windows builds three times: v4.1.0a9's release build (`privacyfence-app`, commit
f3883ac6), a dispatched build during the separated-install work (`PrivacyFenceCompanion`,
3079c985), and `windows-graphical-session.yml` (a companion leaked from an earlier test,
ce1dc693). Each was worked around in the test harness, which stopped the service, killed the
processes and retried Setup once, so the tests stopped proving what the installer does on its own.

7b51a27b then gave the installer a `PrepareToInstall` hook that `sc stop`s the service, waits for
STOPPED, and `taskkill /F`s the three images. That is the real fix, and all three failures predate
it. Two gaps remained. `taskkill /F` returns once termination is requested, not once the process
is gone, and Setup queries Restart Manager as soon as the hook returns. And if anything is still
found, `yes` fails the same way it always did.

## Decision

1. `PrepareToInstall` is the primary mechanism. After stopping the service and killing the three
   images it polls `tasklist` every 500 ms for up to 30 s, killing again on each poll, until none
   of them is running. It logs "PrepareToInstall: no PrivacyFence process is still running",
   or which images were still running, or that it could not look. It still never fails the
   install.
2. `CloseApplications=force`. Restart Manager is only the backstop, but it now terminates a process
   that does not respond (`RmForceShutdown`) instead of aborting Setup.
3. The packaged upgrade test runs Setup once, with the old version's service and companion still
   running, with no sweep and no retry of its own. It asserts the line from decision 1 in Setup's
   log. `_stop_daemon_service`, `_kill_stray_app_processes` and the exit-5 retry are removed.

## Alternatives considered

- **`CloseApplications=no`.** Rejected. It removes Restart Manager but not the failure: a file that
  is still locked goes to `ProcessFileEntry`'s `DeleteFile`, which retries four times, one second
  apart, and then shows a Retry/Skip/Abort box. Under `/SUPPRESSMSGBOXES` that box also answers
  Abort, again exit 5 (`Setup.Install.pas`, same tag). None of the three failures above would
  have been avoided; the log would only have named a DLL instead of the process holding it. For an
  interactive user it replaces the "these applications are using files" page with a raw
  "DeleteFile failed; code 5" box.
- **Keep `yes` and rely on `PrepareToInstall` alone.** Rejected. The backstop would still abort
  exactly where it is needed, and a backstop that can only fail adds nothing.
- **Narrow `CloseApplicationsFilter` to the three PrivacyFence executables**, so that `force` could
  never reach a third-party process. Rejected for now. Only PrivacyFence's own processes load its
  `.exe`/`.dll` files, so the default filter (`*.exe,*.dll,*.chm`) already finds only them in
  practice. Narrowing it would also stop Restart Manager from naming anything else that holds one
  of those files. Revisit if a log ever shows it finding something that is not ours.
- **Keep retrying Setup in the test.** Rejected. A retry hides a real user's failure. A person
  upgrading has no harness to kill their companion for them.

## Consequences

- An upgrade over a running install does not depend on any PrivacyFence process answering a close
  request, silently or interactively.
- `force` could terminate a third-party process that has one of PrivacyFence's executables or DLLs
  open, without asking. That risk is accepted: no known program does this, and it only happens
  after `PrepareToInstall` has already ended every PrivacyFence process.
  `PrepareToInstall`'s own `taskkill /F` was already a forced termination of our processes, so
  nothing new is lost there.
- `RestartApplications` stays at its default. PrivacyFence's processes do not register with
  Restart Manager for restart, and `enable` (ADR 0003 decision 4) starts the service and companion
  after every install anyway.
- In the worst case (the service stuck in STOP_PENDING, or a process that cannot be ended) Setup
  spends up to about a minute in `PrepareToInstall` before reaching the backstop.

## Verification

- `tests/unit/test_privilege_separation.py`'s
  `test_setup_ends_its_own_processes_and_force_closes_what_is_left`.
- `tests/integration/test_windows_packaged_smoke.py`'s
  `test_windows_upgrade_in_place_preserves_user_state`, run by `build.yml`'s `build-windows` job.
  It asserts the confirmation line and runs with no harness sweep or retry.

## Related

- Commits f3883ac6, 3079c985, df1a403d, ce1dc693, 7b51a27b.
- [ADR 0003](0003-separated-installs-only.md) decision 4,
  [ADR 0026](0026-the-companion-manages-the-daemon-through-the-service-manager.md).
