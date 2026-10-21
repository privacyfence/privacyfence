"""Latency budgets for the approval flow, through the real gate and a real
registry-backed WebApprovalUI.

Each test drives a scenario a human perceives as "the page took ages" and
asserts the server side of it stays inside a budget far below that -- so a
change that makes a confirmation or a card wait on something unrelated
fails here instead of in someone's browser. The budgets are deliberately
loose (seconds, not milliseconds): these tests catch a step queueing behind
a 30-second wait, not a few milliseconds of slowdown.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from privacyfence import approval_ui, gate
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import init_audit_logger
from privacyfence.principal import Principal, principal_scope
from privacyfence.web_approval_ui import WebApprovalUI

from .test_gate import (
    FakeEvaluator,
    base_kwargs,
    gated_call_or_pending,
    make_proposal,
    wait_until_async,
)

PII_TEXT = "Card on file: 4111 1111 1111 1111"
BUDGET_SECONDS = 1.0
DEFAULT_POOL_SIZE = min(32, (os.cpu_count() or 1) + 4)


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


def _registry(monkeypatch, *, hold_window: float, n: int = 50) -> PendingApprovalRegistry:
    registry = PendingApprovalRegistry(
        hold_window=hold_window, pending_ttl=300.0, ledger_ttl=300.0, max_pending=n, max_pending_per_principal=n,
    )
    approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
    monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
    return registry


def _rendered(registry, kind, *, exclude=()):
    return [a for a in registry.list_pending() if a.kind == kind and a.html and a.id not in exclude]


async def _time_until(predicate, timeout: float) -> float:
    started = time.monotonic()
    assert await wait_until_async(predicate, timeout=timeout, interval=0.01)
    return time.monotonic() - started


async def _settle(registry, tasks):
    for a in registry.list_pending():
        registry.answer(a.id, "cancel" if a.kind == "confirm" else "deny")
    await asyncio.gather(*tasks, return_exceptions=True)


class TestConfirmationFollowsTheApprovalPromptly:
    """After "Allow once" on a flagged card, its "Proceed" confirmation must be
    ready at once -- not after other pending approvals, or the 30-second hold
    window, get out of the way."""

    async def _approve_flagged_card_among_others(self, monkeypatch, *, hold_window, others):
        registry = _registry(monkeypatch, hold_window=hold_window)
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        tasks = [
            asyncio.ensure_future(gated_call_or_pending(**base_kwargs(summary=f"other {i}", args={"i": i})))
            for i in range(others)
        ]
        await wait_until_async(lambda: len(_rendered(registry, "card")) == others, timeout=10)
        before = {a.id for a in registry.list_pending()}
        flagged = asyncio.ensure_future(gated_call_or_pending(**base_kwargs(details_text=PII_TEXT, args={"i": -1})))
        await wait_until_async(lambda: _rendered(registry, "card", exclude=before), timeout=10)
        [card] = _rendered(registry, "card", exclude=before)

        registry.answer(card.id, "accept")
        elapsed = await _time_until(lambda: _rendered(registry, "confirm"), timeout=10)

        await _settle(registry, [*tasks, flagged])
        return elapsed

    async def test_within_the_hold_window_with_20_other_approvals_pending(self, monkeypatch, audit_dir):
        elapsed = await self._approve_flagged_card_among_others(monkeypatch, hold_window=30.0, others=20)
        assert elapsed < BUDGET_SECONDS

    async def test_after_the_hold_window_returned_pending(self, monkeypatch, audit_dir):
        elapsed = await self._approve_flagged_card_among_others(monkeypatch, hold_window=0.05, others=5)
        assert elapsed < BUDGET_SECONDS

    async def test_always_allow_rule_confirmation_follows_the_pii_confirmation(self, monkeypatch, audit_dir):
        registry = _registry(monkeypatch, hold_window=30.0)
        proposal = make_proposal("i_am_sender", None, "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "add_policy_v2_rules", lambda rules: True)
        task = asyncio.ensure_future(gated_call_or_pending(**base_kwargs(details_text=PII_TEXT)))
        await wait_until_async(lambda: _rendered(registry, "card"), timeout=10)
        [card] = _rendered(registry, "card")

        registry.answer(card.id, "accept_all", 0)
        to_pii = await _time_until(lambda: _rendered(registry, "confirm"), timeout=10)
        [pii] = _rendered(registry, "confirm")
        registry.answer(pii.id, "confirm")
        to_rule = await _time_until(lambda: _rendered(registry, "confirm", exclude={pii.id}), timeout=10)
        [rule] = _rendered(registry, "confirm", exclude={pii.id})
        registry.answer(rule.id, "confirm")
        await task

        assert to_pii < BUDGET_SECONDS
        assert to_rule < BUDGET_SECONDS


class TestHoldWindowsDoNotStarveOtherCalls:
    """Org mode: one daemon, many people. Each person's first approval waits
    out its hold window; that wait must not hold up the next person's card."""

    async def test_a_new_card_renders_while_more_principals_than_pool_workers_wait(self, monkeypatch, audit_dir):
        registry = _registry(monkeypatch, hold_window=15.0)
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        waiting = DEFAULT_POOL_SIZE + 2
        tasks = []
        for i in range(waiting):
            with principal_scope(Principal(id=f"user{i}")):
                tasks.append(asyncio.ensure_future(gated_call_or_pending(**base_kwargs(details_text=PII_TEXT))))
            # Each one parked in its hold window before the next arrives -- the steady state.
            await wait_until_async(lambda n=i + 1: len(_rendered(registry, "card")) >= min(n, DEFAULT_POOL_SIZE), 10)

        with principal_scope(Principal(id="latecomer")):
            late = asyncio.ensure_future(gated_call_or_pending(**base_kwargs(details_text=PII_TEXT)))
        elapsed = await _time_until(
            lambda: any(a.principal_id == "latecomer" and a.html for a in registry.list_pending()), timeout=20,
        )

        await _settle(registry, [*tasks, late])
        assert elapsed < BUDGET_SECONDS
