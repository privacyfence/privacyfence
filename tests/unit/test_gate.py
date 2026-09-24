"""Unit tests for privacyfence.gate.gated_call — the single choke point every
tool call passes through (auto-accept check -> popup -> audit log).

These tests stub out the popup functions (``gate.show_popup``/``gate.
show_read_popup`` -- P10 deleted the native AppKit implementation behind
them, so they now delegate to whichever ``ApprovalUI`` is current, i.e.
``WebApprovalUI``) and the auto-accept decision (P9: ``gate._evaluate_auto_accept``
itself, monkeypatched directly -- see ``FakeEvaluator`` below) so the state
machine can be exercised deterministically, without spawning a real approval
surface. The one invariant that matters more than any individual branch:
gated_call must never return raw_data when filtered_data differs from it --
that's the actual privacy boundary.

This module is cross-checked against the
full gate/policy matrix (auto->allowed, review->Allow/Deny, review+PII->Proceed/
Cancel, popup/write->Allow/Deny, "Always allow"->proposed rule, matching/non-
matching rule, unattended allowed/forbidden) and covers
nearly all of it.
One matrix item is deliberately *not* asserted anywhere in this file: "policy
denial happens before connector execution." gated_call() itself never holds a
reference to a connector's provider client -- for a popup-gated write, the
calling connector method (e.g. GmailConnector._create_draft) always structures
its own code as ``await gated_call(...)`` followed by the real provider call,
so a raised denial (this file's own ``test_deny_raises_and_audits_rejected``
cases) already prevents that second line from ever running, by ordinary
Python control flow -- there is no separate flag or callback for this module
to intercept. The connector-side half of that proof (the provider client mock
asserted ``.assert_not_called()`` after a denial) lives in
``tests/unit/connectors/*.py`` instead, one assertion per write tool, since
that's the only layer that actually holds a reference to the client to make
the assertion against.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from privacyfence import approval_ui, auto_accept, gate
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import get_audit_logger, init_audit_logger
from privacyfence.pii_detector import init_pii_detection
from privacyfence.policy import compat as policy_compat
from privacyfence.policy import describe as policy_describe
from privacyfence.policy import propose as policy_propose
from privacyfence.policy import store as policy_store
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import TOOL_REGISTRY
from privacyfence.web_approval_ui import WebApprovalUI


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    """Poll ``predicate`` until it's true or ``timeout`` elapses.

    Used from a background thread to synchronize with state mutated by the
    event loop's thread, without an artificial fixed sleep -- ``time.sleep``
    releases the GIL, so the event-loop thread gets to make progress while
    this polls.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


async def wait_until_async(predicate, timeout=2.0, interval=0.005) -> bool:
    """``wait_until``'s counterpart for use directly in an async test body
    (the event-loop thread itself): P3's deferred protocol resolves a
    pending approval via a scheduled asyncio task (approvals._drive_
    interaction), not a background OS thread, so the poll here must
    ``await asyncio.sleep`` -- a plain ``time.sleep`` loop would block the
    only thread that task can ever run on, and the predicate would never
    become true."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class FakeEvaluator:
    """Test double for ``gate._evaluate_auto_accept`` -- P9 removed the
    ``AutoAcceptEvaluator`` object that used to get passed into it (and the
    ``get_auto_accept_evaluator()``/``policy.engine`` shadow-mode machinery
    that used to sit around it). This class no longer stands in for an
    evaluator gate.py *consults*; it now largely stands in *as*
    ``gate._evaluate_auto_accept`` itself, monkeypatched in directly:

        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))

    ``result`` is the same ``(bool, rule_name)`` shape v1's own
    ``should_auto_accept`` returned; ``__call__`` adapts it to
    ``_evaluate_auto_accept``'s real ``(operation_key, ctx) -> (bool, rule,
    rule_id)`` signature, reporting the same string for both `rule` and
    `rule_id` when it matches -- exactly what a real v2-store match does
    (see ``_evaluate_auto_accept``'s own docstring), so tests asserting on
    ``auto_accept_rule``/``rule_id`` together don't need two separate canned
    values for the ordinary case.
    """

    def __init__(self, result=(False, "")):
        self.result = result
        self.calls = []

    def __call__(self, operation_key, ctx):
        self.calls.append((operation_key, ctx))
        ok, rule = self.result
        return (ok, rule, rule if ok else "")


def install_rules(rules_config: dict) -> None:
    """Compile a v1-shaped ``{operation_key: [{"rule": name, "value": value}]}`` config into
    real v2 ``PolicyRule``s (v2 scope predicates keep v1's rule names -- ``policy/scopes.py``'s
    own docstring) and install them as the current principal's hot-reloaded rule set, exactly the
    way ``daemon_main.py``'s own startup migration does. Used by the handful of classes below that
    exercise real rule-matching rather than ``FakeEvaluator``'s canned verdict (P9: there's no
    more separate ``AutoAcceptEvaluator`` to construct for this)."""
    compiled = policy_compat.compile_rules(rules_config)
    auto_accept.set_policy_v2_store_rules(policy_store.merge_rules(compiled))


def _scope(predicate: str, *, connector: str | None = None, verb=None):
    """The real ``policy.propose.ProposableScope`` for ``predicate`` -- reused (not re-declared)
    so a canned "Always allow" test choice renders through the real ``policy.describe`` functions
    exactly like a genuine ``proposals_for()`` candidate would."""
    for entry in policy_propose.PROPOSABLE_SCOPES:
        if entry.predicate != predicate:
            continue
        if connector is not None and entry.connector != connector:
            continue
        if verb is not None and verb not in entry.verbs:
            continue
        return entry
    raise KeyError((predicate, connector, verb))


def make_proposal(predicate: str, value, tool: str, *, connector: str | None = None, widenings=()):
    """Build a real ``policy.propose.RuleProposal`` for a canned "Always allow" test choice, the
    same shape ``gate.py``'s own ``policy_propose.proposals_for()`` would offer for ``tool`` --
    ``verb``/``operation`` come from ``tool``'s own real registry entry (so the resulting
    ``PolicyRule`` always carries ``tool``'s real operation key, exactly like a genuine
    candidate would), while ``predicate``/``value`` are the ones this test wants to pretend
    matched."""
    entry = TOOL_REGISTRY[tool]
    scope = _scope(predicate, connector=connector, verb=entry.verb)
    return policy_propose.RuleProposal(
        scope=scope, value=value, verb=entry.verb, operation=entry.operation, widenings=widenings,
    )


def capture_added_rules(monkeypatch) -> list:
    """Patch ``gate.add_policy_v2_rules`` to record every call's rule list instead of writing to
    disk -- the "Always allow" flow's real persistence path (P9), replacing the old
    ``add_auto_accept_rule`` 3-tuple mock."""
    added: list = []
    monkeypatch.setattr(gate, "add_policy_v2_rules", lambda rules: added.append(list(rules)) or True)
    return added


def assert_single_rule(added: list, predicate: str, value, operation: str) -> None:
    assert len(added) == 1
    [rules] = added
    [rule] = rules
    assert rule.predicate == predicate
    assert rule.value == value
    assert rule.operations == frozenset({operation})


def capture_temp_accepts(monkeypatch) -> list:
    """Patch ``gate.register_temp_accept`` (a bare module-level function since P9, not an
    evaluator method) to record every call instead of arming the real grace window."""
    registered: list = []
    monkeypatch.setattr(gate, "register_temp_accept", lambda op, key: registered.append((op, key)))
    return registered


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


def read_audit_entries(audit_dir):
    from privacyfence.audit_log import current_week
    week_file = audit_dir / f"{current_week()}.jsonl"
    if not week_file.exists():
        return []
    return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]


RAW = object()      # sentinel: never returned
FILTERED = object()  # sentinel: always what gated_call must return on success


def base_kwargs(**overrides):
    kwargs = dict(
        connector="gmail",
        tool="gmail_get_message",
        tool_name="Read Gmail message",
        summary="from alice@example.com",
        sender="alice@example.com",
        raw_data=RAW,
        filtered_data=FILTERED,
        gate="review",
        preview={"from": "alice@example.com"},
        details_text="full body here",
        my_email="me@example.com",
    )
    kwargs.update(overrides)
    return kwargs


class TestAutoAcceptPath:
    async def test_auto_accepted_returns_filtered_data_without_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "deny", None))

        result = await gate.gated_call(**base_kwargs())

        assert result is FILTERED
        assert called == []  # popup never shown

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "i_am_sender"

    async def test_auto_accept_evaluated_against_raw_not_filtered_data(self, monkeypatch, audit_dir):
        evaluator = FakeEvaluator((True, "some_rule"))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", evaluator)

        await gate.gated_call(**base_kwargs())

        assert len(evaluator.calls) == 1
        _, ctx = evaluator.calls[0]
        assert ctx.raw_data is RAW

    async def test_operation_key_uses_tool_to_operation_mapping(self, monkeypatch, audit_dir):
        evaluator = FakeEvaluator((True, "x"))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", evaluator)

        await gate.gated_call(**base_kwargs(connector="gmail", tool="gmail_get_message"))

        op_key, _ = evaluator.calls[0]
        assert op_key == "gmail.read_message"

    async def test_operation_key_falls_back_to_connector_dot_tool(self, monkeypatch, audit_dir):
        # Deliberately not a key in TOOL_TO_OPERATION, so this only exercises
        # the f"{connector}.{tool}" fallback formula, independent of however
        # many tools that mapping table grows to cover over time.
        evaluator = FakeEvaluator((True, "x"))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", evaluator)

        await gate.gated_call(**base_kwargs(connector="widget", tool="widget_do_thing"))

        op_key, _ = evaluator.calls[0]
        assert op_key == "widget.widget_do_thing"


class TestPolicyV2StoreRules:
    """P9: gate._evaluate_auto_accept reads the on-disk v2 auto_accept: section
    (auto_accept.get_policy_v2_store_rules(), refreshed by settings_controller.add_policy_rule and
    at daemon startup) unconditionally -- there is no more v1 evaluator and no policy.engine
    switch left to gate this behind (P3-P8's shadow-mode dual-evaluation, which used to run
    alongside a separate v1 AutoAcceptEvaluator and log a WARNING on disagreement, is gone
    entirely -- see auto_accept.py's own module docstring)."""

    def _install(self, monkeypatch, rules):
        monkeypatch.setattr(gate, "get_policy_v2_store_rules", lambda: rules)

    async def test_matching_v2_store_rule_auto_accepts(self, monkeypatch, audit_dir):
        # gmail.anything (P6) is unconditional (like always_allow), so it needs no matching args --
        # what's under test here is that the v2-store layer is consulted at all, not any one
        # predicate's own matching logic (that's scopes.py's own test suite's job).
        self._install(monkeypatch, [PolicyRule(
            id="r-gmail-configure", predicate="gmail.anything", value=None,
            operations=frozenset({"gmail.read_message"}),
        )])

        result = await gate.gated_call(**base_kwargs())

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == "r-gmail-configure"
        # P8: a v2-store rule's own `.id` IS the canonical rule_id, trusted directly rather than
        # recomputed -- see gate._evaluate_auto_accept's own comment on this branch.
        assert entries[0]["rule_id"] == "r-gmail-configure"

    async def test_non_matching_v2_store_rule_falls_through_to_the_popup(self, monkeypatch, audit_dir):
        self._install(monkeypatch, [PolicyRule(
            id="r-other-op", predicate="always_allow", value=None,
            operations=frozenset({"drive.read_file_contents"}),  # not this call's own operation key
        )])
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert popup_calls == [1]  # fell through to the popup -- the v2-store rule never matched

    async def test_evaluation_error_is_swallowed_not_propagated(self, caplog, monkeypatch, audit_dir):
        # A raise from policy_engine.find_matching_rule itself (an unusual selector bug, say) must
        # fail closed to "no match", not crash the call -- see _evaluate_auto_accept's own
        # try/except around that call.
        def _raise(rules, operation_key, ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr(gate.policy_engine, "find_matching_rule", _raise)
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        with caplog.at_level("WARNING", logger="privacyfence.gate"):
            result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED  # fell through to the popup -- the raise never reached gated_call
        assert popup_calls == [1]
        assert any("Policy evaluation raised" in r.message for r in caplog.records)


class TestRuleIdAttribution:
    """P8/P9 (rule attribution and staleness): AuditEntry.rule_id, resolving F9 -- a decision in
    the audit log attributes to exactly one on-disk rule row, never an ambiguous rule name. P9
    retired the v1/v2 shadow comparison this class used to test (there is only ever one rule
    source now, so "agreement" is no longer a meaningful question): every real v2-store match
    always carries a rule_id. These tests exercise the two shapes that remain -- a genuine
    store-rule match (rule_id == auto_accept_rule == the matched rule's own canonical id) and the
    temp-accept pseudo-match (no rule row at all) -- plus the two in-branch race-recheck call
    sites that also have to thread rule_id through correctly.
    """

    async def test_matched_v2_store_rule_gets_its_own_canonical_id(self, monkeypatch, audit_dir):
        install_rules({"gmail.read_message": [{"rule": "always_allow"}]})

        result = await gate.gated_call(**base_kwargs())

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        expected_id = policy_store.rule_id_for("always_allow", None, ())
        assert entries[0]["auto_accept_rule"] == expected_id
        assert entries[0]["rule_id"] == expected_id

    async def test_temp_accept_grace_window_gets_no_rule_id(self, monkeypatch, audit_dir):
        # session_temp_accept is a session-scoped pseudo-match, never a stored rule row.
        install_rules({})
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        sheets_args = {"spreadsheet_id": "sheet-1", "range_a1": "A1:B2", "values": [["x"]]}
        await gate.gated_call(
            **base_kwargs(gate="popup", connector="drive", tool="drive_sheets_write_range", args=sheets_args),
        )
        await gate.gated_call(
            **base_kwargs(gate="popup", connector="drive", tool="drive_sheets_write_range", args=sheets_args),
        )

        entries = read_audit_entries(audit_dir)
        auto_accepted = [e for e in entries if e["decision"] == "auto_accepted"]
        assert len(auto_accepted) == 1
        assert auto_accepted[0]["auto_accept_rule"] == "session_temp_accept"
        assert auto_accepted[0]["rule_id"] == ""

    async def test_review_gates_own_race_recheck_also_carries_a_rule_id(self, monkeypatch, audit_dir):
        # gated_call's review branch re-checks _evaluate_auto_accept a second time, right before
        # showing the popup, for a rule created by another concurrently-resolved approval in the
        # meantime (see that call site's own comment) -- exercised here by having the outer,
        # top-level check see an empty store and the in-branch recheck see the real (installed)
        # one, so this exercises that second call site's own rule_id threading specifically, not
        # just the first (outer) one every other test here reaches.
        install_rules({"gmail.read_message": [{"rule": "always_allow"}]})
        real_rules = auto_accept.get_policy_v2_store_rules()
        calls = []

        def flaky_rules():
            calls.append(1)
            return [] if len(calls) == 1 else real_rules

        monkeypatch.setattr(gate, "get_policy_v2_store_rules", flaky_rules)
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert popup_calls == []  # the second, in-branch check caught it before the popup ran
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["rule_id"] == policy_store.rule_id_for("always_allow", None, ())

    async def test_write_gates_own_race_recheck_also_carries_a_rule_id(self, monkeypatch, audit_dir):
        # The popup/write branch's own version of the review branch's re-check above.
        install_rules({"sheets.write_range": [{"rule": "always_allow"}]})
        real_rules = auto_accept.get_policy_v2_store_rules()
        calls = []

        def flaky_rules():
            calls.append(1)
            return [] if len(calls) == 1 else real_rules

        monkeypatch.setattr(gate, "get_policy_v2_store_rules", flaky_rules)
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range",
            args={"spreadsheet_id": "sheet-1", "range_a1": "A1:B2", "values": [["x"]]},
        ))

        assert result is FILTERED
        assert popup_calls == []  # the second, in-branch check caught it before the popup ran
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["rule_id"] == policy_store.rule_id_for("always_allow", None, ())


