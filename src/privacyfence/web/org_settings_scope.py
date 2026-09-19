"""Org-mode ownership split for routes_settings.py's `_ALLOWED_ACTIONS` (#400).

SettingsController is bound at construction to one principal's own
`config/settings.yaml` -- every action mutates or reads that one file. Local
mode gets away with a single instance because there is only ever one
principal. Org mode has many, so reusing routes_settings.py's dispatcher
wholesale is not an option: an action meaningful per-principal there (a rule
row, a grant, a connector toggle -- `ConnectorRegistry` already caches
connector hosts per principal, see security-and-compliance.md) needs a
controller scoped to `current_principal()`, rebuilt per request the way
`daemon_main._load_principal_settings()` already does for auto-accept rules.
An install-wide action (the PII/privacy policy, the log level) instead needs
exactly one admin-gated instance bound to the server's own `settings.yaml`,
never the per-principal one. Mixing the two under one dispatcher, as local
mode does, would let any signed-in principal flip the install-wide PII
policy for the whole org.

A third group is neither: actions this desktop-app-shaped controller exposes
that mean nothing once the process is a headless server -- the update-
checker banner, Telegram's interactive phone/2FA login, and the per-viewer
notification-detail preference. Those never get an org-mode route, under any
of the allowlists below, ever.

These frozensets are that split. `web/routes_org_settings.py` is what
consumes it: a read+add+remove surface for the per-principal half, and,
since #400 C3e, a real editor for the privacy/PII members of the admin-only
half (`web/org_install_policy.py`'s `SUPPORTED_ACTIONS`, a strict subset of
`ADMIN_ONLY_ACTIONS` -- `set_log_level` and `toggle_calendar_free_busy` are
install-wide too but aren't privacy policy, and each needs a reload path of
its own). This module's own test checks the split against
`routes_settings._ALLOWED_ACTIONS` so a newly added local-mode action can't
silently go unclassified.

`PER_PRINCIPAL_ACTIONS` is deliberately narrower than "every action this
desktop-app-shaped controller exposes that is meaningful per-principal" --
it's exactly the subset `routes_org_settings.py` has an actual route for
(today: adding a rule row, removing a rule row, removing a grant row). A
rule/grant *update*, a grant *add*, a connector toggle, a connector refresh,
or a connector authentication flow is just as meaningful per-principal in
org mode in principle, but no route wires any of them yet, so they live in
`PER_PRINCIPAL_ACTIONS_UNROUTED` instead: still not `NOT_APPLICABLE_ACTIONS`
(they're not meaningless or wrong the way `enable_step_up` is -- an org-mode
route for them is exactly the kind of thing a later PR adds), but
`is_action_permitted` denies them until that route exists and moves them
into `PER_PRINCIPAL_ACTIONS` alongside it. Letting this allow-list claim an
action no route consumes was the bug (#B20 in the 4.1 security review): the
allow-list had run ahead of the routes, so `is_action_permitted` would
happily say yes to an action for a signed-in principal with nothing on the
other end to say no -- exactly the kind of gap a route added later, in good
faith, could have trusted without noticing it was never actually wired for.
`add_rule_row` itself lived in `PER_PRINCIPAL_ACTIONS_UNROUTED` for exactly
that reason until the org-settings page grew its own "Add a rule" form and
`/api/settings/rules/add` route -- see that route's own docstring.

`is_action_permitted` is the other half: `Principal.is_admin` is already
resolved from the IdP and carried end to end (`org_identity.
principal_from_claims` -> `OrgSessionStore` / `OrgOAuthProvider.
_mint_tokens` -> `mcp_auth.principal_from_access_token`), but until now
nothing consumed it for an authorization decision anywhere in this
codebase. This is that decision, in one place, so the eventual org-mode
settings route calls it instead of re-deriving "is this action gated"
from the three sets itself.
"""
from __future__ import annotations

from ..principal import Principal

PER_PRINCIPAL_ACTIONS: frozenset[str] = frozenset({
    "add_rule_row", "remove_rule_row", "remove_grant_row",
})

# Per-principal in concept (see module docstring), but routes_org_settings.py
# doesn't wire a route for any of these yet -- `is_action_permitted` denies
# them, not permits them, until one does. Move an action to
# `PER_PRINCIPAL_ACTIONS` in the same PR that adds its route, never ahead of
# it.
PER_PRINCIPAL_ACTIONS_UNROUTED: frozenset[str] = frozenset({
    "enable_connector", "disable_connector", "refresh_connectors", "authenticate_connector",
    "update_rule_row",
    "toggle_grant_capability", "add_grant_row", "update_grant_row",
})

ADMIN_ONLY_ACTIONS: frozenset[str] = frozenset({
    "toggle_pii_detection", "toggle_pii_category",
    "set_default_policy", "set_category_policy",
    "toggle_calendar_free_busy", "set_log_level",
})

# Meaningless, or actively wrong, on a headless server -- unlike
# PER_PRINCIPAL_ACTIONS_UNROUTED above, never wired into an org-mode route
# under any allowlist here, ever.
NOT_APPLICABLE_ACTIONS: frozenset[str] = frozenset({
    "toggle_update_check", "toggle_update_check_beta", "check_for_updates_now",
    "skip_update", "remind_later_update",
    "telegram_start_auth", "telegram_submit_code", "telegram_submit_2fa", "telegram_cancel_auth",
    "set_notifications_detail",
    # B9: hardcodes LOCAL_PRINCIPAL throughout (the credential check, the
    # config/settings.yaml section it writes, the LiveStepUpConfig it
    # updates) -- local mode's own single-principal, file-based step_up
    # model, not org mode's per-principal-credential, org_config.json-
    # bundle-driven one. Wiring this into an org-mode route would be
    # actively wrong (it would gate on, and mutate state for, the wrong
    # principal), not merely unavailable.
    "enable_step_up",
})


def is_action_permitted(action: str, principal: Principal) -> bool:
    """Whether `principal` may invoke `action` on an org-mode settings
    surface. A `PER_PRINCIPAL_ACTIONS` member is permitted for any signed-in
    principal -- every one of them already resolves against
    `current_principal()` inside the controller/registry it touches, so
    there is nothing further to check here. An `ADMIN_ONLY_ACTIONS` member
    needs `principal.is_admin`. Anything else -- a `PER_PRINCIPAL_ACTIONS_
    UNROUTED` or `NOT_APPLICABLE_ACTIONS` member, or a name in none of the
    four sets at all -- is never permitted; the caller is expected to 404
    those the same way `routes_settings.py`'s own allowlist check does, not
    reach this function with them."""
    if action in ADMIN_ONLY_ACTIONS:
        return principal.is_admin
    return action in PER_PRINCIPAL_ACTIONS
