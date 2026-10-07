"""The reset registry runs every registered reset once per call, in order, never twice for one function."""
from __future__ import annotations

import pytest

from privacyfence.plugins import _testing

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(_testing, "_RESETS", [])
    return _testing._RESETS


class TestRegistry:
    def test_register_is_idempotent(self, isolated_registry):
        calls = []

        def fn():
            calls.append(1)

        _testing.register_reset(fn)
        _testing.register_reset(fn)
        _testing.reset_all()
        assert calls == [1]
        assert isolated_registry == [fn]

    def test_reset_all_runs_in_registration_order(self, isolated_registry):
        order = []
        _testing.register_reset(lambda: order.append("a"))
        _testing.register_reset(lambda: order.append("b"))
        _testing.reset_all()
        _testing.reset_all()
        assert order == ["a", "b", "a", "b"]

    def test_reset_all_with_nothing_registered(self, isolated_registry):
        _testing.reset_all()

    def test_a_reset_may_register_another(self, isolated_registry):
        order = []

        def first():
            order.append("first")
            _testing.register_reset(lambda: order.append("late"))

        _testing.register_reset(first)
        _testing.reset_all()
        assert order == ["first"]

    def test_conftest_resets_through_the_registry(self):
        import tests.conftest as conftest

        called = []
        _testing.register_reset(lambda: called.append(1))
        try:
            conftest._reset()
        finally:
            _testing._RESETS.pop()
        assert called == [1]
