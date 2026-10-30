# ADR 0258: Administration is a separate Admin page

## Status

Accepted — 2026-10-30. Implemented: `src/privacyfence/web/routes_admin.py`.
Amends [ADR 0217](0217-the-org-admin-stats-page-shows-counts-only.md): the Stats section is on the Admin page.
Relates to [ADR 0033](0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md).

## Context

An organization administrator's Settings page showed the same sections as everyone's plus two
admin-only ones (Privacy and Stats), and every operational setting was a bundle key. Moving those
settings to the admin config ([ADR 0257](0257-operational-settings-live-in-a-daemon-written-admin-config.md))
needs a place to edit them. The settings action tables are pinned so that every action has a
desktop-mode route, and the admin actions are not like that.

## Decision

- **`/admin` is a top-level page,** with `POST /api/admin/<action>` and `GET /api/admin/state`. Settings
  becomes personal for everyone: an administrator sees the same sections as any person.
- **Who may use it.** In organization mode a signed-in principal with `is_admin` (an IdP administrator);
  anyone else sees "Only administrators can open this page." On a desktop install the install owner,
  through the same owner-only routes as Settings.
- **One route layer, one auth adapter per mode** ([ADR 0033](0033-one-route-layer-per-surface-with-an-auth-adapter-per-mode.md)),
  with its own action table rather than more entries in the settings tables. Each action declares
  whether it is sensitive and how it takes effect (live, restart, or self-applying).
- **Sections.** Services (offer or withdraw, which app is in use, Upload a connector config, the
  Salesforce install link and login URL, the Slack own-app button), Features (Jira Assets, Grist and
  Peek addresses, unattended sessions, web push, rooms), and on organization servers File delivery,
  Sign-in, Step-up, Scoped tokens, Audit forwarding, Privacy Filter with the PII gate, and Stats.
  The page ends with whether the install is ready for PrivacyFence 8
  ([ADR 0262](0262-7x-is-the-migration-checkpoint-for-8x.md)).
- **Offering a service is sensitive; withdrawing is not** ([ADR 0070](0070-enabling-a-connector-is-sensitive-and-disabling-is-not.md)).
  Every change is audited as a settings change with the administrator's id.
- The Privacy and Stats sections move from Settings. The organization Stats page still shows counts
  only ([ADR 0217](0217-the-org-admin-stats-page-shows-counts-only.md)).

## Alternatives considered

- **Keep the admin sections inside Settings.** Rejected: it keeps a page whose content depends on who
  looks at it, and forces the admin-only actions into tables that assume every action also exists in
  desktop mode.

## Consequences

- Navigation gains an "Admin" item for administrators and desktop owners. `/security` keeps its fixed
  navigation.
- `/settings/privacy` forwards to `/admin`; the old `POST /api/settings/org_config/upload` is gone
  in favour of `POST /api/admin/org_config/upload` on desktop.

## Verification

`tests/unit/web/test_routes_admin.py`, `tests/unit/test_admin_config.py`. Code:
`src/privacyfence/web/routes_admin.py`, `src/privacyfence/web_shell.py` (navigation).

## Related

ADR 0033, 0070, 0217, 0257, 0262.
