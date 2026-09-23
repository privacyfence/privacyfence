# ADR 0009: local mode serves plain HTTP on `localhost`, not HTTPS with a self-signed certificate

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md` §10.2 and §15 D1, deleted in `96cd5af4`). Implemented,
except the TLS opt-in the decision kept open for local mode, which has not been built (see
Consequences).

## Context

Local mode's embedded web server carries the approval pages, the settings page and the `/mcp`
endpoint for clients on the same machine. The plan's original brief asked for HTTPS. The decision
was whether to serve real HTTPS with a certificate PrivacyFence generates itself, or loopback HTTP.

The facts the plan relied on:

- browsers treat `http://localhost` as a secure context, so the web UI loses nothing by it;
- WebAuthn needs a secure context and a registrable-domain RP ID; `localhost` qualifies over plain
  HTTP, a bare IP address such as `127.0.0.1` does not;
- MCP clients reject a self-signed certificate unless the user installs a local CA;
- a TLS private key stored in `~/.privacyfence` protects nothing against an attacker who can already
  read `~/.privacyfence`, which is the boundary the then-current `ipc_token` design already drew.

## Decision

Local mode serves plain HTTP, bound to and addressed as `localhost` rather than `127.0.0.1`, so
`http://localhost:PORT` stays a secure context and WebAuthn step-up stays available.

The plan's decision commit (`88fcfdaf`) records that the `localhost`-not-`127.0.0.1` constraint was
added when D1 was taken, specifically because reached by IP the biometric step-up (D7) would be
unavailable. (The plan's own §9.4 still said "binds `127.0.0.1`"; §10.2 and D1 are the decision.)

Org mode is out of scope here: it requires HTTPS, terminated in `uvicorn` or at a reverse proxy.

## Alternatives considered

- **HTTPS with a self-signed certificate.** Rejected: MCP clients would refuse it without a locally
  installed CA, and the plan judged that the friction carries a real chance of teaching users to
  click through TLS warnings, a net security loss. It also adds no protection against the attacker
  local mode is actually exposed to, per the key-in-`~/.privacyfence` point above.
- **Plain HTTP on `127.0.0.1`.** Rejected because an IP address cannot be a WebAuthn RP ID.

## Consequences

- There is no TLS between browser/client and daemon, so nothing authenticates the server's
  hostname. A page on any origin can make the user's browser send requests to `localhost:PORT`,
  including via DNS rebinding. That risk is accepted and defended at the application layer instead:
  a `Host` allowlist rejects any request not addressed to `localhost`, `127.0.0.1` or `::1` before
  it reaches a route (`_HostAllowlistMiddleware`, with standards-aware Host parsing since SEC-17);
  state-changing requests carry a CSRF double-submit token plus an `Origin` check; browser access
  needs a session minted from a single-use bootstrap code (SEC-06); `/mcp` needs its own bearer
  token. How that token and session are guarded is ADR 0002's and ADR 0006's subject, not this one.
- HSTS is deliberately not sent in local mode: HSTS is scoped by hostname, so a browser honoring it
  would refuse plain HTTP to every other app on `localhost`.
- Local-mode passkey step-up
  ([#426](https://github.com/privacyfence/privacyfence/issues/426)) uses RP ID `localhost`, which
  this decision is what permits.
- Third-party HTTP clients that refuse non-HTTPS targets need a flag to reach local mode; the
  `.mcpb` shim (ADR 0011) exists partly so Claude Desktop users never set one.
- The plan said TLS would remain available in local mode as an opt-in (a generated certificate plus
  trust instructions). The local-mode `WebServer` construction in `daemon_main.py` passes no
  certificate; only org mode wires `cert_file`/`key_file`. The opt-in is unbuilt, not rejected.

## Verification

- `src/privacyfence/web/server.py`: module docstring ("D1's decision applies"), `WebServer` default
  `host="localhost"`, `_HostAllowlistMiddleware`, `_parse_host_header`, the `hsts` gate.
- `src/privacyfence/web/session_auth.py`: `check_origin`; `src/privacyfence/step_up_config.py`:
  `DEFAULT_LOCAL_RP_ID = "localhost"`.
- `tests/unit/web/test_server.py`: `TestHostAllowlist`, `TestParseHostHeader`.

## Related

- `git show 96cd5af4^:docs/https-connector-refactor-plan.md` §10.2, §10.6 ("The RP-ID rule
  constrains D1"), §15 D1 and D7.
- Commit `88fcfdaf` (D1 recorded as a decision, with the `localhost` constraint).
- Issue https://github.com/privacyfence/privacyfence/issues/426 (local-mode passkey).
- ADR 0002 (the local-mode trust boundary is the OS user account), ADR 0011.
