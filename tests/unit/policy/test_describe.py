"""Tests for privacyfence.policy.describe.

`describe.py` exists so that one intent reads the same wherever it is shown, and so that a surface
can state a rule's *width* instead of leaving the user to infer it from an operation key. The
assertions here are mostly about that width: `covered_tools` on a one-verb rule and on the full
sandbox-folder rule set are the two numbers the popup and Settings currently disagree about
without saying so.
"""
from __future__ import annotations

from types import SimpleNamespace

from privacyfence.policy import describe, propose
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import Verb

from ...helpers import make_ctx

_FILE = SimpleNamespace(id="f1", parent_ids=["FOLDER1"], owners=["me@corp.com"])
_WRITE_VERBS = (Verb.UPDATE, Verb.FORMAT, Verb.RESTRUCTURE, Verb.COMMENT, Verb.DELETE, Verb.CREATE, Verb.MOVE)


def _drive_write_ctx():
    return make_ctx(
        connector="drive", tool="drive_sheets_write_range", args={"spreadsheet_id": "f1"},
        raw_data={"file": _FILE}, my_email="me@corp.com",
    )


class TestLabels:

    def test_connector_label_titlecases_an_underscored_name(self):
        assert describe.connector_label("apps_script") == "Apps Script"
        assert describe.connector_label("drive") == "Drive"

    def test_scope_type_label_drops_the_connector_prefix(self):
        assert describe.scope_type_label("drive.folder") == "folder"
        assert describe.scope_type_label("gmail.sender_domain") == "sender domain"

    def test_button_label_is_the_scopes_category_not_its_value(self):
        proposal = propose.proposals_for("drive_sheets_write_range", _drive_write_ctx())[0]
        assert describe.button_label(proposal) == "this folder"

    def test_button_label_is_empty_for_an_unconditional_scope(self):
        draft = propose.proposals_for("gmail_create_draft", make_ctx(args={"to": "x@y.com"}))[0]
        assert describe.button_label(draft) == ""

    def test_widening_label_states_the_tool_count(self):
        proposal = propose.proposals_for("drive_sheets_write_range", _drive_write_ctx())[0]
        labels = {w.verb: describe.widening_label(w) for w in proposal.widenings}
        assert labels[Verb.COMMENT] == "also allow comment (1 tool)"
        assert labels[Verb.FORMAT] == "also allow format (2 tools)"


class TestRuleVerbsAndCoverage:

    def test_a_rule_is_only_credited_with_the_verbs_its_predicate_governs(self):
        """`slack.read_messages` carries both `read` and `search`; `approved_channel` governs only
        the first. Crediting it with both would describe a rule as allowing a multi-channel search
        it cannot actually match."""
        rule = PolicyRule(id="r", predicate="approved_channel", value=["C1"],
                          operations=frozenset({"slack.read_messages"}))
        assert describe.rule_verbs(rule) == (Verb.READ,)
        all_results = PolicyRule(id="r", predicate="approved_channel_all_results", value=["C1"],
                                 operations=frozenset({"slack.read_messages"}))
        assert describe.rule_verbs(all_results) == (Verb.SEARCH,)

    def test_covered_tools_excludes_the_tool_the_other_verb_belongs_to(self):
        rule = PolicyRule(id="r", predicate="approved_channel", value=["C1"],
                          operations=frozenset({"slack.read_messages"}))
        assert "slack_search_messages" not in describe.covered_tools(rule)
        assert "slack_get_channel_history" in describe.covered_tools(rule)

    def test_a_predicate_no_surface_proposes_is_credited_with_its_keys_own_verbs(self):
        """A hand-written v1 rule (or a scope no surface proposes) still has to render.
        Falling back to the operation key's verbs is the most that can honestly be said."""
        rule = PolicyRule(id="r", predicate="file_type_allowlist", value=["text/plain"],
                          operations=frozenset({"drive.read_file_contents"}))
        assert describe.rule_verbs(rule) == (Verb.READ,)
        assert describe.covered_tools(rule) == ("drive_get_file_content",)

    def test_covered_tools_counts_the_sandbox_folders_real_width(self):
        """The width a surface has to state: one "Write auto-accept" toggle, thirteen operation keys, and
        the tools behind them."""
        rules = propose.rules_for_scope_group("drive.folder", ["FOLDER1"], _WRITE_VERBS)
        tools = {tool for rule in rules for tool in describe.covered_tools(rule)}
        assert len(tools) == 13
        assert "drive_sheets_delete_dimensions" in tools

    def test_a_narrow_proposal_covers_one_tool(self):
        proposal = propose.proposals_for("drive_sheets_write_range", _drive_write_ctx())[0]
        rules = propose.rules_for_proposal(proposal)
        assert [describe.covered_tools(rule) for rule in rules] == [("drive_sheets_write_range",)]


