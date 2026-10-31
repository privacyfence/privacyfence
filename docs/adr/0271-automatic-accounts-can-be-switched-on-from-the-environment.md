# ADR 0271: Automatic accounts can be switched on from the environment

## Status

Accepted — 2026-10-31. Implemented: `src/privacyfence/oauth_apps.py` (`environment_auto_provision`, `check_environment_auto_provision`), `src/privacyfence/admin_config.py`, `src/privacyfence/daemon_main.py`.
Relates to [ADR 0196](0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md), [ADR 0256](0256-org-config-holds-only-startup-settings-and-app-credentials.md), [ADR 0264](0264-an-organization-server-can-start-from-environment-variables.md).

## Context

A Docker stack that starts PrivacyFence, Peek, Grist and the Grist provisioner together generates the
provisioning keys itself. It should leave PrivacyFence with Peek and Grist already connected for every
person, with no clicks on the Admin page. Today **Offer** and **Automatic accounts** live only in
`org/admin_config.json`, written by bundle import and by Admin changes.

## Decision

- **Two variables.** `PRIVACYFENCE_PEEK_AUTO_PROVISION=true` and
  `PRIVACYFENCE_GRIST_AUTO_PROVISION=true` offer the service and turn Automatic accounts on. Unset or
  empty leaves the decision to the Admin page. The value is compared after `strip().lower()`; any
  other value refuses the start. There is no `false`: removing the variable is enough.
- **Live and locked, never stored.** The effective config reads the variable on every call. The
  stored `offered`, `auto_provision.enabled` and `trust_unverified_email` are never written by it, so
  removing the variable restores what the Admin page had. While it is set, the Admin row shows the
  service as offered and on, "set by the server", and refuses `offer_service` and `withdraw_service`.
  The trust-unverified-email checkbox stays editable.
- **Checks at startup only.** `check_environment_auto_provision` runs once, in
  `_start_org_web_server`: the address, the provisioning key and, for Grist, the absence of a Grist
  OAuth app. It refuses the start with a message naming the variable. A Grist connector config
  upload is refused while the Grist variable is set.
- **Web server only.** Like the provisioners, the variables apply only when the web server runs.
  Outside organization mode a start-up warning says they are ignored.

## Alternatives considered

- **Seeding `admin_config.json` once, like `PRIVACYFENCE_ALLOWED_DOMAINS`.** Rejected: a compose file
  is re-run with the same environment. A one-time seed would diverge from it after the first Admin
  click, and a fresh volume would behave differently from an old one. A live, locked value matches
  how `PRIVACYFENCE_PEEK_BASE_URL` already behaves.
- **Checks inside `effective_org_config`.** Rejected: it also runs after runtime changes (uploads,
  live Admin changes), where an error would crash a request and leave a state that refuses the next
  start.
- **A `false` value that forces it off.** Rejected: removing the variable is enough.

## Consequences

- An operator changes the setting by editing the environment and restarting, not on the Admin page.
- A variable left set after the key is removed refuses the start, which is the intent.
- The single-user case is decided in [ADR 0272](0272-a-single-user-install-provisions-its-one-person-as-verified.md).
