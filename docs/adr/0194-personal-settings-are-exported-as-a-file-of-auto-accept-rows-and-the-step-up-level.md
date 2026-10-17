# ADR 0194: Personal settings are exported as a file of auto-accept rows and the step-up level

## Status

Accepted — 2026-10-16. Implemented: `src/privacyfence/personal_settings.py`,
`src/privacyfence/web/personal_settings_routes.py`.

## Context

A person who moves from a desktop install to the organization server, or back, rebuilds their
always-allow rules by hand: nothing carries over, and the rules are the part of a setup that took
weeks to accumulate. A local `settings.yaml` mixes three kinds of keys: the person's own rules and
step-up level, install-wide policy (privacy filter, PII detection, which in organization mode are
the administrator's), and desktop-only settings. Each person's step-up level can only widen the
configured minimum ([ADR 0091](0091-the-configured-step-up-scope-is-a-minimum-each-person-can-widen.md)),
and sensitive settings writes need a passkey in both modes
([ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md)).

## Decision

- **The export is one file, `privacyfence-personal-settings.json`, holding the person's
  always-allow rows and their own step-up scope.** Settings > Auto-accept offers **Export…** and
  **Import…** in both modes, with one format for every direction.
- **Nothing else is written.** No privacy filter, PII detection, credentials, passkeys, scoped
  tokens, audit log or desktop-only setting; the file lists these as informational `not_portable`
  entries that import ignores.
- **Rules without a catalogue scope (hand-written custom rules) are not exported.**
- **The file is delivered by the person**: a download from one install and an upload to the other.
  There is no MCP tool and no network path between them.
- **The file is unsigned.** A person has no key to sign with.

## Alternatives considered

- **Copy `settings.yaml` / export everything** — most keys are install-wide or desktop-only and
  meaningless or dangerous in organization mode, so import would have to filter, and the filter is
  the feature.
- **A signed export, like the organization bundle** — there is no key for a person to sign with, and
  a signature would not tell the person whether the contents are what they want.
- **An MCP tool or a server-to-desktop sync** — an agent submitting a rule set is a different trust
  question; a file the person moves themselves keeps the human in the loop and adds no network path.

## Consequences

- A rule names resources (folders, channels, projects) by id, which belong to the account they were
  made with. The rule matches on the new install only if the same accounts are connected there; the
  preview says so.
- Moving an install to another machine does not move connector sign-ins, passkeys, scoped tokens or
  the audit log; the person sets those up again.
- The export contains rule values as stored (a sender domain, a folder id), so the file is as
  sensitive as the rules page.

## Verification

`tests/unit/test_personal_settings.py`, `tests/unit/web/test_personal_settings_routes.py`,
`tests/unit/web/test_routes_org_settings.py::TestPersonalSettingsRoutes`,
`tests/integration/test_settings_personal_transfer_browser.py`.

## Related

[ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md),
[ADR 0091](0091-the-configured-step-up-scope-is-a-minimum-each-person-can-widen.md),
[ADR 0195](0195-importing-personal-settings-is-merge-only-previewed-and-step-up-gated-on-the-file-hash.md).
