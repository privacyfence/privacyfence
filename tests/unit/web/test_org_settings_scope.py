"""Tests for web/org_settings_scope.py -- the #400 design decision splitting
settings actions by which mode(s) they have a real route in, plus the
``is_admin`` authorization decision built on top of it (PSC-4b: this table
is now the primary declaration both web/routes_settings.py's
``build_routes``/``build_org_routes`` project from, rather than a filter
bolted onto local mode's own action list -- see that module's own
docstring). The bucket-shaped tests are invariant checks, not behavior
tests: what needs guarding is that every action web/routes_settings.py's
local dispatcher recognizes is classified here, and that a mode gains an
action only in the same PR that gives it a real route (#B20 in the 4.1
security review -- the allow-list used to claim actions no route
consumed). The ``is_action_permitted`` tests are real behavior tests --
that function *is* an authorization decision.
"""
from __future__ import annotations

import pytest

from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.settings_controller import SettingsController
from privacyfence.web.org_settings_scope import ACTION_SCOPES, LOCAL_MODE, ORG_MODE, is_action_permitted
from privacyfence.web.routes_settings import _ALLOWED_ACTIONS

_ADMIN = Principal(id="alice", is_admin=True)
_NON_ADMIN = Principal(id="bob", is_admin=False)

_ORG_ROUTED_ACTIONS = frozenset(a for a, s in ACTION_SCOPES.items() if ORG_MODE in s.modes)
_ORG_ROUTED_ADMIN_ONLY_ACTIONS = frozenset(a for a in _ORG_ROUTED_ACTIONS if ACTION_SCOPES[a].admin_only)
_ORG_ROUTED_PER_PRINCIPAL_ACTIONS = _ORG_ROUTED_ACTIONS - _ORG_ROUTED_ADMIN_ONLY_ACTIONS
_LOCAL_ONLY_ACTIONS = frozenset(ACTION_SCOPES) - _ORG_ROUTED_ACTIONS


def test_every_action_names_local_mode():
    # Local mode's own dispatcher predates this table and was never gated
    # by it -- every action classified here must still be reachable there,
    # so ACTION_SCOPES can be the one place both modes project from rather
    # than local mode needing a second, separate list.
    for action, scope in ACTION_SCOPES.items():
        assert LOCAL_MODE in scope.modes, action
        assert scope.modes <= {LOCAL_MODE, ORG_MODE}, action


def test_allowed_actions_is_exactly_projected_from_the_table():
    # A newly added local-mode action lands in _ALLOWED_ACTIONS without
    # touching this module -- this is what forces it to be classified here
    # too, instead of silently going unclassified.
    assert _ALLOWED_ACTIONS == frozenset(ACTION_SCOPES)


def test_every_classified_action_actually_exists_on_the_controller():
    for action in ACTION_SCOPES:
        assert callable(getattr(SettingsController, action, None)), action


@pytest.mark.parametrize("action", sorted(_ORG_ROUTED_ADMIN_ONLY_ACTIONS))
def test_org_routed_admin_only_actions_are_permitted_for_an_admin(action):
    assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is True


@pytest.mark.parametrize("action", sorted(_ORG_ROUTED_ADMIN_ONLY_ACTIONS))
def test_org_routed_admin_only_actions_are_denied_for_a_non_admin(action):
    assert is_action_permitted(action, _NON_ADMIN, mode=ORG_MODE) is False


@pytest.mark.parametrize("action", sorted(_ORG_ROUTED_PER_PRINCIPAL_ACTIONS))
def test_org_routed_per_principal_actions_are_permitted_for_any_signed_in_principal(action):
    assert is_action_permitted(action, _NON_ADMIN, mode=ORG_MODE) is True
    assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is True


@pytest.mark.parametrize("action", sorted(_LOCAL_ONLY_ACTIONS))
def test_local_only_actions_are_never_permitted_in_org_mode(action):
    # #B20: some of these are per-principal or admin-wide in concept, but
    # org mode has no route for any of them yet -- is_action_permitted must
    # deny them even so, the same way an allow-list entry with no route
    # behind it was the bug this table exists to make impossible to
    # reintroduce by construction.
    assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is False
    assert is_action_permitted(action, _NON_ADMIN, mode=ORG_MODE) is False


@pytest.mark.parametrize("action", sorted(ACTION_SCOPES))
def test_every_classified_action_is_permitted_in_local_mode_for_any_principal(action):
    # Local mode has no admin concept (LOCAL_PRINCIPAL.is_admin is always
    # False, see principal.py) -- every action valid for LOCAL_MODE is
    # permitted once mode membership itself has passed, admin_only or not.
    assert is_action_permitted(action, LOCAL_PRINCIPAL, mode=LOCAL_MODE) is True
    assert is_action_permitted(action, _NON_ADMIN, mode=LOCAL_MODE) is True


def test_an_unclassified_action_name_is_never_permitted_in_either_mode():
    for action in ("quit_app", "not_a_real_action"):
        assert is_action_permitted(action, _ADMIN, mode=LOCAL_MODE) is False
        assert is_action_permitted(action, _ADMIN, mode=ORG_MODE) is False


def test_mode_is_required_not_defaulted():
    with pytest.raises(TypeError):
        is_action_permitted("add_policy_rule", _ADMIN)  # type: ignore[call-arg]


def test_the_local_principal_is_never_admin_but_may_still_act_on_its_own_settings():
    # LOCAL_PRINCIPAL.is_admin is always False (see principal.py) -- "admin"
    # has never meant anything in local mode, but is_action_permitted only
    # ever consults it for ORG_MODE, so a per-principal *or* admin-only
    # action stays permitted for LOCAL_PRINCIPAL under LOCAL_MODE.
    assert LOCAL_PRINCIPAL.is_admin is False
    assert is_action_permitted(next(iter(_ORG_ROUTED_PER_PRINCIPAL_ACTIONS)), LOCAL_PRINCIPAL, mode=LOCAL_MODE) is True
    assert is_action_permitted(next(iter(_ORG_ROUTED_ADMIN_ONLY_ACTIONS)), LOCAL_PRINCIPAL, mode=LOCAL_MODE) is True
    # ...but the same admin-only action, asked about ORG_MODE for a
    # principal who (like LOCAL_PRINCIPAL) isn't an admin, is still denied
    # -- the two modes' answers genuinely differ for the same action name.
    assert is_action_permitted(next(iter(_ORG_ROUTED_ADMIN_ONLY_ACTIONS)), LOCAL_PRINCIPAL, mode=ORG_MODE) is False
