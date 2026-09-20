"""Tests for privacyfence.policy.engine (P3 of the policy v2 redesign): the v2 rule evaluator.

Equivalence against the old evaluator, on every predicate P2 built a fixture for, lives in
test_compat.py (it needs `policy.compat` to build the `PolicyRule`s in the first place). This
module tests `evaluate()`/`preflight()` mechanics that a per-predicate fixture corpus can't reach
on its own: multiple rules per operation, first-match ordering, `operations` filtering, fail-closed
handling of an unrecognised predicate, and the temp-accept delegation the redesign proposal's §09
files under this module ("evaluate(), preflight(), temp-accept window").
"""
from __future__ import annotations

from privacyfence.policy.engine import PolicyRule, evaluate, find_matching_rule, preflight

from ...helpers import make_ctx


class TestEvaluateOperationsFiltering:
    def test_rule_not_covering_this_operation_key_is_skipped(self):
        rule = PolicyRule(id="r1", predicate="always_allow", value=None, operations=frozenset({"other.op"}))
        ok, matched = evaluate([rule], "gmail.create_draft", make_ctx())
        assert ok is False
        assert matched == ""

    def test_rule_covering_this_operation_key_matches(self):
        rule = PolicyRule(id="r1", predicate="always_allow", value=None, operations=frozenset({"gmail.create_draft"}))
        ok, matched = evaluate([rule], "gmail.create_draft", make_ctx())
        assert (ok, matched) == (True, "r1")


class TestEvaluateFirstMatchWins:
    def test_first_matching_rule_in_order_is_reported(self):
        rules = [
            PolicyRule(id="never", predicate="i_am_owner", value=None, operations=frozenset({"drive.op"})),
            PolicyRule(id="always", predicate="always_allow", value=None, operations=frozenset({"drive.op"})),
            PolicyRule(id="also_always", predicate="always_allow", value=None, operations=frozenset({"drive.op"})),
        ]
        ok, matched = evaluate(rules, "drive.op", make_ctx(connector="drive"))
        assert (ok, matched) == (True, "always")

    def test_no_rule_matches_returns_false_and_empty_string(self):
        rules = [PolicyRule(id="never", predicate="i_am_owner", value=None, operations=frozenset({"drive.op"}))]
        ok, matched = evaluate(rules, "drive.op", make_ctx(connector="drive"))
        assert (ok, matched) == (False, "")


class TestEvaluateFailsClosed:
    def test_unrecognised_predicate_never_matches(self):
        rule = PolicyRule(id="r1", predicate="not_a_real_predicate", value=None, operations=frozenset({"op"}))
        ok, matched = evaluate([rule], "op", make_ctx())
        assert (ok, matched) == (False, "")

    def test_raising_selector_never_matches(self):
        # approved_object_types with a non-iterable value raises inside the selector -- must be
        # swallowed, not propagated, exactly as should_auto_accept's own try/except does.
        rule = PolicyRule(
            id="r1", predicate="approved_recipient_domain", value=object(), operations=frozenset({"op"}),
        )
        ok, matched = evaluate([rule], "op", make_ctx(args={"to": "someone@example.com"}))
        assert (ok, matched) == (False, "")

    def test_unrecognised_condition_never_holds(self):
        rule = PolicyRule(
            id="r1", predicate="always_allow", value=None, operations=frozenset({"op"}),
            conditions=(("not_a_real_condition", None),),
        )
        ok, matched = evaluate([rule], "op", make_ctx())
        assert (ok, matched) == (False, "")


class TestEvaluateConditionsNarrowScope:
    def test_condition_must_also_hold(self):
        rule = PolicyRule(
            id="r1", predicate="always_allow", value=None, operations=frozenset({"op"}),
            conditions=(("no_contact_info_change", None),),
        )
        ok, _ = evaluate([rule], "op", make_ctx(args={"emails": ["a@example.com"]}))
        assert ok is False
        ok, matched = evaluate([rule], "op", make_ctx(args={}))
        assert (ok, matched) == (True, "r1")


class TestEvaluateTempAccept:
    def test_falls_back_to_temp_accept_when_no_rule_matches(self):
        calls = []

        def is_temp_accepted(operation_key, file_key):
            calls.append((operation_key, file_key))
            return True

        ok, matched = evaluate([], "sheets.write_range", make_ctx(args={"spreadsheet_id": "abc"}),
                                is_temp_accepted=is_temp_accepted)
        assert (ok, matched) == (True, "session_temp_accept")
        assert calls == [("sheets.write_range", "abc")]

    def test_no_temp_accept_and_no_rule_is_not_auto_accepted(self):
        ok, matched = evaluate([], "op", make_ctx(), is_temp_accepted=lambda *_: False)
        assert (ok, matched) == (False, "")

    def test_missing_is_temp_accepted_callback_is_not_auto_accepted(self):
        ok, matched = evaluate([], "op", make_ctx())
        assert (ok, matched) == (False, "")


