"""Tests for privacyfence.policy.scope_rows: the one-row-per-scope model (ADR 0172).

The invariant that matters most is the upgrade guarantee: ``migrate_v2`` followed by the written form
(``rows_to_config`` / ``rows_from_section``) and ``compile_rows`` decides exactly what the v2 rules
decided, or keeps the rules it cannot merge as custom rows under their old ids.
"""
from __future__ import annotations

import dataclasses

import pytest

from privacyfence.policy import catalogue, propose, scope_rows, store
from privacyfence.policy.engine import PolicyRule
from privacyfence.policy.registry import Verb
from privacyfence.policy.scope_rows import ScopeRow
from privacyfence.policy.value_format import VALUE_FORMATS

FOLDER = "FOLDER1"
FLAGGED_ID = "r-flagged0001"


def _decisions(rules):
    """What a rule list decides, ignoring ids and rule order."""
    return {(r.predicate, r.operations, r.conditions) for r in rules}


def _v2(group, value, verbs):
    return catalogue.rules_for_catalogue_entry(group, value, verbs)


def _dicts(rules):
    return [store.rule_to_dict(r) for r in rules]


def _through_disk(migration):
    """migrate_v2 -> written form -> read back -> compiled."""
    section = scope_rows.rows_to_config(migration.rows)
    rows = scope_rows.rows_from_section(section)
    return rows, scope_rows.compile_rows(rows)


def _rule(rule_id="r-x", predicate="approved_folder", value=None, operations=("drive.read_file_contents",), conditions=()):
    return PolicyRule(
        id=rule_id, predicate=predicate, value=value, operations=frozenset(operations), conditions=tuple(conditions),
    )


def _row(scope, value, verbs, operations=None, **kwargs):
    verbs = frozenset(verbs)
    ops = scope_rows.implied_operations(scope, verbs) if operations is None else frozenset(operations)
    return ScopeRow(id=scope_rows.row_id_for(scope, value), scope=scope, value=value, verbs=verbs, operations=ops,
                    **kwargs)


def _all_groups():
    return [*propose.SCOPES_BY_GROUP, *catalogue.EXTRA_SCOPES]


def _needs_value(group):
    if group in catalogue.EXTRA_SCOPES:
        return catalogue.EXTRA_SCOPES[group].needs_value
    return propose.scope_needs_value(propose.SCOPES_BY_GROUP[group][0])


def _example_value(group):
    if group in catalogue.EXTRA_SCOPES:
        predicate = catalogue.EXTRA_SCOPES[group].predicate
    else:
        predicate = propose.SCOPES_BY_GROUP[group][0].predicate
    return VALUE_FORMATS[predicate].example if predicate in VALUE_FORMATS else "V1"


# ── Migration ──────────

@pytest.mark.parametrize("group", _all_groups())
def test_each_catalogue_group_round_trips_through_migration(group):
    value = [_example_value(group)] if _needs_value(group) else None
    verbs = scope_rows.scope_verbs(group)
    v2 = _v2(group, value, list(verbs))
    assert v2

    migration = scope_rows.migrate_v2(_dicts(v2))
    rows, compiled = _through_disk(migration)

    assert len(rows) == 1
    assert rows[0].scope == group
    assert rows[0].verbs == frozenset(verbs)
    assert _decisions(compiled) == _decisions(v2)