class TestReviewGateDecisions:
    async def test_deny_raises_and_audits_rejected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_plain_accept_returns_filtered_and_audits_approved(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""

    async def test_no_matching_grant_or_rule_falls_through_to_popup_with_evaluator_consulted(
        self, monkeypatch, audit_dir,
    ):
        # The mismatch counterpart to TestAutoAcceptPath's match case above.
        # Every other test in this class reaches the popup under the same
        # FakeEvaluator() default (False, "") -- proving that's correct
        # behavior, not just a fixture default nothing checks, needs its own
        # case: the evaluator must actually be consulted (not skipped) and
        # its "no match" must be the real reason the popup ran, not an
        # accident of gated_call's control flow.
        evaluator = FakeEvaluator((False, ""))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", evaluator)
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(
            gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None),
        )

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        # gated_call re-checks once more right before showing the popup (the
        # concurrent-approval coalescing race -- see TestCoalescing), so this
        # is >=1, not ==1; every call must still report the configured
        # mismatch, not get silently skipped.
        assert len(evaluator.calls) >= 1
        assert popup_calls == [1]  # ...and its "no match" is why the popup ran
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""

    async def test_show_read_popup_receives_one_choice_when_suggestion_exists(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("i_am_sender", None, "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["accept_all_choices"] = accept_all_choices
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["accept_all_choices"] == [("0", "if I'm sender")]

    async def test_show_read_popup_receives_no_choices_without_a_suggestion(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["accept_all_choices"] = accept_all_choices
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["accept_all_choices"] == []

    async def test_show_read_popup_receives_two_choices_for_a_multi_candidate_item(
        self, monkeypatch, audit_dir,
    ):
        # The multi-button window (issue #151): each matching candidate
        # becomes its own (index, short_label) entry, not a single
        # top-priority hint.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposals = [
            make_proposal("i_am_owner", None, "gmail_get_message"),
            make_proposal("approved_folder", ["f1"], "gmail_get_message"),
        ]
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: proposals)
        captured = {}

        def fake_show_read_popup(*args, **kwargs):
            captured["accept_all_choices"] = args[3]  # positional -- see gate.py's own call
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["accept_all_choices"] == [
            ("0", "if I own it"), ("1", "this folder"),
        ]


class TestDeliveryAuditField:
    """gated_call's own ``delivery`` kwarg (default "") reaches the audit
    entry unchanged --
    the fact that drive_download_file/gmail_download_attachment/
    confluence_download_attachment carry a delivery path is itself worth
    auditing, distinct from the ordinary accept/deny decision."""

    async def test_defaults_to_empty_string_for_an_ordinary_call(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["delivery"] == ""

    async def test_carries_through_on_approval(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review", delivery="inline_base64"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["delivery"] == "inline_base64"

    async def test_carries_through_on_auto_accept(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator(result=(True, "some_rule")))

        await gate.gated_call(**base_kwargs(gate="review", delivery="staged_link"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["delivery"] == "staged_link"

    async def test_carries_through_on_denial(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", delivery="staged_link"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["delivery"] == "staged_link"


class TestAcceptAll:
    async def test_accept_all_confirmed_creates_rule_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("trusted_sender_domain", ["example.com"], "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review", connector="gmail", tool="gmail_get_message"))

        assert result is FILTERED
        assert_single_rule(added, "trusted_sender_domain", ["example.com"], "gmail.read_message")

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "trusted_sender_domain"

    async def test_accept_all_without_suggestion_falls_back_to_plain_approve(self, monkeypatch, audit_dir):
        # Shouldn't happen against the real window (no Always-allow button
        # renders with zero choices) -- but a defensive "no matching
        # candidate" chosen_index must still degrade to a plain accept.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", None))
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_accept_all_cancelled_confirmation_still_returns_data_once_but_no_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("i_am_sender", None, "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: False)
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []  # no standing rule created
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # accepted once, not via a rule


class TestAcceptAllMultipleChoices:
    """When proposals_for() returns 2+ candidates for the same item
    (e.g. a Drive file you own that's also in an approved folder), the
    popup renders one "Always allow" button per candidate (issue #151) --
    which rule gets created is decided by *which button was clicked*
    (chosen_index, the popup's own return value), not a second chooser
    dialog shown after a single generic Always-allow click."""

    def _two_choices(self, monkeypatch):
        proposals = [
            make_proposal("i_am_owner", None, "gmail_get_message"),
            make_proposal("approved_folder", ["f1"], "gmail_get_message"),
        ]
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: proposals)

    async def test_second_candidate_clicked_creates_that_specific_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        self._two_choices(monkeypatch)
        # Index 1 -- the second button ("approved_folder"'s), not the first.
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 1))
        confirm_calls = []
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description, *, sensitive=False: confirm_calls.append(description) or True,
        )
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert len(confirm_calls) == 1  # the same single-item confirm dialog every candidate gets
        assert_single_rule(added, "approved_folder", ["f1"], "gmail.read_message")
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "approved_folder"

    async def test_first_candidate_clicked_creates_that_rule_instead(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        self._two_choices(monkeypatch)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert_single_rule(added, "i_am_owner", None, "gmail.read_message")

    async def test_multiple_choices_cancelled_confirmation_still_accepts_once_but_no_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        self._two_choices(monkeypatch)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 1))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: False)  # cancelled
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # accepted once, not via a rule

    async def test_out_of_range_chosen_index_degrades_to_plain_accept(self, monkeypatch, audit_dir):
        # Defensive bounds-check against the popup's own JS bridge --
        # shouldn't happen against the real button row, but must not raise.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        self._two_choices(monkeypatch)
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 5))
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_single_matching_candidate_still_uses_the_same_confirm_popup(
        self, monkeypatch, audit_dir,
    ):
        # Regression guard: exactly one candidate behaves identically to the
        # multi-candidate case above, just with only index 0 to click.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("i_am_sender", None, "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert_single_rule(added, "i_am_sender", None, "gmail.read_message")


