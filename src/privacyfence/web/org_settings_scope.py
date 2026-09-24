"""Per-action mode/authorization declaration for the settings surface (#400,
PSC-4b).

Through PSC-4a, `web/routes_settings.py`'s `_ALLOWED_ACTIONS` was the
primary list (every action the local desktop-app-shaped `SettingsController`
exposes) and this module held four frozensets that filtered it for org
mode -- a filter bolted onto local's own list, checked against it by this
module's own test so a newly added local-mode action couldn't silently go
unclassified. PSC-4b merges `web/routes_settings.py` and the former
`web/routes_org_settings.py` into one dispatcher (`routes_settings.py`'s
`build_routes`/`build_org_routes`), and that inverts the relationship:
`ACTION_SCOPES` below is now the primary declaration, and `_ALLOWED_ACTIONS`
is *projected* from it (every action whose scope names `LOCAL_MODE` -- every
action there is except AGT-5's two org-only AI-system pin actions, which
have no local-mode meaning at all; local mode's own dispatcher predates this
split and was never itself gated by it).

Each action declares, in one place:

- which modes (`LOCAL_MODE`/`ORG_MODE`) it has an actual route in -- not
  merely "meaningful in", the same distinction the old
  `PER_PRINCIPAL_ACTIONS`/`PER_PRINCIPAL_ACTIONS_UNROUTED` split drew (#B20
  in the 4.1 security review: an allow-list claiming an action no route
  consumes is the bug -- see the per-action comments below for exactly
  which actions are "meaningful per-principal/admin-wide in concept" but
  still `LOCAL_MODE`-only because no org route exists for them yet);
- whether it's `admin_only` -- gated on `Principal.is_admin` in org mode.
  `admin_only` is meaningless for local mode: `SettingsController` is bound
  at construction to one principal's own `config/settings.yaml`, so "admin"
  has never been a local-mode concept (`LOCAL_PRINCIPAL.is_admin` is always
  `False`, see `principal.py`) -- `is_action_permitted` below only ever
  consults it when `mode=ORG_MODE`.

`is_action_permitted` is the authorization decision built on top of this
table: `mode not in scope.modes` covers both "not a real action" and "no
route for this action in this mode" with one check (the caller is expected
to 404 either the same way `routes_settings.py`'s own allowlist check
always has, not distinguish them); `admin_only` is checked only for
`ORG_MODE`, since `Principal.is_admin` is already resolved from the IdP and
carried end to end (`org_identity.principal_from_claims` ->
`OrgSessionStore`/`OrgOAuthProvider._mint_tokens` ->
`mcp_auth.principal_from_access_token`) but local mode has no admin
concept to gate on at all -- every action valid for `LOCAL_MODE` is
permitted for whichever principal is asking, once mode membership itself
has passed.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..principal import Principal

LOCAL_MODE = "local"
ORG_MODE = "org"


@dataclass(frozen=True)
class ActionScope:
    """`modes` -- the subset of `{LOCAL_MODE, ORG_MODE}` a real route exists
    for. `admin_only` -- whether `ORG_MODE` additionally requires
    `principal.is_admin`; ignored for `LOCAL_MODE` (see module docstring)."""

    modes: frozenset[str]
    admin_only: bool = False


ACTION_SCOPES: dict[str, ActionScope] = {
    # ---------------------------------------------------------------- #
    # Per-principal, routed in both modes: a rule row, scoped to
    # current_principal() everywhere it's evaluated -- an admin has no
    # more mutation power here over another principal's rules than that
    # principal does over their own.
    # ---------------------------------------------------------------- #
    "add_policy_rule": ActionScope(modes=frozenset({LOCAL_MODE, ORG_MODE})),
    "remove_policy_rule": ActionScope(modes=frozenset({LOCAL_MODE, ORG_MODE})),

    # ---------------------------------------------------------------- #
    # Admin-only, routed in both modes: the install-wide PII/privacy
    # policy (web/org_install_policy.py's own SUPPORTED_ACTIONS, a strict
    # subset of the admin-only actions below, is exactly these four).
    # ---------------------------------------------------------------- #
    "toggle_pii_detection": ActionScope(modes=frozenset({LOCAL_MODE, ORG_MODE}), admin_only=True),
    "toggle_pii_category": ActionScope(modes=frozenset({LOCAL_MODE, ORG_MODE}), admin_only=True),
    "set_default_policy": ActionScope(modes=frozenset({LOCAL_MODE, ORG_MODE}), admin_only=True),
    "set_category_policy": ActionScope(modes=frozenset({LOCAL_MODE, ORG_MODE}), admin_only=True),

    # Admin-only in concept -- install-wide, not per-principal -- but
    # neither is privacy policy, and each needs a reload path of its own
    # that web/org_install_policy.py doesn't have yet (that module's own
    # docstring). LOCAL_MODE-only until one of those lands with its own
    # route, the same way the per-principal actions below are.
    "toggle_calendar_free_busy": ActionScope(modes=frozenset({LOCAL_MODE}), admin_only=True),
    "toggle_gmail_signature": ActionScope(modes=frozenset({LOCAL_MODE}), admin_only=True),
    "set_log_level": ActionScope(modes=frozenset({LOCAL_MODE}), admin_only=True),

    # ---------------------------------------------------------------- #
    # Per-principal in concept -- a connector toggle, refresh, or auth
    # flow is just as meaningful per-principal in org mode in principle as
    # add_policy_rule/remove_policy_rule above -- but no org-mode route
    # wires any of them yet, so is_action_permitted denies them until one
    # does (#B20: an allow-list entry with no route behind it is the bug
    # this table exists to make impossible to reintroduce by construction
    # -- move an action's ORG_MODE membership in the same PR that adds its
    # route, never ahead of it).
    # ---------------------------------------------------------------- #
    "enable_connector": ActionScope(modes=frozenset({LOCAL_MODE})),
    "disable_connector": ActionScope(modes=frozenset({LOCAL_MODE})),
    "refresh_connectors": ActionScope(modes=frozenset({LOCAL_MODE})),
    "authenticate_connector": ActionScope(modes=frozenset({LOCAL_MODE})),

    # ---------------------------------------------------------------- #
    # Meaningless, or actively wrong, on a headless server -- the
    # update-checker banner, Telegram's interactive phone/2FA login, and
    # the per-viewer notification-detail preference. LOCAL_MODE-only,
    # under any classification, ever -- unlike the two groups above, these
    # never get an org-mode route no matter what else lands.
    # ---------------------------------------------------------------- #
    "toggle_update_check": ActionScope(modes=frozenset({LOCAL_MODE})),
    "toggle_update_check_beta": ActionScope(modes=frozenset({LOCAL_MODE})),
    "check_for_updates_now": ActionScope(modes=frozenset({LOCAL_MODE})),
    "skip_update": ActionScope(modes=frozenset({LOCAL_MODE})),
    "remind_later_update": ActionScope(modes=frozenset({LOCAL_MODE})),
    "telegram_start_auth": ActionScope(modes=frozenset({LOCAL_MODE})),
    "telegram_submit_code": ActionScope(modes=frozenset({LOCAL_MODE})),
    "telegram_submit_2fa": ActionScope(modes=frozenset({LOCAL_MODE})),
    "telegram_cancel_auth": ActionScope(modes=frozenset({LOCAL_MODE})),
    "set_notifications_detail": ActionScope(modes=frozenset({LOCAL_MODE})),
    # ---------------------------------------------------------------- #
    # ORG_MODE-only, admin-only (AGT-5, ADR 0035 decision 3): pin a DCR
    # registration's client_id to a registry AI system, or remove the pin.
    # A pin creates attested identity -- the only kind a rule may key on --
    # so both are also sensitive (routes_settings._ORG_ONLY_SENSITIVE_
    # ACTIONS: step-up gated). There is no LOCAL_MODE route and never will
    # be: local mode has no DCR registrations to pin; its equivalent is
    # settings.yaml's agent_overrides: section (agent_overrides.py).
    # ---------------------------------------------------------------- #
    "pin_agent_client": ActionScope(modes=frozenset({ORG_MODE}), admin_only=True),
    "unpin_agent_client": ActionScope(modes=frozenset({ORG_MODE}), admin_only=True),

    # B9: hardcodes LOCAL_PRINCIPAL throughout (the credential check, the
    # config/settings.yaml section it writes, the LiveStepUpConfig it
    # updates) -- local mode's own single-principal, file-based step_up
    # model, not org mode's per-principal-credential, org_config.json-
    # bundle-driven one. Wiring this into an org-mode route would be
    # actively wrong (it would gate on, and mutate state for, the wrong
    # principal), not merely unavailable.
    "enable_step_up": ActionScope(modes=frozenset({LOCAL_MODE})),
}


# PSC-5: the shared settings renderer's own read of the table above -- every
# action with no real ORG_MODE route at all, i.e. exactly the controls
# settings_window_html.build_html() must never draw when rendering for a
# principal in org mode (there is no dispatcher on the other end for a click
# on one). Computed, not hand-maintained, so a new LOCAL_MODE-only action
# added to ACTION_SCOPES above is automatically excluded from org's rendered
# page the same PR that adds it -- the renderer has no classification of its
# own to fall out of sync with this one.
NOT_APPLICABLE_ACTIONS: frozenset[str] = frozenset(
    action for action, scope in ACTION_SCOPES.items() if ORG_MODE not in scope.modes
)


def is_action_permitted(action: str, principal: Principal, *, mode: str) -> bool:
    """Whether `principal` may invoke `action` on a settings surface running
    in `mode` (`LOCAL_MODE`/`ORG_MODE`). `mode` is required, not defaulted:
    the same action name can be permitted in one mode and not the other
    (see module docstring), so a caller must always say which surface it's
    asking about.

    An action with no entry in `ACTION_SCOPES`, or one whose `modes` doesn't
    include `mode` at all, is never permitted -- the caller is expected to
    404 that the same way `routes_settings.py`'s own allowlist check always
    has, not reach this function expecting a reason. `admin_only` is only
    ever consulted for `ORG_MODE` -- see module docstring for why local mode
    has nothing to gate on there."""
    scope = ACTION_SCOPES.get(action)
    if scope is None or mode not in scope.modes:
        return False
    if mode == ORG_MODE and scope.admin_only:
        return principal.is_admin
    return True