def test_upgrade_round_trip_preserves_every_rule():
    seeded = _rule("r-d76028ec0b", "always_allow", None, ["contacts.edit"], [("no_contact_info_change", None)])
    assert store.rule_id_for("always_allow", None, seeded.conditions) == seeded.id
    handwritten = _rule("my-handwritten", "approved_folder", ["F9"], ["drive.write_file"])
    unknown = _rule("r-unknown", "some_predicate", ["x"], ["gmail.read_message"])
    drive_file = _rule("r-drivefile", "drive.file", ["FILE1"], ["drive.read_file_contents"])
    partial = _rule("r-partial", "approved_sandbox_folder", ["F2"], ["sheets.format_range"])
    grouped = _v2("drive.folder", [FOLDER], [Verb.READ, Verb.DOWNLOAD, Verb.UPDATE])
    v2 = [seeded, handwritten, unknown, drive_file, partial, *grouped]

    migration = scope_rows.migrate_v2(_dicts(v2))
    rows, compiled = _through_disk(migration)

    assert _decisions(compiled) == _decisions(v2)
    custom_ids = {row.id for row in rows if row.scope is None}
    assert custom_ids == {"r-d76028ec0b", "my-handwritten", "r-unknown", "r-drivefile"}
    seeded_row = next(row for row in rows if row.id == "r-d76028ec0b")
    assert seeded_row.verbs == frozenset({Verb.UPDATE})
    assert seeded_row.conditions == (("no_contact_info_change", None),)
    assert seeded_row.former_ids == ()
    assert migration.member_pairs["r-d76028ec0b"] == {("always_allow", "contacts.edit")}
    assert "my-handwritten" not in migration.id_map

def test_screenshot_case_merges_to_one_row():
    narrow = _rule("r-narrow", "approved_folder", [FOLDER], ["drive.download_file"])
    flagged = {
        "id": FLAGGED_ID, "predicate": "approved_folder", "value": [FOLDER],
        "operations": ["drive.read_file_contents", "drive.download_file"], "conditions": [],
        "allow_personal_data": True,
    }

    migration = scope_rows.migrate_v2([*_dicts([narrow]), flagged])

    (row,) = migration.rows
    assert row.scope == "drive.folder"
    assert row.verbs == {Verb.READ, Verb.DOWNLOAD}
    assert row.former_ids == tuple(sorted(["r-narrow", FLAGGED_ID]))
    assert migration.personal_data_ids == {FLAGGED_ID}
    assert migration.id_map == {"r-narrow": row.id, FLAGGED_ID: row.id}
    assert migration.members == {row.id: {"r-narrow", FLAGGED_ID}}

def _assert_read_does_not_become_search(read_pred, search_pred, op):
    read_only = _rule("r-read", read_pred, ["C1"], [op])

    (row,) = scope_rows.migrate_v2(_dicts([read_only])).rows
    compiled = scope_rows.compile_row(row)

    assert row.verbs == {Verb.READ}
    assert [r.predicate for r in compiled] == [read_pred]

    both = scope_rows.migrate_v2(_dicts([read_only, _rule("r-search", search_pred, ["C1"], [op])]))
    (merged,) = both.rows
    assert merged.verbs == {Verb.READ, Verb.SEARCH}
    assert {r.predicate for r in scope_rows.compile_row(merged)} == {read_pred, search_pred}

def test_slack_read_does_not_become_search():
    _assert_read_does_not_become_search(
        "approved_channel", "approved_channel_all_results", "slack.read_messages")

def test_telegram_read_does_not_become_search():
    _assert_read_does_not_become_search(
        "approved_chats", "approved_chats_all_results", "telegram.read_chat_messages")

def test_jira_read_does_not_become_search():
    _assert_read_does_not_become_search(
        "approved_project_keys", "approved_project_keys_all_results", "jira.read_issue")

def test_partial_verb_is_not_widened():
    partial = _rule("r-partial", "approved_sandbox_folder", [FOLDER], ["sheets.format_range"])

    migration = scope_rows.migrate_v2(_dicts([partial]))
    (row,) = migration.rows
    _rows, compiled = _through_disk(migration)

    assert row.verbs == {Verb.UPDATE}
    assert row.operations == {"sheets.format_range"}
    assert _decisions(compiled) == _decisions([partial])
    expected = scope_rows.implied_operations("drive.folder", [Verb.UPDATE]) - {"sheets.format_range"}
    assert expected
    assert scope_rows.missing_operations(row) == expected
    assert "drive_sheets_write_range" in scope_rows.missing_tools(row)

