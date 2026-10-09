"""The SDK's hand-written sample data must have the shape the daemon's source API returns.

A sample is what a plugin author's tests feed their plugin through ``PluginTestHost``. If the
daemon's data drifts from the samples, plugin tests would pass against data PrivacyFence never
sends. Each sample's ``data`` therefore goes through the daemon's own adapter for that operation
(or the connector's own reader of it, for the raw Salesforce result) and must come out unchanged.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import typing
from pathlib import Path
from types import SimpleNamespace

import pytest

from privacyfence.calendar_client import CalendarEvent
from privacyfence.confluence_client import ConfluencePage
from privacyfence.connectors import salesforce as salesforce_connector
from privacyfence.jira_client import JiraIssue
from privacyfence.plugins import cursors
from privacyfence.plugins.constants import DRIVE_CHUNK_BYTES, SOURCE_OPERATIONS
from privacyfence.plugins.source_ops import SOURCE_ADAPTERS
from privacyfence.plugins.spool import DownloadSpool
from privacyfence.salesforce_client import SalesforceClient
from tests.fixtures.salesforce_analytics import COLUMNS, KEY, REPORT_ID, FakeAnalytics, make_rows, tabular_report

pytestmark = pytest.mark.unit

SAMPLES_DIR = (
    Path(__file__).resolve().parents[3] / "plugin-sdk" / "src" / "privacyfence_plugin_sdk" / "testing" / "samples"
)


PAGED = ("jira.search", "calendar.list_events")
SALESFORCE_PAGED_SAMPLES = ("salesforce.report_run.paged", "salesforce.report_run.paged.page2")


def sample(operation: str, page: int = 1) -> dict:
    name = operation if page == 1 else f"{operation}.page{page}"
    return json.loads((SAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def build(cls: type, data: dict):
    """``cls(**data)`` with nested dataclasses built too; an unknown or missing key raises."""
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for key, value in data.items():
        hint = hints.get(key)
        args = typing.get_args(hint)
        if dataclasses.is_dataclass(hint):
            value = build(hint, value)
        elif args and dataclasses.is_dataclass(args[0]) and isinstance(value, list):
            value = [build(args[0], item) for item in value]
        kwargs[key] = value
    return cls(**kwargs)


def run_adapter(operation: str, client, params: dict, spool=None, state=None):
    adapter = SOURCE_ADAPTERS[operation]
    return adapter.run(client, {**adapter.validate(params), "plugin": "sample", "state": state}, spool)


def test_there_is_one_sample_per_source_operation():
    assert sorted(p.stem for p in SAMPLES_DIR.glob("*.json")) == sorted(
        [*SOURCE_OPERATIONS, *(f"{op}.page2" for op in PAGED), *SALESFORCE_PAGED_SAMPLES]
    )
    for operation in SOURCE_OPERATIONS:
        loaded = sample(operation)
        assert loaded["operation"] == operation
        assert set(loaded) == {"operation", "params", "data", "next_cursor"}
    for operation in PAGED:
        loaded = sample(operation, page=2)
        assert loaded["operation"] == operation and set(loaded) == {"operation", "params", "data", "next_cursor"}
        assert loaded["next_cursor"] is None


class TestShapeMatchesTheAdapters:
    def test_calendar_list_events(self):
        data = sample("calendar.list_events")["data"]
        client = SimpleNamespace(
            list_events_page=lambda *args: ([build(CalendarEvent, item) for item in data], None)
        )

        produced, cursor = run_adapter(
            "calendar.list_events", client, {"time_min": "2026-10-07T00:00:00Z", "time_max": "2026-10-08T00:00:00Z"},
        )

        assert produced == data and cursor is None

    def test_jira_search(self):
        data = sample("jira.search")["data"]
        client = SimpleNamespace(
            search_issues_page=lambda jql, size, token: ([build(JiraIssue, item) for item in data], None)
        )

        produced, cursor = run_adapter("jira.search", client, {"jql": "project = EXAMPLE"})

        assert produced == data and cursor is None

    def test_confluence_get_page(self):
        data = sample("confluence.get_page")["data"]
        page = {k: v for k, v in data.items() if k not in ("body_offset", "body_total_chars")}
        client = SimpleNamespace(get_page=lambda page_id: build(ConfluencePage, page))

        produced, cursor = run_adapter("confluence.get_page", client, {"page_id": "EXAMPLE-1"})

        assert produced == data and cursor is None

    def test_sheets_get_values(self):
        data = sample("sheets.get_values")["data"]
        client = SimpleNamespace(get_sheet_values=lambda sheet, cells, option: data["values"])

        produced, cursor = run_adapter("sheets.get_values", client, {"spreadsheet_id": "EXAMPLE-1", "range": "A:B"})

        assert produced == data and cursor is None
        assert data["first_row"] == 0
        assert all(isinstance(row, list) for row in produced["values"])

    def test_drive_download(self, tmp_path):
        loaded = sample("drive.download")
        data = loaded["data"]
        content = base64.b64decode(data["content_base64"])
        client = SimpleNamespace(
            get_file_metadata=lambda file_id: SimpleNamespace(
                size=len(content), mime_type=data["mime_type"], modified_time=data["revision"]
            ),
            download_range=lambda file_id, offset, length: content[offset:offset + length],
        )

        produced, cursor = run_adapter(
            "drive.download", client, {"file_id": data["file_id"]}, DownloadSpool(tmp_path / "spool"),
        )

        assert produced == data and cursor == loaded["next_cursor"]
        assert data["eof"] is True and data["length"] == len(content) <= DRIVE_CHUNK_BYTES

    def test_salesforce_report_run(self):
        data = sample("salesforce.report_run")["data"]
        client = SimpleNamespace(run_report=lambda report_id, filters=None: data)

        produced, cursor = run_adapter("salesforce.report_run", client, {"report_id": "EXAMPLE-1"})

        # Raw, as the adapter returns it; and the connector's own reader finds the rows in it.
        assert produced == data and cursor is None
        [table] = salesforce_connector._report_tables(data)
        assert table["headers"] == data["reportMetadata"]["detailColumns"]
        assert len(table["rows"]) == len(data["factMap"]["T!T"]["rows"]) > 0
        assert all(len(row) == len(table["headers"]) for row in table["rows"])


CALENDAR_CALL = {"time_min": "2026-10-07T00:00:00Z", "time_max": "2026-10-08T00:00:00Z"}
PAGED_CALLS = {"jira.search": {"jql": "project = EXAMPLE"}, "calendar.list_events": CALENDAR_CALL}


class TestPageTwoSamples:
    """The first sample's ``next_cursor`` is what the daemon would issue for it, and page 2 is what follows."""

    @pytest.mark.parametrize("operation", PAGED)
    def test_the_first_cursor_decodes_for_the_samples_own_params(self, operation):
        adapter = SOURCE_ADAPTERS[operation]
        first = sample(operation)
        bound = adapter.bound(adapter.validate(PAGED_CALLS[operation]))

        state = cursors.decode(first["next_cursor"], operation, bound)

        assert state == {"t": "EXAMPLE-PAGE-2", "k": 0}
        assert sample(operation, page=2)["params"] == {"cursor": first["next_cursor"]}

    @pytest.mark.parametrize("operation", PAGED)
    def test_the_first_cursor_is_refused_for_another_query(self, operation):
        adapter = SOURCE_ADAPTERS[operation]
        other = {**PAGED_CALLS[operation], "jql" if operation == "jira.search" else "time_min": "other"}

        with pytest.raises(cursors.CursorError, match="different call"):
            cursors.decode(sample(operation)["next_cursor"], operation, adapter.bound(adapter.validate(other)))

    @pytest.mark.parametrize("operation", PAGED)
    def test_the_adapter_issues_the_samples_cursor_for_page_one(self, operation):
        data = sample(operation)["data"]
        cls = JiraIssue if operation == "jira.search" else CalendarEvent
        client_page = ([build(cls, item) for item in data], "EXAMPLE-PAGE-2")
        method = "search_issues_page" if operation == "jira.search" else "list_events_page"
        client = SimpleNamespace(**{method: lambda *args: client_page})

        produced, cursor = run_adapter(operation, client, PAGED_CALLS[operation])

        assert produced == data and cursor == sample(operation)["next_cursor"]

    @pytest.mark.parametrize("operation", PAGED)
    def test_page_two_has_the_adapters_shape_and_ends_the_listing(self, operation):
        first, second = sample(operation), sample(operation, page=2)
        adapter = SOURCE_ADAPTERS[operation]
        state = cursors.decode(first["next_cursor"], operation, adapter.bound(adapter.validate(PAGED_CALLS[operation])))
        cls = JiraIssue if operation == "jira.search" else CalendarEvent
        seen: list = []

        def fetch(*args):
            seen.append(args[-1])
            return [build(cls, item) for item in second["data"]], None

        method = "search_issues_page" if operation == "jira.search" else "list_events_page"

        produced, cursor = run_adapter(operation, SimpleNamespace(**{method: fetch}), PAGED_CALLS[operation], state=state)

        assert seen == ["EXAMPLE-PAGE-2"]
        assert produced == second["data"] and cursor == second["next_cursor"] is None
        assert {item.get("key") or item["id"] for item in produced}.isdisjoint(
            {item.get("key") or item["id"] for item in first["data"]}
        )


