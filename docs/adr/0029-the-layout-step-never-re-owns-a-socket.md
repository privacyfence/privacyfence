# ADR 0029: the layout step never re-owns a socket

## Status

Accepted — 2026-09-23. Amends [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md).

## Context

`apply_layout()` in `scripts/macos_privilege_separation.sh` and `scripts/linux_privilege_separation.sh`
runs on every `enable`. That includes the re-run every package upgrade makes: the `.pkg` postinstall's
`enable --auto` and the `.deb` postinst's `enable --machine-only`. It re-owned the whole system root
with `chown -R` to the service account, then stripped group and other access with `chmod -R go-rwx`.

On a re-run, `handoff/companion.sock` is usually a live socket: the owner's companion bound it, as the
owner's uid, at mode `0660`. The recursion re-owned it to the service account and dropped it to
`0600`. ADR 0027 made that fatal:

- `handoff/` is sticky (`3770`), so the running companion could no longer unlink its own socket on
  shutdown;
- `_existing_socket_owner_problem()` then refused to let the next companion take over a socket
  owned by another uid, so it ran with no socket at all;
- anyone in the service group got `EACCES` connecting to the stale node.

The daemon's `OPEN`, `MINT COMPANION` and `SHOW RECOVERY` calls then had no companion to reach until
someone deleted the file by hand. ADR 0027's own Consequences anticipated a stale, other-owned
socket needing a human to clear it; it did not anticipate the installer manufacturing one on every
upgrade. `test_macos_upgrade_preserves_user_state` found it once it reached a second `enable`
(build.yml runs 35897583364, 35902612720 and 35904497312 on 2026-09-23).

## Decision

`apply_layout()` re-owns and tightens everything under the system root **except sockets**:
`find "$SYSTEM_ROOT" ! -type s -exec chown -h …` and `find "$SYSTEM_ROOT" ! -type s ! -type l -exec
chmod go-rwx …`. A socket is a runtime object owned by the process that bound it, at the mode it
chose (`control_channel._LineProtocolServer._start_posix()` via
`privilege_separation.socket_mode()`); the layout step has no opinion on it.

`chown -h` and the `chmod`'s `! -type l` are part of the decision, not incidental: `find -exec`
follows a symlink operand where `chown -R`/`chmod -R` did not, which would let the service account
plant a link and have root re-own an arbitrary file.

## Alternatives considered

- **Delete sockets under `handoff/` before re-owning.** Sockets are recreated on start, so this
  looks equivalent, and it is what `move_handoff_files_in()` already does for the *old* paths. But
  on a machine-wide install another logged-in member's companion is bound to its own
  `companion-<uid>.sock` (ADR 0008 D4), and `enable` restarts only the owner's companion. Deleting
  theirs would cut them off until their next login, which is the same outage this ADR fixes,
  moved to a different user.
- **Exempt the socket from ADR 0027's owner check when the owner is the service account.** It
  would let the next companion replace the stale node, but it reopens exactly the takeover 0027
  closes for any socket the daemon's own account holds, and it leaves the `EACCES` window until
  the companion restarts. The installer produced the bad state, so the installer is what changes.

## Consequences

- Re-running `enable` with a companion running leaves that companion's socket exactly as it was;
  `install_services()`'s bootout lets the companion unlink its own socket, and the new one binds
  fresh.
- A socket left in the tree by anything else keeps the owner it had. That was already true of
  every socket bound after the last `enable`, so nothing new becomes reachable.
- Symlinks inside the system root are re-owned (`chown -h`) but never followed.

## Verification

- `tests/unit/test_privilege_separation.py::TestApplyLayoutLeavesSocketsAlone` runs both scripts'
  real `apply_layout()` against a tree holding a bound socket and a symlink, with `chown`/`chmod`
  recorded on `PATH`. It fails against the old `chown -R`/`chmod -R`.
- `tests/integration/test_macos_packaged_smoke.py::test_macos_upgrade_preserves_user_state` runs a
  second `enable` against a live install on a real macOS runner (build.yml).
