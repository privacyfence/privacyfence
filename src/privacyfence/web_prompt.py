"""Generalized "block a worker thread on a human's choice" helper --
``WebApprovalUI._run_card``/``_run_confirm`` are thin wrappers around
``block_on_card``/``block_on_confirm`` below.

A worker thread blocked on a human's answer is the same mechanism whether the
answer is "accept/deny/accept_all" (a card) or "confirm/cancel" (a PII/rule
confirmation): register a ``PendingApproval`` in the one shared registry,
render it as HTML, block the calling thread on its ``threading.Event``, and
read back whatever a human's decision POST (web/routes_approvals.py's
``decide`` endpoint) wrote into ``result``/``chosen_index_result``. One
mechanism, one set of tests, and the dialog gets the same "no longer pending"
landing page an approval link already has -- see
approvals.PendingApprovalRegistry and web/routes_approvals.py for the
registration/serving/deciding side this module sits on top of.
"""
from __future__ import annotations

from .approvals import CARD_RESULTS, CONFIRM_RESULTS, PendingApprovalRegistry


def block_on_card(registry: PendingApprovalRegistry, html: str, approval=None) -> tuple[str, int | None]:
    """Register (or reuse a pre-registered) card, set its HTML, block until
    a human answers, and return ``(decision, chosen_index)`` -- the same
    shape ``ApprovalUI.show_popup``/``show_read_popup`` return. ``approval``
    is gate.py's deferred-protocol pre-registration (see
    web_approval_ui.WebApprovalUI's own docstring); omitted, a fresh confirm-
    shaped registration is used instead -- unchanged from before this
    extraction."""
    card = approval if approval is not None else registry.register_confirm()
    card.kind = "card"
    registry.set_html(card.id, html)
    card.event.wait()
    result = card.result if card.result in CARD_RESULTS else "deny"
    return result, card.chosen_index_result


def block_on_confirm(registry: PendingApprovalRegistry, html: str, *, sensitive: bool = False) -> bool:
    """Register a confirmation dialog, block until answered, and return
    whether it was confirmed (vs. cancelled/denied). ``sensitive`` is passed
    straight through to ``PendingApprovalRegistry.register_confirm``, whose
    docstring says what it costs the caller who sets it."""
    card = registry.register_confirm(sensitive=sensitive)
    registry.set_html(card.id, html)
    card.event.wait()
    return card.result == CONFIRM_RESULTS[0]  # "confirm"

