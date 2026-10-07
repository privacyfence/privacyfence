"""Test-only registry that lets plugin modules reset their own module-level state.

About a dozen modules in this package hold process-wide state. Each registers its reset here, so
``tests/conftest.py`` makes one call instead of every module adding a line to the same function.
Nothing in the daemon calls ``reset_all``; it exists so one test's state cannot leak into the next
(ADR 0120).
"""
from __future__ import annotations

from collections.abc import Callable

_RESETS: list[Callable[[], None]] = []


def register_reset(fn: Callable[[], None]) -> None:
    """Remember ``fn`` for ``reset_all``. Registering the same function twice keeps it once."""
    if fn not in _RESETS:
        _RESETS.append(fn)


def reset_all() -> None:
    """Call every registered reset, in registration order."""
    for fn in list(_RESETS):
        fn()