def test_a_group_that_cannot_merge_losslessly_stays_custom():
    # approved_folder carrying a write operation its own entry does not derive.
    odd = _rule("r-odd", "approved_folder", [FOLDER], ["drive.write_file"])
    fine = _rule("r-fine", "approved_folder", [FOLDER], ["drive.read_file_contents"])

    migration = scope_rows.migrate_v2(_dicts([odd, fine]))
    _rows, compiled = _through_disk(migration)

    assert [(r.id, r.scope) for r in migration.rows] == [("r-odd", None), ("r-fine", None)]
    assert all(r.former_ids == () for r in migration.rows)
    assert _decisions(compiled) == _decisions([odd, fine])
    assert migration.id_map == {}

def test_rows_keep_the_order_of_their_first_member():
    custom = _rule("r-c", "some_predicate", None, ["gmail.read_message"])
    a1 = _rule("r-a1", "approved_folder", ["A"], ["drive.read_file_contents"])
    b = _rule("r-b", "approved_folder", ["B"], ["drive.read_file_contents"])
    a2 = _rule("r-a2", "approved_folder", ["A"], ["drive.download_file"])

    migration = scope_rows.migrate_v2(_dicts([a1, custom, b, a2]))

    assert [r.value for r in migration.rows] == [["A"], None, ["B"]]
    assert migration.rows[0].former_ids == ("r-a1", "r-a2")

def test_malformed_input_migrates_to_nothing():
    assert scope_rows.migrate_v2("nope") == scope_rows.V2Migration()
    assert scope_rows.migrate_v2([{"id": ""}, "x"]).rows == []

def test_condition_rule_outside_its_entry_stays_custom():
    condition = ("no_external_attendees", None)
    inside = _rule("r-in", "always_allow", None, ["calendar.read_event_details"], [condition])
    outside = _rule("r-out", "always_allow", None, ["calendar.delete_event"], [condition])

    assert scope_rows.scope_of_rule(inside) == ("calendar.anything/no_external_attendees", None)
    assert scope_rows.scope_of_rule(outside) is None

def test_scope_of_rule_refuses_what_no_single_scope_claims():
    two_conditions = _rule(
        "r-2", "always_allow", None, ["calendar.read_event_details"],
        [("no_external_attendees", None), ("not_private", None)])
    unknown_condition = _rule("r-u", "always_allow", None, ["calendar.read_event_details"], [("nope", None)])
    no_candidate = _rule("r-n", "approved_folder", None, ["slack.read_messages"])

    assert scope_rows.scope_of_rule(two_conditions) is None
    assert scope_rows.scope_of_rule(unknown_condition) is None
    assert scope_rows.scope_of_rule(no_candidate) is None

def test_scope_of_rule_resolves_a_shared_predicate_by_connector():
    gmail = _rule("r-g", "label_name_allowlist", ["L"], ["gmail.add_label"])
    contacts = _rule("r-c", "label_name_allowlist", ["L"], ["contacts.add_label"])

    assert scope_rows.scope_of_rule(gmail) == ("gmail.label", ["L"])
    assert scope_rows.scope_of_rule(contacts) == ("contacts.label", ["L"])

def test_scope_of_rule_maps_an_extra_scope():
    rule = _v2("grist.document", ["D1"], [Verb.READ])[0]

    assert scope_rows.scope_of_rule(rule) == ("grist.document", ["D1"])


# ── RowId ──────────

def test_row_id_ignores_verbs():
    a = _row("drive.folder", [FOLDER], [Verb.READ])
    b = _row("drive.folder", [FOLDER], [Verb.READ, Verb.DOWNLOAD, Verb.UPDATE])

    assert a.id == b.id
    assert a.id.startswith("r-") and len(a.id) == 12

