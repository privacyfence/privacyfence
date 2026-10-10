# ADR 0143: The bundle pins the Grist server, and a credential stays with its server

## Status

Accepted — 2026-10-10. Implemented.

## Context

Grist can be hosted (`docs.getgrist.com`, team sites) or self-hosted, so the connector cannot fix
one address. A free address is a risk: a credential typed or issued for one server must never reach
another, and in org mode a person must not be able to send organization data to a server of their
choosing.

## Decision

- In local mode with no Grist section in the bundle, the person chooses the server (API key). A
  bundle `server_url` pins it for both kinds of credential. Org mode offers Grist only when the
  bundle names a server.
- A credential is used only with the server it was entered or issued for. A saved record whose
  server differs from the pinned one is refused with "Grist was connected to a different server
  than your organization uses", and the person connects again.
- Only `https` addresses are accepted, plus `http` to `localhost`, `127.0.0.1` or `::1`. A user
  name, password, query, fragment or `/api` is refused. `build_org_bundle.py` applies the same rules.
- Redirects are never followed, for the API and for the sign-in server.
- OAuth discovery starts at the configured server and follows the issuer it names at most once; that
  issuer's own document must name itself. Both endpoints must be on the issuer's host, because the
  token endpoint receives the client secret and the refresh token.
- Every Grist approval card starts its preview with the server, so the person sees where data comes
  from or goes to.
- Connecting Grist stays a non-sensitive Settings action, like every connector sign-in (ADR 0070
  makes only *enabling* a connector sensitive), because the card names the server on every read
  and write.

## Alternatives considered

- **getgrist.com only** — rules out self-hosted Grist.
- **A free server address in org mode** — a person could send organization data to any server.
- **Making `grist_connect` a step-up action** — Grist would be the only connector whose sign-in
  needs a passkey, for a risk the card already shows on every call.

## Consequences

An administrator who moves the bundle to another server invalidates every saved credential, which
is the intended behavior. A local-mode person who types a hostile address can only send their own
key there; the card still names it on every call.

## Verification

- `tests/unit/test_grist_auth.py`: `TestNormalizeServerUrl`, `TestResolveCredential`
  (`test_pinned_server_different_for_an_api_key`, `test_oauth_server_different_for_an_oauth_record`),
  the discovery tests (issuer followed once, endpoint on another host rejected, redirect rejected).
- `tests/unit/test_grist_client.py`: `allow_redirects` is `False` on every call.
- `tests/unit/connectors/test_grist_connector.py`: every card's preview starts with `Server`.
- `tests/unit/test_build_org_bundle.py`: the same URL rules in the bundle builder.
- `tests/unit/web/test_routes_settings.py`: `TestGristApiKeyActions`.

## Related

- [`grist_auth.py`](../../src/privacyfence/grist_auth.py), [`grist_client.py`](../../src/privacyfence/grist_client.py)
- ADR [0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md), ADR [0072](0072-org-mode-persists-only-sealed-refresh-tokens.md)
- ADR [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md)
