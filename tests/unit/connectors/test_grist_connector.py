"""Unit tests for privacyfence.connectors.grist.GristConnector's listing tools."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import grist as grist_module
from privacyfence.connectors.grist import GristConnector
from privacyfence.grist_client import (
    GristAccessDenied,
    GristClientError,
    GristColumn,
    GristClient,
    GristDocument,
    GristRecord,
    GristRecordPage,
    GristTable,
)

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_no_placeholder_fields,
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


def make_doc(**overrides):
    defaults = dict(id=DOC_ID, name="Budget", workspace="Home", team="Acme")
    defaults.update(overrides)
    return GristDocument(**defaults)


def two_records(truncated=False):
    return GristRecordPage(
        records=[
            GristRecord(id=1, fields={"Name": "Ada", "Score": 3}),
            GristRecord(id=2, fields={"Name": "Grace", "Tags": ["a", "b"], "Note": None}),
        ],
        truncated=truncated,
    )


class TestGetRecords:
    ARGS = {"doc_id": DOC_ID, "table_id": "Contacts"}

    async def test_preview_hides_cells_and_details_carry_them(self, gated_call_spy):
        connector, client = make_connector()
        client.host = "docs.getgrist.com"
        client.get_document.return_value = make_doc()
        client.get_records.return_value = two_records()

        result = await connector.call(
            "grist_get_records",
            {**self.ARGS, "filter": '{"Score": [3]}', "sort": "-Score,Name", "limit": 50},
        )

        call = gated_call_spy[0]
        assert list(call["preview"]) == ["Server", "Document", "Team", "Table", "Filter", "Sort"]
        assert call["preview"]["Server"] == "docs.getgrist.com"
        assert call["preview"]["Document"] == "Budget"
        assert call["preview"]["Team"] == "Acme"
        assert call["preview"]["Filter"] == '{"Score": [3]}'
        assert call["preview"]["Sort"] == "-Score,Name"
        assert "Ada" not in " ".join(call["preview"].values())
        assert call["gate"] == "review"
        assert call["tool"] == "grist_get_records"
        assert call["details_text"] == "#1\nName: Ada\nScore: 3\n\n#2\nName: Grace\nTags: [\"a\", \"b\"]\nNote: "
        assert call["pii_scan_text"] == call["details_text"]
        assert call["preview_tables"][0]["headers"] == ["id", "Name", "Score", "Tags", "Note"]
        assert call["preview_tables"][0]["rows"][0] == ["1", "Ada", "3", "", ""]
        assert call["args"] == {
            "doc_id": DOC_ID, "table_id": "Contacts",
            "filter": '{"Score": [3]}', "sort": "-Score,Name", "limit": 50,
        }
        assert call["table_only"] is True
        client.get_records.assert_called_once_with(
            DOC_ID, "Contacts", filters={"Score": [3]}, sort="-Score,Name", limit=50,
        )
        assert result["records"][0] == {"id": 1, "fields": {"Name": "Ada", "Score": 3}}
        assert result["truncated"] is False

    async def test_defaults_show_none(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = two_records()

        await connector.call("grist_get_records", self.ARGS)

        assert gated_call_spy[0]["preview"]["Filter"] == "(none)"
        assert gated_call_spy[0]["preview"]["Sort"] == "(none)"
        assert gated_call_spy[0]["args"]["limit"] == 100

    async def test_truncated_prefixes_the_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = two_records(truncated=True)

        result = await connector.call("grist_get_records", {**self.ARGS, "limit": 2})

        assert gated_call_spy[0]["details_text"].startswith("Showing the first 2 records; more match.\n\n#1")
        assert result["truncated"] is True

    async def test_no_records_gives_no_table(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = GristRecordPage(records=[], truncated=False)

        await connector.call("grist_get_records", self.ARGS)

        assert gated_call_spy[0]["details_text"] == "(no records)"
        assert not gated_call_spy[0].get("preview_tables")

    async def test_long_cell_is_cut(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = GristRecordPage(
            records=[GristRecord(id=1, fields={"Note": "x" * 300})], truncated=False,
        )

        await connector.call("grist_get_records", self.ARGS)

        assert f"Note: {'x' * 200}…" in gated_call_spy[0]["details_text"]

    async def test_document_details_denied_falls_back_to_the_id(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.side_effect = GristAccessDenied("Grist refused the request (HTTP 403).")
        client.get_records.return_value = two_records()

        await connector.call("grist_get_records", self.ARGS)

        assert gated_call_spy[0]["preview"]["Document"] == DOC_ID
        assert gated_call_spy[0]["preview"]["Team"] == "(not shown by Grist)"

    @pytest.mark.parametrize("extra, message", [
        ({"filter": "not json"}, "filter must be a JSON object"),
        ({"filter": "[1]"}, "filter must be a JSON object"),
        ({"filter": '{"Status": "Open"}'}, "filter must be a JSON object"),
        ({"filter": '{"Status": []}'}, "filter must be a JSON object"),
        ({"filter": '{"Status": [{"a": 1}]}'}, "filter must be a JSON object"),
        ({"filter": '{"bad col": ["x"]}'}, "filter must be a JSON object"),
        ({"filter": json.dumps({f"C{i}": [1] for i in range(21)})}, "filter must be a JSON object"),
        ({"sort": "Name,"}, "sort must be column ids"),
        ({"sort": "--Name"}, "sort must be column ids"),
        ({"sort": "a b"}, "sort must be column ids"),
        ({"limit": 0}, "limit must be between 1 and 500."),
        ({"limit": 501}, "limit must be between 1 and 500."),
    ])
    async def test_bad_arguments_raise_before_anything_is_fetched(self, gated_call_spy, extra, message):
        connector, client = make_connector()

        with pytest.raises(ValueError, match=message):
            await connector.call("grist_get_records", {**self.ARGS, **extra})
        assert gated_call_spy == []
        client.get_records.assert_not_called()

    async def test_bad_ids_raise_before_anything_is_fetched(self, gated_call_spy):
        connector, client = make_connector()

        with pytest.raises(ValueError, match="Not a Grist table id"):
            await connector.call("grist_get_records", {"doc_id": DOC_ID, "table_id": "a/b"})
        with pytest.raises(ValueError, match="Not a Grist document id"):
            await connector.call("grist_get_records", {"doc_id": "a/b", "table_id": "T"})
        assert gated_call_spy == []
        client.get_records.assert_not_called()

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.side_effect = GristClientError("Grist found no such document, table or record (HTTP 404).")

        with pytest.raises(RuntimeError, match="no such document"):
            await connector.call("grist_get_records", self.ARGS)
        assert gated_call_spy == []


class TestFieldCompleteness:
    """A real GristClient with a faked HTTP layer, so the raw response -> dataclass -> card
    path is exercised end to end."""

    async def test_get_records_preview_has_no_placeholder_fields(self, gated_call_spy):
        client = GristClient("https://docs.example.com", MagicMock())

        def fake_request(method, path, *, params=None, json_body=None):
            if path == f"/api/docs/{DOC_ID}":
                return {"name": "Budget", "workspace": {"name": "Home", "org": {"name": "Acme"}}}
            return {"records": [{"id": 1, "fields": {"Name": "Ada"}}]}

        client._request = fake_request
        connector = GristConnector(client)

        await connector.call("grist_get_records", {"doc_id": DOC_ID, "table_id": "Contacts",
                                                  "filter": '{"Name": ["Ada"]}', "sort": "Name"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


GRIST_SIBLINGS: dict[str, list[str]] = {
    "grist_list_documents": ["grist_list_tables"],
    "grist_list_tables": ["grist_list_documents", "grist_get_records"],
    "grist_get_records": ["grist_list_tables"],
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
        client.get_document.return_value = GristDocument(id=DOC_ID, name="Budget", workspace="Home", team="Acme")
        client.get_records.return_value = GristRecordPage(records=[], truncated=False)

        await assert_all_tools_leave_an_audit_trail(
            connector, grist_module, monkeypatch, tmp_path,
            arg_overrides={
                "grist_list_tables": {"doc_id": DOC_ID},
                "grist_get_records": {"doc_id": DOC_ID, "table_id": "Contacts"},
            },
        )
