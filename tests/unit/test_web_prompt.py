"""web_prompt.py -- the generalized blocking-prompt mechanism factored out
of web_approval_ui.py.
"""
from __future__ import annotations

import threading
import time

from privacyfence import web_prompt
from privacyfence.approvals import PendingApprovalRegistry


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestBlockOnCard:
    def test_blocks_until_answered_then_returns_result_and_index(self):
        registry = PendingApprovalRegistry()
        box = {}

        def run():
            box["result"] = web_prompt.block_on_card(registry, "<html>card</html>")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: registry.list_pending())
        card = registry.list_pending()[0]
        assert card.html == "<html>card</html>"
        assert card.kind == "card"
        registry.answer(card.id, "accept_all", 2)
        t.join(timeout=2)
        assert box["result"] == ("accept_all", 2)

    def test_unrecognized_result_defaults_to_deny(self):
        registry = PendingApprovalRegistry()
        box = {}

        def run():
            box["result"] = web_prompt.block_on_card(registry, "<html></html>")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: registry.list_pending())
        card = registry.list_pending()[0]
        registry.answer(card.id, "something-unexpected")
        t.join(timeout=2)
        assert box["result"] == ("deny", None)

    def test_uses_a_pre_registered_approval_when_given(self):
        registry = PendingApprovalRegistry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k", connector="gmail", tool="t", gate_kind="popup", request_id="r",
        )
        box = {}

        def run():
            box["result"] = web_prompt.block_on_card(registry, "<html></html>", approval)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: approval.html)
        assert approval.kind == "card"
        registry.answer(approval.id, "accept")
        t.join(timeout=2)
        assert box["result"] == ("accept", None)

    def test_an_expired_approval_releases_its_blocked_worker(self):
        # Regression for the leaked popup-executor thread: pop_expired_
        # events() used to set only finalize_event, never the UI-step
        # `event` that card.event.wait() below actually blocks on -- only
        # PendingApproval.answer() (a human clicking a button) ever set it.
        # A worker parked here on an approval that instead expired (nobody
        # answered before the pending TTL) never returned, permanently
        # occupying one of gate.py's _popup_executor's 8 workers.
        registry = PendingApprovalRegistry(pending_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k", connector="gmail", tool="t", gate_kind="popup", request_id="r",
        )
        box = {}

        def run():
            box["result"] = web_prompt.block_on_card(registry, "<html></html>", approval)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: approval.html)
        time.sleep(0.02)
        expired = registry.pop_expired_events()
        assert [a.id for a in expired] == [approval.id]
        t.join(timeout=2)
        assert not t.is_alive()
        # Unrecognized (non-CARD_RESULTS) result defaults to "deny" -- the
        # approval's own real outcome ("expired", recorded in
        # final_decision) is untouched by this.
        assert box["result"] == ("deny", None)
        assert approval.final_decision == "expired"


class TestBlockOnConfirm:
    def test_confirm_result_is_true(self):
        registry = PendingApprovalRegistry()
        box = {}

        def run():
            box["result"] = web_prompt.block_on_confirm(registry, "<html></html>")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: registry.list_pending())
        card = registry.list_pending()[0]
        registry.answer(card.id, "confirm")
        t.join(timeout=2)
        assert box["result"] is True

    def test_cancel_result_is_false(self):
        registry = PendingApprovalRegistry()
        box = {}

        def run():
            box["result"] = web_prompt.block_on_confirm(registry, "<html></html>")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: registry.list_pending())
        card = registry.list_pending()[0]
        registry.answer(card.id, "cancel")
        t.join(timeout=2)
        assert box["result"] is False


class TestBlockOnChoice:
    def test_chosen_index_is_returned(self):
        registry = PendingApprovalRegistry()
        box = {}

        def run():
            box["result"] = web_prompt.block_on_choice(registry, "<html></html>")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: registry.list_pending())
        card = registry.list_pending()[0]
        assert card.kind == "choice"
        registry.answer(card.id, "2")
        t.join(timeout=2)
        assert box["result"] == 2

    def test_cancel_is_none(self):
        registry = PendingApprovalRegistry()
        box = {}

        def run():
            box["result"] = web_prompt.block_on_choice(registry, "<html></html>")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: registry.list_pending())
        card = registry.list_pending()[0]
        registry.answer(card.id, "cancel")
        t.join(timeout=2)
        assert box["result"] is None

    def test_garbage_result_is_none_not_a_raise(self):
        registry = PendingApprovalRegistry()
        box = {}

        def run():
            box["result"] = web_prompt.block_on_choice(registry, "<html></html>")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert wait_until(lambda: registry.list_pending())
        card = registry.list_pending()[0]
        registry.answer(card.id, "not-a-number")
        t.join(timeout=2)
        assert box["result"] is None