def test_row_id_is_value_order_independent():
    assert scope_rows.row_id_for("drive.folder", ["a", "b"]) == scope_rows.row_id_for("drive.folder", ["b", "a"])
    assert scope_rows.row_id_for("drive.folder", ["a"]) != scope_rows.row_id_for("drive.folder", ["b"])
    assert scope_rows.row_id_for("drive.folder", ["a"]) != scope_rows.row_id_for("tasks.list", ["a"])


# ── CatalogueLookups ──────────

def test_lookups_return_none_for_the_wrong_kind_or_no_scope():
    assert scope_rows.group_entries("drive.folder")
    assert scope_rows.group_entries("grist.document") is None
    assert scope_rows.group_entries(None) is None
    assert scope_rows.extra_scope("grist.document") is catalogue.EXTRA_SCOPES["grist.document"]
    assert scope_rows.extra_scope("drive.folder") is None
    assert scope_rows.extra_scope(None) is None

def test_scope_verbs_follow_the_risk_order_and_unknown_is_empty():
    assert scope_rows.scope_verbs("drive.folder") == (
        Verb.READ, Verb.DOWNLOAD, Verb.ADD, Verb.UPDATE, Verb.RESTRUCTURE, Verb.MOVE, Verb.DELETE)
    assert scope_rows.scope_verbs("grist.document") == (Verb.READ, Verb.ADD, Verb.UPDATE, Verb.RESTRUCTURE)
    assert scope_rows.scope_verbs("nope") == ()
    assert scope_rows.implied_operations("nope", [Verb.READ]) == frozenset()

def test_an_extra_scope_ignores_verbs_it_does_not_offer():
    assert scope_rows.implied_operations("peek.page", [Verb.DELETE]) == frozenset()
    assert scope_rows.implied_operations("peek.page", [Verb.READ])

def test_a_row_for_an_unknown_scope_compiles_to_nothing():
    row = ScopeRow(id="r-x", scope="nope", value=None, verbs=frozenset({Verb.READ}), operations=frozenset({"a.b"}))

    assert scope_rows.compile_row(row) == []


# ── Compile ──────────

def test_a_propose_group_compiles_one_rule_per_entry_with_operations():
    row = _row("drive.folder", [FOLDER], [Verb.READ, Verb.UPDATE])

    rules = scope_rows.compile_rows([row])

    assert {r.predicate for r in rules} == {"approved_folder", "approved_sandbox_folder"}
    assert all(r.id == row.id and r.value == [FOLDER] for r in rules)

def test_operations_the_row_lacks_are_not_compiled():
    row = _row("drive.folder", [FOLDER], [Verb.READ, Verb.DOWNLOAD], operations=["drive.download_file"])

    (rule,) = scope_rows.compile_row(row)

    assert rule.operations == {"drive.download_file"}

def test_a_condition_scope_compiles_with_its_condition():
    row = _row("calendar.anything/not_private", None, [Verb.READ])

    (rule,) = scope_rows.compile_row(row)

    assert (rule.predicate, rule.conditions) == ("always_allow", (("not_private", None),))

def test_an_extra_scope_compiles_to_its_predicate():
    row = _row("slack.group_chats", None, [Verb.SEND])

    (rule,) = scope_rows.compile_row(row)

    assert rule.predicate == "slack.anything"
    assert rule.operations == {"slack.create_group_chat"}

def test_an_extra_row_with_no_matching_operations_compiles_to_nothing():
    row = _row("peek.page", ["P"], [Verb.READ], operations=["peek.not_a_read"])

    assert scope_rows.compile_row(row) == []

def test_a_custom_row_compiles_to_exactly_one_rule():
    row = ScopeRow(
        id="r-c", scope=None, value=["v"], verbs=frozenset(), operations=frozenset({"x.read"}),
        predicate="some_predicate", conditions=(("c", None),))

    assert scope_rows.compile_row(row) == [
        PolicyRule(id="r-c", predicate="some_predicate", value=["v"],
                   operations=frozenset({"x.read"}), conditions=(("c", None),))]


