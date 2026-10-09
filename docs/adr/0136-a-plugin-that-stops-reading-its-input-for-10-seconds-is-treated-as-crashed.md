# ADR 0136: A plugin that stops reading its input for 10 seconds is treated as crashed

## Status

Accepted — 2026-10-09. Implemented: `src/privacyfence/plugins/rpc.py` (`_send`),
`src/privacyfence/plugins/supervisor.py` (`_stop_child`), `src/privacyfence/plugins/constants.py`
(`SEND_TIMEOUT_SECONDS`).
Amends [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md).

## Context

A plugin that stopped reading its stdin froze **Disable**, **Delete this plugin's data** and daemon
shutdown. A write to it waited for good, and the stop notices were sent before any kill, so the
plugin's own stuck loop decided how long the daemon waited.

## Decision

Every message the daemon writes to a plugin must be taken within `SEND_TIMEOUT_SECONDS` (10 s, in
the schema's `x-limits`). Otherwise the peer is closed and the plugin is treated as crashed, which
the existing backoff and crash limit already handle. The stop notices get 1 s each, and a plugin
that did not take them is terminated without the 5 s grace.

## Alternatives considered

- **A timeout scaled by message size.** Rejected. It is more complex, and a plugin that does not
  read for 10 s is not serving its 10 s requests either.
- **Waiting without a limit.** Rejected. It is the freeze this decision removes.

## Consequences

- A plugin whose loop is blocked for more than 10 s while the daemon sends it more than about
  128 KiB is restarted as a crash.
- Stopping a stuck plugin takes about 3 s.
- Connector events reach plugins concurrently, so one stuck plugin no longer delays the others.

## Verification

`tests/unit/plugins/test_rpc.py`, `tests/unit/plugins/test_supervisor.py`,
`tests/unit/plugins/test_host.py`.

## Related

- [Issue 846](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md)
