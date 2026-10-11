"""Unit tests for privacyfence.connectors.peek.PeekConnector."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import peek as peek_module
from privacyfence.connectors.peek import PeekConnector
from privacyfence.peek_client import (
    PeekClient,
    PeekClientError,
    PeekComment,
    PeekPage,
    PeekStats,
    PeekVisit,
)

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_no_placeholder_fields,
    assert_tool_definitions_complete,
)

pytestmark = pytest.mark.unit


def make_connector():
    client = MagicMock()
    client.host = "peek.example.com"
    return PeekConnector(client), client


def make_page(slug: str, created_at: str = "2026-01-01T00:00:00Z") -> PeekPage:
    return PeekPage(
        slug=slug, name=f"{slug}.html", owner="ann", size_bytes=120, visibility="private",
        url=f"https://peek.example.com/p/{slug}", created_at=created_at, version=2,
    )


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(peek_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Peek tool"):
            await connector.call("peek_does_not_exist", {})


PEEK_SIBLINGS: dict[str, tuple[str, ...]] = {
    "peek_list_pages": ("peek_get_comments", "peek_get_stats"),
    "peek_get_comments": ("peek_get_stats",),
    "peek_get_stats": ("peek_get_comments",),
}


class TestToolDefinitions:
    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(PeekConnector(None), PEEK_SIBLINGS)


class TestListPages:
    async def test_returns_pages_without_gating_and_audits(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_pages.return_value = [make_page("a"), make_page("b")]

        result = await connector.call("peek_list_pages", {})

        assert [p["slug"] for p in result] == ["a", "b"]
        assert set(result[0]) == {
            "slug", "name", "owner", "size_bytes", "visibility", "url", "created_at", "version",
        }
        assert gated_call_spy == []
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(entries) == 1
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"tool": "peek_list_pages"' in entries[0]

    async def test_truncates_to_max_results(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_pages.return_value = [make_page(s) for s in "abc"]

        result = await connector.call("peek_list_pages", {"max_results": 2})

        assert [p["slug"] for p in result] == ["a", "b"]

    @pytest.mark.parametrize("bad", [0, 201])
    async def test_max_results_out_of_range_raises(self, bad):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="max_results must be between 1 and 200."):
            await connector.call("peek_list_pages", {"max_results": bad})
        client.list_pages.assert_not_called()

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_pages.side_effect = PeekClientError("Peek refused the saved sign-in")
        with pytest.raises(RuntimeError, match="Peek refused the saved sign-in"):
            await connector.call("peek_list_pages", {})


def make_comment(kind="page", selector="", element_text="", body="Looks good", cid=1):
    return PeekComment(
        id=cid, author="bob", body=body, anchor_kind=kind, selector=selector,
        element_text=element_text, created_at="2026-10-10T11:07:00Z", version=2,
    )


class TestGetComments:
    async def test_gates_with_minimal_preview_and_full_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = [
            make_comment("element", "#hero", "", "Fix the title", 1),
            make_comment("text", "p", "Quoted", "Typo here", 2),
        ]

        result = await connector.call("peek_get_comments", {"slug": "slug1"})

        call = gated_call_spy[0]
        assert call["gate"] == "review"
        assert call["tool"] == "peek_get_comments"
        assert list(call["preview"]) == ["Server", "Page", "Link", "Version"]
        assert call["preview"]["Server"] == "peek.example.com"
        assert call["preview"]["Version"] == "2 of 2"
        assert "Fix the title" not in str(call["preview"])
        assert "bob (Element #hero): Fix the title" in call["details_text"]
        assert "Text \u201cQuoted\u201d" in call["details_text"]
        assert call["pii_scan_text"] == "Fix the title\nTypo here\nQuoted"
        assert call["preview_tables"][0]["headers"] == ["Author", "Date", "Anchored to", "Comment"]
        assert call["table_only"] is True
        assert call["args"] == {"slug": "slug1", "version": 0}
        assert result["version"] == 2
        assert [c["id"] for c in result["comments"]] == [1, 2]

    async def test_earlier_version(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = []

        result = await connector.call("peek_get_comments", {"slug": "slug1", "version": 1})

        assert gated_call_spy[0]["preview"]["Version"] == "1 of 2"
        assert gated_call_spy[0]["args"] == {"slug": "slug1", "version": 1}
        assert result["version"] == 1

    async def test_no_comments(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = []

        await connector.call("peek_get_comments", {"slug": "slug1"})

        assert gated_call_spy[0]["details_text"] == "(no comments)"
        assert gated_call_spy[0]["preview_tables"] == []

    @pytest.mark.parametrize("kind,selector,text,expected", [
        ("page", "", "", "Whole page"),
        ("element", "#a", "", "Element #a"),
        ("text", "p", "hi", "Text \u201chi\u201d"),
    ])
    async def test_anchor_kinds(self, gated_call_spy, kind, selector, text, expected):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_comments.return_value = [make_comment(kind, selector, text)]

        await connector.call("peek_get_comments", {"slug": "slug1"})

        assert expected in gated_call_spy[0]["details_text"]
        assert gated_call_spy[0]["preview_tables"][0]["rows"][0][2] == expected

    async def test_negative_version_raises(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="version must be 0"):
            await connector.call("peek_get_comments", {"slug": "slug1", "version": -1})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_bad_slug_raises(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Peek page slug"):
            await connector.call("peek_get_comments", {"slug": "../x"})
        assert gated_call_spy == []
        client.get_page.assert_not_called()

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.side_effect = PeekClientError("Peek found no such page")
        with pytest.raises(RuntimeError, match="Peek found no such page"):
            await connector.call("peek_get_comments", {"slug": "slug1"})
        assert gated_call_spy == []


class TestGetStats:
    async def test_gates_and_returns_names_and_times_only(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_stats.return_value = PeekStats(
            "slug1", "slug1.html", 5, 3,
            [PeekVisit("Ann", "2026-10-10T11:07:00Z"), PeekVisit("", "2026-10-10T12:00:00Z")],
        )

        result = await connector.call("peek_get_stats", {"slug": "slug1"})

        call = gated_call_spy[0]
        assert call["gate"] == "review"
        assert list(call["preview"]) == ["Server", "Page", "Link"]
        assert call["preview"]["Server"] == "peek.example.com"
        assert list(call["new_info"]) == ["Visits", "Unique visitors", "Recent visitors"]
        assert "(no name given)" in call["details_text"]
        assert call["pii_scan_text"] == "Ann"
        assert call["args"] == {"slug": "slug1"}
        assert all(set(v) == {"visitor_name", "visited_at"} for v in result["recent"])
        assert result["total_visits"] == 5
        assert result["unique_visitors"] == 3

    async def test_no_visits(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_stats.return_value = PeekStats("slug1", "slug1.html", 0, 0, [])

        await connector.call("peek_get_stats", {"slug": "slug1"})

        assert gated_call_spy[0]["details_text"] == "(no visits)"
        assert gated_call_spy[0]["preview_tables"] == []

    async def test_bad_slug_raises(self, gated_call_spy):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Peek page slug"):
            await connector.call("peek_get_stats", {"slug": "a b"})
        assert gated_call_spy == []

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page("slug1")
        client.get_stats.side_effect = PeekClientError("Peek API error (HTTP 500)")
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await connector.call("peek_get_stats", {"slug": "slug1"})
        assert gated_call_spy == []


class TestFieldCompleteness:
    async def test_comments_preview_has_no_placeholder(self, monkeypatch, gated_call_spy):
        pages = [{
            "slug": "slug1", "name": "QA page", "owner": "qa", "size": 10,
            "visibility": "private", "url": "https://peek.example.com/p/slug1",
            "created_at": 1760094420, "version": 2,
        }]
        comments = [{
            "id": 1, "selector": "#a", "element_text": "x", "anchor_kind": "element",
            "author": "bob", "body": "hi", "created_at": 1760094420, "version": 2,
        }]

        def fake_request(self, method, url, **kwargs):
            payload = comments if "/comments" in url else pages
            return SimpleNamespace(status_code=200, json=lambda: payload)

        monkeypatch.setattr("requests.Session.request", fake_request)
        connector = PeekConnector(PeekClient("https://peek.example.com", "tok"))

        await connector.call("peek_get_comments", {"slug": "slug1"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.list_pages.return_value = []
        client.get_page.return_value = PeekPage(
            "slug1", "QA page", "qa", 10, "private",
            "https://peek.example.com/p/slug1", "2026-10-10T11:07:00Z", 2,
        )
        client.get_comments.return_value = []
        client.get_stats.return_value = PeekStats("slug1", "QA page", 0, 0, [])
        await assert_all_tools_leave_an_audit_trail(
            connector, peek_module, monkeypatch, tmp_path,
            arg_overrides={
                "peek_get_comments": {"slug": "slug1"},
                "peek_get_stats": {"slug": "slug1"},
            },
        )
