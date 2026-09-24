"""Unit tests for privacyfence.policy.resource_registry -- the connector resource-type manifest
resource_names.py's display-name resolvers read.

test_resource_names.py already exercises the "drive"/"folders" resolver (_resolve_drive_file) end
to end through ResourceNameResolver; this file covers the other seven resolvers directly (each
duck-types against a different connector client shape), which nothing else in the suite reaches.
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
