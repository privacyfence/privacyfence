"""Tests for web/org_settings_scope.py -- the #400 design decision splitting
routes_settings.py's `_ALLOWED_ACTIONS` by org-mode ownership. These are
invariant checks, not behavior tests: no org-mode route consumes this split
yet (see that module's own docstring), so what needs guarding is that the
three buckets stay disjoint, stay complete against the real allowlist, and
never name a method the controller doesn't actually have.
"""
from __future__ import annotations

from privacyfence.settings_controller import SettingsController
from privacyfence.web.org_settings_scope import (
    ADMIN_ONLY_ACTIONS,
    NOT_APPLICABLE_ACTIONS,
    PER_PRINCIPAL_ACTIONS,
)
from privacyfence.web.routes_settings import _ALLOWED_ACTIONS


def test_the_three_buckets_are_pairwise_disjoint():
    assert not (PER_PRINCIPAL_ACTIONS & ADMIN_ONLY_ACTIONS)
    assert not (PER_PRINCIPAL_ACTIONS & NOT_APPLICABLE_ACTIONS)
    assert not (ADMIN_ONLY_ACTIONS & NOT_APPLICABLE_ACTIONS)


def test_the_three_buckets_exactly_partition_allowed_actions():
    # A newly added local-mode action lands in _ALLOWED_ACTIONS without
    # touching this module -- this is what forces it to be classified here
    # too, instead of silently falling through every org-mode allowlist.
    union = PER_PRINCIPAL_ACTIONS | ADMIN_ONLY_ACTIONS | NOT_APPLICABLE_ACTIONS
    assert union == _ALLOWED_ACTIONS


def test_every_classified_action_actually_exists_on_the_controller():
    for action in PER_PRINCIPAL_ACTIONS | ADMIN_ONLY_ACTIONS | NOT_APPLICABLE_ACTIONS:
        assert callable(getattr(SettingsController, action, None))