class TestExtraVerbPredicates:
    def test_read_and_search_compile_to_two_rules_sharing_the_row_id(self):
        row = _row("slack.mentions_of_me", None, [Verb.READ, Verb.SEARCH])

        rules = scope_rows.compile_row(row)

        assert [r.predicate for r in rules] == ["slack.mentions_of_me", "slack.mentions_of_me_all_results"]
        assert {r.id for r in rules} == {row.id}
        assert all(r.operations == {"slack.read_messages"} for r in rules)

    def test_read_only_compiles_to_the_thread_rule_only(self):
        (rule,) = scope_rows.compile_row(_row("slack.mentions_of_me", None, [Verb.READ]))

        assert rule.predicate == "slack.mentions_of_me"

    def test_search_only_compiles_to_the_all_results_rule_only(self):
        (rule,) = scope_rows.compile_row(_row("slack.mentions_of_me", None, [Verb.SEARCH]))

        assert rule.predicate == "slack.mentions_of_me_all_results"

    def test_add_rules_maps_a_search_only_rule_to_a_scoped_row(self):
        (rule,) = catalogue.rules_for_catalogue_entry("slack.mentions_of_me", None, [Verb.SEARCH])

        rows, touched = scope_rows.add_rules([], [rule])

        (row,) = rows
        assert (row.scope, row.verbs) == ("slack.mentions_of_me", frozenset({Verb.SEARCH}))
        assert touched == [row.id]


# ── Serialization ──────────

def test_row_round_trips_and_sorts_its_lists():
    row = _row("drive.folder", [FOLDER], [Verb.READ, Verb.DOWNLOAD], former_ids=("r-b", "r-a"))

    data = scope_rows.row_to_dict(row)

    assert data["verbs"] == ["download", "read"]
    assert data["operations"] == sorted(data["operations"])
    assert data["former_ids"] == ["r-a", "r-b"]
    assert "predicate" not in data and "conditions" not in data
    assert scope_rows.row_from_dict(data) == dataclasses.replace(row, former_ids=("r-a", "r-b"))

def test_custom_row_round_trips_with_its_conditions_and_no_former_ids():
    row = ScopeRow(
        id="r-c", scope=None, value=None, verbs=frozenset({Verb.UPDATE}),
        operations=frozenset({"contacts.edit"}), predicate="always_allow",
        conditions=(("no_contact_info_change", None),))

    data = scope_rows.row_to_dict(row)

    assert data["conditions"] == [["no_contact_info_change", None]]
    assert "scope" not in data and "former_ids" not in data
    assert scope_rows.row_from_dict(data) == row

def test_legacy_verb_names_load_and_unknown_ones_are_dropped():
    data = {"id": "r-1", "scope": "drive.folder", "value": [FOLDER], "operations": ["drive.write_file"],
            "verbs": ["create", "bogus", 7, "update"]}

    row = scope_rows.row_from_dict(data)

    assert row is not None and row.verbs == {Verb.ADD, Verb.UPDATE}

@pytest.mark.parametrize("data", [
    "not a dict",
    {"scope": "drive.folder", "operations": ["a"], "verbs": ["read"]},
    {"id": "", "scope": "drive.folder", "operations": ["a"], "verbs": ["read"]},
    {"id": "r-1", "scope": "drive.folder", "operations": [], "verbs": ["read"]},
    {"id": "r-1", "scope": "drive.folder", "operations": "a", "verbs": ["read"]},
    {"id": "r-1", "scope": "drive.folder", "operations": [1], "verbs": ["read"]},
    {"id": "r-1", "operations": ["a"], "verbs": ["read"]},
    {"id": "r-1", "predicate": "", "operations": ["a"]},
    {"id": "r-1", "scope": "not.a.scope", "operations": ["a"], "verbs": ["read"]},
    {"id": "r-1", "scope": 5, "operations": ["a"], "verbs": ["read"]},
    {"id": "r-1", "scope": "drive.folder", "operations": ["a"], "verbs": []},
    {"id": "r-1", "scope": "drive.folder", "operations": ["a"], "verbs": ["bogus"]},
    {"id": "r-1", "scope": "drive.folder", "operations": ["a"]},
    {"id": "r-1", "predicate": "p", "operations": ["a"], "conditions": "x"},
    {"id": "r-1", "predicate": "p", "operations": ["a"], "conditions": [["only-one"]]},
    {"id": "r-1", "predicate": "p", "operations": ["a"], "conditions": [[1, None]]},
])
def test_row_from_dict_fails_closed(data):
    assert scope_rows.row_from_dict(data) is None

