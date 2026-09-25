# ADR 0060: The system-root override is ignored on a real install

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-17 in
[#428](https://github.com/privacyfence/privacyfence/issues/428), `3b52aca7`, merged in
[#487](https://github.com/privacyfence/privacyfence/pull/487)).

## Context

`PRIVACYFENCE_SYSTEM_ROOT` (`SYSTEM_ROOT_ENV_VAR` in `src/privacyfence/privilege_separation.py`)
relocates the whole separated layout, marker included, so tests can exercise privilege separation
against a temporary directory instead of one that needs root to create. Every process of an
install has to agree on the root, so the daemon, the companion and the MCPB shim all read it.

The daemon's environment is set by launchd, systemd or the Service Control Manager. The companion
and the shim run in the logged-in user's session, and their environment is whatever that session
sets, which the AI agent can influence. On a separated install, that session is exactly what
privilege separation keeps away from the authority files. A variable the session sets that points
the companion or shim at a root the session controls would redirect them to a fake marker, fake
handoff directory and fake daemon address, bypassing the layout the installer locked down.

## Decision

`system_root()` honours `PRIVACYFENCE_SYSTEM_ROOT` only when it is an absolute path **and** the
platform's real root (`_default_system_root()`: `MACOS_SYSTEM_ROOT`, `LINUX_SYSTEM_ROOT`, or
`%ProgramData%\PrivacyFence` on Windows) has no marker that `_parse_marker()` accepts. Once a real
marker exists there, the override is ignored with a warning and the real root is used. A relative
override is ignored as well.

The MCPB shim applies the same rule (`privilegeSeparationRoot()` in
`mcpb/shim/src/protocol.ts`: `overrideRefused` when `defaultSystemRoot()` holds a marker with
version 1 and this platform). It also ignores `PRIVACYFENCE_DEV_DATA_DIR` on a separated install
for the same reason.

## Alternatives considered

- **Honour the variable unconditionally** (the behaviour before `3b52aca7`). Rejected: on a real
  install it lets the user session redirect the companion and shim, which is the attack privilege
  separation exists to stop.
- **Remove the variable.** Rejected: tests and development need a root that does not require
  administrator rights. A dev or CI machine has never had a marker written to the platform's real
  root, so it keeps the override.
- **Honour it only in the daemon.** Rejected: every process must agree on the root, and the
  companion and shim are the ones exposed to the user session anyway.

## Consequences

- On a machine with a real install, the override no longer works for any process, tests
  included. Development that needs it runs on a machine with no marker at the real root.
- The Python and TypeScript implementations must stay in step; `TestShimContract` checks that the
  shim reads the same marker file and honours the same variable.

## Verification

- `tests/unit/test_privilege_separation.py::TestSystemRootOverride`:
  `test_refuses_the_override_when_the_real_root_has_a_marker`,
  `test_still_honours_the_override_when_the_real_root_has_no_marker`,
  `test_ignores_a_relative_override`.
- `mcpb/shim/test/protocol.test.ts`: "refuses the override when the real default root already has
  a marker" and "still honours the override when the real default root has no marker".

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — the boundary the override could
  cross.
- [ADR 0003](0003-separated-installs-only.md) — every packaged install is separated.
- [ADR 0029](0029-the-layout-step-never-re-owns-a-socket.md) — its `$SYSTEM_ROOT` is the POSIX
  provisioning scripts' hardcoded root, which never reads this environment variable.
