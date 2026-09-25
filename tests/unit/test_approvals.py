"""Unit tests for privacyfence.approvals.PendingApprovalRegistry -- the
deferred-approval protocol's domain object. See that module's own docstring for the two-layer
answer()/finalize() design these tests exercise directly, without gate.py's
own orchestration in the way.
"""
from __future__ import annotations

import time

import pytest

from privacyfence.approvals import (
    ALL_APPROVAL_KINDS,
    IdenticalWriteAwaitingApprovalError,
    LedgerHit,
    PendingApprovalRegistry,
    TooManyPendingApprovalsError,
    _BATCHABLE_KINDS,
    _NON_BATCHABLE_KINDS,
    canonical_key,
)
from privacyfence.principal import Principal, principal_scope


def make_registry(**overrides) -> PendingApprovalRegistry:
    kwargs = dict(hold_window=1.0, pending_ttl=1.0, ledger_ttl=1.0, max_pending=10)
    kwargs.update(overrides)
    return PendingApprovalRegistry(**kwargs)


class TestCanonicalKey:
    def test_same_args_produce_the_same_key_regardless_of_order(self):
        a = canonical_key("gmail", "gmail_get_message", {"id": "1", "x": "y"})
        b = canonical_key("gmail", "gmail_get_message", {"x": "y", "id": "1"})
        assert a == b

    def test_different_args_produce_different_keys(self):
        a = canonical_key("gmail", "gmail_get_message", {"id": "1"})
        b = canonical_key("gmail", "gmail_get_message", {"id": "2"})
        assert a != b

    def test_none_args_is_the_same_as_empty_dict(self):
        assert canonical_key("gmail", "gmail_get_message", None) == canonical_key(
            "gmail", "gmail_get_message", {}
        )


