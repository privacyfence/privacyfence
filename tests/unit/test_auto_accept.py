"""Unit tests for privacyfence.auto_accept -- the tool/gate/temp-accept infrastructure and the
per-principal v2 rule cache.

Rule matching, rule proposals and the on-disk rule schema live in ``policy/`` and have their own
test suites:

- Scope/condition predicate matching: tests/unit/policy/test_scopes.py, test_conditions.py
  (equivalence-checked against a frozen v1 reference, tests/unit/policy/_v1_reference.py).
- Rule evaluation (``policy.engine.evaluate``/``preflight``): tests/unit/policy/test_engine.py.
- Popup/Settings/bridge rule proposals and the one writer: tests/unit/policy/test_propose.py,
  test_describe.py, test_catalogue.py.
- The on-disk v2 schema: tests/unit/policy/test_store.py.

What's left here is exactly what's left in auto_accept.py itself: the tool/gate tables, the
same-file temp-accept grace window, and the rules-changed listener broadcast.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
import yaml
from freezegun import freeze_time

from privacyfence import auto_accept
from privacyfence.auto_accept import (
    TEMP_ACCEPT_ELIGIBLE_OPERATIONS,
    TOOL_TO_GATE,
    TOOL_TO_OPERATION,
    _attendee_email,
    add_policy_v2_rules,
    add_rules_changed_listener,
    init_config_path,
    is_temp_accepted,
    register_temp_accept,
    remove_policy_v2_rule,
    remove_rules_changed_listener,
    set_rules_changed_listener,
    temp_accept_key,
)
from privacyfence.policy import store as policy_store
from privacyfence.policy.engine import PolicyRule, evaluate
from privacyfence.policy.resource_registry import DRIVE_SANDBOX_WRITE_TARGETS

from ..helpers import make_ctx, policy_rules


# --------------------------------------------------------------------------- #
# TOOL_TO_OPERATION: every popup/review-gated write tool needs an entry here,
# or a rule configured under its "natural" dotted name in settings.yaml
# silently never matches -- gate.py falls back to the raw f"{connector}.{tool}"
# key instead (see test_gate.py::test_operation_key_falls_back_to_connector_dot_tool).
# --------------------------------------------------------------------------- #

class TestToolToOperationMapping:
    def test_jira_transition_issue_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["jira_transition_issue"] == "jira.transition_issue"

    def test_calendar_create_out_of_office_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["calendar_create_out_of_office"] == "calendar.out_of_office"

    def test_calendar_set_working_location_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["calendar_set_working_location"] == "calendar.working_location"

    def test_jira_transition_issue_gets_approved_project_keys_via_issue_key(self):
        # jira_transition_issue reuses the same generic scope selector jira_update_issue/
        # jira_get_issue already rely on -- no new selector code needed, just the operation-key
        # mapping above so a configured rule actually gets looked up.
        rules = policy_rules({
            TOOL_TO_OPERATION["jira_transition_issue"]: [{"predicate": "approved_project_keys", "value": ["ENG"]}],
        })
        ctx = make_ctx(tool="jira_transition_issue", args={"issue_key": "ENG-42", "transition_name": "Done"})
        ok, matched = evaluate(rules, TOOL_TO_OPERATION["jira_transition_issue"], ctx)
        assert ok is True
        assert matched == rules[0].id

    def test_sheets_dimension_tools_map_to_clean_operation_keys(self):
        assert TOOL_TO_OPERATION["drive_sheets_insert_dimensions"] == "sheets.insert_dimensions"
        assert TOOL_TO_OPERATION["drive_sheets_delete_dimensions"] == "sheets.delete_dimensions"

    def test_docs_edit_and_format_tools_map_to_clean_operation_keys(self):
        assert TOOL_TO_OPERATION["drive_docs_edit_content"] == "docs.edit_content"
        assert TOOL_TO_OPERATION["drive_docs_format_content"] == "docs.format_content"

    def test_salesforce_search_maps_to_clean_operation_key(self):
        assert TOOL_TO_OPERATION["salesforce_search"] == "salesforce.search"

    def test_calendar_set_event_visibility_maps_to_its_own_operation_key(self):
        assert TOOL_TO_OPERATION["calendar_set_event_visibility"] == "calendar.set_visibility"


# --------------------------------------------------------------------------- #
# TOOL_TO_GATE: exhaustively cross-checked against docs and connector source
# in tests/unit/connectors/test_readme_manifest_alignment.py. This is just a
# couple of direct spot checks for the dict itself.
# --------------------------------------------------------------------------- #

class TestToolToGate:
    def test_auto_tool(self):
        assert TOOL_TO_GATE["gmail_list_messages"] == "auto"

    def test_review_tool(self):
        assert TOOL_TO_GATE["gmail_get_message"] == "review"

    def test_popup_tool(self):
        assert TOOL_TO_GATE["gmail_create_draft"] == "popup"


# --------------------------------------------------------------------------- #
# Drive/Sheets/Docs operation-key lists: policy.resource_registry.
# DRIVE_SANDBOX_WRITE_TARGETS is the single source of truth
# TEMP_ACCEPT_ELIGIBLE_OPERATIONS derives its own list from -- this test is
# the tie between the two.
# --------------------------------------------------------------------------- #

class TestDriveSheetsDocsSingleSourceOfTruth:
    def test_temp_accept_eligible_operations_is_a_subset_of_sandbox_write_targets(self):
        sandbox_op_keys = {op_key for op_key, _rule_name in DRIVE_SANDBOX_WRITE_TARGETS}
        assert set(TEMP_ACCEPT_ELIGIBLE_OPERATIONS) <= sandbox_op_keys

    def test_temp_accept_arg_name_matches_each_operation_s_own_id_arg(self):
        # sheets.* addresses its spreadsheet by spreadsheet_id; every
        # drive.*/docs.* write addresses its file by file_id.
        for op_key, arg_name in TEMP_ACCEPT_ELIGIBLE_OPERATIONS.items():
            expected = "spreadsheet_id" if op_key.startswith("sheets.") else "file_id"
            assert arg_name == expected, (op_key, arg_name)


class TestAttendeeEmail:
    """_attendee_email is a live dependency of policy.conditions._no_external_attendees_matches
    (no_external_attendees) -- it's imported by, not just kept alongside, the condition selectors."""

    def test_dict_shaped_attendee(self):
        assert _attendee_email({"email": "a@example.com"}) == "a@example.com"

    def test_dict_shaped_attendee_missing_email(self):
        assert _attendee_email({}) == ""

    def test_object_shaped_attendee(self):
        assert _attendee_email(SimpleNamespace(email="a@example.com")) == "a@example.com"

    def test_object_shaped_attendee_with_no_email_attribute(self):
        assert _attendee_email(SimpleNamespace()) == ""

    def test_plain_string_attendee(self):
        # calendar_create_event/update_event pass bare email strings, parsed from a
        # comma-separated arg, since the event doesn't exist yet to have real Attendee objects.
        assert _attendee_email("a@example.com") == "a@example.com"


