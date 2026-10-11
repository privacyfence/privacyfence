"""Unit tests for privacyfence.connectors.peek.PeekConnector."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import peek as peek_module
from privacyfence.connectors.peek import PeekConnector
from privacyfence.peek_client import PeekClientError, PeekPage

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
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

    monkeypatch.setattr(peek_module, "gated_call", fake_gated_call, raising=False)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Peek tool"):
            await connector.call("peek_does_not_exist", {})


PEEK_SIBLINGS: dict[str, tuple[str, ...]] = {
    "peek_list_pages": (),
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


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.list_pages.return_value = []
        await assert_all_tools_leave_an_audit_trail(connector, peek_module, monkeypatch, tmp_path)