def test_custom_row_with_empty_verbs_loads():
    row = scope_rows.row_from_dict(
        {"id": "r-c", "predicate": "always_allow", "operations": ["contacts.edit"], "verbs": [],
         "conditions": None})

    assert row is not None
    assert row.scope is None
    assert row.verbs == {Verb.UPDATE}
    assert row.conditions == ()

def test_malformed_former_ids_are_ignored():
    row = scope_rows.row_from_dict(
        {"id": "r-1", "scope": "drive.folder", "operations": ["drive.read_file_contents"], "verbs": ["read"],
         "former_ids": ["r-b", 3, "r-a"]})
    assert row is not None and row.former_ids == ("r-a", "r-b")

def test_rows_from_section_drops_bad_entries():
    good = scope_rows.row_to_dict(_row("drive.folder", [FOLDER], [Verb.READ]))

    assert [r.id for r in scope_rows.rows_from_section({"rules": [good, {"id": "x"}, 4]})] == [good["id"]]
    assert scope_rows.rows_from_section({"rules": "x"}) == []
    assert scope_rows.rows_from_section("x") == []

def test_rows_to_config_is_version_3():
    assert scope_rows.rows_to_config([]) == {"version": 3, "rules": []}
    assert scope_rows.SCHEMA_VERSION == 3


# ── RowsFromConfig ──────────

def test_rows_from_config_converts_v2_in_memory():
    v2 = _v2("drive.folder", [FOLDER], [Verb.READ, Verb.UPDATE])
    cfg = {"auto_accept": {"version": 2, "rules": _dicts(v2)}}

    (row,) = scope_rows.rows_from_config(cfg)

    assert row.scope == "drive.folder"
    assert cfg["auto_accept"]["version"] == 2
    assert _decisions(scope_rows.compile_rows([row])) == _decisions(v2)

def test_a_section_without_a_version_is_read_as_v2():
    cfg = {"auto_accept": {"rules": _dicts(_v2("drive.folder", [FOLDER], [Verb.READ]))}}

    assert len(scope_rows.rows_from_config(cfg)) == 1

def test_version_3_is_read_as_rows():
    row = _row("drive.folder", [FOLDER], [Verb.READ])
    cfg = {"auto_accept": scope_rows.rows_to_config([row])}

    assert scope_rows.rows_from_config(cfg) == [row]

@pytest.mark.parametrize("cfg", [
    None, {}, {"auto_accept": "x"}, {"auto_accept": {"version": 9, "rules": []}},
    {"auto_accept": {"version": None, "rules": "x"}},
])
def test_anything_else_reads_as_no_rows(cfg):
    assert scope_rows.rows_from_config(cfg) == []


# ── AddAndRemove ──────────

def test_add_rules_extends_existing_row():
    existing = scope_rows.add_rules([], _v2("drive.folder", [FOLDER], [Verb.READ]))[0]
    added = _v2("drive.folder", [FOLDER], [Verb.UPDATE])

    rows, _touched = scope_rows.add_rules(existing, added)

    (row,) = rows
    assert row.id == existing[0].id
    assert row.verbs == {Verb.READ, Verb.UPDATE}
    assert _decisions(scope_rows.compile_rows(rows)) == _decisions(
        _v2("drive.folder", [FOLDER], [Verb.READ, Verb.UPDATE]))

