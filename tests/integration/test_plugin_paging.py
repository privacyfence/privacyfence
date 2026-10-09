"""Paged source reads end to end: ``echo`` walks every page of an operation through ``ctx.source``
and a Drive file of any size streams to the end in ranged chunks.

The provider clients are the harness's fakes; the cursors between plugin and daemon are real.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from tests.fixtures.plugins.echo.harness import FakeDrive, Stack, calendar_event, install_echo

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

MIB = 1024 * 1024


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch, serve=True)
    install_echo(stack.plugins, source_operations=["calendar.list_events", "jira.search", "drive.download"])
    await stack.start()
    await stack.enable()
    try:
        yield stack
    finally:
        await stack.stop()


async def collected(stack, op: str, **query: str) -> dict:
    page = await stack.page("/pages", op=op, **query)
    assert page["status"] == 200, page
    return json.loads(page["body"])


def serve_calendar_pages(stack, pages: dict[str | None, tuple[list, str | None]]) -> None:
    """The provider answers by the page token it is given, so a wrong token fails the test."""
    stack.calendar.list_events.side_effect = lambda calendar_id, page_size, time_min, time_max, token=None: pages[token]


class TestCalendar:
    async def test_every_event_of_both_provider_pages_arrives_once(self, stack):
        serve_calendar_pages(stack, {
            None: ([calendar_event(f"A{n}", id=f"a{n}") for n in range(3)], "page-2"),
            "page-2": ([calendar_event(f"B{n}", id=f"b{n}") for n in range(2)], None),
        })

        result = await collected(stack, "calendar.list_events")

        assert result["pages"] == 2 and result["items"] == 5
        assert result["ids"] == ["a0", "a1", "a2", "b0", "b1"]
        assert stack.calendar.list_events.call_count == 2

    async def test_a_single_page_needs_one_call(self, stack):
        serve_calendar_pages(stack, {None: ([calendar_event(id="only")], None)})

        result = await collected(stack, "calendar.list_events")

        assert result == {"pages": 1, "items": 1, "ids": ["only"]}


class TestJira:
    async def test_every_issue_arrives_once_across_provider_pages(self, stack):
        result = await collected(stack, "jira.search", page_size="3")

        assert result["pages"] == 2 and result["items"] == 5
        assert result["ids"] == [f"ECHO-{n}" for n in range(1, 6)]
        # The second request carries the token the provider gave for the first page.
        assert [(size, token) for _, size, token in stack.jira.calls] == [(3, None), (3, "jira-page-3")]

    async def test_a_page_size_that_holds_everything_is_one_page(self, stack):
        result = await collected(stack, "jira.search", page_size="100")

        assert result["pages"] == 1 and result["items"] == 5


class TestDriveBinary:
    @pytest.mark.timeout(120)
    async def test_a_100_mib_file_streams_to_the_end_without_a_full_download(self, stack):
        size = 100 * MIB
        stack.drive = FakeDrive(size=size)
        expected = hashlib.sha256()
        for offset in range(0, size, 8 * MIB):
            expected.update(stack.drive.download_range("big", offset, 8 * MIB))
        stack.drive.ranges.clear()

        page = await stack.page("/download", file_id="BIG")

        result = json.loads(page["body"])
        assert result["size"] == size and result["sha256"] == expected.hexdigest()
        assert result["mime_type"] == "application/octet-stream"
        assert stack.drive.full_downloads == 0
        assert len(stack.drive.ranges) == 13       # 8 MiB at a time, the last one short
        assert all(length <= 8 * MIB for _, length in stack.drive.ranges)
        assert [offset for offset, _ in stack.drive.ranges] == list(range(0, size, 8 * MIB))
