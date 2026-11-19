"""The per-tool Always allow of an upstream tool at the ``auto`` ceiling (ADR 0331)."""
from __future__ import annotations

import pytest

from privacyfence import gate, scoped_tokens
from privacyfence.audit_log import init_audit_logger
from privacyfence.auto_accept import ReviewContext
from privacyfence.policy import catalogue, describe, outdated, propose, scope_rows, scopes
from privacyfence.policy import engine as policy_engine
from privacyfence.policy import store as policy_store
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import Verb
from privacyfence.upstream_mcp_registry import get_registry

from .test_upstream_mcp_registry import AUTO, AUTO_WRITE, POPUP, REVIEW, saved_tool, upstream
from .test_gate import FILTERED, RAW, _scoped_token

SELECTOR = scopes.NEW_SCOPE_SELECTORS["upstream.always_allow"]


def ctx(tool="kh_fetch") -> ReviewContext:
    return ReviewContext(connector="kh", tool=tool, args={}, raw_data=None)


def rule_for(name="kh_fetch", operation="kh.fetch") -> PolicyRule:
    return PolicyRule(
        id="upstream.always_allow", predicate="upstream.always_allow", value=name,
        operations=frozenset({operation}),
    )


@pytest.fixture
def applied():
    get_registry().apply(upstream(), (REVIEW, POPUP, AUTO, AUTO_WRITE))


@pytest.fixture
def audit_dir(tmp_path):
    init_audit_logger(str(tmp_path))
    return tmp_path


class TestProposals:
    def test_an_auto_tool_gets_exactly_one_proposal(self, applied):
        (proposal,) = propose.proposals_for("kh_fetch", ctx())
        assert proposal.scope.id == "upstream.always_allow/kh_fetch"
        assert proposal.widenings == ()
        (rule,) = propose.rules_for_proposal(proposal)
        assert rule.predicate == "upstream.always_allow"
        assert rule.value == "kh_fetch"
        assert rule.operations == {"kh.fetch"}

    def test_a_review_or_popup_tool_gets_none(self, applied):
        assert propose.proposals_for("kh_search", ctx("kh_search")) == []
        assert propose.proposals_for("kh_write", ctx("kh_write")) == []

    def test_a_scope_with_a_fixed_value_needs_none(self, applied):
        (scope,) = propose.SCOPES_BY_GROUP["upstream.always_allow/kh_fetch"]
        assert propose.scope_needs_value(scope) is False

    def test_the_picker_group_ignores_a_typed_value(self, applied):
        (rule,) = propose.rules_for_scope_group("upstream.always_allow/kh_fetch", ["other"], [Verb.READ])
        assert rule.value == "kh_fetch" and rule.operations == {"kh.fetch"}

    def test_the_catalogue_lists_label_dash_tool(self, applied):
        entry = next(e for e in catalogue.scope_catalogue() if e["id"] == "upstream.always_allow/kh_update_note")
        assert entry["label"] == "Knowledge Hub — Update_note"
        assert entry["needs_value"] is False and entry["verbs"] == ["update"]
        assert catalogue.rules_for_catalogue_entry("upstream.always_allow/kh_update_note", None, [Verb.UPDATE]) == [
            dataclass_rule("kh_update_note", "kh.update_note"),
        ]

    def test_the_confirmation_text_names_the_rule(self, applied):
        (proposal,) = propose.proposals_for("kh_fetch", ctx())
        assert describe.confirmation_text(proposal).splitlines()[0] == "Always allow: Always allow Knowledge Hub: kh_fetch"
        assert describe.button_label(proposal) == "fetch"


def dataclass_rule(name, operation) -> PolicyRule:
    (merged,) = policy_store.merge_rules([rule_for(name, operation)])
    return merged


class TestSelector:
    def test_it_matches_only_an_auto_tool_for_its_own_call_or_the_preflight(self, applied):
        assert SELECTOR.matches("kh_fetch", ctx("kh_fetch"))
        assert SELECTOR.matches("kh_fetch", ctx(""))
        assert not SELECTOR.matches("kh_fetch", ctx("kh_update_note"))
        assert not SELECTOR.matches("kh_search", ctx("kh_search"))  # a review tool is not at the ceiling
        assert not SELECTOR.matches(["kh_fetch"], ctx(""))
        assert not SELECTOR.matches(None, ctx(""))

    def test_the_auto_set_holds_only_the_auto_tools(self, applied):
        assert scopes.UPSTREAM_AUTO_TOOLS == {"kh_fetch", "kh_update_note"}