class TestAcceptAllWrites:
    """The write-gate counterpart to TestAcceptAll -- gate.py's popup branch drives its own
    "Always allow" button from the same policy_propose.proposals_for()/rules_for_proposal()
    machinery the review branch already uses (P9: there's no longer a separate
    suggest_write_rule() table)."""

    async def test_accept_all_confirmed_creates_rule_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("label_name_allowlist", ["Newsletters"], "gmail_add_label", connector="gmail")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert result is FILTERED
        assert_single_rule(added, "label_name_allowlist", ["Newsletters"], "gmail.add_label")
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "label_name_allowlist"

    async def test_confirmation_uses_policy_describe_confirmation_text(self, monkeypatch, audit_dir):
        # policy/describe.py's rendering (rule -> sentence, tool coverage) replaced
        # auto_accept.py's old describe_rule()/describe_rule_change() hand-written tables -- this
        # exercises that gate.py's write-gate confirmation dialog goes through it, with the actual
        # chosen proposal, not some stale/ad-hoc text.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("approved_project_keys", ["PFQA"], "jira_create_issue")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        captured = {}

        def fake_confirm(description, *, sensitive=False):
            captured["description"] = description
            # Not marked sensitive, and that is the point: this dialog is a
            # second step inside a card whose own accept_all already took
            # both decide-time gates. Marking it would ask for a second
            # passkey tap on one decision -- see approvals.
            # PendingApprovalRegistry.register_confirm.
            captured["sensitive"] = sensitive
            return True

        monkeypatch.setattr(gate, "show_rule_confirmation_popup", fake_confirm)
        monkeypatch.setattr(gate, "add_policy_v2_rules", lambda rules: True)

        await gate.gated_call(**base_kwargs(gate="popup", connector="jira", tool="jira_create_issue"))

        assert captured["description"] == policy_describe.confirmation_text(proposal)
        assert captured["sensitive"] is False

    async def test_accept_all_without_suggestion_falls_back_to_plain_approve(self, monkeypatch, audit_dir):
        # gmail_send_message has no operation key at all -- even if the (real) popup somehow
        # returned "accept_all" (no Always-allow button ever renders with zero choices), there's
        # no proposal to act on, so this must behave like a plain accept.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", None))
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_send_message"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_accept_all_cancelled_confirmation_still_accepts_once_but_no_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("label_name_allowlist", ["Newsletters"], "gmail_add_label", connector="gmail")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: False)
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert result is FILTERED
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_drive_write_offers_approved_sandbox_folder_end_to_end(self, monkeypatch, audit_dir):
        # End-to-end through the real (unmocked) proposals_for, for one
        # of the 13 Drive/Sheets/Docs write operations that now share the
        # sandbox-folder suggestion.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_write_file_content",
            raw_data={"file": SimpleNamespace(parent_ids=["folder1"]), "content_preview": "x"},
        ))

        assert result is FILTERED
        assert_single_rule(added, "approved_sandbox_folder", ["folder1"], "drive.write_file")
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "approved_sandbox_folder"

    async def test_gmail_create_draft_offers_the_unconditional_always_allow_rule(self, monkeypatch, audit_dir):
        # End-to-end through the real (unmocked) proposals_for -- unlike
        # every other write suggestion, always_allow has no recipient/value
        # to scope it to, so this also exercises policy_describe's own
        # "no value" formatting for the confirmation popup.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        captured = {}
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description, *, sensitive=False: captured.setdefault("description", description) or True,
        )
        added = capture_added_rules(monkeypatch)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="gmail", tool="gmail_create_draft",
            args={"to": "anyone@example.com"},
        ))

        assert result is FILTERED
        assert_single_rule(added, "always_allow", None, "gmail.create_draft")
        expected_proposal = make_proposal("always_allow", None, "gmail_create_draft")
        assert captured["description"] == policy_describe.confirmation_text(expected_proposal)
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["auto_accept_rule"] == "always_allow"

    async def test_show_popup_receives_one_choice_when_suggestion_exists(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("label_name_allowlist", ["Newsletters"], "gmail_add_label", connector="gmail")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["accept_all_choices"] = accept_all_choices
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert captured["accept_all_choices"] == [("0", "this label")]

    async def test_show_popup_receives_no_choices_without_suggestion(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["accept_all_choices"] = accept_all_choices
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_create_draft"))

        assert captured["accept_all_choices"] == []

    async def test_show_popup_receives_the_short_rule_hint_for_the_suggestion(self, monkeypatch, audit_dir):
        # The write-gate counterpart to the review-gate's own equivalent
        # test above -- same policy_describe.button_label() derivation, off
        # a real proposals_for() candidate.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("label_name_allowlist", ["Newsletters"], "gmail_add_label", connector="gmail")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured["accept_all_choices"] = args[8]  # positional -- see gate.py's own call
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_add_label"))

        assert captured["accept_all_choices"] == [("0", "this label")]

    async def test_show_popup_receives_an_empty_hint_for_the_unconditional_always_allow_rule(
        self, monkeypatch, audit_dir,
    ):
        # gmail_create_draft's real suggestion is always_allow -- the one
        # rule with no category to name, so the button stays plain "Always
        # allow" even though a choice is still offered for it.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("always_allow", None, "gmail_create_draft")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured["accept_all_choices"] = args[8]  # positional -- see gate.py's own call
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(gate="popup", connector="gmail", tool="gmail_create_draft"))

        assert captured["accept_all_choices"] == [("0", "")]

    async def test_show_popup_receives_preview_tables_and_blocks_and_table_only(self, monkeypatch, audit_dir):
        # Regression: these three were threaded through show_read_popup
        # (the review-gate branch below) but never forwarded to show_popup
        # at all -- a write connector passing preview_tables/preview_blocks
        # (e.g. drive_sheets_write_range's own values table, jira_create_
        # issue's Description heading) had it silently dropped before ever
        # reaching the real approval window, even though gated_call()'s own
        # signature accepted the kwargs with no error.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["preview_tables"] = preview_tables
            captured["preview_blocks"] = preview_blocks
            captured["table_only"] = table_only
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        table = {"headers": ["A", "B"], "rows": [["1", "2"]]}
        blocks = [{"type": "heading", "label": "Description"}]

        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range",
            preview_tables=[table], preview_blocks=blocks, table_only=True,
        ))

        assert captured["preview_tables"] == [table]
        assert captured["preview_blocks"] == blocks
        assert captured["table_only"] is True

    async def test_pii_gate_never_applies_to_a_write_accept_all(self, monkeypatch, audit_dir):
        # Sanity check on the module docstring's own claim: the popup branch
        # never touches pii_categories/show_pii_confirmation_popup at all,
        # accept_all included -- there's no "possible PII flowed in from an
        # external source" to confirm on a write.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("label_name_allowlist", ["Newsletters"], "gmail_add_label", connector="gmail")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        monkeypatch.setattr(gate, "add_policy_v2_rules", lambda rules: True)

        def boom(*a, **k):
            raise AssertionError("show_pii_confirmation_popup must never be called for a write")

        monkeypatch.setattr(gate, "show_pii_confirmation_popup", boom)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="gmail", tool="gmail_add_label",
            details_text="His SSN is 123-45-6789 on file.",
        ))
        assert result is FILTERED


class TestPreflightAutoAccept:
    """gate.preflight_auto_accept() -- backs privacyfence_check_policy's matched_rule_id (P7),
    now reading the v2 store directly (P9: no more evaluator argument, no more v1/v2 shadow left
    to disagree -- see this module's TestPolicyV2StoreRules/TestRuleIdAttribution for the
    equivalent gated_call()-level coverage)."""

    def setup_method(self):
        auto_accept.set_policy_v2_store_rules([])

    def teardown_method(self):
        auto_accept.set_policy_v2_store_rules([])

    def test_no_configured_rule_is_requires_review_with_no_ids(self):
        verdict, matched_rule, matched_rule_id, _reason = gate.preflight_auto_accept(
            "gmail.read_message", {},
        )
        assert (verdict, matched_rule, matched_rule_id) == ("requires_review", "", "")

    def test_v1_args_only_match_reports_the_same_id_for_both_fields(self):
        install_rules({"gmail.create_draft": [{"rule": "to_is_myself"}]})
        verdict, matched_rule, matched_rule_id, _reason = gate.preflight_auto_accept(
            "gmail.create_draft", {"to": "me@example.com"}, "me@example.com",
        )
        assert verdict == "auto_accept"
        # P9: matched_rule/matched_rule_id are always the same value -- the matched rule's own
        # canonical, content-derived id (policy.store.rule_id_for), never a bare predicate name.
        expected_id = policy_store.rule_id_for("to_is_myself", None, ())
        assert matched_rule == expected_id
        assert matched_rule_id == expected_id

    def test_data_dependent_v1_rule_is_unknown_with_no_ids(self):
        # approved_folder needs the fetched file's parent_ids -- data-dependent, so preflight can
        # never resolve it from args alone.
        install_rules({"drive.read_file_contents": [{"rule": "approved_folder", "value": ["f1"]}]})
        verdict, matched_rule, matched_rule_id, _reason = gate.preflight_auto_accept(
            "drive.read_file_contents", {},
        )
        assert (verdict, matched_rule, matched_rule_id) == ("unknown", "", "")

    def test_store_only_rule_with_no_v1_counterpart_still_predicts_auto_accept(self):
        # apps_script.project (F5) has no v1 rule shape at all -- there was never a v1 evaluator
        # that could predict this operation key at all. The always-on v2-store layer is what
        # makes it predictable.
        auto_accept.set_policy_v2_store_rules([
            PolicyRule(
                id="r-apps-script", predicate="apps_script.project", value=["script1"],
                operations=frozenset({"apps_script.read_content"}),
            ),
        ])
        verdict, matched_rule, matched_rule_id, _reason = gate.preflight_auto_accept(
            "apps_script.read_content", {"script_id": "script1"},
        )
        assert verdict == "auto_accept"
        assert matched_rule == "r-apps-script"
        assert matched_rule_id == "r-apps-script"

    def test_store_layer_upgrades_requires_review_to_unknown_when_data_dependent(self):
        auto_accept.set_policy_v2_store_rules([
            PolicyRule(
                id="r-folder", predicate="approved_folder", value=["f1"],
                operations=frozenset({"apps_script.read_content"}),
            ),
        ])
        verdict, _matched_rule, matched_rule_id, _reason = gate.preflight_auto_accept(
            "apps_script.read_content", {},
        )
        assert verdict == "unknown"
        assert matched_rule_id == ""


