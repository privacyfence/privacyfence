# ADR 0027: a service-group member cannot take over another member's companion socket

## Status

Accepted — 2026-09-23; implemented in [#609](https://github.com/privacyfence/privacyfence/pull/609)
(`7b51a27b`). Still in force after [ADR 0008](0008-one-principal-per-os-user.md) D4 gave each
principal its own socket address: the sticky bit is what keeps those per-user addresses
per-user. #609 first recorded this inside ADR 0002's decision 2; it was moved here when the ADR
rules in [`README.md`](README.md) were adopted.

Amended by [ADR 0029](0029-the-layout-step-never-re-owns-a-socket.md): the installers' layout step no longer re-owns sockets, which had turned every upgrade into a stale-socket lockout.

## Context

ADR 0003 decision 3 deliberately lets an administrator add more than one OS account to a separated
install's service group, and ADR 0002 documented `handoff/` as group-shared by design. The
companion listened on one `handoff/companion.sock` per machine, so any group member could unlink
another member's socket and bind their own: whoever (re)started last received the daemon's
`OPEN`/`MINT COMPANION`/`SHOW RECOVERY` calls.

That is not an inconvenience. `SHOW RECOVERY` puts a fresh recovery code in front of whichever
companion answers, which hands the other account the owner's approval authority. ADR 0002 decision 6
had reasoned about who shares a uid with the agent; this was the other direction — two different
uids sharing one address.

## Decision

Close it two independent ways, because neither alone is airtight:

1. **Sticky bit on `handoff/`**: mode `0o2770` → `0o3770` (`privilege_separation.HANDOFF_DIR_MODE`
   and the POSIX platform scripts). As on `/tmp` since 4.3BSD, only a file's owner (or root) can
   unlink it.
2. **Refuse to rebind someone else's socket.** Before unlinking an existing socket file,
   `web/control_channel.py`'s `_start_posix()` checks its owner
   (`_existing_socket_owner_problem()`); if another uid owns it, the channel refuses to start and
   logs why. This covers what the sticky bit alone does not: a socket bound before the bit existed,
   and the window between an owner's `unlink()` and its own `bind()`.

On Windows, the companion pipe is created with `FILE_FLAG_FIRST_PIPE_INSTANCE`, the counterpart of
the POSIX owner check.

## Alternatives considered

- **Either fix alone.** Rejected for the reasons in the Decision: each leaves a case the other
  closes.
- **Refusing to add a second account at all.** Shipped at the same time as a separate, explicitly
  temporary guard (owner-only `enable --for-user`, `--allow-additional-user` as an escape hatch,
  no-clobber data migration), and superseded by ADR 0008. The socket fix is not temporary: it
  protects per-user addresses too.

## Consequences

- A group member can no longer receive another member's recovery code by racing a companion
  restart.
- A stale socket owned by another uid now stops the companion channel from starting, with a log
  line, rather than being silently replaced. Someone has to clear it (the owner, or root).
- After ADR 0008 D4 every non-owner principal has its own `companion-<uid>.sock`, and this
  decision is what stops a member from unlinking someone else's.

## Verification

- `tests/unit/web/test_control_channel.py`: `_existing_socket_owner_problem` refuses a socket owned
  by a different uid, for the shared address and (since ADR 0008) the per-uid one.
- `tests/unit/test_privilege_separation.py`: `HANDOFF_DIR_MODE` and the scripts' mode agree, and
  `windows_acl.handoff_problems()`/`status` accept it.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — decision 6, and `handoff/` as a
  group-shared directory.
- [ADR 0003](0003-separated-installs-only.md) — decision 3, which makes a shared service group
  possible.
- [ADR 0008](0008-one-principal-per-os-user.md) — D4, per-user companion addresses.
- Source plan: `local-mode-fixes-plan.md` §2.6 item 4, never merged to `main`; read it with
  `git show 453ae02e:local-mode-fixes-plan.md`.