class TestStoredRules:
    def test_two_auto_tools_are_two_custom_rows(self, applied):
        proposals = [propose.proposals_for(tool, ctx(tool))[0] for tool in ("kh_fetch", "kh_update_note")]
        rules = [rule for proposal in proposals for rule in propose.rules_for_proposal(proposal)]
        assert rules[0].id != rules[1].id
        rows, touched = scope_rows.add_rules([], rules)
        assert len(rows) == 2 and len(set(touched)) == 2
        assert all(row.scope is None for row in rows)
        assert {row.value for row in rows} == {"kh_fetch", "kh_update_note"}
        assert [row.operations for row in rows] == [frozenset({"kh.fetch"}), frozenset({"kh.update_note"})]

    def test_a_second_always_allow_does_not_extend_the_first(self, applied):
        first = propose.rules_for_proposal(propose.proposals_for("kh_fetch", ctx())[0])
        second = propose.rules_for_proposal(propose.proposals_for("kh_update_note", ctx("kh_update_note"))[0])
        rows, _ = scope_rows.add_rules([], first)
        rows, _ = scope_rows.add_rules(rows, second)
        assert sorted(row.operations for row in rows) == [frozenset({"kh.fetch"}), frozenset({"kh.update_note"})]

    def test_the_rule_survives_a_save_and_a_load(self, applied):
        rules = propose.rules_for_proposal(propose.proposals_for("kh_fetch", ctx())[0])
        rows, _ = scope_rows.add_rules([], rules)
        loaded = scope_rows.rows_from_section(scope_rows.rows_to_config(rows))
        assert [(r.predicate, r.value, r.operations) for r in loaded] == [("upstream.always_allow", "kh_fetch", frozenset({"kh.fetch"}))]
        assert scope_rows.compile_rows(loaded)[0].value == "kh_fetch"

    def test_scope_of_rule_is_none_and_nothing_is_missing(self, applied):
        rule = rule_for()
        assert scope_rows.scope_of_rule(rule) is None
        assert outdated.missing_operations(rule) == frozenset()
        # the other auto tool of the same upstream is not "missing" from this rule
        assert outdated.missing_operations(rule_for("kh_update_note", "kh.update_note")) == frozenset()

    def test_a_lowered_tool_keeps_its_rule_which_matches_nothing(self, applied):
        rules = propose.rules_for_proposal(propose.proposals_for("kh_fetch", ctx())[0])
        rows, _ = scope_rows.add_rules([], rules)
        assert policy_engine.find_matching_rule(scope_rows.compile_rows(rows), "kh.fetch", ctx()) is not None

        get_registry().apply(upstream(), (REVIEW, POPUP, saved_tool("fetch", "review"), AUTO_WRITE))

        compiled = scope_rows.compile_rows(rows)
        assert [rule.value for rule in compiled] == ["kh_fetch"]
        assert policy_engine.find_matching_rule(compiled, "kh.fetch", ctx()) is None
        loaded = scope_rows.rows_from_section(scope_rows.rows_to_config(rows))
        assert [row.value for row in loaded] == ["kh_fetch"]
        other = propose.rules_for_proposal(propose.proposals_for("kh_update_note", ctx("kh_update_note"))[0])
        rows, _ = scope_rows.add_rules(rows, other)
        assert sorted(row.value for row in rows) == ["kh_fetch", "kh_update_note"]
        assert outdated.missing_operations(compiled[0]) == frozenset()
        assert describe.rule_sentence(compiled[0]) == "Always allow Knowledge Hub: kh_fetch"
        assert describe.row_sentence(rows[0]) == "Always allow Knowledge Hub: kh_fetch"

    def test_the_rule_comes_back_after_the_tool_is_raised_again(self, applied):
        rows, _ = scope_rows.add_rules([], [rule_for()])
        get_registry().apply(upstream(), (saved_tool("fetch", "review"),))
        get_registry().apply(upstream(), (AUTO,))
        assert policy_engine.find_matching_rule(scope_rows.compile_rows(rows), "kh.fetch", ctx()) is not None


class TestGate:
    async def test_a_matching_rule_lets_a_review_call_through_without_a_card(self, applied, audit_dir, monkeypatch):
        monkeypatch.setattr(gate, "get_policy_v2_store_rules", lambda: [rule_for()])
        popups = []
        monkeypatch.setattr(gate, "show_read_popup", lambda *a, **k: (popups.append(a) or "deny", None))

        result = await gate.gated_call(
            connector="kh", tool="kh_fetch", tool_name="Fetch", summary="s", sender="", raw_data=RAW,
            filtered_data=FILTERED, gate="review", preview={}, details_text="d", my_email="me@example.com",
        )

        assert result is FILTERED and popups == []

    def test_a_token_with_the_per_tool_rule_passes_and_one_without_does_not(self, applied, audit_dir):
        (row,) = scope_rows.rows_for_rules([rule_for()], "r-1")
        covered = _scoped_token(rules=(row,), tools=("kh_fetch",))
        bare = _scoped_token(rules=(), tools=("kh_fetch",))
        with scoped_tokens.token_scope(covered):
            gate.check_scoped_upstream_approval("kh", "kh_fetch", {})
        with scoped_tokens.token_scope(bare), pytest.raises(gate.GateDeniedError):
            gate.check_scoped_upstream_approval("kh", "kh_fetch", {})

    def test_a_token_rule_for_a_lowered_tool_no_longer_passes(self, applied, audit_dir):
        (row,) = scope_rows.rows_for_rules([rule_for()], "r-1")
        token = _scoped_token(rules=(row,), tools=("kh_fetch",))
        get_registry().apply(upstream(), (saved_tool("fetch", "review"),))
        with scoped_tokens.token_scope(token), pytest.raises(gate.GateDeniedError):
            gate.check_scoped_upstream_approval("kh", "kh_fetch", {})

    def test_an_approval_built_from_the_declarations_passes_and_an_older_digest_does_not(self, applied, audit_dir):
        current = f"kh.search@{scopes.UPSTREAM_DECLARATIONS['kh.search']}"
        good = PolicyRule(
            id="a", predicate="upstream.approved_tool", value=current, operations=frozenset({"kh.search"}),
        )
        old = PolicyRule(
            id="b", predicate="upstream.approved_tool", value="kh.search@" + "0" * 32,
            operations=frozenset({"kh.search"}),
        )
        (good_row,) = scope_rows.rows_for_rules([good], "r-a")
        (old_row,) = scope_rows.rows_for_rules([old], "r-b")
        with scoped_tokens.token_scope(_scoped_token(rules=(good_row,), tools=("kh_search",))):
            gate.check_scoped_upstream_approval("kh", "kh_search", {})
        with scoped_tokens.token_scope(_scoped_token(rules=(old_row,), tools=("kh_search",))), \
                pytest.raises(gate.GateDeniedError):
            gate.check_scoped_upstream_approval("kh", "kh_search", {})