class TestPagedSalesforceReportSamples:
    """The paged ``salesforce.report_run`` samples carry the page info and cursor the daemon issues."""

    @staticmethod
    def client(monkeypatch):
        fake = FakeAnalytics(tabular_report(), COLUMNS, make_rows(5), row_limit=3)
        client = SalesforceClient(config={"access_token": "tok", "instance_url": "https://my.salesforce.com"})
        monkeypatch.setattr(client, "_get_sf", lambda: SimpleNamespace(restful=fake.restful))
        return client

    @staticmethod
    def pages():
        return sample("salesforce.report_run.paged"), sample("salesforce.report_run.paged.page2")

    def test_page_one_matches_the_daemon(self, monkeypatch):
        first, _ = self.pages()
        assert first["params"]["report_id"] == REPORT_ID and first["params"]["page_by"] == KEY

        produced, cursor = run_adapter("salesforce.report_run", self.client(monkeypatch), first["params"])

        assert produced["page"] == first["data"]["page"] and cursor == first["next_cursor"]

    def test_page_two_matches_the_daemon_for_the_samples_own_cursor(self, monkeypatch):
        first, second = self.pages()
        assert second["params"] == {**first["params"], "cursor": first["next_cursor"]}

        adapter = SOURCE_ADAPTERS["salesforce.report_run"]
        params = {key: value for key, value in second["params"].items() if key != "cursor"}
        state = cursors.decode(first["next_cursor"], "salesforce.report_run", adapter.bound(adapter.validate(params)))

        produced, cursor = run_adapter("salesforce.report_run", self.client(monkeypatch), second["params"], state=state)

        assert produced["page"] == second["data"]["page"] and cursor == second["next_cursor"] is None
