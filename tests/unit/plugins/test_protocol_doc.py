"""The plugin protocol reference lists every field of the records a source call returns."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from privacyfence.calendar_client import CalendarAttachment, CalendarAttendee, CalendarEvent
from privacyfence.confluence_client import ConfluencePage
from privacyfence.jira_client import JiraIssue

DOC = Path(__file__).resolve().parents[3] / "docs" / "plugin-protocol.md"


def _record_shapes() -> str:
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index("#### Record shapes")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")), len(lines))
    return "\n".join(lines[start:end])


@pytest.mark.parametrize(
    "record", [JiraIssue, CalendarEvent, CalendarAttendee, CalendarAttachment, ConfluencePage]
)
def test_every_record_field_is_documented(record):
    text = _record_shapes()
    missing = [f.name for f in dataclasses.fields(record) if f"`{f.name}`" not in text]
    assert not missing, f"{record.__name__} fields missing from Record shapes: {missing}"


@pytest.mark.parametrize("name", ["body_offset", "body_total_chars"])
def test_confluence_paging_fields_are_documented(name):
    assert f"`{name}`" in _record_shapes()
