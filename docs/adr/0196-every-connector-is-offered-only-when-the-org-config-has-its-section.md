# ADR 0196: Every connector is offered only when the organization config has its section

## Status

Accepted — 2026-10-17. Implemented: `org_mode.CONNECTOR_ORG_SECTION` and
`org_mode.connector_enabled_by_org`, used by `daemon_main.build_connectors`,
`settings_controller.SettingsController` and `web/routes_connect.py`.

## Context

Connectors reached for the organization config unevenly. Google, Slack, Salesforce and Atlassian
need an app registration from it. Telegram (its app credentials ship in the build, [ADR
0040](0040-telegram-app-credentials-ship-in-every-distribution.md)), Peek and Grist with an API key
([ADR 0152](0152-which-peek-server-is-used-and-a-token-stays-with-it.md)) needed nothing in local
mode, so a person with no bundle, or a bundle written for other services, could still connect them.
An administrator therefore could not say which services their people may connect to AI clients.
The Settings page also listed every connector, with "Organization config missing" on those that
could not be used.

## Decision

- **The organization config is the single list of enabled connectors**, in both modes. A connector
  is enabled when `org_config.json` has a non-empty section for it: `google` (all six Google
  connectors), `slack`, `salesforce`, `atlassian` (Jira, Confluence, Jira Assets), `telegram`,
  `grist`, `peek`.
- **Telegram, Peek and Grist take a bare switch**, `{"enabled": true}`, because they have no secret
  for an administrator to supply. `build_org_bundle.py` writes it with `--enable-telegram`,
  `--enable-peek` and `--enable-grist`; `--peek-base-url` and `--grist-server-url` write a section
  of their own, so they enable it too. Organization mode still needs the server address for Peek
  and Grist, as before.
- **A connector without its section is not built**, so it reports `no_org_config` to
  `privacyfence_status`, and its sign-in actions in Settings and on `/connect` are refused.
- **A connector without its section is not shown.** The Connectors page in Settings and the
  organization-mode `/connect` page list only enabled connectors; the Settings page says so when
  there are none. `privacyfence_status` still reports every connector, so an AI client and an
  administrator can see why one is missing.
- **Removing a section is how a connector is switched off for everyone.** A person's own
  `connectors.<name>.enabled` setting stays a per-person switch on top of it and cannot enable what
  the organization config does not.

## Alternatives considered

- **Keep the three exceptions and only hide rows.** The page would look uniform while a person could
  still connect a service the administrator never offered through any other surface.
- **An allow-list key (`connectors: [...]`) separate from the sections.** Two places that must
  agree, one of which could name a connector with no app registration behind it.
- **Show disabled connectors greyed out.** It tells people about services they cannot use and asks
  them to chase an administrator for it.

## Consequences

- An install with no bundle, or a bundle without the new sections, lists no Telegram, Peek or Grist
  until the administrator rebuilds it with `--merge --enable-telegram` (and so on). An install with
  no bundle at all lists no connectors.
- A bundle that already had a `grist` or `peek` section is unaffected.
