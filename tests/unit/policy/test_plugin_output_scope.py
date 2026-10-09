"""The output folder scope: the selector a ``plugin:<plugin>:output`` rule is matched by, and the
"Always allow" proposal a ``plugin_outputs_read`` card offers.

A rule's value is a path prefix, not a set: a value ending in ``/`` is a folder and covers every
file below it, any other value is one exact file. The point under test is that a folder never
reaches past itself (``reports/`` is not ``reports2/``), and that a rule proposed from one read
matches that read and not a sibling folder.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from privacyfence import auto_accept, gate
from privacyfence.auto_accept import DynamicToolSpec
from privacyfence.plugins import constants
from privacyfence.policy import describe, engine, propose, scopes
from privacyfence.policy.registry import Verb

from ...helpers import make_ctx

_PLUGIN = "today"
_OUTPUT = constants.scope_predicate(_PLUGIN, "output")
_OPERATION = "plugin_outputs.read"


def _specs() -> list[DynamicToolSpec]:
    return [
        DynamicToolSpec(
            tool="plugin_outputs_list", gate="auto", operation=None, verb=None,
            layout=gate.NARROW, effect="", scope_predicates=(),
        ),
        DynamicToolSpec(
            tool="plugin_outputs_read", gate="review", operation=_OPERATION, verb=Verb.READ,
            layout=gate.WIDE, effect="", scope_predicates=(),
        ),
    ]


@pytest.fixture(autouse=True)
def _outputs_registered():
    auto_accept.register_internal_dynamic_tools("plugin_outputs", _specs())
    scopes.register_plugin_output_selector(_PLUGIN)
    propose.register_dynamic_scope_entry("plugin_outputs", "plugin_outputs_read", propose.plugin_output_scope_entry(_PLUGIN))
    yield
    propose.unregister_dynamic_scope_entries("plugin_outputs")
    scopes.unregister_plugin_output_selector(_PLUGIN)
    auto_accept.unregister_internal_dynamic_tools("plugin_outputs")


def _ctx(path: object, plugin: str = _PLUGIN):
    return make_ctx(
        connector="plugin_outputs", tool="plugin_outputs_read", args={},
        raw_data={"plugin": plugin, "path": path},
    )


def _matches(value: object, ctx) -> bool:
    return scopes.NEW_SCOPE_SELECTORS[_OUTPUT].matches(value, ctx)


class TestOutputSelector:
    def test_selector_is_registered_as_an_identity_scope_from_args(self):
        selector = scopes.NEW_SCOPE_SELECTORS[_OUTPUT]
        assert selector.scope_type == "today.output"
        assert selector.kind is scopes.ScopeKind.IDENTITY
        assert selector.resolves_from is scopes.ResolvesFrom.ARGS

    def test_folder_prefix_matches_files_below_it(self):
        assert _matches(["reports/"], _ctx("reports/x.csv"))
        assert _matches(["reports/"], _ctx("reports/2026/q3.csv"))
        assert _matches("reports/", _ctx("reports/x.csv"))

    def test_folder_prefix_does_not_match_a_sibling_folder(self):
        assert not _matches(["reports/"], _ctx("reports2/x"))
        assert not _matches(["reports/"], _ctx("reports"))
        assert not _matches(["reports/2026/"], _ctx("reports/2025/q3.csv"))

    def test_exact_file_matches_only_itself(self):
        assert _matches(["reports/x.csv"], _ctx("reports/x.csv"))
        assert not _matches(["reports/x.csv"], _ctx("reports/x.csv.bak"))
        assert not _matches(["reports/x.csv"], _ctx("reports/x.cs"))
        # A value without a trailing slash is a file, never a folder.
        assert not _matches(["reports"], _ctx("reports/x.csv"))

    def test_empty_value_never_matches(self):
        assert not _matches([""], _ctx("reports/x.csv"))
        assert not _matches("", _ctx("reports/x.csv"))
        assert not _matches([], _ctx("reports/x.csv"))
        assert not _matches([None, 7], _ctx("reports/x.csv"))
        assert not _matches([""], _ctx(""))

    def test_other_plugin_never_matches(self):
        assert not _matches(["reports/"], _ctx("reports/x.csv", plugin="other"))
        scopes.register_plugin_output_selector("other")
        try:
            other = scopes.NEW_SCOPE_SELECTORS[constants.scope_predicate("other", "output")]
            assert not other.matches(["reports/"], _ctx("reports/x.csv"))
        finally:
            scopes.unregister_plugin_output_selector("other")

    def test_missing_or_malformed_raw_data_never_matches(self):
        assert not _matches(["reports/"], _ctx(None))
        assert not _matches(["reports/"], _ctx(["reports/x.csv"]))
        assert not _matches(["reports/"], make_ctx(connector="plugin_outputs", raw_data=None))
        assert not _matches(["reports/"], make_ctx(connector="plugin_outputs", raw_data="reports/x.csv"))

    def test_registering_twice_keeps_one_selector(self):
        scopes.register_plugin_output_selector(_PLUGIN)
        scopes.unregister_plugin_output_selector(_PLUGIN)
        assert _OUTPUT not in scopes.NEW_SCOPE_SELECTORS

    def test_a_plugin_scope_type_named_output_is_refused(self):
        with pytest.raises(ValueError, match="reserved"):
            scopes.check_plugin_selector(_OUTPUT, "output")
        with pytest.raises(ValueError, match="reserved"):
            scopes.register_plugin_selector(_OUTPUT, "output")

    def test_a_plugin_tool_cannot_declare_the_output_predicate(self):
        spec = DynamicToolSpec(
            tool="today_get_day", gate="review", operation="plugin.today.get_day", verb=Verb.READ,
            layout=gate.WIDE, effect="", scope_predicates=((_OUTPUT, "output"),),
        )
        with pytest.raises(ValueError, match="reserved"):
            auto_accept.register_dynamic_tools(_PLUGIN, [spec])
        assert "today_get_day" not in auto_accept.TOOL_TO_GATE


class TestOutputProposal:
    def test_connector_of_operation_is_plugin_outputs(self):
        assert propose.connector_of_operation(_OPERATION) == "plugin_outputs"

    def test_a_nested_file_proposes_its_folder(self):
        (proposal,) = propose.proposals_for("plugin_outputs_read", _ctx("reports/2026/q3.csv"))
        assert proposal.scope.predicate == _OUTPUT
        assert proposal.scope.scope_type == "today.output"
        assert proposal.scope.connector == "plugin_outputs"
        assert proposal.scope.id == proposal.scope.widening_group == f"{_OUTPUT}@{_OPERATION}"
        assert proposal.value == ["reports/2026/"]
        assert proposal.verb is Verb.READ
        assert proposal.operation == _OPERATION
        assert proposal.widenings == ()
        assert describe.button_label(proposal) == "this folder"

    def test_a_file_at_the_root_proposes_the_file(self):
        (proposal,) = propose.proposals_for("plugin_outputs_read", _ctx("q3.csv"))
        assert proposal.value == ["q3.csv"]
        assert describe.button_label(proposal) == "this file"

    def test_another_plugin_or_no_path_proposes_nothing(self):
        assert propose.proposals_for("plugin_outputs_read", _ctx("reports/x.csv", plugin="other")) == []
        assert propose.proposals_for("plugin_outputs_read", _ctx("")) == []
        assert propose.proposals_for("plugin_outputs_read", _ctx(None)) == []

    def test_the_rule_covers_one_operation(self):
        (proposal,) = propose.proposals_for("plugin_outputs_read", _ctx("reports/x.csv"))
        (rule,) = propose.rules_for_proposal(proposal)
        assert rule.predicate == _OUTPUT
        assert rule.value == ["reports/"]
        assert rule.operations == frozenset({_OPERATION})

    def test_the_list_tool_proposes_nothing(self):
        assert propose.proposals_for("plugin_outputs_list", _ctx("reports/x.csv")) == []

    def test_static_proposals_are_unchanged(self):
        assert not any("output" in group for group in propose.SCOPES_BY_GROUP)

    def test_an_entry_that_does_not_fit_the_tool_is_refused(self):
        entry = propose.plugin_output_scope_entry(_PLUGIN)
        with pytest.raises(ValueError):
            propose.register_dynamic_scope_entry("plugin_outputs", "gmail_get_message", entry)
        with pytest.raises(ValueError):
            propose.register_dynamic_scope_entry("plugin_outputs", "plugin_outputs_list", entry)
        with pytest.raises(ValueError):
            propose.register_dynamic_scope_entry(
                "plugin_outputs", "plugin_outputs_read", propose._scope(
                    _OUTPUT, "today.output", "gmail", (Verb.READ,), entry.value_of, "", widenable=False,
                ),
            )

    def test_registering_an_entry_again_replaces_it(self):
        propose.register_dynamic_scope_entry("plugin_outputs", "plugin_outputs_read", propose.plugin_output_scope_entry(_PLUGIN))
        assert len(propose.proposals_for("plugin_outputs_read", _ctx("reports/x.csv"))) == 1

    def test_unregister_removes_the_selector_and_the_entry(self):
        propose.unregister_dynamic_scope_entries("plugin_outputs")
        scopes.unregister_plugin_output_selector(_PLUGIN)
        assert _OUTPUT not in scopes.NEW_SCOPE_SELECTORS
        assert propose.proposals_for("plugin_outputs_read", _ctx("reports/x.csv")) == []


_SEGMENT = st.text(alphabet="abcxyz019_-", min_size=1, max_size=8)


class TestProposalMatchesItsCall:
    @settings(max_examples=200, deadline=None)
    @given(folders=st.lists(_SEGMENT, max_size=3), name=_SEGMENT, suffix=_SEGMENT)
    def test_rule_matches_its_call_and_not_a_sibling(self, folders, name, suffix):
        path = "/".join([*folders, name])
        ctx = _ctx(path)
        (proposal,) = propose.proposals_for("plugin_outputs_read", ctx)
        rules = propose.rules_for_proposal(proposal)
        assert engine.find_matching_rule(rules, _OPERATION, ctx) is not None
        if folders:
            # A sibling folder sharing the folder's name as a prefix: reports/ is not reports2/.
            sibling = "/".join([*folders[:-1], folders[-1] + suffix, name])
        else:
            sibling = name + suffix
        assert engine.find_matching_rule(rules, _OPERATION, _ctx(sibling)) is None
        assert engine.find_matching_rule(rules, _OPERATION, _ctx(path, plugin="other")) is None