class TestProposePolicyChange:
    """gate.propose_policy_change() -- the P7 bridge writer for the v2 auto_accept: section, and
    the sole write path since PSC-3 deleted its v1-shaped predecessor, propose_rule_change().
    Unchanged by P9 (it was already v2-native from an earlier phase)."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        self._config_path = tmp_path / "settings.yaml"
        self._config_path.write_text("auto_accept_rules: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(self._config_path))
        auto_accept.set_policy_v2_store_rules([])
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)

    def teardown_method(self):
        auto_accept.set_policy_v2_store_rules([])

    async def test_confirmed_add_persists_to_the_v2_section_not_v1(self, audit_dir):
        result = await gate.propose_policy_change(
            operation="add", reason="Trusting the sandbox folder.",
            group="drive.folder", value=["folder1"], verbs=["read", "download"],
        )
        assert result["confirmed"] is True
        assert result["changed"] is True
        text = self._config_path.read_text(encoding="utf-8")
        assert "auto_accept:" in text
        assert "approved_folder" in text
        assert "auto_accept_rules: {}" in text  # v1 section left untouched
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "policy_rule_changed_via_bridge_proposal"

    async def test_the_dialog_it_raises_is_marked_sensitive(self, monkeypatch):
        """The self-approval review's Phase 4. Nothing gated this dialog
        before it appeared -- an MCP client asked, no card was shown -- so
        confirming it is the whole of the gate on a rule that decides what
        auto-accepts in future. web/routes_approvals.py's decide route reads
        that flag off the ``PendingApproval`` and holds the confirm to the
        same two checks web/routes_settings.py holds a
        ``_SENSITIVE_ACTIONS`` name to; without it the dialog inherits
        ``webauthn_stepup.is_step_up_required``'s "a confirm is a second
        step inside a decision the caller's own card already gated", which
        is exactly what this call site is not."""
        seen = {}

        def confirm(description, *, sensitive=False):
            seen["sensitive"] = sensitive
            return True

        monkeypatch.setattr(gate, "show_rule_confirmation_popup", confirm)
        await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
        )
        assert seen["sensitive"] is True

    async def test_confirmed_add_is_visible_to_get_policy_v2_rules(self, audit_dir):
        await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
        )
        rules = auto_accept.get_policy_v2_rules()
        assert any(r.predicate == "approved_folder" and r.value == ["folder1"] for r in rules)

    async def test_add_with_an_ungoverned_verb_raises_before_any_popup(self):
        popup_calls = []
        with pytest.raises(ValueError, match="cannot govern"):
            await gate.propose_policy_change(
                operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["send"],
            )
        assert popup_calls == []

    async def test_add_with_no_real_verbs_raises(self):
        with pytest.raises(ValueError, match="verbs must include"):
            await gate.propose_policy_change(
                operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["not_a_verb"],
            )

    async def test_remove_of_an_unknown_rule_id_raises(self):
        with pytest.raises(ValueError, match="Unknown rule id"):
            await gate.propose_policy_change(operation="remove", reason="x", rule_id="r-does-not-exist")

    async def test_confirmed_remove_deletes_the_rule_and_audits(self, audit_dir):
        add_result = await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
        )
        rule_id = add_result["rule_ids"][0]

        result = await gate.propose_policy_change(operation="remove", reason="Cleaning up.", rule_id=rule_id)

        assert result["confirmed"] is True
        assert result["changed"] is True
        assert auto_accept.get_policy_v2_rules() == []
        entries = read_audit_entries(audit_dir)
        assert entries[-1]["decision"] == "policy_rule_removed_via_bridge_proposal"

    async def test_update_removes_the_old_rule_and_adds_the_new_one(self, audit_dir):
        add_result = await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
        )
        old_id = add_result["rule_ids"][0]

        update_result = await gate.propose_policy_change(
            operation="update", reason="Narrowing to a different folder.", rule_id=old_id,
            group="drive.folder", value=["folder2"], verbs=["read"],
        )

        assert update_result["confirmed"] is True
        rules = auto_accept.get_policy_v2_rules()
        assert [r.value for r in rules] == [["folder2"]]

    async def test_update_with_an_unknown_rule_id_raises_before_any_popup(self, monkeypatch):
        popup_calls = []
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: popup_calls.append(1) or True)
        with pytest.raises(ValueError, match="Unknown rule id"):
            await gate.propose_policy_change(
                operation="update", reason="x", rule_id="r-does-not-exist",
                group="drive.folder", value=["folder1"], verbs=["read"],
            )
        assert popup_calls == []

    async def test_declined_confirmation_raises_and_persists_nothing(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: False)
        with pytest.raises(RuntimeError, match="denied by user"):
            await gate.propose_policy_change(
                operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
            )
        assert auto_accept.get_policy_v2_rules() == []

    async def test_unattended_connection_denies_without_showing_a_popup(self, monkeypatch, audit_dir):
        popup_calls = []
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: popup_calls.append(1) or True)
        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.propose_policy_change(
                    operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
                )
        assert popup_calls == []
        assert auto_accept.get_policy_v2_rules() == []

    async def test_unknown_operation_raises(self):
        with pytest.raises(ValueError, match="Unknown operation"):
            await gate.propose_policy_change(operation="destroy", reason="x")

    async def test_re_adding_the_same_rule_audits_as_no_op(self, audit_dir):
        await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
        )
        result = await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
        )
        assert result["changed"] is False


class TestPopupGateWrites:
    async def test_accept_returns_filtered_and_audits_approved(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        read_popup_called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (read_popup_called.append(1) or "deny", None))
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        assert read_popup_called == []  # write gate never shows the read popup
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_deny_raises_and_audits_rejected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_matching_rule_auto_accepts_without_a_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "trusted_sender_domain")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"

    async def test_no_matching_rule_falls_through_to_popup_with_evaluator_consulted(
        self, monkeypatch, audit_dir,
    ):
        # The mismatch counterpart to test_matching_rule_auto_accepts_
        # without_a_popup above -- proves the popup is reached because the
        # evaluator was genuinely consulted and found no match, the same
        # property TestReviewGateDecisions asserts for the read side.
        evaluator = FakeEvaluator((False, ""))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", evaluator)
        popup_calls = []
        monkeypatch.setattr(
            gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None),
        )

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        # Same coalescing re-check as the review branch's counterpart above.
        assert len(evaluator.calls) >= 1
        assert popup_calls == [1]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["auto_accept_rule"] == ""

    async def test_write_gate_never_triggers_the_pii_confirmation_gate(self, monkeypatch, audit_dir):
        # Unlike the review (read) gate -- see TestPIIGate -- writes are
        # content Claude itself generated, not personal data flowing in from
        # an external source, so this gate's confirmation-dialog machinery
        # (pii_categories / show_pii_confirmation_popup / the audit log's
        # pii_detected field) never engages for a write. It's still scanned
        # for the separate, informational write_content_flags signal -- see
        # TestWriteContentFlags below -- which doesn't touch any of these.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["details"] = details
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert result is FILTERED
        assert confirm_calls == []
        assert "DE89370400440532013000" in captured["details"]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is False


class TestUploadPiiGate:
    """upload_pii_scan_text is the one deliberate exception to
    TestPopupGateWrites's "the write gate never triggers the PII
    confirmation gate" -- only ever set by drive_upload_file, since its
    payload can be external content Claude never read. When set, this
    behaves like the review-gate's own pii_categories: forces
    show_pii_confirmation_popup, overrides a matching auto-accept rule, and
    folds into the audit log's pii_detected field -- unlike
    write_content_flags, which stays informational-only regardless.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000, thanks."

    async def test_no_upload_pii_scan_text_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="drive_upload_file"))

        assert result is FILTERED
        assert confirm_calls == []

    async def test_clean_upload_pii_scan_text_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text="nothing sensitive here",
        ))

        assert result is FILTERED
        assert confirm_calls == []

    async def test_flagged_upload_pii_scan_text_forces_confirmation(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_declining_confirmation_denies_the_whole_upload(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(
                gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
            ))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_flagged_upload_pii_scan_text_sets_upload_forced_true_on_the_popup(
        self, monkeypatch, audit_dir,
    ):
        # v2's "write-forced" PII card (see approval_window_html.py's
        # _risk_section_html) needs to know this popup is about to force the
        # same second confirmation the read side gets -- show_popup's
        # upload_forced kwarg is how gate.py signals that.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
        ))

        assert captured["upload_forced"] is True

    async def test_clean_upload_pii_scan_text_leaves_upload_forced_false_on_the_popup(
        self, monkeypatch, audit_dir,
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(*args, **kwargs):
            captured.update(kwargs)
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text="nothing sensitive here",
        ))

        assert captured["upload_forced"] is False

    async def test_flagged_content_overrides_a_matching_auto_accept_rule(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "parent_folder_allowlist")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text=self.PII_TEXT,
        ))

        assert result is FILTERED
        assert popup_calls == [1]  # the popup was NOT skipped, despite auto_ok
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # not "auto_accepted"
        assert entries[0]["pii_detected"] is True

    async def test_matching_rule_without_flagged_content_still_auto_accepts_silently(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "parent_folder_allowlist")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file", upload_pii_scan_text="nothing sensitive here",
        ))

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["pii_detected"] is False

    async def test_write_content_flags_still_computed_independently(self, monkeypatch, audit_dir):
        # upload_pii_scan_text and details_text are scanned separately --
        # confirms adding the real gate didn't remove the existing
        # informational write_content_flags signal (TestWriteContentFlags).
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file",
            details_text=self.PII_TEXT, upload_pii_scan_text=self.PII_TEXT,
        ))

        assert captured["write_content_flags"] == ["IBAN (bank account number)"]


class TestRequestFingerprint:
    """seen_count: AuditLogger.recent_matches(connector, tool, summary),
    computed once per gated_call and forwarded to both popup functions."""

    async def test_first_time_request_has_zero_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 0

    async def test_repeated_approval_increments_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        # Two prior approvals of the exact same (connector, tool, summary).
        await gate.gated_call(**base_kwargs(gate="review"))
        await gate.gated_call(**base_kwargs(gate="review"))

        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 2

    async def test_different_summary_does_not_count_toward_seen_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review", summary="from bob@example.com"))

        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review", summary="from alice@example.com"))

        assert captured["seen_count"] == 0

    async def test_seen_count_forwarded_to_show_popup_too(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)
        await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["seen_count"] == 1

    async def test_rejected_prior_call_does_not_count(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review"))

        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["seen_count"] = seen_count
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["seen_count"] == 0


class TestWriteContentFlags:
    """The separate, informational-only signal computed for the popup
    (write) gate -- see gate.py's write_content_flags comment. Distinct
    from pii_categories (TestPIIGate): no confirmation gate, never touches
    AuditEntry.pii_detected."""

    async def test_flags_computed_from_details_and_forwarded_to_show_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert captured["write_content_flags"] == ["IBAN (bank account number)"]

    async def test_no_flags_when_content_has_nothing_flaggable(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft", details_text="See you at 3pm tomorrow.",
        ))

        assert captured["write_content_flags"] == []

    async def test_review_gate_call_succeeds_without_write_content_flags_kwarg(self, monkeypatch, audit_dir):
        # show_read_popup's signature has no write_content_flags param at
        # all (it's popup-gate only, unlike pii_categories/visibility,
        # which are read-gate signals) -- if gated_call's review branch
        # ever tried to pass it, this call would raise a TypeError.
        # Succeeding here is the assertion.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text="full body here",
        ))

        assert result is FILTERED

    async def test_flags_never_affect_pii_detected_audit_field(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_detected"] is False  # write_content_flags never feeds this field

    async def test_disabling_pii_detection_also_suppresses_write_content_flags(self, monkeypatch, audit_dir):
        # write_content_flags calls the same detect_pii_categories() entry
        # point, which already respects the menu-bar enable/disable toggle
        # -- no separate toggle needed for this signal.
        from privacyfence import pii_detector
        pii_detector._REGISTRY.get().enabled = False
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["write_content_flags"] = write_content_flags
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        await gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft",
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert captured["write_content_flags"] == []


class TestTempAccept:
    """The 5-minute, in-memory-only grace window that used to require a
    distinct "Allow for 5 min" popup button -- for the operations expected
    to be called repeatedly against the same file in quick succession
    (auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS), it's now armed as a side
    effect of a plain "accept" (Allow once) whenever a file_key resolves,
    with no separate choice offered. show_popup itself only ever returns
    'accept' or 'deny' now (see approval_window.py); gate.py is what decides
    whether an 'accept' also registers the grace window (auto_accept.
    register_temp_accept, a bare module-level function since P9, not an
    evaluator method).
    """

    SHEETS_ARGS = {"spreadsheet_id": "sheet-1", "range_a1": "A1:B2"}

    async def test_show_popup_receives_temp_accept_eligible_true_for_eligible_op_with_file_key(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["temp_accept_eligible"] = temp_accept_eligible
            return "deny", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
            ))

        assert captured["temp_accept_eligible"] is True

    async def test_show_popup_receives_temp_accept_eligible_false_for_ineligible_op(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["temp_accept_eligible"] = temp_accept_eligible
            return "deny", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["temp_accept_eligible"] is False

    async def test_show_popup_receives_temp_accept_eligible_false_when_file_key_missing(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["temp_accept_eligible"] = temp_accept_eligible
            return "deny", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="popup", connector="drive", tool="drive_sheets_write_range", args={"range_a1": "A1:B2"},
            ))

        assert captured["temp_accept_eligible"] is False

    async def test_accept_on_eligible_op_registers_temp_accept_and_audits(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        registered = capture_temp_accepts(monkeypatch)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))

        assert result is FILTERED
        assert registered == [("sheets.write_range", "sheet-1")]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_temp_session"
        assert entries[0]["auto_accept_rule"] == "session_temp_accept"

    async def test_second_write_to_same_file_auto_accepts_without_a_second_popup(
        self, monkeypatch, audit_dir
    ):
        # Real (unmocked) auto-accept evaluation and real (unmocked) temp-accept state -- an
        # empty v2 store never matches, so the only way the second call can auto-accept is the
        # first call's own real register_temp_accept()/is_temp_accepted() round trip.
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        result1 = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))
        result2 = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))

        assert result1 is FILTERED
        assert result2 is FILTERED
        assert len(popup_calls) == 1  # second call skipped the popup entirely

        entries = read_audit_entries(audit_dir)
        decisions = sorted(e["decision"] for e in entries)
        assert decisions == ["accepted_via_temp_session", "auto_accepted"]

    async def test_a_different_spreadsheet_still_shows_its_own_popup(self, monkeypatch, audit_dir):
        popup_calls = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
        ))
        await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range",
            args={"spreadsheet_id": "sheet-2", "range_a1": "A1:B2"},
        ))

        assert len(popup_calls) == 2

    async def test_accept_for_ineligible_op_audits_as_plain_approved(
        self, monkeypatch, audit_dir
    ):
        # No file_key resolves for an ineligible operation, so a plain
        # accept must never register a temp accept or use the
        # accepted_via_temp_session decision -- it's an ordinary approval.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"

    async def test_pii_shaped_content_does_not_gate_a_temp_accept(self, monkeypatch, audit_dir):
        # The write (popup) gate never scans for PII -- see TestPopupGateWrites
        # below -- so PII-shaped content in a temp-accept-eligible write must
        # register the temp accept exactly as any other content would, with
        # no confirmation popup and no "pii_detected" in the audit entry.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        registered = capture_temp_accepts(monkeypatch)
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(
            gate="popup", connector="drive", tool="drive_sheets_write_range", args=self.SHEETS_ARGS,
            details_text="Please wire the deposit to DE89370400440532013000.",
        ))

        assert result is FILTERED
        assert confirm_calls == []
        assert registered == [("sheets.write_range", "sheet-1")]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_temp_session"
        assert entries[0]["pii_detected"] is False


