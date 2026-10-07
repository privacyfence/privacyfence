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
from privacyfence.plugins.constants import DRIVE_CHUNK_BYTES, SOURCE_OPERATIONS
from privacyfence.plugins.source_ops import SOURCE_ADAPTERS
from privacyfence.plugins.spool import DownloadSpool

pytestmark = pytest.mark.unit

SAMPLES_DIR = (
    Path(__file__).resolve().parents[3] / "plugin-sdk" / "src" / "privacyfence_plugin_sdk" / "testing" / "samples"
)


def sample(operation: str) -> dict:
    return json.loads((SAMPLES_DIR / f"{operation}.json").read_text(encoding="utf-8"))


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


def run_adapter(operation: str, client, params: dict, spool=None):
    adapter = SOURCE_ADAPTERS[operation]
    return adapter.run(client, {**adapter.validate(params), "plugin": "sample"}, spool)


def test_there_is_one_sample_per_source_operation():
    assert sorted(p.stem for p in SAMPLES_DIR.glob("*.json")) == sorted(SOURCE_OPERATIONS)
    for operation in SOURCE_OPERATIONS:
        loaded = sample(operation)
        assert loaded["operation"] == operation
        assert set(loaded) == {"operation", "params", "data", "next_cursor"}


class TestShapeMatchesTheAdapters:
    def test_calendar_list_events(self):
        data = sample("calendar.list_events")["data"]
        client = SimpleNamespace(list_events=lambda *args: [build(CalendarEvent, item) for item in data])

        produced, cursor = run_adapter(
            "calendar.list_events", client, {"time_min": "2026-10-07T00:00:00Z", "time_max": "2026-10-08T00:00:00Z"},
        )

        assert produced == data and cursor is None

    def test_jira_search(self):
        data = sample("jira.search")["data"]
        client = SimpleNamespace(search_issues=lambda jql, limit: [build(JiraIssue, item) for item in data])

        produced, cursor = run_adapter("jira.search", client, {"jql": "project = EXAMPLE"})

        assert produced == data and cursor is None

    def test_confluence_get_page(self):
        data = sample("confluence.get_page")["data"]
        client = SimpleNamespace(get_page=lambda page_id: build(ConfluencePage, data))

        produced, cursor = run_adapter("confluence.get_page", client, {"page_id": "EXAMPLE-1"})

        assert produced == data and cursor is None

    def test_sheets_get_values(self):
        data = sample("sheets.get_values")["data"]
        client = SimpleNamespace(get_sheet_values=lambda sheet, cells, option: data["values"])

        produced, cursor = run_adapter("sheets.get_values", client, {"spreadsheet_id": "EXAMPLE-1", "range": "A:B"})

        assert produced == data and cursor is None
        assert all(isinstance(row, list) for row in produced["values"])

    def test_drive_download(self, tmp_path):
        loaded = sample("drive.download")
        data = loaded["data"]
        content = base64.b64decode(data["content_base64"])
        client = SimpleNamespace(
            get_file_metadata=lambda file_id: SimpleNamespace(size=len(content), modified_time=data["revision"]),
            download_file_bytes=lambda file_id: {"data": content, "mime_type": data["mime_type"]},
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
