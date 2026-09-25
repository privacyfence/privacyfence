"""Tests for privacyfence.policy.propose.

Two things are being proved here:

1. **The catalogue is a faithful join, not a sixth hand-kept table.** Every one of the 21 grant
   capabilities in `resource_registry.GRANT_RESOURCE_TYPES` is reproduced exactly by deriving
   `(operation_key, rule_name)` pairs from the catalogue's declared verbs plus the verb registry
   (`policy.registry`) -- including the Drive sandbox folder's thirteen, the width one toggle
   stands for.
2. **One writer.** The popup's intent and Settings' intent, for the same scope and the same verbs,
   produce byte-identical rules.

(There is no v1-persistence path or v1 suggestion table to cross-check against: every rule lives
in the v2 `auto_accept:` store (ADR 0004), and `rules_for_proposal`/`rules_for_scope_group`/
`proposals_for` are what's under test here.)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from privacyfence.policy import propose, registry, resource_registry, scopes, store
from privacyfence.policy.registry import TOOL_REGISTRY, Verb, VerbFamily

from ...helpers import make_ctx

# ── Fixtures: one realistic gated call per tool family ──────────────────────────────────────────

_FILE = SimpleNamespace(id="f1", parent_ids=["FOLDER1"], owners=["me@corp.com"], mime_type="text/plain")
_FROM_ACME = SimpleNamespace(sender="Alice <alice@acme.com>", labels=["INBOX"], attachments=[])
_FROM_ME = SimpleNamespace(sender="Me <me@corp.com>", labels=["INBOX"], attachments=[])
_EVENT = SimpleNamespace(
    organizer_email="me@corp.com", attendees=[{"email": "colleague@corp.com"}], visibility="default",
)


def _drive_ctx(tool: str, **args: object):
    return make_ctx(connector="drive", tool=tool, args=dict(args), raw_data={"file": _FILE}, my_email="me@corp.com")


# (tool, ctx) -- the corpus both the "nothing lost" and the "extras are enumerated" checks run on.
CORPUS: tuple[tuple[str, object], ...] = (
    ("gmail_get_message", make_ctx(my_email="me@corp.com", raw_data=_FROM_ACME, args={"message_id": "m1"})),
    ("gmail_get_message", make_ctx(my_email="me@corp.com", raw_data=_FROM_ME, args={"message_id": "m1"})),
    ("gmail_get_thread", make_ctx(my_email="me@corp.com", raw_data=_FROM_ACME, args={"thread_id": "t1"})),
    ("gmail_download_attachment", make_ctx(my_email="me@corp.com", raw_data=_FROM_ACME, args={"message_id": "m1"})),
    ("gmail_archive_message", make_ctx(my_email="me@corp.com", raw_data=_FROM_ACME, args={"message_id": "m1"})),
    ("drive_get_file_content", _drive_ctx("drive_get_file_content", file_id="f1")),
    ("drive_download_file", _drive_ctx("drive_download_file", file_id="f1")),
    ("drive_sheets_get_values", _drive_ctx("drive_sheets_get_values", spreadsheet_id="f1")),
    (
        "slack_get_channel_history",
        make_ctx(connector="slack", args={"channel_id": "C123"},
                 raw_data=[SimpleNamespace(channel_id="C123", is_private=False, files=[])]),
    ),
    (
        "slack_get_channel_history",
        make_ctx(connector="slack", args={"channel_id": "D123"},
                 raw_data=[SimpleNamespace(channel_id="D123", is_private=True, files=[])]),
    ),
    (
        "slack_get_channel_history",
        make_ctx(connector="slack", args={"channel_id": "G1", "is_group_dm": True},
                 raw_data=[SimpleNamespace(channel_id="G1", is_private=True, files=[])]),
    ),
    (
        "slack_search_messages",
        make_ctx(connector="slack", args={"query": "x"},
                 raw_data=[SimpleNamespace(channel_id="C1", is_private=False, files=[]),
                           SimpleNamespace(channel_id="C2", is_private=False, files=[])]),
    ),
    (
        "calendar_get_event_details",
        make_ctx(connector="calendar", my_email="me@corp.com",
                 args={"calendar_id": "primary", "event_id": "e1"}, raw_data=_EVENT),
    ),
    ("salesforce_get_record", make_ctx(connector="salesforce", args={"object_type": "Account", "record_id": "r1"}, raw_data={})),
    ("salesforce_run_report", make_ctx(connector="salesforce", args={"report_id": "R1"}, raw_data={})),
    ("salesforce_search", make_ctx(connector="salesforce", args={"object_types": "Account,Contact"}, raw_data={})),
    (
        "jira_get_issue",
        make_ctx(connector="jira", my_email="me@corp.com", args={"issue_key": "PROJ-1"},
                 raw_data={"reporter": "me@corp.com", "assignee": "me@corp.com"}),
    ),
    (
        "confluence_get_page",
        make_ctx(connector="confluence", my_email="me@corp.com", args={"page_id": "p1"},
                 raw_data={"author": "me@corp.com", "space_key": "SP"}),
    ),
    (
        "confluence_download_attachment",
        make_ctx(connector="confluence", my_email="me@corp.com", args={"page_id": "p1"},
                 raw_data={"author": "me@corp.com", "space_key": "SP"}),
    ),
    (
        "telegram_get_messages",
        make_ctx(connector="telegram", args={"chat_id": 123}, raw_data=[SimpleNamespace(chat_id=123, media_type=None)]),
    ),
    (
        "telegram_search_messages",
        make_ctx(connector="telegram", args={"query": "x"}, raw_data=[SimpleNamespace(chat_id=123, media_type=None)]),
    ),
    ("gmail_create_draft", make_ctx(args={"to": "someone@acme.com"})),
    ("gmail_add_label", make_ctx(args={"label_name": "Done", "message_id": "m1"})),
    ("gmail_remove_label", make_ctx(args={"label_name": "Done", "message_id": "m1"})),
    ("drive_write_file_content", _drive_ctx("drive_write_file_content", file_id="f1")),
    ("drive_sheets_write_range", _drive_ctx("drive_sheets_write_range", spreadsheet_id="f1")),
    ("drive_sheets_format_range", _drive_ctx("drive_sheets_format_range", spreadsheet_id="f1")),
    ("drive_sheets_delete_dimensions", _drive_ctx("drive_sheets_delete_dimensions", spreadsheet_id="f1")),
    ("drive_docs_edit_content", _drive_ctx("drive_docs_edit_content", document_id="f1")),
    ("drive_add_comment", _drive_ctx("drive_add_comment", file_id="f1")),
    ("drive_move_file", _drive_ctx("drive_move_file", file_id="f1", destination_folder_id="D9")),
    ("drive_upload_file", make_ctx(connector="drive", tool="drive_upload_file", args={"parent_folder_id": "FOLDER1"})),
    ("calendar_create_event", make_ctx(connector="calendar", args={"calendar_id": "primary"})),
    ("calendar_update_event", make_ctx(connector="calendar", args={"calendar_id": "primary", "event_id": "e"})),
    ("calendar_set_event_visibility", make_ctx(connector="calendar", args={"calendar_id": "primary", "event_id": "e"})),
    ("calendar_set_event_color", make_ctx(connector="calendar", args={"calendar_id": "primary", "event_id": "e"})),
    ("calendar_delete_event", make_ctx(connector="calendar", args={"calendar_id": "primary", "event_id": "e"})),
    ("jira_create_issue", make_ctx(connector="jira", args={"project_key": "PROJ"})),
    ("jira_add_comment", make_ctx(connector="jira", args={"issue_key": "PROJ-1"})),
    ("jira_update_issue", make_ctx(connector="jira", args={"issue_key": "PROJ-1"})),
    ("jira_transition_issue", make_ctx(connector="jira", args={"issue_key": "PROJ-1"})),
    ("confluence_create_page", make_ctx(connector="confluence", args={"space_key": "SP"})),
    ("confluence_update_page", make_ctx(connector="confluence", args={"space_key": "SP", "page_id": "p1"})),
    ("tasks_create_task", make_ctx(connector="tasks", args={"task_list_id": "L1"})),
    ("tasks_update_task", make_ctx(connector="tasks", args={"task_list_id": "L1", "task_id": "t"})),
    ("tasks_complete_task", make_ctx(connector="tasks", args={"task_list_id": "L1", "task_id": "t"})),
    ("tasks_move_task", make_ctx(connector="tasks", args={"source_list_id": "L1", "destination_list_id": "L2"})),
    ("slack_send_message", make_ctx(connector="slack", args={"channel_id": "C123", "text": "hi"})),
    ("telegram_send_message", make_ctx(connector="telegram", args={"chat_id": 123, "text": "hi"})),
    ("contacts_add_label", make_ctx(connector="contacts", args={"label_name": "Friends", "resource_name": "c1"})),
)

def _sorted_dicts(rules) -> list[dict]:
    return sorted((store.rule_to_dict(rule) for rule in rules), key=lambda d: d["predicate"])


class TestCatalogueIsDerived:
    """The catalogue declares verbs; operation keys come from the verb registry. These assertions
    are what stop it becoming a sixth table that drifts."""

    @pytest.mark.parametrize(
        "resource_type,capability",
        [
            (rt, cap)
            for rt in resource_registry.GRANT_RESOURCE_TYPES
            for cap in rt.capabilities
        ],
        ids=lambda arg: arg if isinstance(arg, str) else f"{arg.connector}.{arg.config_key}",
    )
    def test_every_grant_capability_is_reproduced_exactly(self, resource_type, capability):
        """A grant capability is a verb set in disguise. Deriving that verb set from the catalogue
        and expanding it back must give the capability's own targets, with no pair added and none
        dropped -- notably the sandbox folder's thirteen, whose `delete` and whose upload/move
        predicate aliases are the easiest things to get wrong."""
        targets = set(resource_type.capabilities[capability].targets)
        predicates = {
            predicate
            for cap in resource_type.capabilities.values()
            for _operation, predicate in cap.targets
        }
        groups = {entry.widening_group for entry in propose.PROPOSABLE_SCOPES if entry.predicate in predicates}
        verbs = {
            verb
            for operation, predicate in targets
            for entry in propose.PROPOSABLE_SCOPES
            if entry.predicate == predicate and entry.connector == propose.connector_of_operation(operation)
            for verb in entry.verbs
            if verb in registry.operation_verbs(operation)
        }
        derived = {
            (operation, entry.predicate)
            for group in groups
            for entry in propose._SCOPES_BY_GROUP[group]
            for verb in verbs
            for operation in propose.operations_for(entry, verb)
        }
        assert derived == targets

    def test_every_catalogue_predicate_is_a_real_selector(self):
        for entry in propose.PROPOSABLE_SCOPES:
            assert entry.predicate in scopes.SCOPE_SELECTORS, entry.id

    def test_operations_stay_inside_the_scopes_connector(self):
        for entry in propose.PROPOSABLE_SCOPES:
            for verb in entry.verbs:
                for operation in propose.operations_for(entry, verb):
                    assert propose.connector_of_operation(operation) == entry.connector
                    assert verb in registry.operation_verbs(operation)

    def test_operations_for_a_verb_the_scope_does_not_govern_is_empty(self):
        folder = next(e for e in propose.PROPOSABLE_SCOPES if e.id == "approved_folder")
        assert propose.operations_for(folder, Verb.DELETE) == frozenset()

    def test_excluded_operations_are_never_derived(self):
        """`personal_calendar` has nothing to check for the two calendar tools that carry no
        `calendar_id` at all, and the two Salesforce read scopes cannot measure each other's
        operation key -- derivation over a shared verb has to be told so, or it writes a rule that
        can never match."""
        calendar = next(e for e in propose.PROPOSABLE_SCOPES if e.id == "personal_calendar")
        derived = {op for verb in calendar.verbs for op in propose.operations_for(calendar, verb)}
        assert "calendar.out_of_office" not in derived
        assert "calendar.working_location" not in derived
        object_types = next(e for e in propose.PROPOSABLE_SCOPES if e.id == "approved_object_types")
        assert "salesforce.run_report" not in propose.operations_for(object_types, Verb.READ)
        reports = next(e for e in propose.PROPOSABLE_SCOPES if e.id == "approved_report_ids")
        assert "salesforce.read_record" not in propose.operations_for(reports, Verb.READ)

    def test_catalogue_ids_are_unique(self):
        """`label_name_allowlist` serves two scope types, so an entry cannot always be identified
        by its predicate -- a duplicate id would make `next(e for e in ... if e.id == ...)` silently
        pick whichever came first."""
        ids = [entry.id for entry in propose.PROPOSABLE_SCOPES]
        assert len(ids) == len(set(ids))

    def test_widening_group_members_share_a_scope_type(self):
        for group, entries in propose._SCOPES_BY_GROUP.items():
            assert len({entry.scope_type for entry in entries}) == 1, group

    def test_connector_of_operation_maps_sheets_and_docs_onto_drive(self):
        assert propose.connector_of_operation("sheets.write_range") == "drive"
        assert propose.connector_of_operation("docs.format_content") == "drive"
        assert propose.connector_of_operation("gmail.read_message") == "gmail"

    def test_verb_sort_key_orders_read_before_write_before_send_before_destructive(self):
        ordered = sorted([Verb.DELETE, Verb.SEND, Verb.UPDATE, Verb.READ], key=propose.verb_sort_key)
        assert [registry.VERB_FAMILY[verb] for verb in ordered] == [
            VerbFamily.READ, VerbFamily.WRITE, VerbFamily.SEND, VerbFamily.DESTRUCTIVE,
        ]


class TestProposalsAgainstTheV1Tables:
    """The five v1 suggestion tables remain the oracle (the redesign proposal's own posture: "the
    old functions are the oracle, not the spec")."""

    @pytest.mark.parametrize("tool,ctx", CORPUS, ids=[f"{t}-{i}" for i, (t, _c) in enumerate(CORPUS)])
    def test_every_proposal_would_have_accepted_the_item_it_came_from(self, tool, ctx):
        """The invariant that lets the catalogue's value builders stay this small: a builder only
        has to find the field, because the scope's own selector decides. A proposal that the
        selector would not match is a proposal the popup must never make."""
        for proposal in propose.proposals_for(tool, ctx):
            rules = propose.rules_for_proposal(proposal)
            from privacyfence.policy import engine

            accepted, _rule_id = engine.evaluate(rules, proposal.operation, ctx)
            assert accepted, proposal.scope.id

    def test_identity_scopes_are_proposed_before_attribute_scopes(self):
        """Narrowest first, which is a deliberate change from v1's `SUGGESTION_FAMILIES` order
        (that put "every file you own" ahead of "this one folder")."""
        ctx = _drive_ctx("drive_get_file_content", file_id="f1")
        assert [p.scope.id for p in propose.proposals_for("drive_get_file_content", ctx)] == [
            "approved_folder", "i_am_owner",
        ]

    def test_an_ungoverned_or_unknown_tool_proposes_nothing(self):
        assert propose.proposals_for("drive_list_files", make_ctx()) == []
        assert propose.proposals_for("not_a_tool", make_ctx()) == []

    def test_the_extra_scope_operations_stay_unproposed(self):
        """ADR 0077: the operations policy/catalogue.py's EXTRA_SCOPES governs are configured
        deliberately, from Settings or privacyfence_propose_policy_change, and the popup never
        proposes a rule for them."""
        for tool, ctx in (
            ("gmail_create_filter", make_ctx(args={"criteria": "x"})),
            ("gmail_update_filter", make_ctx(args={"filter_id": "f"})),
            ("slack_create_group_chat", make_ctx(connector="slack", args={"user_ids": "U1,U2"})),
            ("apps_script_get_content", make_ctx(connector="apps_script", args={"script_id": "s1"})),
            ("apps_script_write_content", make_ctx(connector="apps_script", args={"script_id": "s1"})),
            ("apps_script_get_execution_log", make_ctx(connector="apps_script", args={"script_id": "s1"})),
        ):
            assert propose.proposals_for(tool, ctx) == [], tool

    def test_a_scope_with_nothing_to_derive_is_not_proposed(self):
        """An unscoped Salesforce search reaches the whole globally-searchable set, and a Drive
        file with no parent has no folder -- neither yields a value, so neither is offered."""
        assert propose.proposals_for("salesforce_search", make_ctx(connector="salesforce", args={}, raw_data={})) == []
        orphan = SimpleNamespace(id="f2", parent_ids=[], owners=[])
        ctx = make_ctx(connector="drive", tool="drive_write_file_content", args={"file_id": "f2"}, raw_data=orphan)
        assert propose.proposals_for("drive_write_file_content", ctx) == []

    def test_a_move_proposal_names_both_the_source_and_the_destination_folder(self):
        ctx = _drive_ctx("drive_move_file", file_id="f1", destination_folder_id="D9")
        move = next(p for p in propose.proposals_for("drive_move_file", ctx)
                    if p.scope.id == "move_within_approved_folders")
        assert move.value == ["D9", "FOLDER1"]
        assert scopes.SCOPE_SELECTORS["move_within_approved_folders"].matches(move.value, ctx) is True

    def test_a_move_with_no_destination_or_no_source_folder_is_not_proposed(self):
        no_destination = _drive_ctx("drive_move_file", file_id="f1")
        orphan = make_ctx(connector="drive", tool="drive_move_file", args={"destination_folder_id": "D9"},
                          raw_data={"file": SimpleNamespace(id="f2", parent_ids=[], owners=[])})
        for ctx in (no_destination, orphan):
            assert [p for p in propose.proposals_for("drive_move_file", ctx)
                    if p.scope.id == "move_within_approved_folders"] == []

    def test_a_selector_that_raises_is_a_non_match_not_a_crash(self, monkeypatch):
        boom = scopes.ScopeSelector(
            predicate="approved_folder", scope_type="drive.folder", kind=scopes.ScopeKind.IDENTITY,
            resolves_from=scopes.ResolvesFrom.FETCHED,
            matches=lambda _value, _ctx: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        monkeypatch.setitem(scopes.SCOPE_SELECTORS, "approved_folder", boom)
        ctx = _drive_ctx("drive_get_file_content", file_id="f1")
        assert [p.scope.id for p in propose.proposals_for("drive_get_file_content", ctx)] == ["i_am_owner"]

    def test_an_unknown_predicate_or_condition_is_a_non_match(self):
        """Fail closed on a scope whose selector has gone missing -- the same posture
        `policy.engine.evaluate` takes for a predicate it cannot find. Checked against a fabricated
        catalogue entry rather than by deleting a live selector, which would reorder
        `SCOPE_SELECTORS` for every other test in the process."""
        ctx = _drive_ctx("drive_get_file_content", file_id="f1")
        unknown_scope = propose._scope(
            "not_a_predicate", "drive.folder", "drive", (Verb.READ,), lambda _ctx: ["FOLDER1"], "x",
        )
        assert propose._candidate_value(unknown_scope, ctx) is propose.NO_VALUE
        unknown_condition = propose._scope(
            "always_allow", "drive.anything", "drive", (Verb.READ,), propose._no_value_needed, "x",
            condition=("not_a_condition", None),
        )
        assert propose._candidate_value(unknown_condition, ctx) is propose.NO_VALUE


class TestNarrowestVerbFirst:

    def test_a_proposal_covers_exactly_the_gated_operation_key(self):
        """The whole no-widening argument: accepting a proposal as offered writes one
        operation key, which is what `add_auto_accept_rule` writes today."""
        for tool, ctx in CORPUS:
            for proposal in propose.proposals_for(tool, ctx):
                assert proposal.operations == frozenset({TOOL_REGISTRY[tool].operation})

    def test_widenings_are_ordered_narrowest_family_first(self):
        ctx = _drive_ctx("drive_sheets_write_range", spreadsheet_id="f1")
        proposal = propose.proposals_for("drive_sheets_write_range", ctx)[0]
        families = [registry.VERB_FAMILY[w.verb] for w in proposal.widenings]
        assert families == sorted(families, key=propose._FAMILY_ORDER.index)
        assert families[-1] is VerbFamily.DESTRUCTIVE

    def test_a_widening_never_repeats_what_the_proposal_already_covers(self):
        ctx = _drive_ctx("drive_sheets_write_range", spreadsheet_id="f1")
        proposal = propose.proposals_for("drive_sheets_write_range", ctx)[0]
        for widening in proposal.widenings:
            assert not (widening.pairs & proposal.pairs)

    def test_a_widening_can_add_a_predicate_for_an_operation_key_already_covered(self):
        """A Slack read widened to search is the same operation key under its all-results
        predicate -- the reason a widening's unit is a `(predicate, operation)` pair and not an
        operation key."""
        ctx = make_ctx(connector="slack", args={"channel_id": "C123"},
                       raw_data=[SimpleNamespace(channel_id="C123", is_private=False, files=[])])
        proposal = next(p for p in propose.proposals_for("slack_get_channel_history", ctx)
                        if p.scope.id == "approved_channel")
        search = next(w for w in proposal.widenings if w.verb is Verb.SEARCH)
        assert search.pairs == frozenset({("approved_channel_all_results", "slack.read_messages")})
        assert search.operations == frozenset({"slack.read_messages"})

    def test_an_unconditional_scope_is_never_widenable(self):
        """An unconditional grant reads as unconditional (see test_describe.py); it must also not be one
        click away from being wider."""
        draft = propose.proposals_for("gmail_create_draft", make_ctx(args={"to": "x@y.com"}))[0]
        assert draft.scope.scope_type == "gmail.anything"
        assert draft.widenings == ()
        calendar_ctx = make_ctx(connector="calendar", my_email="me@corp.com",
                                args={"calendar_id": "primary"}, raw_data=_EVENT)
        conditional = [p for p in propose.proposals_for("calendar_get_event_details", calendar_ctx)
                       if p.scope.condition is not None]
        assert conditional and all(p.widenings == () for p in conditional)

    def test_mutually_exclusive_attributes_do_not_widen_into_each_other(self):
        """"My own DM" must never offer "…and every group DM" as a widening of itself."""
        ctx = make_ctx(connector="slack", args={"channel_id": "D123", "is_self_dm": True},
                       raw_data=[SimpleNamespace(channel_id="D123", is_private=True, files=[])])
        dm = next(p for p in propose.proposals_for("slack_get_channel_history", ctx)
                  if p.scope.id == "dm_with_myself")
        assert dm.widenings == ()


class TestOneWriter:
    """Popup and Settings, given the same intent, produce byte-identical rules."""

    SANDBOX_WRITE_VERBS = (
        Verb.UPDATE, Verb.FORMAT, Verb.RESTRUCTURE, Verb.COMMENT, Verb.DELETE, Verb.CREATE, Verb.MOVE,
    )

    def test_popup_and_settings_produce_identical_rules(self):
        ctx = _drive_ctx("drive_sheets_write_range", spreadsheet_id="f1")
        proposal = propose.proposals_for("drive_sheets_write_range", ctx)[0]
        widenings = [w for w in proposal.widenings if w.verb in self.SANDBOX_WRITE_VERBS]
        popup = propose.rules_for_proposal(proposal, widenings)
        settings = propose.rules_for_scope_group("drive.folder", ["FOLDER1"], self.SANDBOX_WRITE_VERBS)
        assert [store.rule_to_dict(r) for r in popup] == [store.rule_to_dict(r) for r in settings]

    def test_rules_are_merged_under_stable_content_derived_ids(self):
        rules = propose.rules_for_scope_group("drive.folder", ["FOLDER1"], self.SANDBOX_WRITE_VERBS)
        # One row per predicate, not one per operation key -- and the id is the one `policy.store`
        # would mint for that row's meaning, so it survives a re-write unchanged.
        assert sorted(r.predicate for r in rules) == [
            "approved_sandbox_folder", "move_within_approved_folders", "parent_folder_allowlist",
        ]
        for rule in rules:
            assert rule.id == store.rule_id_for(rule.predicate, rule.value, rule.conditions)

    def test_pair_order_does_not_change_the_result(self):
        forward = propose.rules_for_scope_group("drive.folder", ["FOLDER1"], self.SANDBOX_WRITE_VERBS)
        backward = propose.rules_for_scope_group("drive.folder", ["FOLDER1"], tuple(reversed(self.SANDBOX_WRITE_VERBS)))
        assert [store.rule_to_dict(r) for r in forward] == [store.rule_to_dict(r) for r in backward]

    def test_an_unknown_scope_group_yields_no_rules(self):
        assert propose.rules_for_scope_group("nope.nothing", ["x"], (Verb.READ,)) == []