class TestPIIGate:
    """gate.py runs pii_detector.detect_pii_categories() over ``details``
    before the review (read) popup only -- see TestPopupGateWrites for the
    write gate, which never scans. A match forces a second, explicit
    confirmation dialog on top of the popup's own Allow once/Always allow --
    declining it is treated as a full deny, same as clicking Deny on the
    original popup.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000, thanks."

    async def test_read_popup_receives_detected_categories(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]

    async def test_read_popup_receives_empty_list_when_no_pii(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert captured["pii_categories"] == []

    async def test_no_pii_never_shows_confirmation_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda *a, **k: confirm_calls.append(1) or True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        assert confirm_calls == []

    async def test_pii_confirmed_returns_data_and_audits_pii_detected(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_pii_declined_denies_the_whole_request(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_non_pii_deny_audits_pii_detected_false(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_detected"] is False

    async def test_pii_confirmation_happens_before_accept_all_rule_confirmation(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("i_am_sender", None, "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        call_order = []
        monkeypatch.setattr(
            gate, "show_pii_confirmation_popup",
            lambda categories: call_order.append("pii") or True,
        )
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description, *, sensitive=False: call_order.append("rule") or True,
        )
        monkeypatch.setattr(gate, "add_policy_v2_rules", lambda rules: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        assert call_order == ["pii", "rule"]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "accepted_via_accept_all"
        assert entries[0]["pii_detected"] is True

    async def test_declining_pii_confirmation_on_accept_all_skips_rule_creation(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("i_am_sender", None, "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)
        rule_confirm_calls = []
        monkeypatch.setattr(
            gate, "show_rule_confirmation_popup",
            lambda description, *, sensitive=False: rule_confirm_calls.append(1) or True,
        )
        added = capture_added_rules(monkeypatch)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert rule_confirm_calls == []  # never reached: PII confirmation already denied
        assert added == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"

    async def test_pii_detection_overrides_a_matching_auto_accept_rule(self, monkeypatch, audit_dir):
        # Auto-accept rules are scoped to metadata (sender domain, folder,
        # "I am the organizer"), not content -- a rule that would otherwise
        # silently pass this through must still stop for human review when
        # the content itself contains likely PII.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        assert popup_calls == [1]  # the popup was NOT skipped, despite auto_ok
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"  # not "auto_accepted"
        assert entries[0]["auto_accept_rule"] == ""
        assert entries[0]["pii_detected"] is True

    async def test_pii_override_still_requires_its_own_confirmation_and_can_be_denied(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_detected"] is True

    async def test_matching_rule_without_pii_still_auto_accepts_silently(self, monkeypatch, audit_dir):
        # Confirms the override is specific to PII-flagged content -- an
        # otherwise-identical rule match with no PII in the content still
        # takes the silent fast path, exactly as before this feature existed.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        assert popup_calls == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["pii_detected"] is False


class TestPiiCategoriesAndMatchDetailsInAuditLog:
    """pii_categories is always recorded (category labels only -- the same
    thing the popup banner already shows). pii_match_details is the opt-in
    PII-refinement trial capture (pii_detection.audit_match_details), off by
    default: '' unless turned on, and even then only the literal/redacted
    matched text for an approved request -- a fixed placeholder, never the
    matched text, for anything else."""

    PII_TEXT = "Please wire the deposit to DE89370400440532013000, thanks."

    async def test_pii_categories_always_populated_regardless_of_trial_setting(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_categories"] == ["IBAN (bank account number)"]
        assert entries[0]["pii_match_details"] == ""  # trial setting is off by default

    async def test_no_pii_leaves_categories_and_details_empty(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_categories"] == []
        assert entries[0]["pii_match_details"] == ""

    async def test_approved_request_gets_redacted_match_text_when_trial_setting_on(self, monkeypatch, audit_dir):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        details = entries[0]["pii_match_details"]
        assert details.startswith("IBAN (bank account number): ")
        assert "DE89370400440532013000" not in details  # value-bearing category -- redacted, not literal
        assert "•" in details

    async def test_denied_request_gets_hidden_placeholder_not_matched_text_when_trial_setting_on(
        self, monkeypatch, audit_dir,
    ):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: False)

        with pytest.raises(RuntimeError, match="denied"):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "rejected"
        assert entries[0]["pii_match_details"] == "User confirmed: details hidden"
        assert "DE89370400440532013000" not in entries[0]["pii_match_details"]

    async def test_label_category_logs_literal_text_when_approved_and_trial_setting_on(self, monkeypatch, audit_dir):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text="Please confirm your salary before Friday.",
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_match_details"] == "Salary/compensation information: salary"

    async def test_auto_accepted_with_no_pii_leaves_match_details_empty_even_with_trial_setting_on(
        self, monkeypatch, audit_dir,
    ):
        init_pii_detection(True, audit_match_details=True)
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("deny", None))  # must never be called

        result = await gate.gated_call(**base_kwargs(gate="review", details_text="nothing sensitive here"))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["pii_match_details"] == ""


class TestPiiAlreadyReviewed:
    """pii_already_reviewed lets a caller that can prove nothing has
    touched this exact content since PrivacyFence's own last write to it
    (connectors/drive.py's own_write_revisions) skip the PII gate's *forced
    confirmation* -- not PII detection itself, and not the ordinary review
    popup when no auto-accept rule matches. See gate.py's module docstring
    ("A second, narrower exception...") for the full reasoning.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000."

    async def test_matching_rule_with_pii_already_reviewed_auto_accepts_silently(
        self, monkeypatch, audit_dir,
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "deny", None))

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text=self.PII_TEXT, pii_already_reviewed=True,
        ))

        assert result is FILTERED
        assert popup_calls == []  # no popup at all -- same fast path as the no-PII case
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        # Audit trail stays honest about what pii_detector actually found,
        # even though the confirmation step it would normally force here
        # was suppressed.
        assert entries[0]["pii_detected"] is True

    async def test_pii_already_reviewed_without_a_matching_rule_shows_the_ordinary_popup_unflagged(
        self, monkeypatch, audit_dir,
    ):
        # pii_already_reviewed only ever suppresses the *extra* PII
        # confirmation step, never the ordinary review popup itself: with no
        # auto-accept rule matching, the popup still appears -- just without
        # the PII banner or the second "Are you sure?" confirmation.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)
        confirm_calls = []
        monkeypatch.setattr(
            gate, "show_pii_confirmation_popup",
            lambda categories: confirm_calls.append(categories) or True,
        )

        result = await gate.gated_call(**base_kwargs(
            gate="review", details_text=self.PII_TEXT, pii_already_reviewed=True,
        ))

        assert result is FILTERED
        assert captured["pii_categories"] == []  # no PII banner shown
        assert confirm_calls == []  # no second confirmation forced
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"
        assert entries[0]["pii_detected"] is True

    async def test_pii_already_reviewed_defaults_to_false_and_changes_nothing(
        self, monkeypatch, audit_dir,
    ):
        # Confirms a caller that doesn't pass this parameter at all gets
        # exactly today's behavior -- the override is strictly opt-in.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))
        monkeypatch.setattr(gate, "show_pii_confirmation_popup", lambda categories: True)

        result = await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert result is FILTERED
        assert popup_calls == [1]  # still shown -- pii_already_reviewed defaults False
        entries = read_audit_entries(audit_dir)
        assert entries[0]["pii_detected"] is True

    async def test_pii_already_reviewed_has_no_effect_on_the_write_gate(self, monkeypatch, audit_dir):
        # gate="popup" never computes pii_categories at all (see gate.py's
        # module docstring) -- pii_already_reviewed has nothing to suppress
        # there, and must not accidentally weaken upload_pii_scan_text's own,
        # separate forced confirmation for drive_upload_file.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: ("accept", None))
        confirm_calls = []
        monkeypatch.setattr(
            gate, "show_pii_confirmation_popup",
            lambda categories: confirm_calls.append(categories) or True,
        )

        result = await gate.gated_call(**base_kwargs(
            gate="popup", tool="drive_upload_file",
            upload_pii_scan_text=self.PII_TEXT, pii_already_reviewed=True,
        ))

        assert result is FILTERED
        assert confirm_calls == [["IBAN (bank account number)"]]  # still forced


class TestPiiScanText:
    """``pii_scan_text`` lets a caller scan different text than what's shown
    in the popup (``details_text``) -- e.g. an email body without its From/To
    headers, which could otherwise flag PII found only in metadata the
    message itself doesn't actually contain.
    """

    PII_TEXT = "Please wire the deposit to DE89370400440532013000."

    async def test_pii_scan_text_overrides_details_text_for_detection(self, monkeypatch, audit_dir):
        # details_text (shown in the popup) has PII in the "headers", but the
        # caller-supplied pii_scan_text (the actual body) does not.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="review",
                details_text=f"From: {self.PII_TEXT}\n\nnothing sensitive in the body",
                pii_scan_text="nothing sensitive in the body",
            ))

        assert captured["pii_categories"] == []

    async def test_pii_scan_text_can_detect_pii_absent_from_details_text(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="review",
                details_text="nothing sensitive here",
                pii_scan_text=self.PII_TEXT,
            ))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]

    async def test_pii_scan_text_empty_string_skips_detection_even_if_details_has_pii(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(
                gate="review",
                details_text=self.PII_TEXT,
                pii_scan_text="",
            ))

        assert captured["pii_categories"] == []

    async def test_pii_scan_text_omitted_falls_back_to_details_text(self, monkeypatch, audit_dir):
        # No pii_scan_text passed at all -- same behavior as before this
        # parameter existed.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["pii_categories"] = pii_categories
            return "deny", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with pytest.raises(RuntimeError):
            await gate.gated_call(**base_kwargs(gate="review", details_text=self.PII_TEXT))

        assert captured["pii_categories"] == ["IBAN (bank account number)"]


class TestConcurrentApprovals:
    """_popup_lock is retired: "one dialog at a time" is obsolete, not
    preserved by some other
    mechanism -- several genuinely different gated calls now run their
    interactions concurrently, bounded only by _popup_executor's own worker
    count. This holds even with no deferred registry active (a plain
    native-only install), since the registry isn't what used to provide the
    serialization -- _popup_lock was."""

    async def test_different_requests_run_concurrently(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        concurrent = 0
        max_concurrent = 0

        def fake_show_read_popup(*a, **k):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.05)
            concurrent -= 1
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        results = await asyncio.gather(*[
            gate.gated_call(**base_kwargs(gate="review", tool=f"gmail_get_message_{i}"))
            for i in range(5)
        ])

        assert results == [FILTERED] * 5
        assert max_concurrent > 1


class TestCoalescing:
    """§6's "New coalescing case": two concurrent identical gated calls (the
    same connector/tool/args) become one pending approval, not two --
    exercised here with a real WebApprovalUI-backed registry, since
    coalescing is specifically the registry's job (gate.py has no more lock
    to serialize identical concurrent calls with, and doesn't try to)."""

    async def test_two_identical_concurrent_calls_share_one_card_and_decision(
        self, monkeypatch, audit_dir,
    ):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        async def _decide_once_pending():
            deadline = time.monotonic() + 2
            while not registry.list_pending() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            pending = registry.list_pending()
            assert len(pending) == 1  # one card, not two, for the identical request
            registry.answer(pending[0].id, "accept")

        # base_kwargs() carries no `args`, so both calls key identically.
        (result_a, result_b), _ = await asyncio.gather(
            asyncio.gather(
                gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message")),
                gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message")),
            ),
            _decide_once_pending(),
        )

        assert result_a is FILTERED
        assert result_b is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2  # each invocation still gets exactly one entry of its own
        assert sorted(e["decision"] for e in entries) == ["approved", "approved"]


