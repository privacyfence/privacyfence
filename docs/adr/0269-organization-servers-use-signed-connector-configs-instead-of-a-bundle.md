# ADR 0269: Organization servers use signed connector configs instead of a bundle

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/connector_configs.py`, `scripts/sign_connector_config.py`, `website/tools/connector-config/index.html`.
Amends [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md): organization servers read no bundle of their own; an upgraded server's legacy bundle is read-only.
Amends [ADR 0200](0200-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md): connector configs carry per-provider serials.
Relates to [ADR 0256](0256-org-config-holds-only-startup-settings-and-app-credentials.md), [ADR 0263](0263-the-6-to-7-upgrade-is-reversible.md) and [ADR 0264](0264-an-organization-server-can-start-from-environment-variables.md).

## Context

Once startup settings come from the environment
([ADR 0264](0264-an-organization-server-can-start-from-environment-variables.md)) and everything
operational is the admin config ([ADR 0256](0256-org-config-holds-only-startup-settings-and-app-credentials.md)),
a signed bundle on an organization server would hold only an organization's own app credentials, and
would be rebuilt and reinstalled to change one of them. An organization that wants its own Google,
Slack, Salesforce, Atlassian, Miro or Grist app needs a way to supply one that an attacker with admin
access to the web page cannot swap for their own.

## Decision

- **Organization servers read no bundle.** Startup settings come from the environment, an
  organization's own app for a provider is a **signed connector config** uploaded on Admin → Services,
  a provider with none uses the built-in app ([ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md)),
  and everything operational is the admin config. Desktop installs keep the bundle until desktop mode is
  removed; `build_org_bundle.py` builds desktop bundles only and refuses every organization-mode option
  with a message that names `privacyfence.env` and connector configs.
- **The format is signed text,** one file per provider (`google`, `slack`, `salesforce`, `atlassian`,
  `miro`, `grist`): a header `privacyfence-connector-config v1`, then `provider`, `serial`, the
  provider's fields in a fixed order (`client_id` and `client_secret`, or `consumer_key` and
  `consumer_secret` for Salesforce, with optional `login_url`; Grist adds `auth_server_url` and
  `server_url`), `signed_at`, `key` and `signature`. The signed message is every line before
  `signature:`, as UTF-8. Values are printable ASCII, at most 512 characters; a file is at most 8 KB.
  Plain text, not JSON, so Python and a browser agree on every byte without canonicalization.
- **Ed25519, pinned on first use.** The first upload pins its key in
  `org/connector_config_signing_pubkey.txt`, and only after the administrator confirms the key's
  fingerprint; an upgraded server starts with its legacy bundle's pinned key. A different key is
  refused; rotation means deleting the pin file on the server.
- **Serials only increase, per provider.** An upload's serial must exceed the highest ever accepted for
  that provider, recorded in `org/connector_config_serials.json`, which survives removal. The loader
  also refuses, at start, a stored file whose serial is below that record, so a replaced older file
  cannot come back. An accepted file is stored unchanged at `org/connector_configs/<provider>.txt`
  (mode 0600) and verified against the pin at every start; one that no longer verifies refuses the
  start and names the provider.
- **Admin page.** The row shows the app in use, its client ID, serial, signing date and key
  fingerprint, never the secret. **Upload a connector config** and **Remove our own app** are sensitive
  (a passkey always), CSRF-checked, audited and apply live. Removal falls back to the built-in app.
- **Upgraded servers.** The first start imports each credential section of an adopted 6.x bundle once,
  as an unsigned `org/connector_configs/<provider>.json` marked `legacy-bundle` (the bundle's verified
  signature covered it); a later upload replaces it. The bundle is read only for `mode`, `server` and
  `idp`, with a warning at every start. `privacyfence-app --print-env-from-bundle [--idp-secret-file
  PATH]` prints the equivalent `PRIVACYFENCE_*` lines without the secret, and `privacyfence-app
  --retire-bundle` ends the bundle once the environment matches it. A different `org_bundle.json` than
  the adopted one is refused at start.
- **Tooling.** `scripts/sign_connector_config.py` (`keygen`, `sign`; the secret from a file or a hidden
  prompt, never the command line) and a self-contained browser page, `website/tools/connector-config/`,
  which signs with WebCrypto and sends nothing anywhere. The page is served as an unlisted site page
  (no navigation, sitemap or `llms.txt` entry) with its own hash-based Content-Security-Policy
  exception for `/tools/connector-config/`, and the same file is attached to each stable release for
  offline use. A cross-language fixture proves that the Python tool and the page produce the same
  bytes.

## Alternatives considered

- **Keeping the signed org bundle for servers.** Rejected: a rebuild and reinstall per credential
  change, and the bundle's other contents are now elsewhere.
- **Client IDs and secrets typed on the Admin page, unsigned.** Rejected: a hijacked admin session could
  then swap in an attacker's app. With a signed file it can only remove an own app, which falls back
  to the built-in one and is audited.
- **Signed JSON with canonicalization.** Rejected: Python and JavaScript would have to agree on every
  byte of a canonical form.
- **A signing page served by the PrivacyFence server itself.** Rejected: a compromised server could
  steal the key it is supposed to be checked against.
- **Refusing upgraded 6.x servers until their IdP is in the environment.** Rejected: it would break
  them on upgrade; the legacy bundle stays readable for its server and IdP settings until 8.x.

## Consequences

- **The first upload decides the key.** Whoever uploads first on a new server pins it, so the docs tell
  administrators to check the fingerprint.
- **The browser tool is as trustworthy as privacyfence.eu.** Whoever controls the site could change the
  page to steal keys. The CSP (no external script, style or network), the single self-contained file
  and the offline copy in the repository and the release are the mitigations; the Python tool is the
  alternative.
- An operator's script that builds or installs an organization bundle stops working from 7.0; the
  refusal says where each setting went.
- Switching between an own app and the built-in one makes people reconnect where the provider's refresh
  uses the current app.

## Verification

`tests/unit/test_connector_configs.py`, `tests/unit/test_sign_connector_config.py`,
`tests/unit/test_connector_config_web_tool.py`, `tests/unit/test_build_org_bundle.py`,
`tests/unit/web/test_routes_admin.py`, `tests/unit/test_website_security_headers.py`. Code:
`src/privacyfence/connector_configs.py`, `scripts/sign_connector_config.py`,
`website/tools/connector-config/index.html`, `scripts/site_security_headers.py`.

## Related

ADR 0016, 0200, 0255, 0256, 0263, 0264.