class TestSentences:

    def test_an_identity_rule_names_its_connector_scope_and_verbs(self):
        rule = PolicyRule(id="r", predicate="approved_sandbox_folder", value=["FOLDER1"],
                          operations=frozenset({"sheets.write_range", "sheets.format_range"}))
        assert describe.rule_sentence(rule) == "Drive - folder FOLDER1: allow update, format"

    def test_a_value_less_attribute_rule_names_no_value(self):
        rule = PolicyRule(id="r", predicate="i_am_owner", value=None,
                          operations=frozenset({"drive.read_file_contents"}))
        assert describe.rule_sentence(rule) == "Drive - owned by me: allow read"

    def test_a_condition_rule_says_it_is_unconditional_and_names_the_condition(self):
        """An unconditional grant should read as unconditional rather than hide behind a rule
        name under an operation key."""
        rule = PolicyRule(id="r", predicate="always_allow", value=None,
                          operations=frozenset({"calendar.read_event_details"}),
                          conditions=(("not_private", None),))
        assert describe.rule_sentence(rule) == "Calendar - anything: allow read - when not private"

    def test_a_dual_scope_predicate_resolves_against_the_rules_own_connector(self):
        """`label_name_allowlist` serves `gmail.label` and `contacts.label`; which one it is
        depends on the operations the rule actually carries."""
        gmail = PolicyRule(id="r", predicate="label_name_allowlist", value=["Done"],
                           operations=frozenset({"gmail.add_label"}))
        contacts = PolicyRule(id="r", predicate="label_name_allowlist", value=["Friends"],
                              operations=frozenset({"contacts.add_label"}))
        assert describe.rule_sentence(gmail) == "Gmail - label Done: allow label"
        assert describe.rule_sentence(contacts) == "Contacts - label Friends: allow label"

    def test_a_dual_scope_predicate_with_no_matching_connector_falls_back_to_the_first(self):
        orphan = PolicyRule(id="r", predicate="label_name_allowlist", value=["X"],
                            operations=frozenset({"jira.read_issue"}))
        assert describe.rule_sentence(orphan).startswith("Jira - label X:")

    def test_an_unrecognised_predicate_renders_as_itself(self):
        rule = PolicyRule(id="r", predicate="not_a_predicate", value=None,
                          operations=frozenset({"drive.read_file_contents"}))
        assert describe.rule_sentence(rule) == "Drive - not_a_predicate: allow read"

    def test_a_rule_with_no_operations_says_it_allows_nothing(self):
        rule = PolicyRule(id="r", predicate="i_am_owner", value=None, operations=frozenset())
        assert describe.rule_sentence(rule) == "owned by me: allow nothing"

    def test_a_scalar_value_renders_without_list_punctuation(self):
        rule = PolicyRule(id="r", predicate="approved_space_keys", value="SP",
                          operations=frozenset({"confluence.read_page"}))
        assert describe.rule_sentence(rule) == "Confluence - space SP: allow read"

    def test_value_display_overrides_the_raw_value_phrase(self):
        """A caller that already resolved the id(s) to a friendly name (Settings'
        Auto-accept page) can pass it through so the sentence agrees with it, instead of
        `rule_sentence` re-deriving the raw id from `rule.value` on its own."""
        rule = PolicyRule(id="r", predicate="approved_sandbox_folder", value=["FOLDER1"],
                          operations=frozenset({"sheets.write_range"}))
        assert (describe.rule_sentence(rule, value_display="Groceries")
                == "Drive - folder Groceries: allow update")

    def test_value_display_is_ignored_for_a_value_less_rule(self):
        rule = PolicyRule(id="r", predicate="i_am_owner", value=None,
                          operations=frozenset({"drive.read_file_contents"}))
        assert describe.rule_sentence(rule, value_display="should not appear") == "Drive - owned by me: allow read"


class TestConfirmationText:

    def test_it_describes_the_rules_that_will_actually_be_written(self):
        proposal = propose.proposals_for("drive_sheets_write_range", _drive_write_ctx())[0]
        text = describe.confirmation_text(proposal)
        assert text.splitlines() == [
            "Always allow: Drive - folder FOLDER1: allow update",
            "Covers 1 tool: drive_sheets_write_range",
        ]

    def test_taking_widenings_widens_the_text_too(self):
        proposal = propose.proposals_for("drive_sheets_write_range", _drive_write_ctx())[0]
        widenings = [w for w in proposal.widenings if w.verb in _WRITE_VERBS]
        text = describe.confirmation_text(proposal, widenings)
        assert "Covers 13 tools:" in text
        assert "delete" in text