class TestPendingApprovalCarriesPreview:
    """Phase 0 of the approval-binder plan: the ``preview`` dict gated_call()
    hands to show_popup()/show_read_popup() is now also stamped onto the
    PendingApproval itself, at registration time -- before any
    _popup_executor worker has ever run build_card_html for it. A future
    consumer (the binder's own read-only fragment endpoint) can disclose
    from it without waiting on that worker -- see approvals.PendingApproval.
    preview's own docstring, and test_approvals.py's own registry-level
    tests for the "known before html" ordering itself (deterministic there;
    racy to observe through a real, unsaturated _popup_executor, which is
    why this class doesn't try)."""

    async def test_review_gate_stamps_the_preview_dict_at_registration(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        task = asyncio.create_task(gate.gated_call(**base_kwargs(
            gate="review", preview={"from": "alice@example.com", "subject": "Q3 plan"},
        )))
        try:
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)
            approval = registry.list_pending()[0]
            assert approval.preview == {"from": "alice@example.com", "subject": "Q3 plan"}
        finally:
            # Always release the still-blocked worker, even if an assertion
            # above failed -- otherwise it's stuck on card.event.wait()
            # forever and the test process never exits.
            for pending in registry.list_pending():
                registry.answer(pending.id, "deny")
            with contextlib.suppress(RuntimeError):
                await task

    async def test_popup_gate_stamps_the_preview_dict_too(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        task = asyncio.create_task(gate.gated_call(**base_kwargs(
            gate="popup", tool="gmail_create_draft", preview={"to": "bob@example.com"},
        )))
        try:
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)
            approval = registry.list_pending()[0]
            assert approval.preview == {"to": "bob@example.com"}
        finally:
            for pending in registry.list_pending():
                registry.answer(pending.id, "deny")
            with contextlib.suppress(RuntimeError):
                await task

    async def test_no_preview_given_stamps_an_empty_dict_not_none(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        task = asyncio.create_task(gate.gated_call(**base_kwargs(gate="review", preview=None)))
        try:
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)
            approval = registry.list_pending()[0]
            assert approval.preview == {}
        finally:
            for pending in registry.list_pending():
                registry.answer(pending.id, "deny")
            with contextlib.suppress(RuntimeError):
                await task

    async def test_stamped_preview_never_carries_details_text_or_body_content(self, monkeypatch, audit_dir):
        # §1.5: "preview dicts carry metadata only... never body/content".
        # gated_call() never merges details_text/raw content into preview
        # before it reaches register_or_coalesce -- assert that directly
        # against gate.py's own call sites, not just against whatever a
        # particular test happens to pass in.
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        preview = {"from": "alice@example.com"}
        task = asyncio.create_task(gate.gated_call(**base_kwargs(
            gate="review", preview=preview, details_text="the full message body, never in preview",
        )))
        try:
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)
            approval = registry.list_pending()[0]
            assert approval.preview == preview
            assert "the full message body" not in json.dumps(approval.preview)
        finally:
            for pending in registry.list_pending():
                registry.answer(pending.id, "deny")
            with contextlib.suppress(RuntimeError):
                await task


class TestManyPendingApprovalsAreAllReviewable:
    """Phase 0 residual work: the popup executor must hold at least as many
    workers as the registry can have approvals live at once, or an approval
    past its worker count never gets its card HTML built at all -- 87c30cc's
    fix only covered for that with a placeholder page, it didn't remove the
    underlying stall (see gate.py's own _popup_executor comment)."""

    async def test_past_the_old_literal_eight_every_approval_still_gets_rendered(self, monkeypatch, audit_dir):
        from concurrent.futures import ThreadPoolExecutor

        n = 20  # past the old literal-8 worker count; approvals.DEFAULT_MAX_PENDING_PER_PRINCIPAL
        registry = PendingApprovalRegistry(
            # Deliberately NOT this file's usual hold_window=5.0/pending_ttl=5.0:
            # those are fine for a test that registers one approval, and
            # actively wrong for the only test that needs n of them alive at
            # the same instant.
            #
            # pending_ttl bounds how long an approval may sit un-answered,
            # and gate.gated_call() sweeps every lapsed one (_pop_registry_
            # expirations -> pop_expired_events, which finalizes them as
            # "expired" and sets their UI-step event, so they leave
            # list_pending()). That sweep runs *partway through* gated_call,
            # after its asyncio.to_thread PII/audit hops -- so on a runner
            # slow enough that the n calls stagger over more than pending_ttl,
            # the last ones to arrive expire the first ones' approvals before
            # the set is ever complete, and "n pending at once" stops being
            # reachable at all rather than merely being slow. Observed
            # directly: 14 of 20 swept at t+14s, leaving 6 pending forever.
            # 300s is simply longer than this test can take; production's own
            # default is 15 minutes.
            #
            # hold_window is the other half. Every registered call parks a
            # thread of asyncio.to_thread's *default* pool for the whole
            # window inside registry.wait_async() -- and that is the same
            # pool the calls that haven't registered yet need for their own
            # PII/audit hops. At 5.0 with fewer default workers than n
            # (min(32, cpu_count + 4): 7 on a 3-core macOS runner), the
            # already-registered calls starve the rest into exactly the
            # stagger above. Collapsing it to ~0 removes that self-inflicted
            # serialization: every call returns its "approval_pending" result
            # promptly and the approval stays live for _drive_interaction to
            # render. Nothing here is testing the hold window.
            hold_window=0.05, pending_ttl=300.0, ledger_ttl=300.0,
            max_pending=n, max_pending_per_principal=n,
        )
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        # Sized to the registry's own max_pending -- exactly the
        # relationship daemon_main.py's configure_popup_executor() call
        # establishes for the real executor -- so this proves the sizing
        # relationship itself, not just that a bigger pool happens to work.
        # (monkeypatch restores gate._popup_executor to whatever it was
        # before this test regardless of how the test exits.)
        test_executor = ThreadPoolExecutor(max_workers=registry.max_pending, thread_name_prefix="pf-popup-test")
        monkeypatch.setattr(gate, "_popup_executor", test_executor)

        tasks = [
            asyncio.create_task(gate.gated_call(**base_kwargs(gate="review", tool=f"gmail_get_message_{i}")))
            for i in range(n)
        ]
        try:
            # 10s each, not more: the whole test runs in ~0.2s, and the
            # two budgets together have to leave pyproject.toml's global
            # 30s pytest-timeout enough room to still report a *failed
            # assertion* rather than a SIGALRM landing mid-cleanup -- an
            # interrupted cleanup is how this test would leak the very
            # blocked worker thread it exists to reason about.
            assert await wait_until_async(lambda: len(registry.list_pending()) == n, timeout=10.0)
            # The actual regression: every one of these must have real card
            # HTML, not merely be registered and listed -- a worker-starved
            # approval sits at html == "" forever.
            assert await wait_until_async(lambda: all(a.html for a in registry.list_pending()), timeout=10.0)
        finally:
            # Order matters. Awaiting the gated_call tasks first is what
            # makes the deny below exhaustive: an approval is registered
            # from inside gated_call, so once every one of these has
            # returned, no further approval can appear -- whereas a single
            # deny-the-current-snapshot pass taken while they were still
            # arriving would miss whichever registered a moment later,
            # leaving its _run_in_popup_executor worker blocked on
            # card.event.wait() forever: a leaked non-daemon thread the
            # whole process hangs on at interpreter shutdown, long after
            # pytest has printed its result. They return promptly whatever
            # the asserts above did, because hold_window is ~0.
            await asyncio.gather(*tasks, return_exceptions=True)
            # Every worker still blocked is blocked on a card whose event is
            # unset, which is exactly what list_pending() returns -- so this
            # releases all of them, and none of the confirm-dialog follow-ups
            # that could register something new is reachable from "deny".
            for approval in registry.list_pending():
                registry.answer(approval.id, "deny")
            # wait=True rather than the usual fire-and-forget: it turns a
            # leaked worker into an ordinary test failure (a hang the global
            # pytest-timeout ends, here, with a traceback) instead of a
            # clean-looking run that wedges at interpreter exit.
            test_executor.shutdown(wait=True)


class TestDeferredApprovalProtocol:
    """A call that doesn't get a human decision within the registry's hold
    window returns a structured
    "approval_pending" result instead of continuing to block; a later,
    identical call finds the decision in the ledger and releases without a
    second prompt."""

    async def test_hold_window_elapsing_returns_pending_instead_of_blocking(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        # show_read_popup is deliberately left un-mocked here: the real
        # WebApprovalUI-backed implementation genuinely blocks on a
        # threading.Event until answered, and nothing in this test ever
        # answers it -- so this reliably stays pending past the hold window,
        # unlike a synchronous mock racing the clock.

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result["status"] == "approval_pending"
        assert result["approval_id"]
        assert "message" in result

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approval_pending"

        # Clean up the still-running background interaction.
        pending = registry.get(result["approval_id"])
        registry.answer(pending.id, "deny")
        await asyncio.sleep(0.02)

    async def test_pending_result_carries_the_configured_base_url(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        registry.set_base_url("http://localhost:8765")
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result["url"] == f"http://localhost:8765/approvals/{result['approval_id']}"
        registry.answer(registry.get(result["approval_id"]).id, "deny")
        await asyncio.sleep(0.02)

    async def test_reissued_identical_call_releases_from_the_ledger_without_a_second_popup(
        self, monkeypatch, audit_dir,
    ):
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        first = await gate.gated_call(**base_kwargs(gate="review"))
        assert first["status"] == "approval_pending"

        approval = registry.get(first["approval_id"])
        registry.answer(approval.id, "accept")
        assert await wait_until_async(lambda: approval.final_decision is not None, timeout=2.0)
        assert approval.final_decision == "accept"

        second = await gate.gated_call(**base_kwargs(gate="review"))

        assert second is FILTERED
        entries = read_audit_entries(audit_dir)
        decisions = [e["decision"] for e in entries]
        assert decisions == ["approval_pending", "approved"]
        # The release entry's decided_at is the human's real click, distinct
        # from this entry's own (later) write time -- §5.4.
        assert entries[1]["decided_at"]

    async def test_reissued_call_after_a_binder_decision_audits_with_the_batch_id(self, monkeypatch, audit_dir):
        # Phase 2 of the approval binder plan: a decision released through
        # answer_batch()'s decided_via/batch_id stamping (approvals.py)
        # survives finalize() -> consume_ledger() -> LedgerHit ->
        # gate.py's own audit() closure, all the way into the audit entry
        # that actually releases the re-issued call -- see gate.py's
        # module docstring.
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        first = await gate.gated_call(**base_kwargs(gate="review"))
        assert first["status"] == "approval_pending"

        approval = registry.get(first["approval_id"])
        assert registry.answer(approval.id, "accept", decided_via="binder", batch_id="batch-123") is True
        assert await wait_until_async(lambda: approval.final_decision is not None, timeout=2.0)

        second = await gate.gated_call(**base_kwargs(gate="review"))

        assert second is FILTERED
        entries = read_audit_entries(audit_dir)
        decisions = [e["decision"] for e in entries]
        assert decisions == ["approval_pending", "approved"]
        assert entries[1]["decided_via"] == "binder"
        assert entries[1]["batch_id"] == "batch-123"
        # The pending entry never carries binder provenance -- there was
        # no decision yet when it was written.
        assert entries[0]["decided_via"] == ""
        assert entries[0]["batch_id"] == ""

    async def test_write_gate_ledger_entry_is_single_use(self, monkeypatch, audit_dir):
        # D3: read decisions stay reusable within the ledger TTL; write
        # decisions don't -- a second identical write must re-gate, not
        # silently replay the first's approval.
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        first = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))
        assert first["status"] == "approval_pending"
        approval = registry.get(first["approval_id"])
        registry.answer(approval.id, "accept")
        assert await wait_until_async(lambda: approval.final_decision is not None, timeout=2.0)

        second = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))
        assert second is FILTERED  # ledger hit, no popup

        third = await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))
        assert third["status"] == "approval_pending"  # ledger already consumed -- re-gates

        # Clean up the third call's own still-running background interaction.
        registry.answer(registry.get(third["approval_id"]).id, "deny")
        await asyncio.sleep(0.02)


