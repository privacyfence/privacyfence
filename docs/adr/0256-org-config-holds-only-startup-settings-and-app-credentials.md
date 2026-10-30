# ADR 0256: The org config holds only startup settings and app credentials

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/oauth_apps.py`, `src/privacyfence/admin_config.py`.
Amends [ADR 0196](0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md): which connectors are offered is no longer read from the bundle.
Amends [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md): the signature no longer covers operational settings.
Relates to [ADR 0241](0241-privacyfence-provisions-peek-accounts-with-a-scoped-key-and-only-calls-peek.md) and [ADR 0244](0244-grist-api-keys-come-from-a-grist-side-provisioner.md).

## Context

The signed bundle carried everything an administrator controlled: the startup settings (`mode`,
`server`, `idp`), the connector app credentials, which connectors are offered
([ADR 0196](0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md)) and every
operational policy (file delivery, web push, sign-in restrictions, step-up, scoped tokens, audit
forwarding, unattended sessions, rooms). Changing any of it meant rebuilding, re-signing and
reinstalling the bundle. With built-in apps ([ADR 0255](0255-built-in-oauth-apps-ship-in-every-distribution.md))
a fresh install no longer needs a bundle to offer anything, so the bundle's remaining job is small.

## Decision

- **The bundle carries app credentials and bookkeeping only** (plus, for a server upgraded from 6.x,
  the startup settings it already holds; [ADR 0269](0269-organization-servers-use-signed-connector-configs-instead-of-a-bundle.md)
  ends that). It stays signed ([ADR 0016](0016-org-config-bundle-hash-log-and-signing.md),
  [ADR 0200](0200-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md)).
- **Everything operational moves to the admin config** ([ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md)).
  `admin_config.LEGACY_BUNDLE_KEYS` names the bundle keys that are imported once and afterwards
  ignored, with a startup warning that names them.
- **One effective config.** `oauth_apps.effective_org_config(bundle, admin, mode=...)` composes the
  bundle, the admin config and the built-in apps into the single dict every reader already used, so
  no connector or parser changed its input. A service the admin config does not offer has no section.
  An offered service takes its section from the bundle, else the built-in app (organization mode
  only), else none. The shared dict is refreshed in place under a lock and read through
  `oauth_apps.snapshot`.
- **Peek and Grist automatic accounts** ([ADR 0241](0241-privacyfence-provisions-peek-accounts-with-a-scoped-key-and-only-calls-peek.md),
  [ADR 0244](0244-grist-api-keys-come-from-a-grist-side-provisioner.md)): the on/off switch is an
  admin-config setting, but the addresses the provisioning key is sent to come only from the
  environment (`PRIVACYFENCE_PEEK_BASE_URL`, `PRIVACYFENCE_GRIST_SERVER_URL`,
  `PRIVACYFENCE_GRIST_PROVISIONER_URL`). An address from the environment wins over an imported
  locked address, which wins over the Admin page's, and the first two are shown locked. So a hijacked
  Admin session cannot point the key at another host.

## Alternatives considered

- **Keep everything in the signed bundle.** Rejected: a change to one switch needs an offline rebuild,
  and the bundle can only be installed by someone with the signing key.
- **A third startup-only file for mode, server and IdP.** Rejected: the environment already carries
  those in a container, and a file is one more thing to ship and keep in step.
- **Let the daemon write the organization's own credentials into an unsigned `org_config.json`.**
  Rejected: a credential an attacker can replace is the one thing that must stay signed.

## Consequences

- **Signing no longer covers operational settings.** `authz`, `step_up`, `audit_forwarding` and
  `scoped_tokens` now have settings.yaml-level protection (the install directory), not a signature.
  Step-up can only be tightened from the Admin page.
- A bundle that adds a provider later does not offer it by itself after the import; an administrator
  switches it on.
- Changing a service's app makes people reconnect where the provider's refresh uses the current app.
- On a desktop install the owner, not the bundle's author, now decides which services are offered.

## Verification

`tests/unit/test_oauth_apps.py`, `tests/unit/test_admin_config.py`. Code:
`src/privacyfence/oauth_apps.py` (`effective_org_config`, `refresh_in_place`, `snapshot`,
`environment_addresses`), `src/privacyfence/admin_config.py` (`LEGACY_BUNDLE_KEYS`, `legacy_keys_in`).

## Related

ADR 0016, 0196, 0200, 0241, 0244, 0255, 0257, 0269.
