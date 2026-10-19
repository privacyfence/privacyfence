"""Unit tests for privacyfence.policy.resource_registry -- the connector resource-type manifest
resource_names.py's display-name resolvers read.

test_resource_names.py already exercises the "drive"/"folders" resolver (_resolve_drive_file) end
to end through ResourceNameResolver; this file covers the other seven resolvers directly (each
duck-types against a different connector client shape), which nothing else in the suite reaches.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from privacyfence.policy import resource_registry as rg


def _resolver_for(connector: str, config_key: str):
    rt = rg.resource_type(connector, config_key)
    assert rt is not None
    return rt.resolver


def _raise(exc: Exception):
    def fail(*args, **kwargs):
        raise exc
    return fail


class _HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.response = SimpleNamespace(status_code=status)


class _ClientError(Exception):
    """A connector client's own error wrapping the HTTP one, the way JiraClientError does."""


def _wrapped(status: int) -> Exception:
    try:
        raise _HttpError(status)
    except _HttpError as http:
        try:
            raise _ClientError("lookup failed") from http
        except _ClientError as exc:
            return exc


class TestResolvers:
    """A resolver returns None only when the lookup worked and found no such resource -- the page
    shows that as deleted. A lookup that could not answer raises, so it is never shown as one."""

    def test_resolve_task_list(self):
        client = SimpleNamespace(list_task_lists=lambda: [SimpleNamespace(id="L1", name="Groceries")])
        assert _resolver_for("tasks", "task_lists")(client, "L1") == "Groceries"

    def test_resolve_task_list_raises_a_client_error(self):
        client = SimpleNamespace(list_task_lists=_raise(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _resolver_for("tasks", "task_lists")(client, "L1")

    def test_resolve_task_list_unknown_id_resolves_to_none(self):
        client = SimpleNamespace(list_task_lists=lambda: [SimpleNamespace(id="L1", name="Groceries")])
        assert _resolver_for("tasks", "task_lists")(client, "UNKNOWN") is None

    def test_resolve_slack_channel_adds_a_hash_prefix(self):
        client = SimpleNamespace(list_channels=lambda **kw: [SimpleNamespace(id="C1", name="general")])
        assert _resolver_for("slack", "channels")(client, "C1") == "#general"

    def test_resolve_slack_channel_raises_a_client_error(self):
        client = SimpleNamespace(list_channels=_raise(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _resolver_for("slack", "channels")(client, "C1")

    def test_resolve_slack_channel_unknown_id_resolves_to_none_not_hash_empty(self):
        client = SimpleNamespace(list_channels=lambda **kw: [SimpleNamespace(id="C1", name="general")])
        assert _resolver_for("slack", "channels")(client, "UNKNOWN") is None

    def test_a_slack_channel_missing_from_a_cut_off_list_is_inconclusive(self):
        channels = [SimpleNamespace(id=f"C{i}", name=f"c{i}") for i in range(rg._SLACK_CHANNEL_LIMIT)]
        client = SimpleNamespace(list_channels=lambda **kw: channels)
        with pytest.raises(rg.InconclusiveLookup):
            _resolver_for("slack", "channels")(client, "UNKNOWN")

    def test_resolve_telegram_chat(self):
        async def list_chats(limit=50):
            return [SimpleNamespace(id="T1", name="Family")]

        client = SimpleNamespace(list_chats=list_chats)
        assert _resolver_for("telegram", "chats")(client, "T1") == "Family"

    def test_resolve_telegram_chat_raises_a_client_error(self):
        async def list_chats(limit=50):
            raise RuntimeError("boom")

        client = SimpleNamespace(list_chats=list_chats)
        with pytest.raises(RuntimeError):
            _resolver_for("telegram", "chats")(client, "T1")

    def test_resolve_jira_project_by_key(self):
        client = SimpleNamespace(get_project=lambda key: SimpleNamespace(key=key, name=f"Project {key}"))
        assert _resolver_for("jira", "projects")(client, "VWHCM") == "Project VWHCM"

    def test_a_jira_project_jira_404s_on_resolves_to_none(self):
        client = SimpleNamespace(get_project=_raise(_wrapped(404)))
        assert _resolver_for("jira", "projects")(client, "GONE") is None

    def test_resolve_jira_project_raises_any_other_client_error(self):
        client = SimpleNamespace(get_project=_raise(_wrapped(429)))
        with pytest.raises(_ClientError):
            _resolver_for("jira", "projects")(client, "ENG")

    def test_resolve_assets_schema(self):
        client = SimpleNamespace(get_schema=lambda sid: SimpleNamespace(name="Hardware"))
        assert _resolver_for("assets", "schemas")(client, "2") == "Hardware"

    def test_an_assets_schema_that_404s_resolves_to_none(self):
        client = SimpleNamespace(get_schema=_raise(_wrapped(404)))
        assert _resolver_for("assets", "schemas")(client, "2") is None

    def test_resolve_assets_schema_raises_any_other_client_error(self):
        client = SimpleNamespace(get_schema=_raise(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _resolver_for("assets", "schemas")(client, "2")

    def test_resolve_confluence_space_by_key(self):
        client = SimpleNamespace(get_space=lambda key: SimpleNamespace(key=key, name="Engineering"))
        assert _resolver_for("confluence", "spaces")(client, "ENG") == "Engineering"

    def test_an_unknown_confluence_space_resolves_to_none(self):
        client = SimpleNamespace(get_space=lambda key: None)
        assert _resolver_for("confluence", "spaces")(client, "GONE") is None

    def test_resolve_confluence_space_raises_a_client_error(self):
        client = SimpleNamespace(get_space=_raise(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _resolver_for("confluence", "spaces")(client, "ENG")

    def test_resolve_calendar(self):
        client = SimpleNamespace(list_calendars=lambda: [SimpleNamespace(id="cal1", name="Personal")])
        assert _resolver_for("calendar", "calendars")(client, "cal1") == "Personal"

    def test_resolve_calendar_raises_a_client_error(self):
        client = SimpleNamespace(list_calendars=_raise(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _resolver_for("calendar", "calendars")(client, "cal1")

    def test_a_drive_file_google_404s_on_resolves_to_none(self):
        error = RuntimeError("not found")
        error.resp = SimpleNamespace(status="404")  # googleapiclient's HttpError shape
        client = SimpleNamespace(get_file_metadata=_raise(error))
        assert _resolver_for("drive", "folders")(client, "F1") is None

    def test_resolve_salesforce_report(self):
        client = SimpleNamespace(list_reports=lambda: [SimpleNamespace(id="R1", name="Pipeline")])
        assert _resolver_for("salesforce", "reports")(client, "R1") == "Pipeline"

    def test_resolve_salesforce_report_by_direct_lookup(self):
        client = SimpleNamespace(get_report_name=lambda rid: "Pipeline", list_reports=lambda: [])
        assert _resolver_for("salesforce", "reports")(client, "00O000000000001") == "Pipeline"

    def test_resolve_salesforce_report_matches_15_char_id_to_18_char(self):
        client = SimpleNamespace(list_reports=lambda: [SimpleNamespace(id="00O000000000001AAA", name="Pipeline")])
        assert _resolver_for("salesforce", "reports")(client, "00O000000000001") == "Pipeline"

    def test_resolve_grist_document(self):
        client = SimpleNamespace(get_document=lambda did: SimpleNamespace(name="Budget"))
        assert _resolver_for("grist", "documents")(client, "abc") == "Budget"

    def test_resolve_grist_document_falls_back_to_the_list(self):
        def boom(did):
            raise RuntimeError("boom")

        client = SimpleNamespace(get_document=boom, list_documents=lambda: [SimpleNamespace(id="abc", name="Budget")])
        assert _resolver_for("grist", "documents")(client, "abc") == "Budget"

    def test_grist_document_rule_maps_to_the_resource_type(self):
        from privacyfence.settings_controller import RULE_NAME_TO_RESOURCE_TYPE
        assert RULE_NAME_TO_RESOURCE_TYPE["grist.document"].config_key == "documents"

    def test_resolve_salesforce_report_raises_when_every_lookup_fails(self):
        client = SimpleNamespace(list_reports=_raise(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            _resolver_for("salesforce", "reports")(client, "R1")

    def test_a_salesforce_report_nobody_finds_resolves_to_none(self):
        client = SimpleNamespace(get_report_name=lambda rid: None, list_reports=lambda: [])
        assert _resolver_for("salesforce", "reports")(client, "00O000000000001") is None