class TestAdaptiveHoldWindow:
    """Approval binder, Phase 4: without this, a sequential agent never
    fills the binder -- it stalls the full hold_window on call #1, relays
    that one link, and only issues call #2 once a human has already
    answered. Once this principal has one unfinalized approval outstanding,
    a later, distinct gated call collapses its own wait to zero instead of
    blocking the full window too."""

    async def test_second_distinct_call_returns_pending_immediately(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        first_task = asyncio.create_task(
            gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message_1"))
        )
        try:
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)

            started = time.monotonic()
            second = await gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message_2"))
            elapsed = time.monotonic() - started

            assert second["status"] == "approval_pending"
            # Nowhere near hold_window=5.0 -- proves the wait actually
            # collapsed rather than merely returning to check twice as fast.
            assert elapsed < 1.0
        finally:
            for pending in registry.list_pending():
                registry.answer(pending.id, "deny")
            with contextlib.suppress(RuntimeError):
                await first_task

    async def test_first_call_alone_still_holds_and_resolves_inline_when_a_human_is_quick(
        self, monkeypatch, audit_dir,
    ):
        registry = PendingApprovalRegistry(hold_window=2.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        async def _decide_once_pending():
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)
            registry.answer(registry.list_pending()[0].id, "accept")

        result, _ = await asyncio.gather(
            gate.gated_call(**base_kwargs(gate="review")), _decide_once_pending(),
        )

        # No other approval was ever pending, so adaptive_hold never
        # applies: the call holds long enough for the quick decision above
        # to land, and returns the real result rather than "pending".
        assert result is FILTERED

    async def test_adaptive_hold_false_keeps_the_full_window_for_a_second_call(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(
            hold_window=0.2, pending_ttl=5.0, ledger_ttl=5.0, adaptive_hold=False,
        )
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        first_task = asyncio.create_task(
            gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message_1"))
        )
        try:
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)

            started = time.monotonic()
            second = await gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message_2"))
            elapsed = time.monotonic() - started

            assert second["status"] == "approval_pending"
            assert elapsed >= 0.2
        finally:
            for pending in registry.list_pending():
                registry.answer(pending.id, "deny")
            with contextlib.suppress(RuntimeError):
                await first_task


class TestPendingResultPointsAtTheBinder:
    """Approval binder, Phase 4: _pending_result() gains pending_count and
    binder_url, and the message asks Claude to batch outstanding approvals
    through privacyfence_await_approval instead of relaying one link at a
    time -- but only once there's actually more than one to batch."""

    async def test_solo_pending_result_carries_a_count_of_one_and_the_original_message(
        self, monkeypatch, audit_dir,
    ):
        registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=5.0, ledger_ttl=5.0)
        registry.set_base_url("http://localhost:8765")
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result["pending_count"] == 1
        assert result["binder_url"] == "http://localhost:8765/approvals"
        assert "url so they can" in result["message"]  # unchanged single-approval wording

        registry.answer(registry.get(result["approval_id"]).id, "deny")
        await asyncio.sleep(0.02)

    async def test_batched_pending_result_names_the_binder(self, monkeypatch, audit_dir):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        registry.set_base_url("http://localhost:8765")
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        first_task = asyncio.create_task(
            gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message_1"))
        )
        try:
            assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)

            second = await gate.gated_call(**base_kwargs(gate="review", tool="gmail_get_message_2"))

            assert second["pending_count"] == 2
            assert second["binder_url"] == "http://localhost:8765/approvals"
            assert "http://localhost:8765/approvals" in second["message"]
            assert "privacyfence_await_approval once" in second["message"]
        finally:
            for pending in registry.list_pending():
                registry.answer(pending.id, "deny")
            with contextlib.suppress(RuntimeError):
                await first_task


class TestApprovedObjectTypesNeverPopsUp:
    """Regression/repro for a QA discrepancy that couldn't be resolved from
    the audit log alone: the operator reported seeing a live approval popup
    for a Salesforce Account read (salesforce_get_record), while the audit
    log said "auto_accepted" for that same call -- a genuine contradiction,
    since gated_call's own logic makes the two mutually exclusive: the popup
    functions are never invoked once _evaluate_auto_accept() has already
    returned True with no PII detected. This drives real v2 rules configured
    the way the Salesforce connector's approved_object_types rule is meant
    to be used, args shaped exactly like connectors/salesforce.py::_get_record
    builds them, to lock in that invariant -- if this ever starts failing,
    that's the actual bug; if it keeps passing, a future recurrence of the
    live discrepancy is a config or observation issue (e.g. the popup
    belonged to a different call), not a gate.py bug.
    """

    async def test_approved_object_type_read_never_shows_a_popup(self, monkeypatch, audit_dir):
        install_rules({
            "salesforce.read_record": [{"rule": "approved_object_types", "value": ["Account"]}],
        })

        def fail_if_called(*a, **k):
            raise AssertionError("show_read_popup must not be called when the object type is auto-accepted")

        monkeypatch.setattr(gate, "show_read_popup", fail_if_called)

        result = await gate.gated_call(**base_kwargs(
            connector="salesforce", tool="salesforce_get_record", gate="review",
            args={"object_type": "Account", "record_id": "001xx0000012345"},
        ))

        assert result is FILTERED
        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["auto_accept_rule"] == policy_store.rule_id_for(
            "approved_object_types", ["Account"], (),
        )

    async def test_object_type_outside_allowlist_still_shows_the_popup(self, monkeypatch, audit_dir):
        # Contrast case: Opportunity isn't in the allowlist, so it must take
        # the normal interactive path -- proving the guard above is actually
        # meaningful (it can be reached) and not vacuously always-skipped.
        install_rules({
            "salesforce.read_record": [{"rule": "approved_object_types", "value": ["Account"]}],
        })
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        popup_calls = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popup_calls.append(1) or "accept", None))

        result = await gate.gated_call(**base_kwargs(
            connector="salesforce", tool="salesforce_get_record", gate="review",
            args={"object_type": "Opportunity", "record_id": "006xx"},
        ))

        assert result is FILTERED
        assert popup_calls == [1]
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "approved"


class TestRequestId:
    async def test_decision_entries_carry_a_non_empty_request_id(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))

        await gate.gated_call(**base_kwargs())

        entries = read_audit_entries(audit_dir)
        assert entries[0]["request_id"]

    async def test_each_call_gets_a_distinct_request_id(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))

        await gate.gated_call(**base_kwargs())
        await gate.gated_call(**base_kwargs())

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 2
        assert entries[0]["request_id"] != entries[1]["request_id"]


class TestAuditGapSafety:
    """Regression for a real audit-log gap found during QA: a call that
    visibly ran to completion (real data returned, the user saw and
    completed the approval flow) left zero matching entries in the log.
    gated_call now guarantees a decision entry on every exit path, including
    one triggered by an exception from code nobody expected to fail (e.g. a
    native popup call itself raising) -- see the `finally` block in
    gated_call.
    """

    async def test_unexpected_exception_in_review_gate_still_leaves_an_audit_entry(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])

        def boom(*a, **k):
            raise RuntimeError("native popup crashed")

        monkeypatch.setattr(gate, "show_read_popup", boom)

        with pytest.raises(RuntimeError, match="native popup crashed"):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "error"
        assert entries[0]["request_id"]

    async def test_unexpected_exception_in_popup_gate_still_leaves_an_audit_entry(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())

        def boom(*a, **k):
            raise RuntimeError("native popup crashed")

        monkeypatch.setattr(gate, "show_popup", boom)

        with pytest.raises(RuntimeError, match="native popup crashed"):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "error"

    async def test_exception_while_persisting_an_accept_all_rule_still_audits(
        self, monkeypatch, audit_dir
    ):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        proposal = make_proposal("i_am_sender", None, "gmail_get_message")
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [proposal])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept_all", 0))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)

        def boom(*a, **k):
            raise OSError("rules file write failed")

        monkeypatch.setattr(gate, "add_policy_v2_rules", boom)

        with pytest.raises(OSError, match="rules file write failed"):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[0]["decision"] == "error"

    async def test_normal_decision_paths_are_not_double_audited(self, monkeypatch, audit_dir):
        # The finally-block safety net must not add a second entry on top of
        # a normal decision.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review"))

        assert len(read_audit_entries(audit_dir)) == 1


class TestUnattendedMode:
    """gate.is_unattended()/unattended_scope() back the fail-fast path for
    scheduled/unattended Cowork tasks: web/mcp_dispatch.py's McpDispatcher.
    call() wraps a request in unattended_scope(True) when its session
    called privacyfence_begin_unattended_session(). See docs/TECHNICAL_
    REFERENCE.md's "Scheduled / unattended Cowork tasks" section.

    The one invariant that matters more than any individual branch: this
    must never change what auto-accepts -- only what happens when nothing
    does (denies fast instead of opening a popup nobody will answer).
    """

    @pytest.fixture(autouse=True)
    def _reset_unattended_flag(self):
        # unattended_scope always resets on its own __exit__, but guard
        # against a test raising before reaching that point and leaking the
        # flag into a later, unrelated test.
        token = gate._unattended_ctx.set(False)
        yield
        gate._unattended_ctx.reset(token)

    def test_is_unattended_defaults_false(self):
        assert gate.is_unattended() is False

    def test_unattended_scope_sets_and_resets(self):
        assert gate.is_unattended() is False
        with gate.unattended_scope(True):
            assert gate.is_unattended() is True
        assert gate.is_unattended() is False

    def test_unattended_scope_restores_prior_value_not_just_false(self):
        with gate.unattended_scope(True):
            with gate.unattended_scope(False):
                assert gate.is_unattended() is False
            assert gate.is_unattended() is True

    async def test_review_gate_denies_without_popup_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "accept", None))

        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="review"))

        assert called == []  # popup never shown
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"

    async def test_popup_gate_denies_without_popup_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        called = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (called.append(a) or "accept", None))

        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="popup"))

        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"

    async def test_matching_rule_still_auto_accepts_silently_even_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "deny", None))

        with gate.unattended_scope(True):
            result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"

    async def test_matching_temp_accept_still_auto_accepts_on_writes_when_unattended(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "session_temp_accept")))
        called = []
        monkeypatch.setattr(gate, "show_popup", lambda *a, **k: (called.append(a) or "deny", None))

        with gate.unattended_scope(True):
            result = await gate.gated_call(**base_kwargs(gate="popup"))

        assert result is FILTERED
        assert called == []

    async def test_rule_matched_but_pii_detected_still_denies_unattended(self, monkeypatch, audit_dir):
        # A matching rule alone isn't enough once the PII gate fires -- see
        # gate.py's module docstring on how PII overrides a matching rule.
        # Unattended mode must deny this exactly like the no-match case.
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "trusted_sender_domain")))
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "accept", None))

        pii_text = "Please wire the deposit to DE89370400440532013000, thanks."
        with gate.unattended_scope(True):
            with pytest.raises(RuntimeError, match="unattended session"):
                await gate.gated_call(**base_kwargs(gate="review", details_text=pii_text))

        assert called == []
        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "denied_unattended"
        assert entries[0]["pii_detected"] is True

    async def test_not_unattended_still_shows_popup_as_before(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        called = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (called.append(a) or "accept", None))

        result = await gate.gated_call(**base_kwargs(gate="review"))

        assert result is FILTERED
        assert len(called) == 1


