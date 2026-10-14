"""Unit-test fixtures shared across tests/unit (the repository-wide ones are in tests/conftest.py)."""
from __future__ import annotations

import pytest

from privacyfence import approval_ui


@pytest.fixture(autouse=True)
def _deny_left_over_cards():
    """A test that fails while a gated call waits on a card leaves a pf-popup worker blocked on
    it, and the process then never exits. Deny whatever is still pending, so a failure stays a
    failure instead of a hung job."""
    yield
    ui = approval_ui._INSTANCE
    registry = getattr(ui, "deferred_registry", None) if ui is not None else None
    if registry is None:
        return
    for card in registry.list_pending():
        ui.resolve(card.id, "deny")