class TestRegisterOrCoalesce:
    def test_first_registration_creates_a_new_approval(self):
        registry = make_registry()
        approval, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        assert created is True
        assert approval.kind == "card"
        assert approval.dedupe_key == "k1"

    def test_second_call_with_the_same_key_coalesces_onto_the_first(self):
        registry = make_registry()
        first, created1 = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        second, created2 = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r2",
        )
        assert created2 is False
        assert second is first
        assert len(registry.list_pending()) == 1

    def test_a_different_key_gets_its_own_approval(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        second, created2 = registry.register_or_coalesce(
            dedupe_key="k2", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r2",
        )
        assert created2 is True
        assert second is not first
        assert len(registry.list_pending()) == 2

    def test_a_finalized_approval_does_not_block_a_fresh_registration_for_the_same_key(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        registry.finalize(first.id, "deny")
        second, created2 = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r2",
        )
        assert created2 is True
        assert second is not first

    def test_cap_is_enforced_for_genuinely_new_keys(self):
        registry = make_registry(max_pending=2)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.register_or_coalesce(
            dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        with pytest.raises(TooManyPendingApprovalsError):
            registry.register_or_coalesce(
                dedupe_key="k3", connector="c", tool="t", gate_kind="review", request_id="r3",
            )

    def test_cap_is_not_charged_against_a_coalescing_hit(self):
        registry = make_registry(max_pending=1)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        # Same key again -- coalesces, does not count as a second "new" entry.
        _, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        assert created is False

    def test_preview_is_stamped_onto_the_approval(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            preview={"from": "alice@example.com", "size": "12 KB"},
        )
        assert approval.preview == {"from": "alice@example.com", "size": "12 KB"}
        # Known at registration -- html is only ever set later, by
        # WebApprovalUI's own _run_card (gate.py's _popup_executor), which
        # nothing here ever triggers. A consumer that wants to disclose what
        # this approval is about doesn't have to wait for that worker.
        assert approval.html == ""

    def test_no_preview_given_defaults_to_an_empty_dict(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert approval.preview == {}

    def test_preview_is_copied_not_aliased(self):
        # Mutating the caller's own dict after registration must not reach
        # back into the stored approval -- the same defensive-copy contract
        # pii_categories already gets a few lines below (list(pii_categories
        # or [])).
        registry = make_registry()
        caller_dict = {"from": "alice@example.com"}
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            preview=caller_dict,
        )
        caller_dict["from"] = "mallory@example.com"
        assert approval.preview == {"from": "alice@example.com"}


class TestAnswerVsFinalize:
    def test_answer_resolves_the_ui_step_only_not_the_whole_approval(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.answer(approval.id, "accept") is True
        assert approval.event.is_set()
        assert not approval.is_finalized()

    def test_finalize_writes_the_ledger_and_wakes_waiters(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.finalize(approval.id, "accept", "some_rule") is True
        assert approval.is_finalized()
        hit = registry.consume_ledger("k1")
        assert hit == LedgerHit(decision="accept", rule_name="some_rule", decided_at=approval.decided_at)

    def test_answer_is_idempotent_first_wins(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.answer(approval.id, "accept") is True
        assert registry.answer(approval.id, "deny") is False
        assert approval.result == "accept"

    def test_finalize_is_idempotent_first_wins(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.finalize(approval.id, "accept") is True
        assert registry.finalize(approval.id, "deny") is False
        assert approval.final_decision == "accept"

    def test_answer_and_finalize_on_an_unknown_id_return_false(self):
        registry = make_registry()
        assert registry.answer("nope", "accept") is False
        assert registry.finalize("nope", "accept") is False

    def test_finalize_without_a_prior_answer_still_wakes_the_ui_step(self):
        # Regression: finalize() used to set only finalize_event, never
        # event. A thread blocked in web_prompt.block_on_card
        # (card.event.wait(), no timeout) waits on `event`, not
        # `finalize_event` -- so a finalize that never went through
        # answer() first (reevaluate_all() finding a matching rule; see
        # pop_expired_events() below for the other such path) left that
        # thread, and its gate.py popup-executor worker, blocked forever.
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert not approval.event.is_set()
        registry.finalize(approval.id, "auto_accepted", "some_rule")
        assert approval.event.is_set()
        # web_prompt.block_on_card maps a result outside CARD_RESULTS to
        # "deny" -- finalize() must not overwrite the UI-step result with
        # the final decision, since the real outcome ("auto_accepted")
        # already lives in final_decision.
        assert approval.result not in ("accept", "deny", "accept_all")


class TestLedgerSingleUse:
    def test_review_gate_ledger_entry_is_reusable(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        first = registry.consume_ledger("k1")
        second = registry.consume_ledger("k1")
        assert first is not None
        assert second == first

    def test_popup_gate_ledger_entry_is_single_use(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        first = registry.consume_ledger("k1")
        second = registry.consume_ledger("k1")
        assert first is not None
        assert second is None

    def test_consuming_a_single_use_entry_frees_the_key_for_a_fresh_registration(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        registry.consume_ledger("k1")
        fresh, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r2",
        )
        assert created is True
        assert fresh is not approval

    def test_unfinalized_approval_has_no_ledger_entry_yet(self):
        registry = make_registry()
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.consume_ledger("k1") is None

    def test_mark_collected_consumes_a_popup_entry(self):
        # A write decided within the hold window reaches its caller directly;
        # that delivery consumes it exactly as a ledger hit would, so an
        # identical write afterwards cannot replay it (ADR 0073).
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1", waiting=True,
        )
        registry.finalize(approval.id, "accept")
        registry.mark_collected(approval)
        registry.release_waiter(approval)
        assert approval.ledger_collected and approval.ledger_consumed
        assert registry.consume_ledger("k1") is None
        assert registry.get(approval.id) is None
        fresh, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r2",
        )
        assert created is True
        assert fresh is not approval

    def test_mark_collected_leaves_a_review_entry_replayable(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1", waiting=True,
        )
        registry.finalize(approval.id, "accept")
        registry.mark_collected(approval)
        registry.release_waiter(approval)
        assert approval.ledger_collected and not approval.ledger_consumed
        hit = registry.consume_ledger("k1")
        assert hit is not None and hit.decision == "accept"

    def test_mark_collected_before_finalize_changes_nothing(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.mark_collected(approval)
        assert not approval.ledger_collected and not approval.ledger_consumed
        assert registry.get(approval.id) is approval

    def test_a_popup_entry_with_a_waiter_is_not_handed_out_by_the_ledger(self):
        # Between finalize() and the waiting call's own mark_collected(), a
        # re-issued identical write must not take the same decision.
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1", waiting=True,
        )
        registry.finalize(approval.id, "accept")
        assert registry.consume_ledger("k1") is None
        registry.release_waiter(approval)
        # Nobody collected it after all (the waiter timed out just before
        # the decision): a re-issued call takes it once, as usual.
        assert registry.consume_ledger("k1") is not None
        assert registry.consume_ledger("k1") is None


class TestWriteCoalescingWithWaiters:
    """Identical writes share one approval only while nobody is waiting on
    it; reads coalesce however many calls wait (ADR 0073)."""

    def test_an_identical_write_is_refused_while_another_call_waits(self):
        registry = make_registry()
        first, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1", waiting=True,
        )
        assert created is True and first.waiters == 1
        with pytest.raises(IdenticalWriteAwaitingApprovalError, match="already awaiting approval"):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r2", waiting=True,
            )
        # Refused without side effects: still one approval, still one waiter.
        assert first.waiters == 1
        assert len(registry.list_pending()) == 1

    def test_an_identical_write_is_refused_between_decision_and_collection(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1", waiting=True,
        )
        registry.finalize(first.id, "accept")
        with pytest.raises(IdenticalWriteAwaitingApprovalError):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r2", waiting=True,
            )

    def test_a_reissued_write_coalesces_once_nobody_is_waiting(self):
        # The deferred-write path: the first call timed out into
        # approval_pending and stopped waiting; the re-issue must find the
        # same approval to collect its decision.
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1", waiting=True,
        )
        registry.release_waiter(first)
        second, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r2", waiting=True,
        )
        assert created is False
        assert second is first
        assert first.waiters == 1

    def test_identical_reads_coalesce_whatever_the_waiter_count(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1", waiting=True,
        )
        for i in range(3):
            again, created = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id=f"r{i + 2}",
                waiting=True,
            )
            assert created is False and again is first
        assert first.waiters == 4

    def test_release_waiter_never_goes_below_zero(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.release_waiter(approval)
        assert approval.waiters == 0


class TestHoldWindow:
    async def test_wait_async_returns_true_once_finalized_within_the_window(self):
        registry = make_registry(hold_window=1.0)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        assert await registry.wait_async(approval, 1.0) is True

    async def test_wait_async_returns_false_when_nothing_decides_in_time(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert await registry.wait_async(approval, 0.05) is False
        assert not approval.is_finalized()


class TestPendingTTLExpiry:
    def test_unanswered_approval_past_its_ttl_is_reported_as_expired(self):
        registry = make_registry(pending_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        time.sleep(0.02)
        expired = registry.pop_expired_events()
        assert [a.id for a in expired] == [approval.id]
        assert approval.final_decision == "expired"
        assert approval.is_finalized()

    def test_expiry_frees_the_dedupe_key_for_a_fresh_registration(self):
        registry = make_registry(pending_ttl=0.01)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        time.sleep(0.02)
        registry.pop_expired_events()
        fresh, created = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        assert created is True

    def test_a_not_yet_expired_approval_is_not_reported(self):
        registry = make_registry(pending_ttl=5.0)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.pop_expired_events() == []

    def test_a_finalized_approval_is_never_reported_as_pending_expired(self):
        registry = make_registry(pending_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        time.sleep(0.02)
        assert registry.pop_expired_events() == []


class TestLedgerTTLExpiry:
    def test_a_decided_but_never_reclaimed_entry_is_reported_once_the_ledger_ttl_lapses(self):
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        time.sleep(0.02)
        events = registry.pop_expired_ledger_events()
        assert [a.id for a in events] == [approval.id]
        # Not consumable anymore -- either via the ledger (it just expired)
        # or by finding it in the registry at all (swept on report).
        assert registry.consume_ledger("k1") is None
        assert registry.get(approval.id) is None

    def test_a_replayed_review_entry_is_not_reported_once_the_ledger_ttl_lapses(self):
        # The read's data was released through the ledger, so its lapse is
        # not "expired" (ADR 0073) -- but the entry is still cleaned up.
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        assert registry.consume_ledger("k1") is not None
        assert approval.ledger_collected
        time.sleep(0.02)
        assert registry.pop_expired_ledger_events() == []
        assert registry.get(approval.id) is None
        assert registry.consume_ledger("k1") is None

    def test_an_entry_collected_within_the_hold_window_is_not_reported(self):
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1", waiting=True,
        )
        registry.finalize(approval.id, "accept")
        registry.mark_collected(approval)
        registry.release_waiter(approval)
        time.sleep(0.02)
        assert registry.pop_expired_ledger_events() == []
        assert registry.get(approval.id) is None

    def test_an_uncollected_popup_entry_is_still_reported(self):
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1", waiting=True,
        )
        registry.release_waiter(approval)  # timed out into approval_pending
        registry.finalize(approval.id, "accept")
        time.sleep(0.02)
        assert [a.id for a in registry.pop_expired_ledger_events()] == [approval.id]

    def test_an_entry_with_a_waiter_is_not_reported_until_the_waiter_is_done(self):
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1", waiting=True,
        )
        registry.finalize(approval.id, "accept")
        time.sleep(0.02)
        assert registry.pop_expired_ledger_events() == []
        registry.mark_collected(approval)
        registry.release_waiter(approval)
        assert registry.pop_expired_ledger_events() == []
        assert registry.get(approval.id) is None

    def test_a_reclaimed_entry_is_never_reported_as_a_ledger_expiry(self):
        registry = make_registry(ledger_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="popup", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        registry.consume_ledger("k1")  # reclaimed (and popped -- single-use)
        time.sleep(0.02)
        assert registry.pop_expired_ledger_events() == []


class TestReevaluateAll:
    def test_a_pending_card_covered_by_a_new_rule_is_auto_accepted(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(),
        )

        def should_auto_accept(operation_key, ctx):
            return True, "trusted_sender_domain"

        resolved = registry.reevaluate_all(should_auto_accept)

        assert [a.id for a in resolved] == [approval.id]
        assert approval.final_decision == "auto_accepted"
        assert approval.final_rule_name == "trusted_sender_domain"
        assert approval.is_finalized()

    def test_a_card_not_covered_by_any_rule_is_left_alone(self):
        registry = make_registry()
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(),
        )

        resolved = registry.reevaluate_all(lambda op, ctx: (False, ""))

        assert resolved == []

    def test_a_pii_forced_card_is_never_auto_resolved_even_if_a_rule_would_match(self):
        registry = make_registry()
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(), pii_forces_confirmation=True,
        )

        resolved = registry.reevaluate_all(lambda op, ctx: (True, "trusted_sender_domain"))

        assert resolved == []

    def test_an_already_answered_card_is_not_reevaluated(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", review_ctx=object(),
        )
        registry.answer(approval.id, "deny")  # a human already clicked

        resolved = registry.reevaluate_all(lambda op, ctx: (True, "trusted_sender_domain"))

        assert resolved == []

    def test_a_confirm_dialog_with_no_operation_key_is_never_reevaluated(self):
        registry = make_registry()
        registry.register_confirm()

        resolved = registry.reevaluate_all(lambda op, ctx: (True, "some_rule"))

        assert resolved == []