# --------------------------------------------------------------------------- #
# Session temp accept -- gate.py's lighter alternative to a standing Always
# allow rule for write ops expected to be called repeatedly against the
# same file (sheets writes/formats, drive comments): an Allow once on one
# of these operations also arms this window, with no separate button.
# Unlike the YAML-backed rules, this state is in-memory only and never
# persisted.
# --------------------------------------------------------------------------- #

class TestTempAcceptKey:
    def test_eligible_operation_returns_its_configured_arg(self):
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1", "range_a1": "A1:B2"})
        assert temp_accept_key("sheets.write_range", ctx) == "sheet-1"

    def test_drive_comment_uses_file_id(self):
        ctx = make_ctx(args={"file_id": "file-1", "comment": "hi"})
        assert temp_accept_key("drive.comment_file", ctx) == "file-1"

    def test_ineligible_operation_returns_none(self):
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1"})
        assert temp_accept_key("gmail.create_draft", ctx) is None

    def test_eligible_operation_missing_arg_returns_none(self):
        ctx = make_ctx(args={"range_a1": "A1:B2"})
        assert temp_accept_key("sheets.write_range", ctx) is None

    def test_eligible_operation_falsy_arg_returns_none(self):
        ctx = make_ctx(args={"spreadsheet_id": ""})
        assert temp_accept_key("sheets.write_range", ctx) is None

    def test_covers_every_declared_eligible_operation(self):
        # Every entry in TEMP_ACCEPT_ELIGIBLE_OPERATIONS must actually resolve
        # a key when its arg is present -- otherwise the popup would show the
        # temp-accept disclosure caption for an operation that can never
        # actually register one.
        for op_key, arg_name in TEMP_ACCEPT_ELIGIBLE_OPERATIONS.items():
            ctx = make_ctx(args={arg_name: "some-id"})
            assert temp_accept_key(op_key, ctx) == "some-id"

    def test_insert_dimensions_is_eligible_scoped_to_spreadsheet_id(self):
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1", "dimension": "ROWS"})
        assert temp_accept_key("sheets.insert_dimensions", ctx) == "sheet-1"

    def test_delete_dimensions_is_deliberately_not_eligible(self):
        # Resolved design decision: unlike format_range/insert_dimensions,
        # deleting rows/columns is destructive with no undo path through
        # PrivacyFence, so it only ever gets the standing-rule treatment, not
        # the lighter-weight temp-accept grace window.
        assert "sheets.delete_dimensions" not in TEMP_ACCEPT_ELIGIBLE_OPERATIONS
        ctx = make_ctx(args={"spreadsheet_id": "sheet-1", "dimension": "ROWS"})
        assert temp_accept_key("sheets.delete_dimensions", ctx) is None

    def test_docs_edit_and_format_content_are_eligible_scoped_to_file_id(self):
        ctx = make_ctx(args={"file_id": "f1"})
        assert temp_accept_key("docs.edit_content", ctx) == "f1"
        assert temp_accept_key("docs.format_content", ctx) == "f1"


