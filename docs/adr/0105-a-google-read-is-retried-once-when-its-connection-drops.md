# ADR 0105: a Google API read is retried once when its connection drops; PrivacyFence never retries a write

## Status

Accepted — 2026-09-29. Fixes [issue 800](https://github.com/privacyfence/privacyfence/issues/800).

## Context

A Gmail read failed once with `EOF occurred in violation of protocol (_ssl.c:2427)`. googleapiclient
reuses a pooled keep-alive TLS connection between calls, and Google or a middlebox had already closed
that one. The agent reported that it couldn't read the email, and the identical call succeeded when
retried by hand.

httplib2 does not retry this. It re-raises an `ssl.SSLEOFError` or a `ConnectionResetError`, and after
a failure while sending it leaves the broken connection in its pool, so a naive retry can reuse it.
httplib2 does reconnect and re-send on its own in two narrow cases, for any method: a `BadStatusLine`
on the first attempt (which covers `RemoteDisconnected`) and an `HTTPException` while sending. That is
library behaviour this decision neither adds nor removes, which is why the decision is worded "PrivacyFence
never retries a write", not "a write is never re-sent".

Drive's full download/export (`drive_client.py`, `_stream_full_content`) and `fetch_thumbnail` use
`google.auth.transport.requests.AuthorizedSession`, not googleapiclient. Each builds a new session per
call, so it has no stale pooled connection to hit, and they are out of scope.

ADR 0073 (an approved write is single-use) is why a write must never be replayed underneath the gate.

## Decision

- Every Google client builds its googleapiclient service with `http=google_http.authorized_http(creds)`,
  never `credentials=`. That returns `RetryOnceAuthorizedHttp`, a `google_auth_httplib2.AuthorizedHttp`
  over the same `googleapiclient.http.build_http()` transport as before (same timeout, 308 still not a
  redirect for Drive resumable uploads).
- A `GET` that fails with `ssl.SSLEOFError`, `http.client.RemoteDisconnected`, `ConnectionResetError`,
  `ConnectionAbortedError` or `BrokenPipeError` is retried once: the transport is closed, so the pooled
  connection is forgotten, and the request is sent again with no sleep. A second failure propagates
  unchanged; no new exception type is added (§1.4).
- The retry lives in the transport, below the approval gate, so it causes no second approval and no
  second audit row.
- The 401-refresh re-entry of `AuthorizedHttp.request` (`_credential_refresh_attempt`) never retries,
  so a 401 followed by two drops retries once, not twice.
- Not retried: any method other than `GET` (including read-only `POST`s such as Calendar
  `freebusy.query`), timeouts, `ssl.SSLCertVerificationError` and every other `ssl.SSLError`,
  `ConnectionRefusedError`, DNS failures, and every HTTP response whatever its status. A connect timeout
  cannot be told from a read timeout at this layer, retrying a read timeout would double a 60 s wait, and
  a stale pooled connection fails fast rather than timing out.
- The log line is INFO, and carries the method, the host and the exception class name. It never carries
  the path, query (a Gmail search sits there) or the exception text (§1.8).
- `tests/unit/test_google_http.py` scans the client sources so a new client cannot build with
  `credentials=`.

## Alternatives considered

- **googleapiclient's `execute(num_retries=1)`.** Rejected: it retries every method (writes included),
  also retries 5xx and 429 responses, sleeps with random backoff, and logs the full URI, with a Gmail
  search query in it, at WARNING.
- **Wrap each of the ~115 `.execute()` calls.** Rejected: every call site has to be classified by hand,
  and a new call site silently gets no retry or, worse, a retried write. The transport classifies by HTTP
  method, so a new write is safe by construction.
- **Retry in the connector or at `gated_call`.** Rejected: a second pass through the gate means a second
  approval and a second audit row.
- **Retry writes that Google makes idempotent (request IDs).** Deferred, not rejected forever: few of the
  APIs used offer one, and each would be an exception to audit.
- **Retry timeouts.** Rejected, for the reasons under Decision.

## Consequences

- A dropped connection on a read is invisible to the user, and logged once at INFO.
- A drop during a token refresh inside a `GET` retries the whole `GET`. That is harmless because a
  refresh is idempotent, but the log line names the API host.
- A write that loses its connection still fails and the agent sees the error, as before.
- A new Google client that builds with `credentials=` fails `test_google_http.py`'s source scan.
