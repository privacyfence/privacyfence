# ADR 0061: the MCP token and the browser session are audience-separated

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-08-28 in
`docs/https-connector-refactor-plan.md` §10.3, deleted in `96cd5af4` — read it with
`git show 96cd5af4^:docs/https-connector-refactor-plan.md` — and implemented the same day in
`930b5719`, merged as [#184](https://github.com/privacyfence/privacyfence/pull/184)).

## Context

Local mode's one embedded web app serves two very different callers on the same loopback port:
the MCP client (the agent) on `/mcp` and the file bridge on `/mcp-files/*`, and the human's browser
on `/approvals`, `/settings`, `/security` and their `/api/*` routes. The browser surface is where
an agent's gated call gets *released*. If any credential the agent legitimately holds for `/mcp`
also worked on the decide route, the agent could approve its own request, and every other control
around the approval surface would be moot. The plan called this "the control that matters most".

## Decision

In local mode, **the `/mcp` bearer token is never accepted on the approval or settings routes, and
the `pf_session` cookie (with the CSRF value derived from it) is never accepted on `/mcp` or the
file bridge.** The two are different secrets, generated independently and never compared against
each other, and each is checked by a different piece of code:

- `/mcp` is its own ASGI app (`web/routes_mcp.py`'s `build_mcp_asgi_app`), wrapped in the MCP SDK's
  `AuthenticationMiddleware(BearerAuthBackend(verifier))` / `AuthContextMiddleware` /
  `RequireAuthMiddleware` stack. It reads only the `Authorization: Bearer` header, checked by
  `web/mcp_auth.py`'s `PerUserTokenVerifier` (tokens minted over the control channel's `MINT MCP`).
  Missing or wrong credentials get `401` before the request reaches the session manager.
- The bearer-authenticated file bridge (`PUT /mcp-files/uploads/{slot}`,
  `GET /mcp-files/downloads/{token}`) is a small nested Starlette app with its own copy of that same
  stack (`web/routes_file_bridge.py`'s `build_file_bridge_asgi_app`), and its principal always comes
  from the bearer token (`principal_from_access_token`), never from a cookie.
- The approval and settings routes authenticate with `session_auth.authenticated` (the `pf_session`
  cookie against `LocalSessionStore`) and `session_auth.check_csrf` (the cookie value against the
  submitted `csrf`), and never read an `Authorization` header.

This is why `/mcp` and the file bridge are mounted as separate apps (`mount_mcp`,
`mount_file_bridge`) rather than as more routes on the approval app: the separation holds by
structure, not by each route remembering which credential it may accept. `/settings` is the
deliberate contrast: it shares the approval surface's session, because it is the same human-facing
application (`build_app`'s docstring).

## Alternatives considered

The plan states the rule without listing alternatives; these are the two the implementation's own
comments argue against.

- **One local secret for both surfaces** (the approval surface's `web_token` of the time doubling
  as the MCP token). Rejected: whoever holds the agent's credential then holds the approver's.
- **Plain routes on the approval app, each checking the right credential.** Rejected: separation by
  convention fails the first time a new route forgets which audience it belongs to; separate apps
  with separate auth stacks make the wrong credential unreadable rather than merely unchecked.

## Consequences

- An agent holding its MCP token gets `401` on `/api/approvals/{id}/decide`, even when it also
  presents that token as the CSRF value; a browser session id presented as a bearer token gets
  `401` on `/mcp`.
- This separates *credentials*, not *processes*: an agent that also obtains a `pf_session` by
  another route is the subject of ADR 0002 decision 6 and ADR 0062, not of this ADR.
- The capability pair (`/mcp-files/slots|fetch`, ADR 0028) sits outside both stacks by design; the
  URL's capability token is its only credential.
- Org mode holds the same property with different stores (`OrgOAuthProvider` tokens vs.
  `OrgSessionStore` cookies), recorded as a consequence of ADR 0011.

## Verification

- `tests/unit/web/test_server.py`'s `TestAudienceSeparation`: `/mcp` rejects a session id as a
  bearer token, accepts only its own token, and the decide route rejects the MCP token as CSRF;
  the capability routes answer `404`, not `401`, while their bearer-authenticated siblings `401`.
- `tests/unit/web/test_routes_mcp.py`'s `TestAuth`: no credential but the right bearer token
  passes `/mcp`'s auth layer.
- `mcpb/shim/test/index.test.ts`: every request the shim sends carries exactly the minted token.

## Related

- [ADR 0002](0002-local-mode-trust-boundary-and-companion-app.md) — the local-mode trust boundary
  this control sits inside.
- [ADR 0010](0010-local-mode-serves-plain-http-on-localhost.md) — the one loopback origin both
  surfaces share.
- [ADR 0011](0011-org-mode-runs-its-own-oauth-authorization-server.md) — org mode's side.
- [ADR 0007](0007-local-file-bridge.md), [ADR 0028](0028-clients-without-the-shim-get-capability-urls.md)
  — the file bridge and its capability URLs.
- [ADR 0062](0062-only-a-companion-attested-session-may-approve.md) — what a session needs to
  approve.
