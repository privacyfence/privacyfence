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
notification-detail preference. Those never get an org-mode route, under
either allowlist, ever.

These three frozensets are that split -- the design decision #400's own
follow-on work (admin-gating `ADMIN_ONLY_ACTIONS` behind `Principal.
is_admin`, and a read+remove settings surface for `PER_PRINCIPAL_ACTIONS`)
builds on, not the routes themselves; neither org-mode route module exists
yet. This module's own test checks the split against
`routes_settings._ALLOWED_ACTIONS` so a newly added local-mode action can't
silently go unclassified.
"""
from __future__ import annotations

PER_PRINCIPAL_ACTIONS: frozenset[str] = frozenset({
    "toggle_connector", "refresh_connectors", "authenticate_connector",
    "update_rule_row", "add_rule_row", "remove_rule_row",
    "toggle_grant_capability", "add_grant_row", "update_grant_row", "remove_grant_row",
})

ADMIN_ONLY_ACTIONS: frozenset[str] = frozenset({
    "toggle_pii_detection", "toggle_pii_category",
    "set_default_policy", "set_category_policy",
    "toggle_calendar_free_busy", "set_log_level",
})

# Meaningless, or actively wrong, on a headless server -- never wired into an
# org-mode route under either allowlist above.
NOT_APPLICABLE_ACTIONS: frozenset[str] = frozenset({
    "toggle_update_check", "toggle_update_check_beta", "check_for_updates_now",
    "skip_update", "remind_later_update",
    "telegram_start_auth", "telegram_submit_code", "telegram_submit_2fa", "telegram_cancel_auth",
    "set_notifications_detail",
})
