# ADR 0010: org mode runs its own OAuth authorization server

## Status

Accepted (recorded retroactively on 2026-09-23; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md` §9.4 and §15 D5, deleted in `96cd5af4`). The own
authorization server is implemented (phase P7, 2026-09-03). The IdP-delegation configuration the
decision also allowed for has not been built (see Consequences).

## Context

In org mode PrivacyFence is an OAuth 2.1 protected resource: Claude adds it as a remote connector
and presents an access token on every `/mcp` request. Humans separately sign in to the approval
pages in a browser, via OIDC against the organization's identity provider. Something has to be the
authorization server that issues the MCP access token. The plan named two shapes:

- **(A) Delegate.** The org IdP is the authorization server; PrivacyFence only publishes
  protected-resource metadata pointing at it.
- **(B) Own minimal AS.** PrivacyFence issues its own tokens via authorization code + PKCE, with
  Dynamic Client Registration, and authenticates the human against the IdP by OIDC behind the
  scenes.

## Decision

**(B), with (A) as a supported configuration.** PrivacyFence runs its own OAuth 2.1 authorization
server for `/mcp` in org mode. Its `authorize` step redirects the browser to the org IdP; the IdP's
answer comes back to PrivacyFence, which resolves a `Principal` from the ID-token claims and only
then mints its own authorization code bound to that principal.

The deciding argument, as the plan gave it: with (B), the browser session and the MCP token are
provably the *same identity*, which is what lets an approval page be trusted to answer for a given
MCP caller. The code makes that true by construction: the AS callback and the browser's own
`/login` both resolve the human through the same function, `org_identity.principal_from_claims`.

The protocol endpoints themselves (`/authorize`, `/token`, `/register`, `/revoke`, the AS metadata
and RFC 9728 resource-metadata documents) come from the official MCP SDK's auth routes, not
hand-written code: the same reasoning as ADR 0008.

## Alternatives considered

- **(A) Delegate to the org IdP.** The plan's assessment: least code, but it depends on the IdP
  supporting Dynamic Client Registration or on pre-registering Claude as a client, and it does not
  by itself tie the MCP token to the same identity as the browser session. Not rejected outright:
  kept as a configuration to support on top of (B).

## Consequences

- PrivacyFence holds authorization-server state: DCR client registrations persist in
  `oauth_clients.json`; codes and access tokens are in memory; refresh tokens persist, sealed
  (`sealed_refresh_store.py`, [#402](https://github.com/privacyfence/privacyfence/issues/402)).
- `/register` is unauthenticated by design, so `OrgOAuthProvider` itself must bound it (SEC-16's
  client cap), not a reverse proxy.
- Claude adds the connector by DCR with no IdP-side client setup for Claude.
- The OAuth client record is available as an identity signal; ADR 0006 builds its org-mode
  attribution (option C) on `OrgOAuthProvider.register_client()`.
- Audience separation holds: an org-mode MCP access token is rejected as a browser session and vice
  versa.
- (A) is not implemented. `routes_mcp.mount_org_oauth` always publishes
  `authorization_servers=[issuer]` (PrivacyFence itself), and `org_mode.py` has no setting to point
  the resource metadata at the IdP. Whether (A) is still wanted is not recorded anywhere.

## Verification

- `src/privacyfence/web/oauth_provider.py`: `OrgOAuthProvider` (implements the SDK's
  `OAuthAuthorizationServerProvider`), `handle_idp_callback`, `register_client`.
- `src/privacyfence/web/routes_mcp.py`: `mount_org_oauth` (SDK `create_auth_routes` +
  `create_protected_resource_routes`, plus the IdP callback route).
- `src/privacyfence/org_identity.py`: `principal_from_claims`, shared with
  `src/privacyfence/web/routes_org_identity.py`'s browser login.
- `tests/unit/web/test_org_mcp_e2e.py` (DCR, `/authorize`, `/token`, a real tool call; two humans
  resolve to two isolated principals; audience separation), `tests/unit/web/test_oauth_provider.py`.

## Related

- `git show 96cd5af4^:docs/https-connector-refactor-plan.md` §9.4, §10.3, §15 D5; its status
  header's P7 paragraph describes what shipped.
- Commit `88fcfdaf` (D5 recorded as a decision), commit `4d93b234` (P7 implemented).
- ADR 0008 (the SDK this is built on), ADR 0006 (attribution from the OAuth client).
