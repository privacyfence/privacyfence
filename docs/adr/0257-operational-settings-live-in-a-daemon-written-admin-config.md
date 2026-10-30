# ADR 0257: Operational settings live in a daemon-written admin config

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/admin_config.py`, `src/privacyfence/web/routes_admin.py`.
Amends [ADR 0196](0196-every-connector-is-offered-only-when-the-org-config-has-its-section.md): the admin config says which services are offered.
Amends [ADR 0253](0253-ai-token-proposals-are-off-in-organization-mode-until-the-administrator-turns-them-on.md): the AI token proposals switch is `scoped_tokens.allow_ai_proposals` in `org/admin_config.json`, set on the Admin page.
Relates to [ADR 0016](0016-org-config-bundle-hash-log-and-signing.md), [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md), [ADR 0068](0068-step-up-is-turned-on-from-the-ui-and-off-only-by-a-config-edit.md), [ADR 0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md) and [ADR 0200](0200-an-organization-bundle-carries-a-signed-serial-and-an-older-one-is-refused.md).

## Context

Moving operational settings out of the bundle ([ADR 0256](0256-org-config-holds-only-startup-settings-and-app-credentials.md))
needs a home that the daemon can write, an administrator can edit in a browser, and that existing
parsers can still read.

## Decision

- **The file is `org/admin_config.json`** (`admin_config.admin_config_path()`), version 1, written only
  by the daemon through `secure_files.atomic_write_json` (mode 0600). It holds `services`
  (`google`, `slack`, `salesforce`, `atlassian`, `miro`, `telegram`, `grist`, `peek`, each with
  `offered` and its own fields), `rooms`, `unattended_sessions`, and the sections `download_delivery`,
  `web_push`, `authz`, `step_up`, `scoped_tokens` and `audit_forwarding`, each in exactly the shape the
  bundle used, so the existing `from_org_config` parsers read it unchanged. `load()` fills missing
  keys with defaults.
- **Imported once from the bundle.** On the first start the legacy operational keys are copied in, the
  file records the bundle's SHA-256, and an audit entry `admin_config_imported` is written. After
  that they are ignored in the bundle, with a warning. A bundle that listed connectors keeps exactly
  that list offered. A bundle that arrives after a config created with no bundle is merged, never
  overwriting an Admin-page decision: it fills only values still at their default, never lowers
  step-up, never turns an offered service off.
- **First-run defaults.** On a new multi-user organization server every built-in service starts off;
  offering the first one is the administrator's opt-in to the relay
  ([ADR 0259](0259-org-servers-use-built-in-apps-through-a-domain-verified-relay.md)). A single-user
  install (an organization-mode install without an IdP) starts with the built-in services on and
  with step-up on and a passkey required. A desktop install offers only Telegram until the owner
  offers more.
- **Trust.** The file has the trust of `settings.yaml`: whoever can write the install directory can
  change it. Changes go through the Admin page ([ADR 0258](0258-administration-is-a-separate-admin-page.md))
  and need the install owner (desktop) or an IdP administrator (organization).
- **Sensitive actions always need a passkey,** whatever the step-up configuration says. A principal
  with no passkey is told to add one on `/security` first.
- **Step-up can only be made stricter from the page.** Relaxing it is still a file edit followed by a
  restart, so [ADR 0068](0068-step-up-is-turned-on-from-the-ui-and-off-only-by-a-config-edit.md) holds.
  `step_up.rp_id`, `rp_name` and `batch` have no control and are set by editing the file.
- **Audit forwarding.** The bearer-token *variable name* is not editable from the page. Changing the
  forwarding kind, or the scheme, host or port of its URL, clears `http.bearer_token_env`, so a
  changed URL never receives the old token.
- **Server addresses** set on the page must be `https://` on a public host name in organization mode.
- **Room text is untrusted.** The room directory is uploaded as a file; control characters are refused,
  and the text reaches agents as untrusted data.
- Request bodies carry only the action's declared parameters; unknown keys are refused, and server
  state (the bundle, the mode) is never taken from the request.

## Alternatives considered

- **Refuse to start with the legacy keys in the bundle.** Rejected: it would break every upgraded
  server on the first start.
- **Keep them as read-only defaults beside the admin config.** Rejected: two sources for one setting,
  and the page could not say which one is in force.
- **Keep the switches in the bundle for desktop installs.** Rejected: desktop mode is deprecated, and
  two code paths for the same switch would outlive it.

## Consequences

- A desktop install's owner, not an IT-distributed bundle, now decides which services are offered and
  whether unattended sessions are on.
- Most organization settings are read at startup, so a change to them is saved and listed as waiting
  for a restart; only the services list, Jira Assets, the Peek and Grist addresses and the Salesforce
  login URL apply live.
- The admin config must be included in backups.

## Verification

`tests/unit/test_admin_config.py`, `tests/unit/web/test_routes_admin.py`. Code:
`src/privacyfence/admin_config.py` (`ensure`, `_merge_reimport`, `apply_change`),
`src/privacyfence/web/routes_admin.py`, `src/privacyfence/web/routes_settings.py` (`_apply_step_up_gate`).

## Related

ADR 0016, 0034, 0068, 0070, 0196, 0200, 0253, 0256, 0258, 0259.