class TestFindMatchingRule:
    """P8 (rule attribution and staleness): the rule *object* ``evaluate()`` matched, factored
    out so a caller (gate.py's ``_evaluate_auto_accept``) can derive a canonical, content-based
    id from it (``policy.store.rule_id_for_rule``) instead of the possibly-ambiguous name
    ``evaluate()`` itself returns -- see that function's own docstring for why looking a rule back
    up by name is unsafe when two rows can share one."""

    def test_returns_the_matching_rule_object(self):
        rule = PolicyRule(id="r1", predicate="always_allow", value=None, operations=frozenset({"op"}))
        assert find_matching_rule([rule], "op", make_ctx()) is rule

    def test_returns_none_when_nothing_matches(self):
        rule = PolicyRule(id="r1", predicate="i_am_owner", value=None, operations=frozenset({"op"}))
        assert find_matching_rule([rule], "op", make_ctx(connector="drive")) is None

    def test_never_considers_temp_accept(self):
        # Unlike evaluate(), find_matching_rule has no is_temp_accepted parameter at all -- a
        # caller resolving "which rule row matched" must get None for the grace window, never a
        # pseudo-rule, since there is no on-disk row it could possibly refer to.
        assert find_matching_rule([], "op", make_ctx()) is None

    def test_picks_the_same_rule_evaluate_reports_by_id_when_ids_collide(self):
        # Two rules sharing one `.id` (the F9 shape a v1-compiled rule list can have -- see
        # policy.compat.compile_rule_entry's own docstring) but naming different resources: only
        # one of them actually matches this ctx, and find_matching_rule must return that exact
        # object, not merely "the first rule with a matching id".
        a = PolicyRule(id="dup", predicate="i_am_owner", value=None, operations=frozenset({"op"}))
        b = PolicyRule(id="dup", predicate="always_allow", value=None, operations=frozenset({"op"}))
        ok, matched_id = evaluate([a, b], "op", make_ctx(connector="drive"))
        assert (ok, matched_id) == (True, "dup")
        assert find_matching_rule([a, b], "op", make_ctx(connector="drive")) is b


class TestPreflightVerdicts:
    def test_no_configured_rule_requires_review(self):
        verdict, matched, _ = preflight([], "op", {})
        assert (verdict, matched) == ("requires_review", "")

    def test_args_only_rule_matching_auto_accepts(self):
        rule = PolicyRule(id="r1", predicate="approved_project_keys", value=["ENG"], operations=frozenset({"op"}))
        verdict, matched, _ = preflight([rule], "op", {"project_key": "ENG"})
        assert (verdict, matched) == ("auto_accept", "r1")

    def test_args_only_rule_not_matching_requires_review(self):
        rule = PolicyRule(id="r1", predicate="approved_project_keys", value=["ENG"], operations=frozenset({"op"}))
        verdict, matched, _ = preflight([rule], "op", {"project_key": "OTHER"})
        assert (verdict, matched) == ("requires_review", "")

    def test_fetched_scope_rule_is_unknown(self):
        rule = PolicyRule(id="r1", predicate="approved_folder", value=["f1"], operations=frozenset({"op"}))
        verdict, matched, _ = preflight([rule], "op", {})
        assert (verdict, matched) == ("unknown", "")

    def test_unrecognised_predicate_is_unknown_not_requires_review(self):
        rule = PolicyRule(id="r1", predicate="not_a_real_predicate", value=None, operations=frozenset({"op"}))
        verdict, matched, _ = preflight([rule], "op", {})
        assert (verdict, matched) == ("unknown", "")

    def test_args_only_scope_with_fetched_condition_is_unknown(self):
        # not_shared_drive (FETCHED) narrows an otherwise ARGS-resolvable always_allow rule --
        # the whole rule must be undetermined, not just the condition.
        rule = PolicyRule(
            id="r1", predicate="always_allow", value=None, operations=frozenset({"op"}),
            conditions=(("not_shared_drive", None),),
        )
        verdict, matched, _ = preflight([rule], "op", {})
        assert (verdict, matched) == ("unknown", "")

    def test_temp_accept_wins_over_everything(self):
        verdict, matched, _ = preflight([], "op", {}, is_temp_accepted=lambda *_: True)
        assert (verdict, matched) == ("auto_accept", "session_temp_accept")

    def test_raising_selector_is_unknown_not_propagated(self):
        rule = PolicyRule(
            id="r1", predicate="approved_recipient_domain", value=object(), operations=frozenset({"op"}),
        )
        verdict, matched, _ = preflight([rule], "op", {"to": "someone@example.com"})
        assert (verdict, matched) == ("unknown", "")
