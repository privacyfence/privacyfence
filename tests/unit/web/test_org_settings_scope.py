"""Tests for web/org_settings_scope.py -- the #400 design decision splitting
routes_settings.py's `_ALLOWED_ACTIONS` by org-mode ownership, plus the
`is_admin` authorization decision built on top of it. The bucket tests are
invariant checks, not behavior tests: what needs guarding is that the four
buckets stay disjoint, stay complete against the real allowlist, and never
name a method the controller doesn't actually have. `routes_org_settings.py`
now consumes the split for real (remove-a-row and the admin privacy/PII
routes), which is exactly why `PER_PRINCIPAL_ACTIONS_UNROUTED` exists
alongside `PER_PRINCIPAL_ACTIONS`: an action only belongs in the latter once
a route actually exists for it (#B20 in the 4.1 security review -- the
allow-list used to claim actions no route consumed). The
`is_action_permitted` tests are real behavior tests -- that function *is* an
authorization decision.
"""
from __future__ import annotations

import pytest

from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.settings_controller import SettingsController
from privacyfence.web.org_settings_scope import (
    ADMIN_ONLY_ACTIONS,
    NOT_APPLICABLE_ACTIONS,
    PER_PRINCIPAL_ACTIONS,
    PER_PRINCIPAL_ACTIONS_UNROUTED,
    is_action_permitted,
)
from privacyfence.web.routes_settings import _ALLOWED_ACTIONS

_ADMIN = Principal(id="alice", is_admin=True)
_NON_ADMIN = Principal(id="bob", is_admin=False)

_ALL_BUCKETS = (
    PER_PRINCIPAL_ACTIONS, PER_PRINCIPAL_ACTIONS_UNROUTED, ADMIN_ONLY_ACTIONS, NOT_APPLICABLE_ACTIONS,
)


def test_the_four_buckets_are_pairwise_disjoint():
    for i, a in enumerate(_ALL_BUCKETS):
        for b in _ALL_BUCKETS[i + 1:]:
            assert not (a & b)


def test_the_four_buckets_exactly_partition_allowed_actions():
    # A newly added local-mode action lands in _ALLOWED_ACTIONS without
    # touching this module -- this is what forces it to be classified here
    # too, instead of silently falling through every org-mode allowlist.
    union: frozenset[str] = frozenset()
    for bucket in _ALL_BUCKETS:
        union |= bucket
    assert union == _ALLOWED_ACTIONS


def test_every_classified_action_actually_exists_on_the_controller():
    for bucket in _ALL_BUCKETS:
        for action in bucket:
            assert callable(getattr(SettingsController, action, None))


@pytest.mark.parametrize("action", sorted(ADMIN_ONLY_ACTIONS))
def test_admin_only_actions_are_permitted_for_an_admin(action):
    assert is_action_permitted(action, _ADMIN) is True


@pytest.mark.parametrize("action", sorted(ADMIN_ONLY_ACTIONS))
def test_admin_only_actions_are_denied_for_a_non_admin(action):
    assert is_action_permitted(action, _NON_ADMIN) is False


@pytest.mark.parametrize("action", sorted(PER_PRINCIPAL_ACTIONS))
def test_per_principal_actions_are_permitted_for_any_signed_in_principal(action):
    assert is_action_permitted(action, _NON_ADMIN) is True
    assert is_action_permitted(action, _ADMIN) is True


@pytest.mark.parametrize("action", sorted(PER_PRINCIPAL_ACTIONS_UNROUTED))
def test_unrouted_per_principal_actions_are_never_permitted(action):
    # #B20: routes_org_settings.py has no route for any of these yet, so
    # is_action_permitted must deny them even though they're per-principal
    # in concept -- an allow-list entry with no route behind it is the bug.
    assert is_action_permitted(action, _ADMIN) is False
    assert is_action_permitted(action, _NON_ADMIN) is False


@pytest.mark.parametrize("action", sorted(NOT_APPLICABLE_ACTIONS))
def test_not_applicable_actions_are_never_permitted(action):
    assert is_action_permitted(action, _ADMIN) is False
    assert is_action_permitted(action, _NON_ADMIN) is False


def test_an_unclassified_action_name_is_never_permitted():
    assert is_action_permitted("quit_app", _ADMIN) is False
    assert is_action_permitted("not_a_real_action", _ADMIN) is False


def test_the_local_principal_is_never_admin_but_may_still_act_on_its_own_settings():
    # LOCAL_PRINCIPAL.is_admin is always False (see principal.py) -- "admin"
    # has never meant anything in local mode, but this function only gates
    # ADMIN_ONLY_ACTIONS on it, so a per-principal action stays permitted.
    assert LOCAL_PRINCIPAL.is_admin is False
    assert is_action_permitted(next(iter(PER_PRINCIPAL_ACTIONS)), LOCAL_PRINCIPAL) is True
    assert is_action_permitted(next(iter(ADMIN_ONLY_ACTIONS)), LOCAL_PRINCIPAL) is False
