"""Plugin scope types in the policy tables: the selector a plugin rule is matched by, and the
"Always allow" proposals a plugin tool's card offers.

A plugin reports, with every gated call, which values of each declared scope type the call
touched (``raw_data["scopes"]``). The point under test is that a rule proposed from one call can
never match more than that call: every value the call returned must be in the rule, an empty
anything never matches, and a proposal covers the one operation key that was gated (ADR 0120).
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from privacyfence import auto_accept, gate
from privacyfence.auto_accept import DynamicToolSpec
from privacyfence.policy import describe, engine, propose, scopes, store
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import Verb

from ...helpers import make_ctx

_PLUGIN = "today"
_CALENDAR = f"plugin:{_PLUGIN}:calendar"
_PROJECT = f"plugin:{_PLUGIN}:project"
_ANYTHING = f"plugin:{_PLUGIN}:anything"


def _specs() -> list[DynamicToolSpec]:
    return [
        DynamicToolSpec(
            tool="today_get_day", gate="review", operation="plugin.today.get_day", verb=Verb.READ,
            layout=gate.WIDE, effect="", scope_predicates=((_CALENDAR, "calendar"),),
        ),
        DynamicToolSpec(
            tool="today_get_week", gate="review", operation="plugin.today.get_week", verb=Verb.READ,
            layout=gate.WIDE, effect="", scope_predicates=((_CALENDAR, "calendar"), (_PROJECT, "project")),
        ),
        DynamicToolSpec(
            tool="today_add_note", gate="popup", operation="plugin.today.add_note", verb=Verb.UPDATE,
            layout=gate.WIDE, effect="A note is added.", scope_predicates=(),
        ),
        DynamicToolSpec(
            tool="today_clear_day", gate="popup", operation="plugin.today.clear_day", verb=Verb.UPDATE,
            layout=gate.WIDE, effect="The day's notes are deleted.",
            scope_predicates=((_CALENDAR, "calendar"),), destructive=True,
        ),
    ]


@pytest.fixture(autouse=True)
def _today_registered():
    auto_accept.register_dynamic_tools(_PLUGIN, _specs())
    yield
    auto_accept.unregister_dynamic_tools(_PLUGIN)


def _ctx(tool: str = "today_get_day", **reported: object):
    return make_ctx(connector=_PLUGIN, tool=tool, args={}, raw_data={"scopes": dict(reported)})


def _matches(value: object, ctx) -> bool:
    return scopes.NEW_SCOPE_SELECTORS[_CALENDAR].matches(value, ctx)


class TestPluginScope:
    def test_selector_is_registered_with_a_dotted_scope_type(self):
        selector = scopes.NEW_SCOPE_SELECTORS[_CALENDAR]
        assert selector.scope_type == "today.calendar"
        assert selector.kind is scopes.ScopeKind.IDENTITY
        assert selector.resolves_from is scopes.ResolvesFrom.FETCHED

    def test_matching_values_accepted(self):
        assert _matches(["work", "home"], _ctx(calendar=["work"]))
        assert _matches(["work", "home"], _ctx(calendar=["home", "work"]))
        assert _matches("work", _ctx(calendar=["work"]))

    def test_non_matching_value_not_accepted(self):
        assert not _matches(["work"], _ctx(calendar=["home"]))
        # One value outside the rule is enough to refuse the whole call.
        assert not _matches(["work"], _ctx(calendar=["work", "home"]))

    def test_empty_scope_never_matches(self):
        assert not _matches(["work"], _ctx(calendar=[]))
        assert not _matches([], _ctx(calendar=["work"]))
        assert not _matches(["", None], _ctx(calendar=[""]))
        assert not _matches("", _ctx(calendar=[""]))

    def test_missing_scope_never_matches(self):
        assert not _matches(["work"], _ctx(project=["work"]))
        assert not _matches(["work"], make_ctx(connector=_PLUGIN, raw_data=None))
        assert not _matches(["work"], make_ctx(connector=_PLUGIN, raw_data={"scopes": ["work"]}))
        assert not _matches(["work"], make_ctx(connector=_PLUGIN, raw_data=["work"]))

    def test_a_returned_string_is_one_value_not_its_characters(self):
        assert not _matches(["w", "o", "r", "k"], _ctx(calendar="work"))
        assert _matches(["work"], _ctx(calendar="work"))

    def test_values_compare_as_strings(self):
        assert _matches(["7"], _ctx(calendar=[7]))

    def test_scope_type_label_finds_the_noun(self):
        assert describe.scope_type_label(scopes.NEW_SCOPE_SELECTORS[_CALENDAR].scope_type) == "calendar"

    def test_static_or_malformed_predicates_are_refused(self):
        with pytest.raises(ValueError):
            scopes.register_plugin_selector("approved_channel", "calendar")
        with pytest.raises(ValueError):
            scopes.register_plugin_selector("plugin:today:calendar", "project")
        with pytest.raises(ValueError):
            scopes.register_plugin_selector("plugin:today:anything", "anything")

    def test_a_shared_predicate_stays_until_its_last_holder_goes(self):
        scopes.register_plugin_selector(_CALENDAR, "calendar")
        scopes.unregister_plugin_selector(_CALENDAR)
        assert _CALENDAR in scopes.NEW_SCOPE_SELECTORS
        auto_accept.unregister_dynamic_tools(_PLUGIN)
        assert _CALENDAR not in scopes.NEW_SCOPE_SELECTORS

    def test_unregistering_a_static_predicate_is_a_no_op(self):
        scopes.unregister_plugin_selector("drive.file")
        assert "drive.file" in scopes.NEW_SCOPE_SELECTORS


class TestProposals:
    def test_connector_of_operation_is_the_plugin(self):
        assert propose.connector_of_operation("plugin.today.get_day") == "today"
        assert propose.connector_of_operation("sheets.write_range") == "drive"

    def test_scoped_tool_offers_scope_rule(self):
        proposals = propose.proposals_for("today_get_day", _ctx(calendar=["work"]))
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.scope.predicate == _CALENDAR
        assert proposal.scope.scope_type == "today.calendar"
        assert proposal.value == ["work"]
        assert proposal.verb is Verb.READ
        assert proposal.widenings == ()
        assert describe.button_label(proposal) == "this calendar"
        assert propose.rules_for_proposal(proposal) == [
            PolicyRule(
                id=store.rule_id_for(_CALENDAR, ["work"], ()), predicate=_CALENDAR, value=["work"],
                operations=frozenset({"plugin.today.get_day"}),
            )
        ]

    def test_more_than_one_value_says_so_on_the_button(self):
        proposal = propose.proposals_for("today_get_day", _ctx(calendar=["work", "home"]))[0]
        assert proposal.value == ["home", "work"]
        assert describe.button_label(proposal) == "these calendar values"

    def test_one_proposal_per_declared_scope_type(self):
        proposals = propose.proposals_for("today_get_week", _ctx(calendar=["work"], project=["p1"]))
        assert [p.scope.predicate for p in proposals] == [_CALENDAR, _PROJECT]

    def test_a_scope_the_call_did_not_report_is_not_offered(self):
        proposals = propose.proposals_for("today_get_week", _ctx(calendar=["work"]))
        assert [p.scope.predicate for p in proposals] == [_CALENDAR]
        assert propose.proposals_for("today_get_day", _ctx(calendar=[])) == []
        assert propose.proposals_for("today_get_day", _ctx(calendar=["work", ""])) == []
        assert propose.proposals_for("today_get_day", make_ctx(connector=_PLUGIN, raw_data=None)) == []

    def test_unscoped_tool_offers_whole_tool_rule(self):
        proposals = propose.proposals_for("today_add_note", _ctx("today_add_note"))
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.scope.predicate == _ANYTHING
        assert proposal.scope.predicate != "always_allow"
        assert proposal.value is True
        assert describe.button_label(proposal) == ""
        (rule,) = propose.rules_for_proposal(proposal)
        assert rule.operations == frozenset({"plugin.today.add_note"})
        assert engine.find_matching_rule([rule], "plugin.today.add_note", _ctx("today_add_note")) == rule
        # Gmail's unconditional drafting rule stays its own row; removing one never removes both.
        gmail = PolicyRule(
            id="always_allow", predicate="always_allow", value=True,
            operations=frozenset({"gmail.create_draft"}),
        )
        merged = store.merge_rules([gmail, rule])
        assert len(merged) == 2
        assert {r.operations for r in merged} == {
            frozenset({"gmail.create_draft"}), frozenset({"plugin.today.add_note"}),
        }

    def test_destructive_tool_offers_nothing(self):
        assert propose.proposals_for("today_clear_day", _ctx("today_clear_day", calendar=["work"])) == []

    def test_proposal_covers_one_operation(self):
        for tool, ctx in (
            ("today_get_day", _ctx(calendar=["work"])),
            ("today_get_week", _ctx("today_get_week", calendar=["work"], project=["p1"])),
            ("today_add_note", _ctx("today_add_note")),
        ):
            for proposal in propose.proposals_for(tool, ctx):
                rules = propose.rules_for_proposal(proposal)
                assert {op for rule in rules for op in rule.operations} == {proposal.operation}
                # A rule for one tool of the plugin never reaches another tool of the same plugin.
                other = "plugin.today.get_week" if tool == "today_get_day" else "plugin.today.get_day"
                assert engine.find_matching_rule(rules, other, ctx) is None

    def test_confirmation_text_names_scope(self):
        proposal = propose.proposals_for("today_get_day", _ctx(calendar=["work"]))[0]
        text = describe.confirmation_text(proposal)
        assert "calendar work" in text
        assert "Covers 1 tool: today_get_day" in text

    def test_static_proposals_are_unchanged(self):
        ctx = make_ctx(
            connector="slack", tool="slack_get_channel_history", args={"channel_id": "C1"}, raw_data=[],
        )
        assert [p.scope.predicate for p in propose.proposals_for("slack_get_channel_history", ctx)] == [
            "approved_channel",
        ]
        assert not any(group.startswith("plugin:") for group in propose.SCOPES_BY_GROUP)

    def test_unregister_removes_the_proposals(self):
        auto_accept.unregister_dynamic_tools(_PLUGIN)
        assert propose.proposals_for("today_get_day", _ctx(calendar=["work"])) == []
        assert _ANYTHING not in scopes.NEW_SCOPE_SELECTORS


_VALUE = st.text(min_size=1, max_size=12)


class TestProposalMatchesItsCall:
    @settings(max_examples=200, deadline=None)
    @given(values=st.lists(_VALUE, min_size=1, max_size=6, unique=True), extra=_VALUE)
    def test_rule_matches_its_call_and_not_one_more_value(self, values, extra):
        ctx = _ctx(calendar=values)
        proposals = propose.proposals_for("today_get_day", ctx)
        assert proposals
        rules = propose.rules_for_proposal(proposals[0])
        assert engine.find_matching_rule(rules, "plugin.today.get_day", ctx) is not None
        if extra not in values:
            wider = _ctx(calendar=[*values, extra])
            assert engine.find_matching_rule(rules, "plugin.today.get_day", wider) is None