class TestClaudeReason:
    """The mandatory "reason" ToolSpec param, carried the same way
    is_unattended() is: a contextvar set by web/mcp_dispatch.py's
    McpDispatcher.call(), read internally by gated_call() via
    current_reason() -- no caller passes it as an explicit kwarg."""

    async def test_reason_scope_value_reaches_the_audit_entry(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        with gate.reason_scope("Summarizing the Q3 budget for the user."):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == "Summarizing the Q3 budget for the user."

    async def test_no_reason_scope_defaults_to_empty_string(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == ""

    async def test_reason_forwarded_to_show_read_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        captured = {}

        def fake_show_read_popup(title, preview, details, accept_all_choices, pii_categories=None, visibility=None, claude_reason="", seen_count=0, content_kind="generic", pdf_bytes=b"", connector="", preview_bytes=b"", preview_mime_type="", new_info=None, preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow"):
            captured["claude_reason"] = claude_reason
            return "accept", None

        monkeypatch.setattr(gate, "show_read_popup", fake_show_read_popup)

        with gate.reason_scope("Checking for calendar conflicts."):
            await gate.gated_call(**base_kwargs(gate="review"))

        assert captured["claude_reason"] == "Checking for calendar conflicts."

    async def test_reason_forwarded_to_show_popup(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        captured = {}

        def fake_show_popup(title, preview, details, temp_accept_eligible=False, claude_reason="", write_content_flags=None, seen_count=0, connector="", accept_all_choices=None, preview_bytes=b"", preview_mime_type="", preview_tables=None, preview_blocks=None, table_only=False, upload_forced=False, layout="narrow", tool=""):
            captured["claude_reason"] = claude_reason
            return "accept", None

        monkeypatch.setattr(gate, "show_popup", fake_show_popup)

        with gate.reason_scope("Sending the confirmation the user asked for."):
            await gate.gated_call(**base_kwargs(gate="popup", tool="gmail_create_draft"))

        assert captured["claude_reason"] == "Sending the confirmation the user asked for."

    async def test_auto_accepted_call_still_records_reason(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "i_am_sender")))

        with gate.reason_scope("Reading my own sent mail."):
            await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["decision"] == "auto_accepted"
        assert entries[0]["claude_reason"] == "Reading my own sent mail."

    async def test_scope_does_not_leak_to_calls_outside_it(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator())
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: ("accept", None))

        with gate.reason_scope("Only for this one call."):
            pass  # scope already exited before gated_call runs
        await gate.gated_call(**base_kwargs(gate="review"))

        entries = read_audit_entries(audit_dir)
        assert entries[0]["claude_reason"] == ""


class TestDefaultDetails:
    def test_object_with_dict_is_json_dumped(self):
        class Obj:
            def __init__(self):
                self.sender = "alice@example.com"
                self.subject = "hi"

        out = gate._default_details(Obj())
        assert json.loads(out) == {"sender": "alice@example.com", "subject": "hi"}

    def test_plain_dict_is_json_dumped(self):
        out = gate._default_details({"a": 1, "b": [1, 2]})
        assert json.loads(out) == {"a": 1, "b": [1, 2]}

    def test_unserializable_falls_back_to_str(self):
        # json.dumps(..., default=str) succeeds for almost anything, so to
        # exercise the except-path we need attribute access itself to raise.
        class Weird:
            def __getattribute__(self, item):
                if item == "__dict__":
                    raise RuntimeError("boom")
                return object.__getattribute__(self, item)

            def __str__(self):
                return "weird-fallback"

        out = gate._default_details(Weird())
        assert out == "weird-fallback"


class TestPiiAndAuditWorkOffTheEventLoop:
    """detect_pii_categories/scan_pii_for_audit and AuditLogger.recent_matches
    used to run inline on
    gated_call's own coroutine -- synchronous, CPU-bound-ish work that
    blocked every other concurrently-dispatched request on the IPC server's
    single event loop for however long it took. Proven here the standard
    way: a slow stand-in for each, run concurrently with a ticker coroutine
    that must keep making progress throughout -- if the slow call still ran
    inline, the ticker would freeze for its whole duration instead.
    """

    @staticmethod
    async def _ticks_while(coro) -> list[float]:
        ticks: list[float] = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        ticker_task = asyncio.create_task(ticker())
        try:
            await coro
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task
        return ticks

    async def test_detect_pii_categories_does_not_block_concurrent_tasks(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "rule")))
        monkeypatch.setattr(gate, "detect_pii_categories", lambda text: time.sleep(0.15) or [])

        ticks = await self._ticks_while(gate.gated_call(**base_kwargs(gate="review")))

        # >5 ticks in 0.15s (a 0.01s ticker interval) means the event loop
        # kept running throughout -- inline, blocked for the whole sleep, it
        # would show at most one or two.
        assert len(ticks) > 5

    async def test_recent_matches_does_not_block_concurrent_tasks(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((True, "rule")))
        monkeypatch.setattr(
            get_audit_logger(), "recent_matches", lambda *a, **k: time.sleep(0.15) or 0
        )

        ticks = await self._ticks_while(gate.gated_call(**base_kwargs(gate="review")))

        assert len(ticks) > 5


class TestCancellation:
    """gated_call's own asyncio.CancelledError handling -- what runs when
    the daemon's IPC server cancels this coroutine's Task in response to a
    "cancel" request (ipc.py's module docstring): the request still owes
    exactly one audit entry, now decision="cancelled" instead of the
    generic "error" fallback.
    """

    @pytest.mark.timeout(5)  # TST-11: bounded by its own internal Event.wait(timeout=2.0)s, not the 30s suite default
    async def test_cancellation_while_waiting_on_the_popup_records_cancelled(self, monkeypatch, audit_dir):
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((False, "")))
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        release = threading.Event()
        # TST-11: started is
        # set by slow_popup itself, the actual event this test needs to
        # synchronize on -- a fixed sleep here was only ever guessing how
        # long _run_in_popup_executor takes to actually reach slow_popup.
        started = threading.Event()

        def slow_popup(*a, **k):
            started.set()
            release.wait(timeout=2.0)
            return ("deny", None)

        monkeypatch.setattr(gate, "show_read_popup", slow_popup)

        task = asyncio.create_task(gate.gated_call(**base_kwargs(gate="review")))
        assert await wait_until_async(started.is_set, timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()  # let the still-blocked popup thread finish; don't leak it

        entries = read_audit_entries(audit_dir)
        assert entries[-1]["decision"] == "cancelled"

    @pytest.mark.timeout(5)  # TST-11: bounded by its own internal Event.wait(timeout=2.0)s, not the 30s suite default
    async def test_cancellation_while_coalesced_onto_anothers_interaction_records_cancelled(
        self, monkeypatch, audit_dir,
    ):
        # P3 replacement for the old "cancelled while queued behind
        # _popup_lock" case: with the lock gone, the analogous "not the one
        # actually driving the interaction" scenario is a coalesced caller
        # (§6's "New coalescing case") -- cancelling it must not touch the
        # card the *other*, still-running caller is showing, and must still
        # leave exactly one audit entry.
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
        monkeypatch.setattr(gate, "_evaluate_auto_accept", FakeEvaluator((False, "")))
        monkeypatch.setattr(gate.policy_propose, "proposals_for", lambda *a, **k: [])
        # show_read_popup left un-mocked: the real WebApprovalUI-backed
        # implementation, which blocks until answered -- nothing in this
        # test ever answers it, so the driving call's own interaction never
        # completes either.

        driver = asyncio.create_task(gate.gated_call(**base_kwargs(gate="review")))
        assert await wait_until_async(lambda: bool(registry.list_pending()), timeout=2.0)

        # TST-11: a spy on
        # the real registry.wait_async -- called only after the coalesced
        # call has found the existing approval and started waiting on it --
        # replaces a fixed sleep that was only ever guessing when the event
        # loop would actually get around to running the freshly created
        # task that far.
        entered_wait = threading.Event()
        original_wait_async = registry.wait_async

        async def spy_wait_async(approval, timeout):
            entered_wait.set()
            return await original_wait_async(approval, timeout)

        monkeypatch.setattr(registry, "wait_async", spy_wait_async)

        coalesced = asyncio.create_task(gate.gated_call(**base_kwargs(gate="review")))
        assert await wait_until_async(entered_wait.is_set, timeout=2.0)
        coalesced.cancel()
        with pytest.raises(asyncio.CancelledError):
            await coalesced

        entries = read_audit_entries(audit_dir)
        assert len(entries) == 1
        assert entries[-1]["decision"] == "cancelled"
        assert len(registry.list_pending()) == 1  # the driving call's own card is untouched

        # Clean up the still-running driver.
        pending = registry.list_pending()[0]
        registry.answer(pending.id, "deny")
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await driver


class TestRunInPopupExecutor:
    """gate._run_in_popup_executor -- the dedicated single-thread executor
    every native dialog call runs on, instead of asyncio.to_thread's default
    pool shared with every connector's own blocking I/O.
    """

    async def test_runs_the_call_and_returns_its_result(self):
        def fn(x, *, y):
            return x, y

        result = await gate._run_in_popup_executor(fn, 5, y=9)

        assert result == (5, 9)

    async def test_runs_on_a_dedicated_thread_not_the_default_pool(self):
        seen = {}

        def fn():
            seen["thread"] = threading.current_thread().name

        await gate._run_in_popup_executor(fn)

        assert seen["thread"].startswith("pf-popup")

    @pytest.mark.timeout(5)  # TST-11: bounded by its own internal Event.wait(timeout=2.0)s, not the 30s suite default
    async def test_stays_prompt_while_the_default_to_thread_pool_is_saturated(self):
        # The scenario this executor exists for: a handful of slow
        # connector calls (a Slack rate-limit retry sleeping out
        # Retry-After, worst case) occupy every worker in the default
        # asyncio.to_thread pool. A popup dispatched at the same time must
        # not queue behind them.
        default_pool_size = min(32, (__import__("os").cpu_count() or 1) + 4)
        release = threading.Event()
        # TST-11: all_started
        # fires only once every occupier has actually begun running (not
        # merely been submitted to the pool) -- a fixed sleep here was only
        # ever guessing how long the default pool takes to schedule all of
        # them, on whatever machine happens to run this test.
        started_count = 0
        started_lock = threading.Lock()
        all_started = threading.Event()

        def occupy_a_worker():
            nonlocal started_count
            with started_lock:
                started_count += 1
                if started_count == default_pool_size:
                    all_started.set()
            release.wait(timeout=2.0)

        # asyncio.create_task (not a bare asyncio.to_thread(...) coroutine
        # object, which doesn't run at all until awaited/gathered) so every
        # occupier is actually scheduled now, not only once the finally
        # block below gets around to gathering them -- a real, pre-existing
        # gap in this test found while replacing its old fixed sleep:
        # without this, the occupiers never even started before the popup
        # dispatch below, so the "saturated pool" this test is meant to
        # prove against wasn't actually saturated yet.
        occupiers = [asyncio.create_task(asyncio.to_thread(occupy_a_worker)) for _ in range(default_pool_size)]
        assert await wait_until_async(all_started.is_set, timeout=2.0)

        def popup():
            return "still responsive"

        try:
            result = await asyncio.wait_for(gate._run_in_popup_executor(popup), timeout=1.0)
        finally:
            release.set()
            await asyncio.gather(*occupiers)

        assert result == "still responsive"

    async def test_exceptions_propagate(self):
        def fn():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await gate._run_in_popup_executor(fn)


class TestConfigurePopupExecutor:
    """gate.configure_popup_executor -- daemon_main.py's own hook for tying
    _popup_executor's worker count to the real PendingApprovalRegistry's
    max_pending (settings.yaml's web.approvals.max_pending can override the
    module-import-time default -- see _popup_executor's own comment)."""

    def _isolate(self, monkeypatch):
        # Swaps in a throwaway starting executor before calling
        # configure_popup_executor, so its own "shut down the old one"
        # behavior never touches the real, process-wide _popup_executor
        # every other test in this module (and this process) depends on.
        # monkeypatch.setattr's teardown restores both globals to their
        # real originals regardless of what configure_popup_executor does
        # to them meanwhile.
        from concurrent.futures import ThreadPoolExecutor

        throwaway = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pf-popup-test-throwaway")
        monkeypatch.setattr(gate, "_popup_executor", throwaway)
        monkeypatch.setattr(gate, "_popup_executor_max_workers", 1)
        return throwaway

    def test_resizes_to_the_given_worker_count(self, monkeypatch):
        self._isolate(monkeypatch)
        gate.configure_popup_executor(3)
        try:
            assert gate._popup_executor_max_workers == 3
            assert gate._popup_executor._max_workers == 3
        finally:
            gate._popup_executor.shutdown(wait=False)

    def test_is_a_no_op_when_the_size_already_matches(self, monkeypatch):
        throwaway = self._isolate(monkeypatch)
        gate.configure_popup_executor(1)  # matches the throwaway's own size
        assert gate._popup_executor is throwaway
        throwaway.shutdown(wait=False)

    async def test_the_new_executor_is_immediately_the_one_used(self, monkeypatch):
        self._isolate(monkeypatch)
        gate.configure_popup_executor(2)
        try:
            seen = {}

            def fn():
                seen["thread"] = threading.current_thread().name

            await gate._run_in_popup_executor(fn)

            assert seen["thread"].startswith("pf-popup")
        finally:
            gate._popup_executor.shutdown(wait=False)
