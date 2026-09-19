"""Unit tests for privacyfence.policy.resource_registry -- the connector resource-type manifest
that survives resource_grants.py's deletion at P9 (policy v2 redesign), for its three remaining
live callers: migration, resource_names.py's display-name resolvers, and gate.py's deprecated
target="grant" bridge alias.

test_resource_names.py already exercises the "drive"/"folders" resolver (_resolve_drive_file) end
to end through ResourceNameResolver; this file covers the other seven resolvers directly (each
duck-types against a different connector client shape) and expand_grants' capability-filtering
branches, which nothing else in the suite reaches.
"""
from __future__ import annotations

from types import SimpleNamespace

from privacyfence.policy import resource_registry as rg


def _resolver_for(connector: str, config_key: str):
    rt = rg.resource_type(connector, config_key)
    assert rt is not None
    return rt.resolver


class TestResolvers:
    def test_resolve_task_list(self):
        client = SimpleNamespace(list_task_lists=lambda: [SimpleNamespace(id="L1", name="Groceries")])
        assert _resolver_for("tasks", "task_lists")(client, "L1") == "Groceries"

    def test_resolve_task_list_swallows_a_client_error(self):
        client = SimpleNamespace(list_task_lists=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _resolver_for("tasks", "task_lists")(client, "L1") is None

    def test_resolve_task_list_unknown_id_resolves_to_none(self):
        client = SimpleNamespace(list_task_lists=lambda: [SimpleNamespace(id="L1", name="Groceries")])
        assert _resolver_for("tasks", "task_lists")(client, "UNKNOWN") is None

    def test_resolve_slack_channel_adds_a_hash_prefix(self):
        client = SimpleNamespace(list_channels=lambda: [SimpleNamespace(id="C1", name="general")])
        assert _resolver_for("slack", "channels")(client, "C1") == "#general"

    def test_resolve_slack_channel_swallows_a_client_error(self):
        client = SimpleNamespace(list_channels=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _resolver_for("slack", "channels")(client, "C1") is None

    def test_resolve_slack_channel_unknown_id_resolves_to_none_not_hash_empty(self):
        client = SimpleNamespace(list_channels=lambda: [SimpleNamespace(id="C1", name="general")])
        assert _resolver_for("slack", "channels")(client, "UNKNOWN") is None

    def test_resolve_telegram_chat(self):
        async def list_chats():
            return [SimpleNamespace(id="T1", name="Family")]

        client = SimpleNamespace(list_chats=list_chats)
        assert _resolver_for("telegram", "chats")(client, "T1") == "Family"

    def test_resolve_telegram_chat_swallows_a_client_error(self):
        async def list_chats():
            raise RuntimeError("boom")

        client = SimpleNamespace(list_chats=list_chats)
        assert _resolver_for("telegram", "chats")(client, "T1") is None

    def test_resolve_jira_project(self):
        client = SimpleNamespace(list_projects=lambda: [SimpleNamespace(key="ENG", name="Engineering")])
        assert _resolver_for("jira", "projects")(client, "ENG") == "Engineering"

    def test_resolve_jira_project_swallows_a_client_error(self):
        client = SimpleNamespace(list_projects=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _resolver_for("jira", "projects")(client, "ENG") is None

    def test_resolve_confluence_space(self):
        client = SimpleNamespace(list_spaces=lambda: [SimpleNamespace(key="ENG", name="Engineering")])
        assert _resolver_for("confluence", "spaces")(client, "ENG") == "Engineering"

    def test_resolve_confluence_space_swallows_a_client_error(self):
        client = SimpleNamespace(list_spaces=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _resolver_for("confluence", "spaces")(client, "ENG") is None

    def test_resolve_calendar(self):
        client = SimpleNamespace(list_calendars=lambda: [SimpleNamespace(id="cal1", name="Personal")])
        assert _resolver_for("calendar", "calendars")(client, "cal1") == "Personal"

    def test_resolve_calendar_swallows_a_client_error(self):
        client = SimpleNamespace(list_calendars=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _resolver_for("calendar", "calendars")(client, "cal1") is None

    def test_resolve_salesforce_report(self):
        client = SimpleNamespace(list_reports=lambda: [SimpleNamespace(id="R1", name="Pipeline")])
        assert _resolver_for("salesforce", "reports")(client, "R1") == "Pipeline"

    def test_resolve_salesforce_report_swallows_a_client_error(self):
        client = SimpleNamespace(list_reports=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _resolver_for("salesforce", "reports")(client, "R1") is None


class TestGrantResourceTypeIdOf:
    def test_id_of_reads_the_configured_id_field(self):
        rt = rg.resource_type("jira", "projects")
        assert rt is not None
        assert rt.id_of({"key": "ENG", "read": True}) == "ENG"

    def test_id_of_missing_field_is_empty_string(self):
        rt = rg.resource_type("jira", "projects")
        assert rt is not None
        assert rt.id_of({}) == ""


class TestExpandGrants:
    def test_disabled_capability_contributes_nothing(self):
        # A grant entry with "read" on but "send" off must not produce a send-family rule --
        # this is the branch expand_grants' own docstring calls out ("Both are gone as anything
        # live writes or evaluates") as the reason a grant's booleans matter at all.
        grants_cfg = {"slack": {"channels": [{"id": "C1", "read": True, "send": False}]}}
        compiled = rg.expand_grants(grants_cfg)
        assert compiled.get("slack.send_message") is None
        assert compiled["slack.read_messages"] == [
            {"rule": "approved_channel", "value": ["C1"]},
            {"rule": "approved_channel_all_results", "value": ["C1"]},
        ]

    def test_no_entries_enable_a_given_capability_at_all(self):
        # Every entry has every capability off -- the capability's own bucket must never appear,
        # not appear with an empty value list.
        grants_cfg = {"jira": {"projects": [{"key": "ENG", "read": False, "create": False}]}}
        compiled = rg.expand_grants(grants_cfg)
        assert compiled == {}

    def test_multiple_entries_merge_into_one_rule_s_value_list(self):
        grants_cfg = {"jira": {"projects": [
            {"key": "ENG", "read": True}, {"key": "OPS", "read": True},
        ]}}
        compiled = rg.expand_grants(grants_cfg)
        assert compiled["jira.read_issue"] == [{"rule": "approved_project_keys", "value": ["ENG", "OPS"]}]
