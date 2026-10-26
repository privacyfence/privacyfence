# ADR 0230: Miro connects only by OAuth, with an expiring-token app that the organization bundle carries

## Status

Accepted — 2026-10-25. Implemented: `miro_auth.py`, the `miro` bundle section and
`credentials/miro_token.json`.

## Context

Miro offers three ways in: an OAuth app, a personal token from the app page's "Install app and get
OAuth token" button, and Miro's own MCP server. An OAuth app made with "Expire user authorization
token" off gives an access token that works until the app is uninstalled; the choice is made when
the app is created and cannot be changed afterwards. Miro's SDKs send the client secret, the code
and the refresh token as query parameters. Miro takes no `scope` parameter: permissions are set on
the app. Miro documents no PKCE.

## Decision

- **The connection uses an app the organization registered,** carried in the organization bundle's
  `miro` section (`client_id`, `client_secret`), through the authorization code flow of a
  confidential client with a checked `state` and no PKCE.
- **The app's permissions are `boards:read` and `boards:write`,** set on the app. Sign-in refuses a
  token whose scope lacks either one, and refuses a token that never expires or has no refresh
  token.
- **Credentials go in the form body,** never in the URL, and redirects are never followed.
- **One file per principal,** `credentials/miro_token.json`, written only by
  `miro_auth.save_token_file` (`atomic_write_json`, mode 0600). A refresh rotates the refresh
  token. It re-reads the file first, so two live instances cannot void each other's refresh token.
- **A connection is one Miro team,** the one chosen on Miro's install screen. Connecting again
  replaces it.

## Alternatives considered

- **Non-expiring tokens** — a leaked token would work until the app is uninstalled.
- **Personal tokens from the app page** — they bypass the organization's app, and Miro does not
  expire them either.
- **Query-string credentials, as Miro's SDKs send them** — request-line logging could capture them.
- **Miro's own MCP server (`mcp.miro.com`) instead of a connector** — it has no PrivacyFence
  approval card, policy or audit, runs with its own OAuth 2.1 grant outside the organization
  bundle, and is enabled per Enterprise organization.

## Consequences

- An administrator must create the Miro app with the expiry option on. An app made without it is
  refused at sign-in with a message that says so, and cannot be repaired, only replaced.
- Every person signs in for themselves; no token is shared.
- A person who is away longer than a refresh token lasts signs in again.
- The live QA runner holds a hand-obtained token for the same reason as the other connectors
  ([ADR 0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md)).

## Verification

- `tests/unit/test_miro_auth.py`: form body and no URL credentials, the scope and expiry checks,
  rotation, adoption of a newer token file, and no secret in a message or repr.
- `tests/unit/test_systemic_gate_invariants.py`: the token write site is listed.
- `tests/unit/test_build_org_bundle.py`, `tests/unit/web/test_routes_connect.py`,
  `tests/unit/test_settings_controller.py` and `tests/unit/test_daemon_main.py`: the bundle
  section, the organization sign-in and the local sign-in.

## Related

- ADR [0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md),
  ADR [0142](0142-grist-connects-by-oauth-with-an-app-in-the-bundle-and-by-api-key-otherwise.md),
  ADR [0196](0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)
- [`miro_auth.py`](../../src/privacyfence/miro_auth.py),
  [`oauth_loopback.py`](../../src/privacyfence/oauth_loopback.py)