def test_add_rules_returns_touched_ids():
    rules = [
        *_v2("drive.folder", [FOLDER], [Verb.READ, Verb.UPDATE]),
        *_v2("tasks.list", ["L1"], [Verb.ADD]),
    ]

    rows, touched = scope_rows.add_rules([], rules)

    assert touched == [row.id for row in rows]
    assert len(rows) == 2 and len(touched) == 2

def test_add_rules_matches_values_regardless_of_order():
    first, _ = scope_rows.add_rules([], _v2("drive.folder", ["a", "b"], [Verb.READ]))
    rows, _ = scope_rows.add_rules(first, _v2("drive.folder", ["b", "a"], [Verb.UPDATE]))

    assert len(rows) == 1

def test_add_rules_creates_and_extends_a_custom_row():
    unknown = _rule("ignored", "some_predicate", ["x"], ["gmail.read_message"])
    other_op = _rule("ignored", "some_predicate", ["x"], ["gmail.create_draft"])
    row_id = store.rule_id_for("some_predicate", ["x"], ())

    rows, touched = scope_rows.add_rules([], [unknown])
    rows, touched_again = scope_rows.add_rules(rows, [other_op])

    (row,) = rows
    assert touched == touched_again == [row_id]
    assert row.id == row_id and row.scope is None
    assert row.operations == {"gmail.read_message", "gmail.create_draft"}
    assert row.verbs == {Verb.READ, Verb.ADD}

def test_a_rule_its_scope_cannot_reproduce_becomes_a_custom_row():
    odd = _rule("ignored", "approved_folder", [FOLDER], ["drive.write_file"])

    rows, _ = scope_rows.add_rules([], [odd])

    assert [r.scope for r in rows] == [None]
    assert _decisions(scope_rows.compile_rows(rows)) == _decisions([odd])

def test_add_rules_does_not_mutate_its_input():
    rows: list[ScopeRow] = []
    scope_rows.add_rules(rows, _v2("drive.folder", [FOLDER], [Verb.READ]))
    assert rows == []

def test_remove_row():
    a, b = _row("drive.folder", ["a"], [Verb.READ]), _row("drive.folder", ["b"], [Verb.READ])

    assert scope_rows.remove_row([a, b], a.id) == [b]
    assert scope_rows.remove_row([a, b], "r-none") == [a, b]


# ── Missing ──────────

def test_missing_operations_of_a_scoped_row():
    full = _row("grist.document", ["D"], [Verb.READ, Verb.ADD])
    stale = ScopeRow(**{**vars(full), "operations": full.operations - {"grist.import_csv"}})

    assert scope_rows.missing_operations(full) == frozenset()
    assert scope_rows.missing_operations(stale) == {"grist.import_csv"}
    assert scope_rows.missing_tools(stale) == ("grist_import_csv",)
    assert scope_rows.missing_tools(full) == ()

def test_missing_operations_of_a_custom_row_come_from_outdated():
    full = _v2("grist.document", ["D"], [Verb.READ, Verb.ADD])[0]
    stale = PolicyRule(id="r-c", predicate=full.predicate, value=full.value,
                       operations=full.operations - {"grist.import_csv"})
    (row,) = scope_rows.add_rules([], [stale])[0]
    # grist.document is a known scope, so force the custom path.
    custom = ScopeRow(
        id="r-c", scope=None, value=["D"], verbs=row.verbs, operations=stale.operations, predicate=stale.predicate)

    assert scope_rows.missing_operations(custom) == {"grist.import_csv"}

def test_fill_missing_adds_only_that_rows_missing_operations():
    full = _row("drive.folder", [FOLDER], [Verb.UPDATE])
    stale = ScopeRow(**{**vars(full), "operations": frozenset({"sheets.format_range"})})
    other = _row("drive.folder", ["B"], [Verb.READ], operations=["drive.read_file_contents"])

    rows = scope_rows.fill_missing([stale, other], stale.id)

    assert rows[0].operations == full.operations
    assert rows[1] == other
    assert scope_rows.fill_missing([other], "r-none") == [other]


