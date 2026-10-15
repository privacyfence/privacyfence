# ADR 0187: the `.mcpb` shim trusts only handoff entries the service account owns; the daemon replaces a foreign control socket

## Status

Accepted — 2026-10-15. Implemented.
Amends [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md).

## Context

`handoff/` is group-writable by design ([ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md)).
The shim, which runs as the person, connected to `control.sock` and read `mcp_url` there with no
check of who made them. A group member could bind its own socket at that address while the daemon
was down, and answer the shim's request for an MCP token (or point `mcp_url` at another host). The
daemon's side of the problem: [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md)
decision 2 makes it refuse to start a channel over a socket another uid owns, which for the
control channel left the daemon running with none.

## Decision

1. **The shim checks ownership before it connects or reads.** On a separated install it requires
   `control.sock` to be a socket, and `mcp_url` a regular file, owned by the uid that owns the
   system root. It also requires the `mcp_url` host to be `127.0.0.1`, `[::1]` or `localhost`.
2. **Only the daemon's control channel replaces a foreign socket.** In that case the daemon
   unlinks the socket, logs a warning, and binds its own; if the unlink fails it keeps today's
   behaviour of running without the channel. **The companion channel keeps refusing**, as ADR 0027
   decision 2 requires, because the sticky bit does not protect a socket bound before the bit
   existed.

## Alternatives considered

- **Replace a foreign socket on both channels.** Rejected: a companion socket is per account, and
  one account's companion could then be unlinked by another's daemon-side retry.
- **Pin a socket path or check the peer after connecting.** Rejected: the owner check before
  connecting keeps the shim small and needs no protocol change.
- **Keep refusing on the control channel.** Rejected: a group member could then keep the daemon
  without a control channel by leaving a socket behind.

## Consequences

- The shim refuses a socket or URL file a group member planted, with an error naming the entry.
- The daemon's service account owns `handoff/`, so it can unlink there; the daemon recovers its
  control channel instead of running without one.
- The companion channel is unchanged.

## Verification

- `mcpb/shim/src/protocol.ts` (`checkHandoffEntry`, `readMcpUrl`), `mcpb/shim/src/controlChannel.ts`,
  and their tests; the shim's `npm test` enforces a coverage threshold.
- `src/privacyfence/web/control_channel.py` (`replace_foreign_socket`) and
  `tests/unit/web/test_control_channel.py`.

## Related

- [ADR 0027](0027-a-group-member-cannot-take-over-another-members-companion-socket.md)
- [ADR 0007](0007-local-file-bridge.md)
