"""Per-action declaration for the settings surface.

`ACTION_SCOPES` below is the one declaration of which settings actions have a route on an
organization server and which of those need `principal.is_admin`. `routes_settings.py`'s
`_ORG_ALLOWED_ACTIONS` is checked against it, and `NOT_APPLICABLE_ACTIONS` is its read for the
shared renderer.

Each action declares, in one place:

- which modes (`ORG_MODE`) it has an actual route in -- not merely "meaningful in" (an allow-list
  claiming an action no route consumes is the bug; an action with an empty `modes` has no route
  at all, and the renderer never draws a control for it);
- whether it's `admin_only` -- gated on `Principal.is_admin`.

`is_action_permitted` is the authorization decision built on top of this table: `mode not in
scope.modes` covers both "not a real action" and "no route for this action" with one check (the
caller is expected to 404 either the same way, not distinguish them).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..principal import Principal

# Read by settings_window_html.py for the capabilities it renders, until that module drops its
# own mode parameter.
LOCAL_MODE = "local"
ORG_MODE = "org"


@dataclass(frozen=True)
class ActionScope:
    """`modes` -- `{ORG_MODE}` where a real route exists, empty where none does.
    `admin_only` -- whether `ORG_MODE` additionally requires `principal.is_admin`."""

    modes: frozenset[str]
    admin_only: bool = False


ACTION_SCOPES: dict[str, ActionScope] = {
    # ---------------------------------------------------------------- #
    # Per-principal: a rule row, scoped to
    # current_principal() everywhere it's evaluated -- an admin has no
    # more mutation power here over another principal's rules than that
    # principal does over their own.
    # ---------------------------------------------------------------- #
    "add_policy_rule": ActionScope(modes=frozenset({ORG_MODE})),
    "remove_policy_rule": ActionScope(modes=frozenset({ORG_MODE})),
    "update_policy_rule": ActionScope(modes=frozenset({ORG_MODE})),
    "set_policy_rule_verbs": ActionScope(modes=frozenset({ORG_MODE})),
    # A principal's own step-up scope, widened past the configured minimum
    # (step_up_config.effective_scope, ADR 0091) -- stored per principal and
    # applied only to that principal's own approvals, so any principal may
    # set their own, admin or not.
    "set_step_up_scope": ActionScope(modes=frozenset({ORG_MODE})),

    # ---------------------------------------------------------------- #
    # The install-wide PII/privacy policy (web/org_install_policy.py's own
    # SUPPORTED_ACTIONS is exactly these four). An organization server's administrator changes
    # them through the Admin dispatcher (web/routes_admin.py), not through
    # Settings, which is the same page for everyone -- so there is no
    # ORG_MODE route here.
    # ---------------------------------------------------------------- #
    "toggle_pii_detection": ActionScope(modes=frozenset(), admin_only=True),
    "toggle_pii_category": ActionScope(modes=frozenset(), admin_only=True),
    "set_default_policy": ActionScope(modes=frozenset(), admin_only=True),
    "set_category_policy": ActionScope(modes=frozenset(), admin_only=True),

    # Admin-only in concept -- install-wide, not per-principal -- but
    # neither is privacy policy, and each needs a reload path of its own
    # that web/org_install_policy.py doesn't have yet (that module's own
    # docstring). No route until one of those lands, the same way the
    # per-principal actions below have none.
    "toggle_calendar_free_busy": ActionScope(modes=frozenset(), admin_only=True),
    "toggle_gmail_signature": ActionScope(modes=frozenset(), admin_only=True),
    "set_log_level": ActionScope(modes=frozenset(), admin_only=True),

    # ---------------------------------------------------------------- #
    # Per-principal in concept -- a connector toggle, refresh, or auth
    # flow is just as meaningful per-principal in org mode in principle as
    # add_policy_rule/remove_policy_rule above -- but no org-mode route
    # wires any of them yet, so is_action_permitted denies them until one
    # does (an allow-list entry with no route behind it is the bug
    # this table exists to make impossible to reintroduce by construction
    # -- move an action's ORG_MODE membership in the same PR that adds its
    # route, never ahead of it).
    # ---------------------------------------------------------------- #
    "enable_connector": ActionScope(modes=frozenset()),
    "disable_connector": ActionScope(modes=frozenset()),
    "refresh_connectors": ActionScope(modes=frozenset()),
    "authenticate_connector": ActionScope(modes=frozenset()),

    # ---------------------------------------------------------------- #
    # Meaningless, or actively wrong, on a headless server -- the
    # update-checker banner, Telegram's interactive phone/2FA login, and
    # the per-viewer notification-detail preference. Unlike the two groups
    # above, these never get a route no matter what else lands.
    # ---------------------------------------------------------------- #
    "toggle_update_check": ActionScope(modes=frozenset()),
    "toggle_update_check_beta": ActionScope(modes=frozenset()),
    "check_for_updates_now": ActionScope(modes=frozenset()),
    "skip_update": ActionScope(modes=frozenset()),
    "remind_later_update": ActionScope(modes=frozenset()),
    "telegram_start_auth": ActionScope(modes=frozenset()),
    "telegram_submit_code": ActionScope(modes=frozenset()),
    "telegram_submit_2fa": ActionScope(modes=frozenset()),
    "telegram_cancel_auth": ActionScope(modes=frozenset()),
    "peek_start_login": ActionScope(modes=frozenset()),
    "peek_finish_login": ActionScope(modes=frozenset()),
    "peek_cancel_login": ActionScope(modes=frozenset()),
    "grist_connect": ActionScope(modes=frozenset()),
    "grist_cancel_auth": ActionScope(modes=frozenset()),
    "set_notifications_detail": ActionScope(modes=frozenset()),

    # enable_step_up hardcodes one principal's file-based step-up model, not the per-principal-credential,
    # admin-config-driven one an organization uses. Wiring it into a route would be actively wrong
    # (it would gate on, and mutate state for, the wrong principal), not merely unavailable.
    "enable_step_up": ActionScope(modes=frozenset()),
}


# The shared settings renderer's own read of the table above (ADR 0032) -- every
# action with no real ORG_MODE route at all, i.e. exactly the controls
# settings_window_html.build_html() must never draw when rendering for a
# principal in org mode (there is no dispatcher on the other end for a click
# on one). Computed, not hand-maintained, so a new action without a route
# added to ACTION_SCOPES above is automatically excluded from org's rendered
# page the same PR that adds it -- the renderer has no classification of its
# own to fall out of sync with this one.
NOT_APPLICABLE_ACTIONS: frozenset[str] = frozenset(
    action for action, scope in ACTION_SCOPES.items() if ORG_MODE not in scope.modes
)


def is_action_permitted(action: str, principal: Principal, *, mode: str) -> bool:
    """Whether `principal` may invoke `action` on a settings surface running
    in `mode` (`ORG_MODE`). `mode` is required, not defaulted.

    An action with no entry in `ACTION_SCOPES`, or one whose `modes` doesn't
    include `mode` at all, is never permitted -- the caller is expected to
    404 that the same way `routes_settings.py`'s own allowlist check always
    has, not reach this function expecting a reason."""
    scope = ACTION_SCOPES.get(action)
    if scope is None or mode not in scope.modes:
        return False
    if scope.admin_only:
        return principal.is_admin
    return True