class TestTempAcceptGraceWindow:
    """register_temp_accept()/is_temp_accepted() are module-level functions against the current
    principal's own _AutoAcceptState
    (auto_accept._REGISTRY), reset between tests by tests/conftest.py's autouse fixture."""

    def test_not_accepted_before_registration(self):
        assert is_temp_accepted("sheets.write_range", "sheet-1") is False

    def test_registered_file_is_accepted(self):
        register_temp_accept("sheets.write_range", "sheet-1")
        assert is_temp_accepted("sheets.write_range", "sheet-1") is True

    def test_different_file_key_not_covered(self):
        register_temp_accept("sheets.write_range", "sheet-1")
        assert is_temp_accepted("sheets.write_range", "sheet-2") is False

    def test_different_operation_on_same_file_not_covered(self):
        register_temp_accept("sheets.write_range", "sheet-1")
        assert is_temp_accepted("sheets.format_range", "sheet-1") is False

    def test_none_file_key_never_matches(self):
        register_temp_accept("sheets.write_range", "sheet-1")
        assert is_temp_accepted("sheets.write_range", None) is False

    def test_expires_after_ttl(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            assert is_temp_accepted("sheets.write_range", "sheet-1") is True

            frozen.tick(delta=301)
            assert is_temp_accepted("sheets.write_range", "sheet-1") is False

    def test_still_valid_just_before_ttl_expires(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            frozen.tick(delta=299)
            assert is_temp_accepted("sheets.write_range", "sheet-1") is True

    def test_re_registering_resets_the_ttl(self):
        with freeze_time("2024-01-01 00:00:00") as frozen:
            register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            frozen.tick(delta=290)
            register_temp_accept("sheets.write_range", "sheet-1", ttl_seconds=300)
            frozen.tick(delta=290)
            assert is_temp_accepted("sheets.write_range", "sheet-1") is True

    def test_no_temp_accepts_registered_never_matches(self):
        assert is_temp_accepted("sheets.write_range", "sheet-1") is False


# --------------------------------------------------------------------------- #
# Rules-changed listener broadcast -- what wakes approvals.
# PendingApprovalRegistry.reevaluate_all() and pushes a fresh Settings
# snapshot whenever a v2 rule is added/removed.
# --------------------------------------------------------------------------- #

class TestRulesChangedListener:
    def test_notify_rules_changed_fires_registered_listener(self):
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))
        auto_accept.notify_rules_changed()
        assert calls == [1]

    def test_notify_rules_changed_is_safe_with_no_listener_registered(self):
        set_rules_changed_listener(None)
        auto_accept.notify_rules_changed()  # must not raise

    def test_add_policy_v2_rules_fires_listener(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({}), encoding="utf-8")
        init_config_path(str(config_path))
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))

        add_policy_v2_rules([PolicyRule(
            id="i_am_sender", predicate="i_am_sender", value=None,
            operations=frozenset({"gmail.read_message"}),
        )])

        assert calls == [1]

    def test_remove_policy_v2_rule_fires_listener(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({}), encoding="utf-8")
        init_config_path(str(config_path))
        add_policy_v2_rules([PolicyRule(
            id="i_am_sender", predicate="i_am_sender", value=None,
            operations=frozenset({"gmail.read_message"}),
        )])
        rule_id = policy_store.rule_id_for("i_am_sender", None, ())
        calls = []
        set_rules_changed_listener(lambda: calls.append(1))

        assert remove_policy_v2_rule(rule_id) is True
        assert calls == [1]

    def test_remove_policy_v2_rule_is_a_no_op_for_an_unknown_id(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({}), encoding="utf-8")
        init_config_path(str(config_path))
        assert remove_policy_v2_rule("no-such-rule") is False

    def test_set_rules_changed_listener_replaces_the_previous_one(self):
        first_calls, second_calls = [], []
        set_rules_changed_listener(lambda: first_calls.append(1))
        set_rules_changed_listener(lambda: second_calls.append(1))

        auto_accept.notify_rules_changed()

        assert first_calls == []
        assert second_calls == [1]

    def test_add_and_remove_rules_changed_listener(self):
        calls = []

        def listener():
            calls.append(1)

        add_rules_changed_listener(listener)
        auto_accept.notify_rules_changed()
        assert calls == [1]

        remove_rules_changed_listener(listener)
        auto_accept.notify_rules_changed()
        assert calls == [1]  # not called again once removed

    def test_remove_rules_changed_listener_is_a_no_op_for_an_unregistered_callback(self):
        remove_rules_changed_listener(lambda: None)  # must not raise

    def test_notify_rules_changed_survives_a_raising_listener(self):
        calls = []
        add_rules_changed_listener(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        add_rules_changed_listener(lambda: calls.append(1))

        auto_accept.notify_rules_changed()  # must not raise, and later listeners still fire

        assert calls == [1]

    def test_get_policy_v2_rules_requires_an_initialized_config_path(self):
        with pytest.raises(RuntimeError, match="not initialized"):
            auto_accept.get_policy_v2_rules()

    def test_add_policy_v2_rules_requires_an_initialized_config_path(self):
        with pytest.raises(RuntimeError, match="not initialized"):
            add_policy_v2_rules([])

    def test_remove_policy_v2_rule_requires_an_initialized_config_path(self):
        with pytest.raises(RuntimeError, match="not initialized"):
            remove_policy_v2_rule("some-id")


# --------------------------------------------------------------------------- #
# Concurrent rule persistence: real OS threads racing on add_policy_v2_rules.
#
# gate.py's popup handling serializes calls through one asyncio.Lock, but
# add_policy_v2_rules() is also reachable directly from the MCP bridge and
# from settings_controller.py's own writer at the same time the IPC server's
# thread is confirming an "Always allow". _AutoAcceptState.write_lock is
# what's supposed to keep the read-modify-write of the YAML file race-free;
# these tests hammer it with real threads rather than asyncio tasks, since
# asyncio concurrency alone never exercises actual OS-level lock contention
# or genuine interleaving of file reads/writes.
# --------------------------------------------------------------------------- #

class TestConcurrentRulePersistence:
    def test_many_threads_adding_the_identical_rule_produce_no_duplicates(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({}), encoding="utf-8")
        init_config_path(str(config_path))

        barrier = threading.Barrier(20)
        rule = PolicyRule(
            id="i_am_sender", predicate="i_am_sender", value=None,
            operations=frozenset({"gmail.read_message"}),
        )

        def worker():
            barrier.wait()  # maximize actual overlap, not just interleaving
            add_policy_v2_rules([rule])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()

        on_disk = policy_store.compile_rules_from_config(
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
        )
        assert len(on_disk) == 1
        assert on_disk[0].predicate == "i_am_sender"
        assert on_disk[0].operations == frozenset({"gmail.read_message"})

    def test_many_threads_adding_distinct_rules_lose_no_writes(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({}), encoding="utf-8")
        init_config_path(str(config_path))

        domains = [f"domain{i}.com" for i in range(20)]
        barrier = threading.Barrier(len(domains))

        def worker(domain):
            barrier.wait()
            add_policy_v2_rules([PolicyRule(
                id=domain, predicate="trusted_sender_domain", value=[domain],
                operations=frozenset({"gmail.read_message"}),
            )])

        threads = [threading.Thread(target=worker, args=(d,)) for d in domains]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive()

        # A lost update under a broken lock would show up as fewer than 20
        # rules here; a corrupted concurrent write would fail to parse as
        # YAML at all (safe_load below would already have raised).
        on_disk = policy_store.compile_rules_from_config(
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
        )
        assert len(on_disk) == len(domains)
        persisted_domains = {rule.value[0] for rule in on_disk}
        assert persisted_domains == set(domains)

    def test_concurrent_adds_keep_the_live_cache_and_disk_file_in_sync(self, tmp_path):
        # Every successful add_policy_v2_rules() call also refreshes
        # get_policy_v2_store_rules() while still holding the write lock, so
        # the in-memory cache gate.py evaluates against should never lag
        # behind what's on disk, even under concurrent writers.
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({}), encoding="utf-8")
        init_config_path(str(config_path))

        domains = [f"domain{i}.com" for i in range(10)]
        barrier = threading.Barrier(len(domains))

        def worker(domain):
            barrier.wait()
            add_policy_v2_rules([PolicyRule(
                id=domain, predicate="trusted_sender_domain", value=[domain],
                operations=frozenset({"gmail.read_message"}),
            )])

        threads = [threading.Thread(target=worker, args=(d,)) for d in domains]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        on_disk = policy_store.compile_rules_from_config(
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
        )
        live = auto_accept.get_policy_v2_store_rules()
        assert sorted(r.id for r in live) == sorted(r.id for r in on_disk)
