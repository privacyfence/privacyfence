"""Unit tests for privacyfence.connectors.grist.GristConnector's listing tools."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import grist as grist_module
from privacyfence.connectors.grist import GristConnector
from privacyfence.grist_client import (
    GristAccessDenied,
    GristClientError,
    GristColumn,
    GristDocument,
    GristTable,
)

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_tool_definitions_complete,
)

pytestmark = pytest.mark.unit

DOC_ID = "8CAN8gKdxY7zAbCdEfGh12"


def make_connector():
    client = MagicMock()
    return GristConnector(client), client


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(grist_module, "gated_call", fake_gated_call, raising=False)
    return calls


def audit_lines(tmp_path):
    return (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Grist tool"):
            await connector.call("grist_does_not_exist", {})


class TestListDocuments:
    async def test_returns_documents_without_a_card(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_documents.return_value = [
            GristDocument(id=DOC_ID, name="Budget", workspace="Home", team="Acme"),
        ]

        result = await connector.call("grist_list_documents", {})

        assert result == {"documents": [{"id": DOC_ID, "name": "Budget", "workspace": "Home", "team": "Acme"}]}
        assert gated_call_spy == []
        entries = audit_lines(tmp_path)
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"tool": "grist_list_documents"' in entries[0]

    async def test_access_denied_gives_the_document_id_hint(self):
        connector, client = make_connector()
        client.list_documents.side_effect = GristAccessDenied("Grist refused the request (HTTP 403).")

        with pytest.raises(RuntimeError, match="does not let PrivacyFence list your documents") as info:
            await connector.call("grist_list_documents", {})
        assert "Document ID" in str(info.value)

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_documents.side_effect = GristClientError("server unreachable")

        with pytest.raises(RuntimeError, match="server unreachable"):
            await connector.call("grist_list_documents", {})


class TestListTables:
    async def test_returns_tables_and_columns_without_a_card(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_tables.return_value = [
            GristTable(id="Contacts", columns=[
                GristColumn(id="Name", label="Name", type="Text", is_formula=False),
                GristColumn(id="Score", label="Score", type="Numeric", is_formula=True),
            ]),
        ]

        result = await connector.call("grist_list_tables", {"doc_id": DOC_ID})

        assert result == {"doc_id": DOC_ID, "tables": [{"id": "Contacts", "columns": [
            {"id": "Name", "label": "Name", "type": "Text", "is_formula": False},
            {"id": "Score", "label": "Score", "type": "Numeric", "is_formula": True},
        ]}]}
        client.list_tables.assert_called_once_with(DOC_ID)
        assert gated_call_spy == []
        entries = audit_lines(tmp_path)
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"tool": "grist_list_tables"' in entries[0]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_tables.side_effect = GristClientError("Grist found no such document, table or record (HTTP 404).")

        with pytest.raises(RuntimeError, match="no such document"):
            await connector.call("grist_list_tables", {"doc_id": DOC_ID})

    @pytest.mark.parametrize("bad", ["", "a/b", "../x", "has space"])
    async def test_bad_doc_id_raises_before_the_client_is_called(self, bad):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="Not a Grist document id"):
            await connector.call("grist_list_tables", {"doc_id": bad})
        client.list_tables.assert_not_called()


GRIST_SIBLINGS: dict[str, list[str]] = {
    "grist_list_documents": ["grist_list_tables"],
    "grist_list_tables": ["grist_list_documents"],
}


class TestToolDefinitions:
    """What an AI client reads to choose and call these tools (ADR 0115)."""

    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(GristConnector(MagicMock()), GRIST_SIBLINGS)


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.list_documents.return_value = []
        client.list_tables.return_value = []

        await assert_all_tools_leave_an_audit_trail(
            connector, grist_module, monkeypatch, tmp_path,
            arg_overrides={"grist_list_tables": {"doc_id": DOC_ID}},
        )
