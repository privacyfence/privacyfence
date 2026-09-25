# ADR 0058: Nothing runs elevated unless only an administrator can rewrite it

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-17 in
[#428](https://github.com/privacyfence/privacyfence/issues/428)). The macOS check landed in
`9dda910a` ([#477](https://github.com/privacyfence/privacyfence/pull/477)); it was split into
per-platform checks for the companion's `enable --for-user` in `c8caba0f`
([#547](https://github.com/privacyfence/privacyfence/pull/547)), and reused by
`service_control.run_elevated()` in `7b51a27b`
([#609](https://github.com/privacyfence/privacyfence/pull/609)).

## Context

PrivacyFence asks for administrator rights from inside the user's session in four places: the
macOS auto-enable prompt (`maybe_auto_enable_macos`, via `osascript ... with administrator
privileges`), the full auto-enable on Linux and Windows (`_run_full_auto_enable_non_macos`), the
companion completing the per-user half (`complete_per_user_separation`), and the companion
starting or stopping the daemon (`service_control.run_elevated`). Each resolves a provisioning
script (`installer_script_path()`) and runs it elevated.

On macOS that script sits in the `.app` bundle's `Resources/` or in a source checkout, both as
writable as anything else the logged-in user owns, and so as writable as anything the AI agent can
edit. Running whatever is at that path behind a routine-looking password dialog would give an agent
a one-shot local privilege escalation: rewrite the script, wait for the prompt, and the human
approves root for the agent's code.

## Decision

Before any elevation prompt, `_elevation_script_problem(script)` must return `None`. Otherwise the
caller logs the reason and does not prompt (`service_control.run_elevated` returns
`(False, "could not verify ... is safe to run as an administrator: ...")`).

- **POSIX** (`_posix_script_elevation_problem`): the script is owned by uid 0 and has neither
  `S_IWGRP` nor `S_IWOTH`. A failed `stat` is a problem.
- **macOS** (`_macos_auto_enable_script_problem`): the POSIX check, and, when the process runs
  from an app bundle (`paths.app_bundle_path()`), `/usr/bin/codesign --verify --deep <bundle>`
  must exit 0. A `codesign` that cannot run or times out is a problem.
- **Windows** (`_windows_script_elevation_problem`): the script's DACL (`windows_acl.read_dacl`)
  grants write to no trustee outside `windows_acl.TRUSTED_TRUSTEES` (SYSTEM, Administrators and
  `NT SERVICE\TrustedInstaller`). An unreadable ACL is a problem.

Every uncertainty reads as "not safe". A source checkout never passes the ownership check, which is
intended: the only thing this code runs as root is a script an installer shipped. A refusal falls
back to the manual path (the command `per_user_command_text()` prints, or the documented `enable`).

## Alternatives considered

- **Trust the path because the installer put it there.** Rejected: the macOS bundle and a checkout
  are user-writable, so the path says nothing about who wrote its contents.
- **Rely on the prompt itself as the consent.** Rejected: the human approves the dialog, not the
  script's contents, and cannot see what is about to run as root.
- **Check the code signature alone on macOS.** The signature does not cover a source checkout at
  all, so ownership and mode stay the primary check everywhere and the signature is added on top.

## Consequences

- A developer running from a checkout never gets the auto-enable prompt, and `enable --for-user`
  or daemon start/stop from the companion log the reason and point to the manual command.
- A packaged install is expected to pass: the `.deb` ships `/usr/sbin/privacyfence-privilege-separation`
  root-owned, the `.pkg` installs the bundle as root, and the Windows installer puts
  `privilege-separation.ps1` next to the executable under `{app}`. An install whose script fails the
  check gets no prompt, only the log line.
- Any new elevation site must call `_elevation_script_problem()` first. `service_control.py`
  imports it for that reason rather than duplicating it.

## Verification

- `tests/unit/test_privilege_separation.py`: `TestMacosAutoEnableScriptProblem` (ownership, group
  and world write, `stat` failure, codesign failing or not running) and `TestElevationScriptProblem`
  (`test_posix_refuses_a_script_this_account_owns`, `test_posix_refuses_a_group_writable_script`,
  `test_macos_still_checks_the_bundle_signature`, `test_windows_refuses_a_user_writable_script`,
  `test_windows_refuses_an_acl_it_cannot_read`, `test_windows_accepts_an_administrators_only_script`).
- `tests/unit/test_service_control.py::TestNotEligibleToElevate::test_an_untrusted_script_refuses`.

## Related

- [ADR 0003](0003-separated-installs-only.md) — decisions 3 and 6, the elevations this guards.
- [ADR 0026](0026-the-companion-manages-the-daemon-through-the-service-manager.md) — the companion's
  daemon start/stop elevation.
- [#428](https://github.com/privacyfence/privacyfence/issues/428) — privilege separation.
