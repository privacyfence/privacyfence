# ADR 0330: OAuth upstreams discover their server, register a client and keep tokens per person

## Status

Accepted — 2026-11-19. Implemented.
Relates to [ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md),
[ADR 0072](0072-org-mode-persists-only-sealed-refresh-tokens.md),
[ADR 0178](0178-a-sign-in-or-connect-flow-is-finished-in-the-session-that-started-it.md),
[ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md).
Issue [#1155](https://github.com/privacyfence/privacyfence/issues/1155); the record of the change is
[`docs/1155/plan.md`](../1155/plan.md).

## Context

[ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md)
has each person sign in at the vendor. The server must therefore find the vendor's authorization
server from the MCP address alone, get a client id, run an authorization-code flow with PKCE, and keep
tokens for later calls and refreshes. Every address involved comes from a document the vendor serves,
so it is untrusted input that PrivacyFence will fetch from inside an organization's network.

## Decision

1. **Discovery** (`upstream_oauth_discovery.discover`). One unauthenticated `initialize` to the MCP
   address (redirects off, body capped at 64 KiB) yields an optional `resource_metadata` hint from
   `WWW-Authenticate`; the protected-resource metadata is fetched and its `resource` must be allowed for
   the declared address (`check_resource_allowed`); the authorization server is `oauth.issuer` when
   pinned (it must be one of the listed servers), else the first listed; its metadata is fetched and the
   issuer validated. `S256` must be listed. The SDK fallbacks to `<origin>/register`, `<origin>/token`
   and the legacy well-known URL are never used.
2. **Address rules.** `_checked_url` accepts `https` only (`http` only when the declared upstream is a
   loopback address), no user name, password or fragment, and, unless loopback, refuses IP literals in
   private, loopback, link-local, multicast, reserved or unspecified ranges and names with fewer than
   two labels or ending in `.local`, `.internal`, `.home.arpa` or `.lan`. Every metadata URL must have
   the host of the document that named it (the upstream's, then the issuer's), so a hint on another host
   is never followed. A local upstream relaxes this only for URLs whose own host is loopback, so it cannot
   point the server at `169.254.169.254`.
3. **Public-address check and the DNS-rebinding residual.** The issuer's host is resolved once
   (`_resolve`) and every address must be public. The check refuses multicast and reserved addresses as
   well as non-global ones: it is stricter than `ipaddress.is_global`, which accepts some multicast ranges.
   The connection resolves the name again later, so a name that answers differently the second time can
   defeat the check. This is an accepted residual risk: closing it means pinning the resolved address in
   the transport, which the HTTP client in use does not offer; the host rules in 2 and the capped,
   redirect-free fetches limit what such a name can reach.
4. **Client registration** (`RegistrationStore`). In order: the operator's declared `oauth.client_id`
   (the secret read from the environment variable `client_secret_env` at call time; an empty variable is
   `not_configured`; nothing is written); a cached dynamic registration under
   `org/upstream_oauth/<id>.json` whose issuer and redirect URI match and which has not expired; a new
   RFC 7591 registration. The cache records the registration method, so a later method needs no
   migration. A vendor that dropped a registered client (`invalid_client` on refresh) makes
   `forget` remove the cache so the next Connect registers again.
5. **The registration store holds no upstream data.** It takes an optional `upstreams=` argument so
   `client_for(upstream_id)` can answer for a declared client. After a restart and before any Connect,
   the token auth method of a declared client with a secret is not known from discovery; the runtime's
   `_client_source` therefore reads it from the newest saved token record with the same `client_id`
   (falling back to `client_for`), so a refresh uses the method that worked at Connect.
6. **Tokens** (`upstream_oauth_tokens`). One file per person and upstream,
   `<user dir>/credentials/upstream_mcp/<id>.json`, mode 0600, plaintext like every other third-party
   token. The authorization-code exchange and refreshes are done with the SDK's pure helpers (URL and
   metadata builders, models, expiry), not its `OAuthClientProvider`, which starts an interactive
   authorization from inside a request and keeps no authorization-server metadata, so a refresh after a
   restart would go to `<origin>/token`, wrong whenever the authorization server is on another host.
   A refresh runs under a per-file lock; a 400 or 401 deletes the file (`auth_expired`); a client whose
   id differs from the record's is `auth_expired`. No log line or message holds a response body, token,
   code or secret.
7. **The Connect flow** (`web/routes_upstream_oauth.py`). The redirect goes straight to the server
   (`<issuer>/upstream-mcp/<id>/callback`), not through the relay of ADR 0259, which serves only
   built-in apps. The state is single-use with a 10-minute lifetime; the callback must be finished by the
   principal who started it ([ADR 0178](0178-a-sign-in-or-connect-flow-is-finished-in-the-session-that-started-it.md));
   an `iss` that differs (or is missing when the server says it sends one, RFC 9207) is refused.

## Alternatives considered

- **The SDK's `OAuthClientProvider`.** See 6.
- **A Client ID Metadata Document served by PrivacyFence.** Left out (owner decision): PrivacyFence cannot
  check that the vendor can fetch the document. The cache records the method so adding it later is not a
  migration.
- **Encrypt tokens at rest or seal them to the bearer.** [ADR 0072](0072-org-mode-persists-only-sealed-refresh-tokens.md)
  rejected a key stored beside the data, and refreshes and scoped-token calls run with nobody present
  to supply one.
- **Pin the resolved address to defeat rebinding.** Not offered by the HTTP client; recorded as residual
  in 3.
- **Ask the operator for every endpoint.** Defeats the point; discovery with strict address rules is
  checkable and needs no per-vendor configuration.

## Consequences

- An operator declares an address and, at most, scopes, a client id and an issuer pin.
- A person's tokens are readable by anything that can read the server's data directory, as with every
  other connector; a declared client secret is readable by anything that can read the daemon's
  environment, and the docs say to use the orchestrator's secret store.
- A reverse proxy that logs the callback query keeps the authorization code; PKCE bounds the damage.
- A declared upstream with no `oauth.scopes` asks for every scope the server lists; the runtime warns at start.

## Verification

- `tests/unit/test_upstream_oauth_discovery.py`: the metadata chain, host binding, address refusals
  (multicast and reserved included).
- `tests/unit/test_upstream_oauth_tokens.py`: exchange, refresh, locks, deletion on `invalid_client`
  or expiry, no secrets in messages.
- `tests/unit/test_upstream_mcp_runtime.py`: `_client_source` reading the auth method from the saved record.
- `tests/unit/web/test_routes_upstream_oauth.py`: state, principal, `iss` and error handling.
- `tests/integration/test_upstream_oauth_org.py`: Connect, refresh and revocation against the reference server.

## Related

Issue [#1155](https://github.com/privacyfence/privacyfence/issues/1155),
[ADR 0329](0329-hosted-mcp-servers-sign-in-each-person-with-their-own-oauth-and-the-claim-feature-is-removed.md),
[ADR 0331](0331-the-administrator-manages-each-upstream-servers-tools.md),
[ADR 0072](0072-org-mode-persists-only-sealed-refresh-tokens.md),
[ADR 0178](0178-a-sign-in-or-connect-flow-is-finished-in-the-session-that-started-it.md),
[ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md).
