# ADR 0142: Grist connects by OAuth when the bundle carries an app, and by a personal API key otherwise

## Status

Accepted — 2026-10-10. Implemented.

## Context

Grist offers two ways in. A personal API key exists on every edition but carries the person's whole
access, with no scopes. OAuth apps, with scopes and refresh tokens, exist only in Grist's paid
editions, and only an administrator can register one. The other connectors with an organization
app (Slack, Salesforce, Atlassian) are OAuth-only; a Grist that required OAuth would shut out
everyone on a free or community self-hosted server.

The scopes also carry a known cost: Grist documents that `doc.schema:write` can reveal any data in
a document through formulas.

## Decision

- When the organization bundle's `grist` section carries a client id and secret, Grist connects by
  OAuth: authorization code with PKCE (S256), a confidential client, `prompt=consent` and
  `offline_access`. The scopes are exactly `doc:read`, `doc:write`, `doc.schema:write` and
  `offline_access`. The connector uses `doc.schema:write` only to add tables and columns.
- Without an app in the bundle, Grist connects by a personal API key typed into Settings (local
  mode) or the connections page (org mode).
- With an app in the bundle, an API key is refused: the organization chose OAuth, and a key would
  bypass the scopes it chose.
- Both kinds live in one per-principal file, `credentials/grist_token.json`, written only by
  `grist_auth.save_token_file` through `secure_files.atomic_write_json` (mode `0600`). A credential
  is never in `settings.yaml`, the settings snapshot, a log line or the audit log.

## Alternatives considered

- **OAuth only** — free and community self-hosted Grist could not connect at all.
- **API key only** — full account access with no scopes, where the organization could have scoped
  OAuth.
- **Accept either kind when the bundle carries an app** — the organization's choice of scoped
  access would be optional per person.

## Consequences

Each install shows one sign-in path, decided by its bundle. A bundle that later gains or loses the
OAuth app makes the saved credential unusable, and the person is asked to connect again. The
broad `doc.schema:write` scope is accepted knowingly, and limited by what the connector does with
it (ADR 0145).

## Verification

- `tests/unit/test_grist_auth.py`: `TestResolveCredential` (a bundle with an app refuses an API-key
  record, one without refuses an OAuth record), the scope constant, `TestGristTokenProvider`.
- `tests/unit/test_systemic_gate_invariants.py`: `TOKEN_WRITE_SITES` lists `grist_auth.save_token_file`
  as the only Grist credential writer.
- `tests/unit/test_settings_controller.py` (`TestAuthenticateGrist`, `TestGristApiKeyConnect`) and
  `tests/unit/web/test_routes_connect.py` (`TestGristOAuth`, `TestGristApiKey`).

## Related

- [`grist_auth.py`](../../src/privacyfence/grist_auth.py)
- ADR [0019](0019-live-connector-credentials-only-on-a-self-hosted-runner.md) (the live check uses an API key)
- ADR [0072](0072-org-mode-persists-only-sealed-refresh-tokens.md) (per-principal third-party credentials)
- ADR [0143](0143-the-bundle-pins-the-grist-server-and-a-credential-stays-with-its-server.md), ADR [0145](0145-the-grist-connector-only-adds.md)