class TestApprovalUrl:
    def test_no_base_url_configured_returns_none(self):
        registry = make_registry()
        assert registry.approval_url("abc123") is None

    def test_base_url_is_used_once_set(self):
        registry = make_registry()
        registry.set_base_url("http://localhost:8765")
        assert registry.approval_url("abc123") == "http://localhost:8765/approvals/abc123"


class TestBinderUrl:
    """Approval binder: the list page itself, distinct from any one
    approval's own approval_url()."""

    def test_no_base_url_configured_returns_none(self):
        registry = make_registry()
        assert registry.binder_url() is None

    def test_base_url_is_used_once_set(self):
        registry = make_registry()
        registry.set_base_url("http://localhost:8765")
        assert registry.binder_url() == "http://localhost:8765/approvals"


class TestHasOtherLive:
    """Approval binder: gate.py's adaptive hold window collapses to
    zero exactly when this returns True for a call that just registered."""

    def test_false_when_nothing_else_is_pending(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.has_other_live(approval.principal_id, approval.id) is False

    def test_true_when_another_approval_is_still_unfinalized(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t1", gate_kind="review", request_id="r1",
        )
        second, _ = registry.register_or_coalesce(
            dedupe_key="k2", connector="c", tool="t2", gate_kind="review", request_id="r2",
        )
        assert registry.has_other_live(second.principal_id, second.id) is True
        assert registry.has_other_live(first.principal_id, first.id) is True

    def test_false_once_the_other_approval_is_finalized(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t1", gate_kind="review", request_id="r1",
        )
        second, _ = registry.register_or_coalesce(
            dedupe_key="k2", connector="c", tool="t2", gate_kind="review", request_id="r2",
        )
        registry.finalize(first.id, "accept")
        assert registry.has_other_live(second.principal_id, second.id) is False

    def test_another_principals_pending_approval_does_not_count(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t1", gate_kind="review", request_id="r1",
            )
        with principal_scope(Principal(id="bob")):
            bobs, _ = registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t2", gate_kind="review", request_id="r2",
            )
        assert registry.has_other_live("bob", bobs.id) is False


