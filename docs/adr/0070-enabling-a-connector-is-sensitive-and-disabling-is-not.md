# ADR 0070: Enabling a connector is a sensitive action, and disabling one is not

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-09-19 in `151d6994`, merged as
[#557](https://github.com/privacyfence/privacyfence/pull/557)).

## Context

Local mode's settings dispatcher (`web/routes_settings.py`) splits its actions into
`_SENSITIVE_ACTIONS`, which need a fresh passkey assertion under `step_up.require_passkey`
(ADR 0034), and `_NON_SENSITIVE_ACTIONS`, which do not. Connector management was classified as
non-sensitive because an agent that already has access to a connector gains nothing by
managing it.

Connectors were switched with a single `toggle_connector` action, classified non-sensitive in both
directions. That reasoning holds for switching a connector off. It does not hold for switching one
back on. A connector a human deliberately disabled is one the agent currently cannot reach, and
re-enabling it gives the agent access it did not have. An agent with a session could do that with
one ungated POST.

## Decision

Connector switching is two actions with different classifications:

- `enable_connector` is in `_SENSITIVE_ACTIONS`. With `step_up.enabled` and `require_passkey` on,
  it needs a fresh assertion (a `428` challenge, or a `403` naming `/security` when nothing is
  enrolled) and, on a local install that requires it, a human-attributed session.
- `disable_connector` is in `_NON_SENSITIVE_ACTIONS` and is never step-up gated.
- `toggle_connector` no longer exists as an action.

The Settings page's connector toggle (`settings_window_html.py`) posts `disable_connector` when the
connector is on and `enable_connector` when it is off, so each click sends the action for the
direction it actually takes. Both actions are local-mode only in `org_settings_scope.ACTION_SCOPES`.

## Alternatives considered

- **Keep one toggle, non-sensitive.** Rejected: it leaves the re-enable path open to any session,
  which undoes a human's decision to cut an agent off.
- **Keep one toggle, sensitive in both directions.** Rejected: switching a connector off only
  removes access, and a passkey prompt for that adds friction to the safe direction. A human
  cutting an agent off in a hurry should not have to complete a ceremony first.

## Consequences

- Disabling a connector stays one click, with no prompt.
- Re-enabling one costs the same ceremony as adding an always-allow rule.
- The same test applies to future settings actions: an action that can only remove access may be
  non-sensitive, and one that can grant or restore it is sensitive.

## Verification

- `web/routes_settings.py`: `_SENSITIVE_ACTIONS` and `_NON_SENSITIVE_ACTIONS`.
- `tests/unit/web/test_routes_settings.py`: `TestConnectorToggleDirectional`
  (`test_toggle_connector_no_longer_exists_as_a_dispatchable_action`,
  `test_enable_connector_is_sensitive_disable_is_not`, `test_disabling_is_never_step_up_gated`,
  `test_enabling_is_step_up_gated`).
- `tests/unit/test_settings_controller.py`: `TestConnectorEnableDisable`.

## Related

- [ADR 0034](0034-sensitive-settings-writes-require-step-up-in-both-modes.md): the sensitive-action
  gate. It lists `enable_connector` among the sensitive actions; this ADR records why the reverse
  direction is not.