# ── SetVerbs ──────────

def test_set_verbs_keeps_existing_operations_of_kept_verbs():
    full = _row("drive.folder", [FOLDER], [Verb.READ, Verb.UPDATE])
    partial = ScopeRow(**{**vars(full), "operations": frozenset({"drive.read_file_contents", "sheets.format_range"})})

    (row,) = scope_rows.set_verbs([partial], partial.id, [Verb.UPDATE]) or []

    assert row.verbs == {Verb.UPDATE}
    assert row.operations == {"sheets.format_range"}
    assert row.id == partial.id

def test_set_verbs_adds_full_operations_for_new_verbs():
    base = _row("drive.folder", [FOLDER], [Verb.READ], operations=["drive.read_file_contents"])

    (row,) = scope_rows.set_verbs([base], base.id, [Verb.READ, Verb.DOWNLOAD]) or []

    assert row.operations == {"drive.read_file_contents"} | scope_rows.implied_operations("drive.folder", [Verb.DOWNLOAD])

def test_set_verbs_keeps_shared_operation_implied_by_kept_verb():
    base = _row("calendar.calendar", ["primary"], [Verb.ADD, Verb.UPDATE])
    assert "calendar.create_modify_event" in base.operations

    (row,) = scope_rows.set_verbs([base], base.id, [Verb.UPDATE]) or []

    assert "calendar.create_modify_event" in row.operations
    assert row.verbs == {Verb.UPDATE}
    (gone,) = scope_rows.set_verbs([base], base.id, [Verb.READ]) or []
    assert "calendar.create_modify_event" not in gone.operations

def test_set_verbs_refuses_empty_unknown_custom_or_foreign_verbs():
    scoped = _row("drive.folder", [FOLDER], [Verb.READ])
    custom = ScopeRow(id="r-c", scope=None, value=None, verbs=frozenset(), operations=frozenset({"x"}),
                      predicate="p")
    rows = [scoped, custom]

    assert scope_rows.set_verbs(rows, scoped.id, []) is None
    assert scope_rows.set_verbs(rows, "r-none", [Verb.READ]) is None
    assert scope_rows.set_verbs(rows, "r-c", [Verb.READ]) is None
    assert scope_rows.set_verbs(rows, scoped.id, [Verb.SEARCH]) is None

def test_set_verbs_leaves_other_rows_alone():
    a, b = _row("drive.folder", ["a"], [Verb.READ]), _row("drive.folder", ["b"], [Verb.READ])

    rows = scope_rows.set_verbs([a, b], a.id, [Verb.READ])

    assert rows == [a, b]


# ── Pairs ──────────

def test_pairs_of_matches_compile_row():
    row = _row("drive.folder", [FOLDER], [Verb.READ, Verb.UPDATE])

    expected = {(r.predicate, op) for r in scope_rows.compile_row(row) for op in r.operations}

    assert scope_rows.pairs_of([row], row.id) == expected
    assert expected

def test_pairs_of_an_unknown_row_is_empty():
    assert scope_rows.pairs_of([], "r-none") == frozenset()



def test_gmail_label_row_with_update_and_move_compiles_to_two_rules():
    row = _row("gmail.label", ["Proposed Archive"], {Verb.UPDATE, Verb.MOVE})
    rules = scope_rows.compile_row(row)
    by_predicate = {r.predicate: r for r in rules}
    assert set(by_predicate) == {"label_name_allowlist", "label_match"}
    assert by_predicate["label_match"].operations == frozenset({"gmail.archive_message"})
    assert by_predicate["label_match"].value == ["Proposed Archive"]
    assert "gmail.archive_message" not in by_predicate["label_name_allowlist"].operations


def test_gmail_label_row_with_only_update_has_no_label_match_rule():
    row = _row("gmail.label", ["Proposed Archive"], {Verb.UPDATE})
    assert {r.predicate for r in scope_rows.compile_row(row)} == {"label_name_allowlist"}