class TestAwaitStatus:
    def test_unknown_id_is_unknown(self):
        registry = make_registry()
        assert registry.await_status("nope") == "unknown"

    def test_unanswered_is_pending(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert registry.await_status(approval.id) == "pending"

    def test_finalized_accept_is_approved(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept")
        assert registry.await_status(approval.id) == "approved"

    def test_finalized_accept_all_is_approved(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "accept_all", "some_rule")
        assert registry.await_status(approval.id) == "approved"

    def test_finalized_deny_is_denied(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "deny")
        assert registry.await_status(approval.id) == "denied"

    def test_finalized_expired_is_expired(self):
        registry = make_registry(pending_ttl=0.01)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        time.sleep(0.02)
        registry.pop_expired_events()
        assert registry.await_status(approval.id) == "expired"

    def test_status_never_leaks_content(self):
        # Structural check on the contract: await_status's return type is a
        # plain str enum member, never the approval's own html/summary/etc.
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            summary="a very secret subject line",
        )
        registry.finalize(approval.id, "accept")
        status = registry.await_status(approval.id)
        assert status == "approved"
        assert "secret" not in status


class TestRegisterConfirm:
    def test_creates_a_confirm_kind_entry_with_no_dedupe_key(self):
        registry = make_registry()
        approval = registry.register_confirm()
        assert approval.kind == "confirm"
        assert approval.dedupe_key is None
        assert approval in registry.list_pending()

    def test_two_confirms_never_coalesce(self):
        registry = make_registry()
        first = registry.register_confirm()
        second = registry.register_confirm()
        assert first.id != second.id
        assert len(registry.list_pending()) == 2

    def test_does_not_count_against_the_pending_cap(self):
        registry = make_registry(max_pending=1)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        # Would raise if this were charged against the same cap as cards.
        registry.register_confirm()


class TestListPendingAndGet:
    def test_list_pending_excludes_answered_cards(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert approval in registry.list_pending()
        registry.answer(approval.id, "accept")
        assert approval not in registry.list_pending()

    def test_list_pending_is_newest_first(self):
        registry = make_registry()
        first, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        first.created_at -= 10  # force a deterministic ordering
        second, _ = registry.register_or_coalesce(
            dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
        )
        assert registry.list_pending() == [second, first]

    def test_get_returns_none_for_an_unknown_id(self):
        registry = make_registry()
        assert registry.get("nope") is None


class TestPrincipalDimension:
    """approvals.py's principal dimension (see approvals.py's own module
    docstring). Every approval defaults to LOCAL_PRINCIPAL_ID when nothing
    entered principal_scope() -- so every test above this class, none of
    which passes a principal_id anywhere, stays correct unchanged."""

    def test_default_principal_is_local(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert approval.principal_id == "local"

    def test_registration_stamps_the_current_principal(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert approval.principal_id == "alice"

    def test_two_principals_with_the_identical_dedupe_key_get_two_approvals(self):
        # The cross-principal dedupe-key collision (see
        # approvals.py's own module docstring) -- without the principal
        # dimension in _by_key, the second registration below would
        # coalesce onto the first instead of creating its own.
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            alice_approval, alice_created = registry.register_or_coalesce(
                dedupe_key="same-key", connector="c", tool="t", gate_kind="popup", request_id="r1",
            )
        with principal_scope(Principal(id="bob")):
            bob_approval, bob_created = registry.register_or_coalesce(
                dedupe_key="same-key", connector="c", tool="t", gate_kind="popup", request_id="r2",
            )
        assert alice_created and bob_created
        assert alice_approval.id != bob_approval.id

    def test_ledger_entry_is_not_shared_across_principals(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="same-key", connector="c", tool="t", gate_kind="popup", request_id="r1",
            )
            registry.finalize(approval.id, "accept")
            assert registry.consume_ledger("same-key") == LedgerHit(decision="accept", rule_name="", decided_at=approval.decided_at)
        with principal_scope(Principal(id="bob")):
            # Bob issuing the identical call must not see Alice's decision.
            assert registry.consume_ledger("same-key") is None

    def test_list_pending_filters_by_principal(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        with principal_scope(Principal(id="bob")):
            registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
            )
        assert len(registry.list_pending("alice")) == 1
        assert len(registry.list_pending("bob")) == 1
        assert len(registry.list_pending()) == 2  # no filter -- every principal's approvals

    def test_get_with_principal_id_rejects_a_foreign_approval(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert registry.get(approval.id, principal_id="bob") is None
        assert registry.get(approval.id, principal_id="alice") is approval
        assert registry.get(approval.id) is approval  # unfiltered

    def test_answer_with_principal_id_rejects_a_foreign_decision(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert registry.answer(approval.id, "accept", principal_id="bob") is False
        assert registry.answer(approval.id, "accept", principal_id="alice") is True

    def test_await_status_with_principal_id_hides_a_foreign_approval(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        assert registry.await_status(approval.id, principal_id="bob") == "unknown"
        assert registry.await_status(approval.id, principal_id="alice") == "pending"

    def test_reevaluate_all_only_touches_the_current_principals_own_cards(self):
        registry = make_registry()
        with principal_scope(Principal(id="alice")):
            alice_approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
                operation_key="op1",
            )
        with principal_scope(Principal(id="bob")):
            bob_approval, _ = registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
                operation_key="op1",
            )
            # Bob's rules now cover op1 -- must not touch Alice's own
            # pending card for the same operation_key under her rules.
            resolved = registry.reevaluate_all(lambda op, ctx: (True, "bobs-rule"))
        assert resolved == [bob_approval]
        assert not alice_approval.is_finalized()
        assert bob_approval.is_finalized()


class TestPerPrincipalApprovalCap:
    """One principal issuing a burst of distinct gated calls must not be able to
    fill the whole shared registry and lock every other principal out with
    TooManyPendingApprovalsError. max_pending_per_principal is the fix;
    max_pending (exercised by TestRegisterOrCoalesce above) stays in force
    unchanged as the secondary, whole-registry backstop."""

    def test_a_principal_hitting_their_own_cap_does_not_block_a_different_principal(self):
        registry = make_registry(max_pending=10, max_pending_per_principal=1)
        with principal_scope(Principal(id="alice")):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
            with pytest.raises(TooManyPendingApprovalsError):
                registry.register_or_coalesce(
                    dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
                )
        # Bob is a different principal -- Alice's own cap must not spill
        # over onto him, and there is plenty of headroom left in the
        # whole-registry cap.
        with principal_scope(Principal(id="bob")):
            approval, created = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r3",
            )
        assert created is True
        assert approval.principal_id == "bob"

    def test_per_principal_cap_message_names_the_per_principal_limit(self):
        registry = make_registry(max_pending=10, max_pending_per_principal=1)
        registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        with pytest.raises(TooManyPendingApprovalsError, match="pending for this principal"):
            registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
            )

    def test_a_finalized_approval_frees_the_principals_own_cap(self):
        registry = make_registry(max_pending=10, max_pending_per_principal=1)
        with principal_scope(Principal(id="alice")):
            first, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
            registry.finalize(first.id, "deny")
            # The cap only counts *live* (not-yet-finalized) approvals, same
            # rule the whole-registry cap already follows.
            second, created = registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
            )
        assert created is True
        assert second is not first

    def test_a_coalescing_hit_is_not_charged_against_the_principals_own_cap(self):
        registry = make_registry(max_pending=10, max_pending_per_principal=1)
        with principal_scope(Principal(id="alice")):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
            # Same key again -- coalesces onto the existing approval rather
            # than counting as a second live one against alice's own cap.
            _, created = registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r2",
            )
        assert created is False

    def test_whole_registry_cap_still_binds_across_multiple_principals_within_their_own_caps(self):
        # Each of alice and bob stays within their own per-principal cap,
        # but together they exceed the shared registry cap -- the secondary
        # backstop this plan item explicitly keeps in force.
        registry = make_registry(max_pending=2, max_pending_per_principal=5)
        with principal_scope(Principal(id="alice")):
            registry.register_or_coalesce(
                dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            )
        with principal_scope(Principal(id="bob")):
            registry.register_or_coalesce(
                dedupe_key="k2", connector="c", tool="t", gate_kind="review", request_id="r2",
            )
            with pytest.raises(TooManyPendingApprovalsError, match=r"already pending \(2\)"):
                registry.register_or_coalesce(
                    dedupe_key="k3", connector="c", tool="t", gate_kind="review", request_id="r3",
                )

    def test_default_per_principal_cap_is_lower_than_the_default_whole_registry_cap(self):
        # The whole point (see module docstring's DEFAULT_MAX_PENDING_PER_
        # PRINCIPAL comment): a single principal must never be able to
        # exhaust the shared registry cap alone.
        from privacyfence.approvals import DEFAULT_MAX_PENDING, DEFAULT_MAX_PENDING_PER_PRINCIPAL

        assert DEFAULT_MAX_PENDING_PER_PRINCIPAL < DEFAULT_MAX_PENDING


class TestBatchableKindsCoverAllApprovalKinds:
    """Mirrors web/routes_settings.py's own TestSensitiveActionsCoverAllAllowedActions:
    _BATCHABLE_KINDS/_NON_BATCHABLE_KINDS are both explicit sets, not one
    derived from the other, so a future PendingApproval.kind value that
    lands in ALL_APPROVAL_KINDS with no matching entry in either fails here
    instead of silently defaulting to either "batchable" or "not batchable"."""

    def test_the_two_sets_are_disjoint_and_cover_every_known_kind(self):
        assert _BATCHABLE_KINDS & _NON_BATCHABLE_KINDS == frozenset()
        assert _BATCHABLE_KINDS | _NON_BATCHABLE_KINDS == ALL_APPROVAL_KINDS


class TestIsBatchableAndBlockedReason:
    def test_a_plain_card_is_batchable_with_no_blocked_reason(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
        )
        assert approval.is_batchable() is True
        assert approval.blocked_reason() == ""

    def test_a_pii_forced_card_is_not_batchable(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            pii_forces_confirmation=True,
        )
        assert approval.is_batchable() is False
        assert "PII confirmation" in approval.blocked_reason()

    def test_a_confirm_dialog_is_not_batchable(self):
        registry = make_registry()
        approval = registry.register_confirm()
        assert approval.is_batchable() is False
        assert approval.blocked_reason() != ""

    def test_a_choice_dialog_is_not_batchable(self):
        registry = make_registry()
        approval = registry.register_confirm()
        approval.kind = "choice"
        assert approval.is_batchable() is False
        assert approval.blocked_reason() != ""

    def test_to_summary_dict_carries_the_batching_fields(self):
        registry = make_registry()
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="c", tool="t", gate_kind="review", request_id="r1",
            operation_key="gmail.read_message", pii_detected=True,
        )
        summary = approval.to_summary_dict()
        assert summary["operation_key"] == "gmail.read_message"
        assert summary["pii_detected"] is True
        assert summary["batchable"] is True
        assert summary["blocked_reason"] == ""

    def test_to_summary_dict_defaults_operation_key_to_empty_string(self):
        registry = make_registry()
        approval = registry.register_confirm()
        assert approval.to_summary_dict()["operation_key"] == ""
