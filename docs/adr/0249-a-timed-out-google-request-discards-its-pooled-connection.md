# ADR 0249: A timed-out Google request discards its pooled connection

## Status

Accepted — 2026-10-28. Implemented.
Amends 0105.

## Context

ADR 0105 retries a Google `GET` once when its pooled connection drops, and assumes a stale connection
fails fast. That holds when the peer sends a FIN or RST. It does not hold when a NAT, firewall or load
balancer silently drops an idle connection: the request is written into the void and the read waits the
full 60 s, then fails with `The read operation timed out`.

httplib2 (0.32.0, `Http._conn_request`) closes the connection on most errors but not when
`getresponse()` times out: that path is a bare `except socket.timeout: raise`. The dead connection stays
in the pool, so every later request on that service reuses it and times out too, until the daemon
restarts. A live install showed exactly that: three timeouts in a row on Drive and Sheets, on a
one-row read and on a plain metadata lookup, with `curl` to Google fast from the same host, and a
daemon restart fixing it.

## Decision

- `RetryOnceAuthorizedHttp.request` catches `TimeoutError` (any method), calls `self.close()` so the
  pooled connection is forgotten, and re-raises the same exception. The next request opens a new
  connection.
- A timeout is still not retried, for the reasons in 0105: a connect timeout cannot be told from a read
  timeout at this layer, a retry would double a 60 s wait, and a write may already have been applied.
- The INFO log line carries the method, host and exception class name, never the path, query or
  exception text (§1.8).

## Alternatives considered

- **Retry the timed-out `GET` once.** Rejected, as in 0105: it doubles the wait to 120 s and cannot tell
  a dead connection from a slow server.
- **A shorter timeout.** Rejected: it only shortens the wait; the dead connection would still be reused.
- **TCP keepalive on the sockets.** Not done here: it would prevent some idle drops but is a larger,
  platform-dependent change, and it would not repair a connection that is already dead.

## Consequences

- One stalled call no longer breaks every later call: the call that hit the dead connection fails
  after its timeout, and the next one succeeds on a fresh connection.
- A write that times out closes the connection too. This does not replay the write.
- The agent still sees the timeout error for the one call that hit it.

## Verification

`tests/unit/test_google_http.py`: `test_timeout_is_not_retried_but_discards_the_connection` and
`test_request_after_a_timeout_goes_out_normally`.

## Related

0105 (a Google read is retried once when its connection drops), 0073 (an approved write is single-use).
