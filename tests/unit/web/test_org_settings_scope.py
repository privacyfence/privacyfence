"""Tests for web/org_settings_scope.py -- which settings actions have a real route on an
organization server, plus the ``is_admin`` authorization decision built on top of it (this table is
the declaration web/routes_settings.py's ``build_org_routes`` projects from -- see ADR 0033 and that
module's own docstring).
The bucket-shaped tests are invariant checks, not behavior tests: what needs guarding is that an
action gains a route only in the same PR that gives it one -- an allow-listed action with no route
behind it is denied until it is routed. The ``is_action_permitted`` tests are real behavior tests --
that function *is* an authorization decision.
"""
from __future__ import annotations

import pytest

from privacyfence.principal import Principal
from privacyfence.web.org_settings_scope import ACTION_SCOPES, ORG_MODE, ActionScope, is_action_permitted
from privacyfence.web.routes_settings import _ORG_ALLOWED_ACTIONS, _SENSITIVE_ACTIONS

_ADMIN = Principal(id="alice", is_admin=True)
_NON_ADMIN = Principal(id="bob", is_admin=False)

_ORG_ROUTED_ACTIONS = frozenset(a for a, s in ACTION_SCOPES.items() if ORG_MODE in s.modes)
_ORG_ROUTED_ADMIN_ONLY_ACTIONS = frozenset(a for a in _ORG_ROUTED_ACTIONS if ACTION_SCOPES[a].admin_only)
_ORG_ROUTED_PER_PRINCIPAL_ACTIONS = _ORG_ROUTED_ACTIONS - _ORG_ROUTED_ADMIN_ONLY_ACTIONS
_UNROUTED_ACTIONS = frozenset(ACTION_SCOPES) - _ORG_ROUTED_ACTIONS


def test_every_scope_names_only_the_organization_mode():
    for action, scope in ACTION_SCOPES.items():
        assert scope.modes <= {ORG_MODE}, action


def test_org_allowed_actions_is_exactly_the_org_routed_projection():
    assert _ORG_ALLOWED_ACTIONS == _ORG_ROUTED_ACTIONS


def test_every_org_allowed_action_is_classified_sensitive():
    """An action that can change what gets gated must need a passkey whenever step-up is
    required; a new org-routed action fails here until it is classified (ADR 0034)."""
    assert _SENSITIVE_ACTIONS == _ORG_ALLOWED_ACTIONS


def test_no_action_is_org_routed_and_admin_only():
    # An organization's install-wide changes go through the Admin dispatcher (web/routes_admin.py),
    # which has its own gate; the settings table holds only what every principal may do to their
    # own settings.
    assert _ORG_ROUTED_ADMIN_ONLY_ACTIONS == frozenset()


def test_the_install_wide_privacy_actions_have_no_settings_route():
    for action in ("toggle_pii_detection", "toggle_pii_category", "set_default_policy", "set_category_policy"):
        assert ACTION_SCOPES[action].modes == frozenset(), action
        assert ACTION_SCOPES[action].admin_only is True, action


def test_an_org_routed_admin_only_action_would_need_an_admin(monkeypatch):
    # Nothing is routed this way today; the gate stays so that a future one cannot skip it.
    monkeypatch.setitem(
        ACTION_SCOPES, "hypothetical_admin_action", ActionScope(modes=frozenset({ORG_MODE}), admin_only=True),
    )
    assert is_action_permitted("hypothetical_admin_action", _ADMIN, mode=ORG_MODE) is True
    assert is_action_permitted("hypothetical_admin_action", _NON_ADMIN, mode=ORG_MODE) is False


@pytest.mark.parametrize("action", sorted(_ORG_ROUTED_PER_PRINCIPAL_ACTIONS))
def test_org_routed_per_principal_actions_are_permitted_for_any_signed_in_principal(action):
    assert is_action_permitted(action, _NON_ADMIN, mode=ORG_MODE) is True
    assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is True


@pytest.mark.parametrize("action", sorted(_UNROUTED_ACTIONS))
def test_actions_with_no_route_are_never_permitted(action):
    # Some of these are per-principal or admin-wide in concept, but nothing routes any of them --
    # is_action_permitted must deny them even so: an allow-list entry with no route behind it is
    # what this table exists to make impossible by construction.
    assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is False
    assert is_action_permitted(action, _NON_ADMIN, mode=ORG_MODE) is False


def test_an_unclassified_action_name_is_never_permitted():
    for action in ("quit_app", "not_a_real_action"):
        assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is False


def test_mode_is_required_not_defaulted():
    with pytest.raises(TypeError):
        is_action_permitted("add_policy_rule", _ADMIN)  # type: ignore[call-arg]


@pytest.mark.parametrize("action", [
    "add_policy_rule", "remove_policy_rule", "update_policy_rule", "set_policy_rule_verbs",
])
def test_a_rule_write_is_per_principal_and_routed(action):
    """Widening a stored rule needs no more authority than adding one: the signed-in principal's
    own rules, and no administrator (ADR 0166)."""
    assert ACTION_SCOPES[action].modes == frozenset({ORG_MODE})
    assert ACTION_SCOPES[action].admin_only is False
    assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is True
    assert is_action_permitted(action, _NON_ADMIN, mode=ORG_MODE) is True
