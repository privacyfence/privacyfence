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


def two_records(truncated=False, next_after_id=None):
    return GristRecordPage(
        records=[
            GristRecord(id=1, fields={"Name": "Ada", "Score": 3}),
            GristRecord(id=2, fields={"Name": "Grace", "Tags": ["a", "b"], "Note": None}),
        ],
        truncated=truncated,
        next_after_id=next_after_id,
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
        assert list(call["preview"]) == ["Server", "Document", "Team", "Table", "Filter", "Sort", "Page"]
        assert call["preview"]["Server"] == "docs.getgrist.com"
        assert call["preview"]["Document"] == "Budget"
        assert call["preview"]["Team"] == "Acme"
        assert call["preview"]["Filter"] == '{"Score": [3]}'
        assert call["preview"]["Sort"] == "-Score,Name"
        assert call["preview"]["Page"] == "First page; last page"
        assert "Ada" not in " ".join(call["preview"].values())
        assert call["gate"] == "review"
        assert call["tool"] == "grist_get_records"
        assert call["details_text"] == "#1\nName: Ada\nScore: 3\n\n#2\nName: Grace\nTags: [\"a\", \"b\"]\nNote: "
        assert call["pii_scan_text"] == call["details_text"]
        assert call["preview_tables"][0]["headers"] == ["id", "Name", "Score", "Tags", "Note"]
        assert call["preview_tables"][0]["rows"][0] == ["1", "Ada", "3", "", ""]
        assert call["args"] == {
            "doc_id": DOC_ID, "table_id": "Contacts",
            "filter": '{"Score": [3]}', "sort": "-Score,Name", "limit": 50, "after_id": 0,
        }
        assert call["table_only"] is True
        client.get_records.assert_called_once_with(
            DOC_ID, "Contacts", filters={"Score": [3]}, sort="-Score,Name", limit=50, after_id=0,
        )
        assert result["records"][0] == {"id": 1, "fields": {"Name": "Ada", "Score": 3}}
        assert result["truncated"] is False
        assert result["next_after_id"] is None

    async def test_defaults_show_none(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = two_records()

        await connector.call("grist_get_records", self.ARGS)

        assert gated_call_spy[0]["preview"]["Filter"] == "(none)"
        assert gated_call_spy[0]["preview"]["Sort"] == "(none)"
        assert gated_call_spy[0]["args"]["limit"] == 100
        assert gated_call_spy[0]["args"]["after_id"] == 0
        assert gated_call_spy[0]["preview"]["Page"] == "First page, records #1–#2; last page"

    async def test_truncated_prefixes_the_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = two_records(truncated=True, next_after_id=2)

        result = await connector.call("grist_get_records", {**self.ARGS, "limit": 2})

        assert gated_call_spy[0]["details_text"].startswith("Showing 2 records; more match.\n\n#1")
        assert gated_call_spy[0]["preview"]["Page"] == "First page, records #1–#2; more follow"
        assert result["truncated"] is True
        assert result["next_after_id"] == 2

    async def test_middle_page(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = GristRecordPage(
            records=[GristRecord(id=i, fields={"N": i}) for i in (6, 7)],
            truncated=True, next_after_id=7,
        )

        result = await connector.call("grist_get_records", {**self.ARGS, "limit": 2, "after_id": 5})

        client.get_records.assert_called_once_with(
            DOC_ID, "Contacts", filters={}, sort="", limit=2, after_id=5,
        )
        call = gated_call_spy[0]
        assert call["preview"]["Page"] == "After record #5, records #6–#7; more follow"
        assert call["args"]["after_id"] == 5
        assert call["summary"] == "Read 2 record(s) from Budget / Contacts"
        assert result["next_after_id"] == 7
        assert [r["id"] for r in result["records"]] == [6, 7]

    async def test_last_page_past_the_end(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = GristRecordPage(records=[], truncated=False)

        result = await connector.call("grist_get_records", {**self.ARGS, "after_id": 9})

        assert gated_call_spy[0]["preview"]["Page"] == "After record #9; last page"
        assert gated_call_spy[0]["details_text"] == "(no records)"
        assert result["records"] == [] and result["truncated"] is False
        assert result["next_after_id"] is None

    async def test_sorted_page_shows_no_id_range(self, gated_call_spy):
        connector, client = make_connector()
        client.get_document.return_value = make_doc()
        client.get_records.return_value = two_records(truncated=True)

        result = await connector.call("grist_get_records", {**self.ARGS, "sort": "Name", "limit": 2})

        assert gated_call_spy[0]["preview"]["Page"] == "First page; more follow"
        assert result["truncated"] is True and result["next_after_id"] is None

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
        ({"after_id": -1}, "after_id must be a record id"),
        ({"after_id": True}, "after_id must be a record id"),
        ({"after_id": "5"}, "after_id must be a record id"),
        ({"after_id": 2**31}, "after_id must be a record id"),
        ({"after_id": 5, "sort": "Name"}, "leave sort empty when passing after_id"),
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


NAME_COLUMNS = [
    GristColumn("Name", "Name", "Text", False),
    GristColumn("Score", "Score", "Int", False),
    GristColumn("Total", "Total", "Numeric", True),
]


def write_connector():
    connector, client = make_connector()
    client.host = "docs.getgrist.com"
    client.get_document.return_value = make_doc()
    client.list_columns.return_value = NAME_COLUMNS
    return connector, client


@pytest.fixture
def raising_gated_call(monkeypatch):
    async def deny(**kwargs):
        raise RuntimeError("denied")

    monkeypatch.setattr(grist_module, "gated_call", deny, raising=False)


class TestAddRecords:
    ARGS = {"doc_id": DOC_ID, "table_id": "Contacts"}

    async def test_card_and_write(self, gated_call_spy):
        connector, client = write_connector()
        client.add_records.return_value = [7, 8]
        rows = [{"Name": "Ada", "Score": 3}, {"Name": "Grace"}]

        result = await connector.call(
            "grist_add_records", {**self.ARGS, "records": json.dumps(rows)},
        )

        call = gated_call_spy[0]
        assert call["tool"] == "grist_add_records"
        assert call["gate"] == "popup"
        assert call["filtered_data"] is None
        assert list(call["preview"]) == ["Server", "Document", "Table", "Records"]
        assert call["preview"] == {
            "Server": "docs.getgrist.com", "Document": "Budget", "Table": "Contacts", "Records": "2",
        }
        assert call["preview_tables"] == [{
            "headers": ["Name", "Score"], "rows": [["Ada", "3"], ["Grace", ""]],
        }]
        assert json.loads(call["details_text"]) == rows
        assert call["raw_data"] == {"doc_id": DOC_ID, "table_id": "Contacts", "records": rows}
        assert call["args"] == self.ARGS
        assert call["summary"] == "Add 2 record(s) to Budget / Contacts"
        client.add_records.assert_called_once_with(DOC_ID, "Contacts", rows)
        assert result == {"doc_id": DOC_ID, "table_id": "Contacts", "added_ids": [7, 8]}

    async def test_write_runs_only_after_the_gate_returns(self, monkeypatch):
        connector, client = write_connector()
        order = []

        async def fake_gated_call(**kwargs):
            order.append("gate")

        client.add_records.side_effect = lambda *a: order.append("write") or [1]
        monkeypatch.setattr(grist_module, "gated_call", fake_gated_call, raising=False)

        await connector.call("grist_add_records", {**self.ARGS, "records": '[{"Name": "a"}]'})

        assert order == ["gate", "write"]

    async def test_denial_means_no_write(self, raising_gated_call):
        connector, client = write_connector()

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("grist_add_records", {**self.ARGS, "records": '[{"Name": "a"}]'})
        client.add_records.assert_not_called()

    @pytest.mark.parametrize("records", [
        "not json", "{}", "[]", '["a"]', '[{"Name": [1]}]', '[{"bad key": 1}]',
        json.dumps([{"Name": "x"}] * 101),
    ])
    async def test_bad_records_are_refused(self, gated_call_spy, records):
        connector, client = write_connector()

        with pytest.raises(ValueError, match="records must be a JSON array of 1 to 100 objects"):
            await connector.call("grist_add_records", {**self.ARGS, "records": records})
        assert gated_call_spy == []
        client.add_records.assert_not_called()

    async def test_unknown_column_is_refused(self, gated_call_spy):
        connector, client = write_connector()

        with pytest.raises(ValueError, match=r"Unknown column\(s\) in table Contacts: Nope, Zed\. Call grist_list_tables"):
            await connector.call("grist_add_records", {**self.ARGS, "records": '[{"Zed": 1, "Nope": 2, "Name": "a"}]'})
        assert gated_call_spy == []
        client.add_records.assert_not_called()

    async def test_formula_column_is_refused(self, gated_call_spy):
        connector, client = write_connector()

        with pytest.raises(ValueError, match=r"Column\(s\) Total in table Contacts are formula columns and cannot be written\."):
            await connector.call("grist_add_records", {**self.ARGS, "records": '[{"Total": 1}]'})
        assert gated_call_spy == []
        client.add_records.assert_not_called()

    async def test_bad_ids_are_refused(self, gated_call_spy):
        connector, client = write_connector()

        with pytest.raises(ValueError, match="Not a Grist table id"):
            await connector.call("grist_add_records", {"doc_id": DOC_ID, "table_id": "a/b", "records": "[{}]"})
        assert gated_call_spy == []


class TestUpdateRecords:
    ARGS = {"doc_id": DOC_ID, "table_id": "Contacts"}

    def update_connector(self):
        connector, client = write_connector()
        client.get_records_by_id.return_value = [
            GristRecord(5, {"Name": "old", "Score": 1}), GristRecord(6, {"Name": "x"}),
        ]
        return connector, client

    async def test_card_and_write(self, gated_call_spy):
        connector, client = self.update_connector()
        records = [{"id": 5, "fields": {"Name": "new", "Score": 2}}, {"id": 6, "fields": {"Score": 9}}]

        result = await connector.call(
            "grist_update_records", {**self.ARGS, "records": json.dumps(records)},
        )

        call = gated_call_spy[0]
        assert call["tool"] == "grist_update_records"
        assert call["gate"] == "popup"
        assert call["filtered_data"] is None
        assert call["preview"] == {
            "Server": "docs.getgrist.com", "Document": "Budget", "Table": "Contacts",
            "Records": "2", "Cells changed": "3",
        }
        assert call["preview_tables"] == [{
            "headers": ["Record", "Column", "Current", "New"],
            "rows": [
                ["#5", "Name", "old", "new"],
                ["#5", "Score", "1", "2"],
                ["#6", "Score", "", "9"],
            ],
        }]
        assert call["details_text"] == "#5 Name: old → new\n#5 Score: 1 → 2\n#6 Score:  → 9"
        assert call["raw_data"] == {"doc_id": DOC_ID, "table_id": "Contacts", "records": records}
        assert call["args"] == self.ARGS
        assert call["summary"] == "Update 2 record(s) in Budget / Contacts"
        client.get_records_by_id.assert_called_once_with(DOC_ID, "Contacts", [5, 6])
        client.update_records.assert_called_once_with(
            DOC_ID, "Contacts", [(5, {"Name": "new", "Score": 2}), (6, {"Score": 9})],
        )
        assert result == {"doc_id": DOC_ID, "table_id": "Contacts", "updated_ids": [5, 6]}

    async def test_write_runs_only_after_the_gate_returns(self, monkeypatch):
        connector, client = self.update_connector()
        order = []

        async def fake_gated_call(**kwargs):
            order.append("gate")

        client.update_records.side_effect = lambda *a: order.append("write")
        monkeypatch.setattr(grist_module, "gated_call", fake_gated_call, raising=False)

        await connector.call("grist_update_records", {**self.ARGS, "records": '[{"id": 5, "fields": {"Name": "n"}}]'})

        assert order == ["gate", "write"]

    async def test_denial_means_no_write(self, raising_gated_call):
        connector, client = self.update_connector()

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("grist_update_records", {**self.ARGS, "records": '[{"id": 5, "fields": {"Name": "n"}}]'})
        client.update_records.assert_not_called()

    @pytest.mark.parametrize("records", [
        "not json", "{}", "[]", '[1]', '[{"id": 0, "fields": {"Name": "a"}}]',
        '[{"id": true, "fields": {"Name": "a"}}]', '[{"id": "5", "fields": {"Name": "a"}}]',
        '[{"id": 5, "fields": {}}]', '[{"id": 5}]', '[{"id": 5, "fields": {"Name": [1]}}]',
        '[{"id": 5, "fields": {"Name": "a"}}, {"id": 5, "fields": {"Name": "b"}}]',
        json.dumps([{"id": i + 1, "fields": {"Name": "x"}} for i in range(101)]),
    ])
    async def test_bad_records_are_refused(self, gated_call_spy, records):
        connector, client = self.update_connector()

        with pytest.raises(ValueError, match=r'records must be a JSON array of 1 to 100 \{"id": <record id>'):
            await connector.call("grist_update_records", {**self.ARGS, "records": records})
        assert gated_call_spy == []
        client.update_records.assert_not_called()

    async def test_unknown_column_is_refused(self, gated_call_spy):
        connector, client = self.update_connector()

        with pytest.raises(ValueError, match=r"Unknown column\(s\) in table Contacts: Nope\."):
            await connector.call("grist_update_records", {**self.ARGS, "records": '[{"id": 5, "fields": {"Nope": 1}}]'})
        assert gated_call_spy == []
        client.update_records.assert_not_called()

    async def test_formula_column_is_refused(self, gated_call_spy):
        connector, client = self.update_connector()

        with pytest.raises(ValueError, match=r"Column\(s\) Total in table Contacts are formula columns"):
            await connector.call("grist_update_records", {**self.ARGS, "records": '[{"id": 5, "fields": {"Total": 1}}]'})
        assert gated_call_spy == []
        client.update_records.assert_not_called()

    async def test_missing_ids_are_refused(self, gated_call_spy):
        connector, client = self.update_connector()
        records = '[{"id": 99, "fields": {"Name": "a"}}, {"id": 5, "fields": {"Name": "a"}}, {"id": 42, "fields": {"Name": "a"}}]'

        with pytest.raises(ValueError, match=r"Record id\(s\) not found in table Contacts: 42, 99\."):
            await connector.call("grist_update_records", {**self.ARGS, "records": records})
        assert gated_call_spy == []
        client.update_records.assert_not_called()


class TestCreateTable:
    ARGS = {"doc_id": DOC_ID, "table_id": "Orders"}
    COLUMNS = '[{"id": "Title", "label": "Order title", "type": "Text"}, {"id": "Qty", "type": "Int"}]'

    async def test_card_and_write(self, gated_call_spy):
        connector, client = write_connector()
        client.list_tables.return_value = [GristTable("Contacts", [])]
        client.add_table.return_value = "Orders2"

        result = await connector.call("grist_create_table", {**self.ARGS, "columns": self.COLUMNS})

        call = gated_call_spy[0]
        assert call["tool"] == "grist_create_table"
        assert call["gate"] == "popup"
        assert call["filtered_data"] is None
        assert list(call["preview"]) == ["Server", "Document", "New table", "Columns"]
        assert call["preview"]["New table"] == "Orders"
        assert call["preview"]["Columns"] == "2"
        assert call["preview_tables"] == [{
            "headers": ["Column", "Label", "Type"],
            "rows": [["Title", "Order title", "Text"], ["Qty", "Qty", "Int"]],
        }]
        assert call["details_text"] == "Title (Text) Order title\nQty (Int) Qty"
        assert call["summary"] == "Create table Orders in Budget"
        assert call["args"] == self.ARGS
        cols = [
            {"id": "Title", "label": "Order title", "type": "Text"},
            {"id": "Qty", "label": "Qty", "type": "Int"},
        ]
        client.add_table.assert_called_once_with(DOC_ID, "Orders", cols)
        assert result == {"doc_id": DOC_ID, "table_id": "Orders2", "column_ids": ["Title", "Qty"]}

    async def test_default_type_is_text(self, gated_call_spy):
        connector, client = write_connector()
        client.list_tables.return_value = []
        client.add_table.return_value = "Orders"

        await connector.call("grist_create_table", {**self.ARGS, "columns": '[{"id": "Title"}]'})

        assert gated_call_spy[0]["preview_tables"][0]["rows"] == [["Title", "Title", "Text"]]

    @pytest.mark.parametrize("col_type", [
        "Text", "Numeric", "Int", "Bool", "Date", "Choice", "ChoiceList", "Any",
        "Ref:Contacts", "RefList:Contacts",
    ])
    async def test_every_allowed_type_is_accepted(self, gated_call_spy, col_type):
        connector, client = write_connector()
        client.list_tables.return_value = []
        client.add_table.return_value = "Orders"

        await connector.call(
            "grist_create_table",
            {**self.ARGS, "columns": json.dumps([{"id": "C", "type": col_type}])},
        )

        assert gated_call_spy[0]["preview_tables"][0]["rows"] == [["C", "C", col_type]]

    @pytest.mark.parametrize("col_type", ["Ref:Bad-Id", "DateTime", "Ref:", "Ref", 5])
    async def test_unsupported_type_is_refused(self, gated_call_spy, col_type):
        connector, client = write_connector()

        with pytest.raises(ValueError) as exc_info:
            await connector.call(
                "grist_create_table",
                {**self.ARGS, "columns": json.dumps([{"id": "C", "type": col_type}])},
            )
        assert str(exc_info.value) == (
            f"Unsupported column type {col_type!r}. Use one of Text, Numeric, Int, Bool, Date, "
            "Choice, ChoiceList, Any, Ref:<TableId>, RefList:<TableId>."
        )
        assert gated_call_spy == []
        client.add_table.assert_not_called()

    @pytest.mark.parametrize("columns", [
        "not json", "{}", "[]", '["a"]', '[{"id": "bad id"}]', '[{}]',
        '[{"id": "A", "label": 5}]', json.dumps([{"id": "A", "label": "x" * 101}]),
        '[{"id": "A"}, {"id": "A"}]',
        json.dumps([{"id": f"C{i}"} for i in range(51)]),
    ])
    async def test_bad_columns_are_refused(self, gated_call_spy, columns):
        connector, client = write_connector()

        with pytest.raises(ValueError, match="columns must be a JSON array of 1 to 50"):
            await connector.call("grist_create_table", {**self.ARGS, "columns": columns})
        assert gated_call_spy == []
        client.add_table.assert_not_called()

    async def test_existing_table_is_refused(self, gated_call_spy):
        connector, client = write_connector()
        client.list_tables.return_value = [GristTable("Orders", [])]

        with pytest.raises(ValueError) as exc_info:
            await connector.call("grist_create_table", {**self.ARGS, "columns": self.COLUMNS})
        assert str(exc_info.value) == (
            "Table Orders already exists in this document. "
            "Use grist_add_columns to add columns to it."
        )
        assert gated_call_spy == []
        client.add_table.assert_not_called()

    async def test_denial_means_no_write(self, raising_gated_call):
        connector, client = write_connector()
        client.list_tables.return_value = []

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("grist_create_table", {**self.ARGS, "columns": self.COLUMNS})
        client.add_table.assert_not_called()

    async def test_bad_ids_are_refused(self, gated_call_spy):
        connector, _ = write_connector()

        with pytest.raises(ValueError, match="Not a Grist table id"):
            await connector.call(
                "grist_create_table", {"doc_id": DOC_ID, "table_id": "a/b", "columns": self.COLUMNS},
            )
        assert gated_call_spy == []


class TestAddColumns:
    ARGS = {"doc_id": DOC_ID, "table_id": "Contacts"}
    COLUMNS = '[{"id": "Count", "type": "Int"}, {"id": "Note"}]'

    def connector_with_table(self):
        connector, client = write_connector()
        client.list_tables.return_value = [
            GristTable("Contacts", [GristColumn("Name", "Name", "Text", False)]),
        ]
        return connector, client

    async def test_card_and_write(self, gated_call_spy):
        connector, client = self.connector_with_table()
        client.add_columns.return_value = ["Count", "Note"]

        result = await connector.call("grist_add_columns", {**self.ARGS, "columns": self.COLUMNS})

        call = gated_call_spy[0]
        assert call["tool"] == "grist_add_columns"
        assert call["gate"] == "popup"
        assert call["filtered_data"] is None
        assert list(call["preview"]) == ["Server", "Document", "Table", "New columns"]
        assert call["preview"]["New columns"] == "2"
        assert call["preview_tables"] == [{
            "headers": ["Column", "Label", "Type"],
            "rows": [["Count", "Count", "Int"], ["Note", "Note", "Text"]],
        }]
        assert call["details_text"] == "Count (Int) Count\nNote (Text) Note"
        assert call["summary"] == "Add 2 column(s) to Budget / Contacts"
        assert call["args"] == self.ARGS
        client.add_columns.assert_called_once_with(DOC_ID, "Contacts", [
            {"id": "Count", "label": "Count", "type": "Int"},
            {"id": "Note", "label": "Note", "type": "Text"},
        ])
        assert result == {"doc_id": DOC_ID, "table_id": "Contacts", "column_ids": ["Count", "Note"]}

    async def test_missing_table_is_refused(self, gated_call_spy):
        connector, client = write_connector()
        client.list_tables.return_value = []

        with pytest.raises(ValueError) as exc_info:
            await connector.call("grist_add_columns", {**self.ARGS, "columns": self.COLUMNS})
        assert str(exc_info.value) == (
            "Table Contacts does not exist in this document. Use grist_create_table to create it."
        )
        assert gated_call_spy == []
        client.add_columns.assert_not_called()

    async def test_existing_column_is_refused(self, gated_call_spy):
        connector, client = self.connector_with_table()

        with pytest.raises(ValueError) as exc_info:
            await connector.call(
                "grist_add_columns", {**self.ARGS, "columns": '[{"id": "Name"}, {"id": "Count"}]'},
            )
        assert str(exc_info.value) == "Column(s) Name already exist in table Contacts."
        assert gated_call_spy == []
        client.add_columns.assert_not_called()

    async def test_bad_columns_are_refused(self, gated_call_spy):
        connector, client = self.connector_with_table()

        with pytest.raises(ValueError, match="columns must be a JSON array of 1 to 50"):
            await connector.call("grist_add_columns", {**self.ARGS, "columns": "[]"})
        assert gated_call_spy == []
        client.add_columns.assert_not_called()

    async def test_denial_means_no_write(self, raising_gated_call):
        connector, client = self.connector_with_table()

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("grist_add_columns", {**self.ARGS, "columns": self.COLUMNS})
        client.add_columns.assert_not_called()

    async def test_bad_ids_are_refused(self, gated_call_spy):
        connector, _ = write_connector()

        with pytest.raises(ValueError, match="Not a Grist table id"):
            await connector.call(
                "grist_add_columns", {"doc_id": DOC_ID, "table_id": "a/b", "columns": self.COLUMNS},
            )
        assert gated_call_spy == []


class TestCoverageGaps:
    def test_client_property(self):
        connector, client = make_connector()
        assert connector.client is client

    async def test_update_records_bad_ids_are_refused(self, gated_call_spy):
        connector, _ = write_connector()
        with pytest.raises(ValueError, match="Not a Grist table id"):
            await connector.call(
                "grist_update_records",
                {"doc_id": DOC_ID, "table_id": "a/b", "records": '[{"id": 1, "fields": {"Name": "x"}}]'},
            )
        assert gated_call_spy == []

    async def test_other_document_errors_are_not_swallowed(self, gated_call_spy):
        connector, client = write_connector()
        client.get_document.side_effect = GristClientError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            await connector.call("grist_create_table", {
                "doc_id": DOC_ID, "table_id": "Orders", "columns": '[{"id": "A"}]',
            })

    def test_audit_failure_is_logged_not_raised(self, monkeypatch):
        connector, _ = make_connector()

        def broken():
            raise OSError("disk")

        monkeypatch.setattr(grist_module, "get_audit_logger", broken)
        connector._auto_audit("grist_list_tables", "List Grist Tables", "s", "x", 0.0)


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
    "grist_add_records": ["grist_update_records"],
    "grist_update_records": ["grist_add_records"],
    "grist_create_table": ["grist_add_columns"],
    "grist_add_columns": ["grist_create_table"],
    "grist_import_csv": ["grist_add_records"],
    "grist_update_csv": ["grist_update_records", "grist_import_csv"],
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
        client.host = "docs.getgrist.com"
        client.get_document.return_value = GristDocument("DOC1", "QA doc", "Home", "Personal")
        client.list_columns.return_value = [GristColumn("Name", "Name", "Text", False)]
        client.list_tables.return_value = [GristTable("Table1", [GristColumn("Name", "Name", "Text", False)])]
        client.get_records_by_id.return_value = [GristRecord(1, {"Name": "old"})]
        client.add_records.return_value = [2]
        client.add_table.return_value = "NewTable"
        client.add_columns.return_value = ["Count"]
        client.get_records.return_value = GristRecordPage(records=[], truncated=False)
        csv = tmp_path / "a.csv"
        csv.write_bytes(b"Name\nnew\n")

        await assert_all_tools_leave_an_audit_trail(
            connector, grist_module, monkeypatch, tmp_path,
            arg_overrides={
                "grist_list_tables": {"doc_id": "DOC1"},
                "grist_get_records": {"doc_id": "DOC1", "table_id": "Table1"},
                "grist_add_records": {
                    "doc_id": "DOC1", "table_id": "Table1", "records": '[{"Name": "a"}]',
                },
                "grist_update_records": {
                    "doc_id": "DOC1", "table_id": "Table1",
                    "records": '[{"id": 1, "fields": {"Name": "b"}}]',
                },
                "grist_create_table": {
                    "doc_id": "DOC1", "table_id": "NewTable", "columns": '[{"id": "Title"}]',
                },
                "grist_add_columns": {
                    "doc_id": "DOC1", "table_id": "Table1",
                    "columns": '[{"id": "Count", "type": "Int"}]',
                },
                "grist_import_csv": {"doc_id": "DOC1", "table_id": "Table1", "local_path": str(csv)},
                "grist_update_csv": {
                    "doc_id": "DOC1", "table_id": "Table1", "local_path": str(csv),
                    "key_column": "Name", "create_missing": True,
                },
            },
        )


# ---------------------------------------------------------------------- #
# Bulk CSV import and update (ADR 0147)
# ---------------------------------------------------------------------- #

CSV_COLUMNS = [
    GristColumn("Name", "Name", "Text", False),
    GristColumn("Score", "Score", "Int", False),
    GristColumn("Born", "Born", "Date", False),
    GristColumn("Total", "Total", "Numeric", True),
]
CSV_ARGS = {"doc_id": DOC_ID, "table_id": "Contacts"}


def calendar_seconds(iso):
    import calendar
    from datetime import date

    return calendar.timegm(date.fromisoformat(iso).timetuple())


def csv_connector(tmp_path, text, name="people.csv"):
    connector, client = write_connector()
    client.list_columns.return_value = CSV_COLUMNS
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return connector, client, str(path)


def sha_of(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_slot(data):
    from privacyfence import local_files
    from privacyfence.principal import LOCAL_PRINCIPAL
    from privacyfence.upload_staging import get_upload_staging_store

    store = get_upload_staging_store()
    token = store.create_slot(LOCAL_PRINCIPAL, "people.csv", max_bytes=1_000_000)
    store.fill(token, LOCAL_PRINCIPAL.id, [data])
    return store, token, local_files._encode_token(token), LOCAL_PRINCIPAL


class TestCsvFileHandoff:
    @pytest.mark.parametrize("tool,extra", [
        ("grist_import_csv", {}), ("grist_update_csv", {"key_column": "Name"}),
    ])
    @pytest.mark.parametrize("paths", [{}, {"local_path": "/x.csv", "upload_id": "abc"}])
    async def test_exactly_one_of_local_path_and_upload_id(self, tmp_path, gated_call_spy, tool, extra, paths):
        connector, client, _ = csv_connector(tmp_path, "Name\nAda\n")
        with pytest.raises(ValueError, match="Give exactly one of local_path and upload_id"):
            await connector.call(tool, {**CSV_ARGS, **extra, **paths})
        assert gated_call_spy == []
        client.get_document.assert_not_called()

    @pytest.mark.parametrize("tool,extra", [
        ("grist_import_csv", {}), ("grist_update_csv", {"key_column": "Name"}),
    ])
    async def test_local_path_is_refused_in_org_mode(self, tmp_path, gated_call_spy, tool, extra):
        connector, client, path = csv_connector(tmp_path, "Name\nAda\n")
        connector.download_mode = "org"
        with pytest.raises(ValueError, match="local_path is not available"):
            await connector.call(tool, {**CSV_ARGS, **extra, "local_path": path})
        assert gated_call_spy == []

    async def test_the_size_cap_is_checked_before_the_file_is_read(self, tmp_path, gated_call_spy, monkeypatch):
        from privacyfence import local_files

        connector, _, path = csv_connector(tmp_path, "Name\nAda\n")
        monkeypatch.setattr(grist_module, "_CSV_MAX_BYTES", 5)
        reader = MagicMock()
        monkeypatch.setattr(local_files, "read_local_file", reader)
        with pytest.raises(ValueError, match="larger than 50 MB"):
            await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        reader.assert_not_called()

    def test_the_download_mode_defaults_to_local(self):
        assert GristConnector(MagicMock()).download_mode == "local"

    async def test_a_bad_id_is_refused_before_the_file_is_read(self, tmp_path, gated_call_spy):
        connector, _, path = csv_connector(tmp_path, "Name\nAda\n")
        with pytest.raises(ValueError, match="Not a Grist table id"):
            await connector.call(
                "grist_import_csv", {"doc_id": DOC_ID, "table_id": "a/b", "local_path": path},
            )
        with pytest.raises(ValueError, match="Not a Grist column id"):
            await connector.call("grist_update_csv", {
                **CSV_ARGS, "local_path": path, "key_column": "a b",
            })

    async def test_a_parse_error_reaches_the_model_and_never_the_gate(self, tmp_path, gated_call_spy):
        from privacyfence.grist_csv import GristCsvError

        connector, client, path = csv_connector(tmp_path, "Name,Score\nAda,SECRET\n")
        with pytest.raises(GristCsvError) as info:
            await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        assert str(info.value) == "Line 2, column Score: not a whole number."
        assert "SECRET" not in str(info.value)
        assert gated_call_spy == []
        client.add_records.assert_not_called()


class TestImportCsv:
    async def test_the_card_names_the_server_counts_and_sample(self, tmp_path, gated_call_spy):
        text = "Name,Score,Born\nAda,36,1815-12-10\nGrace,,\n"
        connector, client, path = csv_connector(tmp_path, text)
        client.add_records.return_value = [7, 8]

        result = await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})

        assert result == {
            "doc_id": DOC_ID, "table_id": "Contacts", "added": 2,
            "first_added_id": 7, "last_added_id": 8,
        }
        kwargs = gated_call_spy[0]
        assert kwargs["tool"] == "grist_import_csv" and kwargs["gate"] == "popup"
        assert kwargs["tool_name"] == "Import CSV into Grist"
        assert kwargs["summary"] == "Add 2 record(s) from people.csv to Budget / Contacts"
        assert kwargs["sender"] == "Budget"
        assert list(kwargs["preview"]) == [
            "Server", "Document", "Table", "File", "SHA-256", "Rows in file", "Records to add",
        ]
        assert kwargs["preview"] == {
            "Server": "docs.getgrist.com", "Document": "Budget", "Table": "Contacts",
            "File": "people.csv", "SHA-256": sha_of(text), "Rows in file": "2",
            "Records to add": "2",
        }
        assert kwargs["preview_tables"] == [{
            "caption": "First rows",
            "headers": ["Name", "Score", "Born"],
            "rows": [["Ada", "36", "1815-12-10"], ["Grace", "", ""]],
            "footer": "Showing 2 of 2 rows.",
        }]
        assert kwargs["details_text"] == "\n".join(f"{k}: {v}" for k, v in kwargs["preview"].items())
        assert kwargs["write_content_scan_text"] == text
        assert "upload_pii_scan_text" not in kwargs
        assert kwargs["filtered_data"] is None and kwargs["my_email"] == ""

    async def test_raw_data_holds_no_row_values(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name\nSECRET-NAME\n")
        client.add_records.return_value = [1]
        await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        raw = gated_call_spy[0]["raw_data"]
        assert raw == {
            "doc_id": DOC_ID, "table_id": "Contacts", "sha256": sha_of("Name\nSECRET-NAME\n"),
            "rows": 1, "columns": ["Name"],
        }
        assert "SECRET-NAME" not in json.dumps(raw)

    async def test_the_sample_is_capped_and_the_scan_text_too(self, tmp_path, gated_call_spy, monkeypatch):
        monkeypatch.setattr(grist_module, "_CSV_SCAN_CHARS", 10)
        text = "Name\n" + "".join(f"row{i}\n" for i in range(30))
        connector, client, path = csv_connector(tmp_path, text)
        client.add_records.return_value = list(range(30))
        await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        kwargs = gated_call_spy[0]
        assert len(kwargs["preview_tables"][0]["rows"]) == 20
        assert kwargs["preview_tables"][0]["footer"] == "Showing 20 of 30 rows."
        assert kwargs["write_content_scan_text"] == text[:10]

    async def test_dates_render_as_in_the_file(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Born\n2024-02-29\n")
        client.add_records.return_value = [1]
        await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        assert gated_call_spy[0]["preview_tables"][0]["rows"] == [["2024-02-29"]]
        client.add_records.assert_called_once_with(DOC_ID, "Contacts", [{"Born": 1709164800}])

    async def test_the_approval_is_bound_to_the_file(self, tmp_path, gated_call_spy):
        from privacyfence.approvals import canonical_key

        connector, client, path = csv_connector(tmp_path, "Name\nAda\n")
        client.add_records.return_value = [1]
        await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        (tmp_path / "people.csv").write_text("Name\nGrace\n", encoding="utf-8")
        await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})

        first, second = gated_call_spy[0]["args"], gated_call_spy[1]["args"]
        assert first["sha256"] == sha_of("Name\nAda\n") and second["sha256"] == sha_of("Name\nGrace\n")
        assert set(first) == {"doc_id", "table_id", "local_path", "upload_id", "sha256"}
        assert canonical_key("grist", "grist_import_csv", first) != canonical_key(
            "grist", "grist_import_csv", second,
        )

    async def test_1001_rows_are_written_in_three_chunks(self, tmp_path, gated_call_spy):
        text = "Name\n" + "".join(f"n{i}\n" for i in range(1001))
        connector, client, path = csv_connector(tmp_path, text)
        client.add_records.side_effect = lambda doc, table, rows: list(range(len(rows)))

        result = await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})

        assert [len(c.args[2]) for c in client.add_records.call_args_list] == [500, 500, 1]
        assert result["added"] == 1001
        assert result["first_added_id"] == 0 and result["last_added_id"] == 0
        assert len(gated_call_spy) == 1

    async def test_chunks_split_on_json_size(self, tmp_path, gated_call_spy, monkeypatch):
        monkeypatch.setattr(grist_module, "_CSV_CHUNK_JSON_CHARS", 60)
        text = "Name\n" + "".join(f"name-{i:02d}\n" for i in range(6))
        connector, client, path = csv_connector(tmp_path, text)
        client.add_records.side_effect = lambda doc, table, rows: [1] * len(rows)

        await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})

        sizes = [len(c.args[2]) for c in client.add_records.call_args_list]
        assert sum(sizes) == 6 and len(sizes) > 1
        for call in client.add_records.call_args_list:
            assert len(json.dumps(call.args[2])) <= 60

    async def test_an_oversized_row_forms_a_chunk_on_its_own(self, tmp_path, gated_call_spy, monkeypatch):
        monkeypatch.setattr(grist_module, "_CSV_CHUNK_JSON_CHARS", 5)
        connector, client, path = csv_connector(tmp_path, "Name\na-very-long-name\nb\n")
        client.add_records.side_effect = lambda doc, table, rows: [1] * len(rows)
        await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        assert [len(c.args[2]) for c in client.add_records.call_args_list] == [1, 1]

    async def test_a_failure_on_the_second_chunk_says_how_far_it_got(self, tmp_path, gated_call_spy):
        from privacyfence import safe_errors

        text = "Name\n" + "".join(f"n{i}\n" for i in range(1001))
        connector, client, path = csv_connector(tmp_path, text)
        client.add_records.side_effect = [list(range(500)), GristClientError("SECRET grist text")]

        with pytest.raises(grist_module.GristCsvWriteError) as info:
            await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})

        message = str(info.value)
        assert message == (
            "Grist refused a write after 500 of 1001 row(s) were written; see the PrivacyFence log "
            "for Grist's reason. The rows up to CSV line 501 are in the table; do not rerun the "
            "whole file."
        )
        assert "SECRET" not in message
        assert safe_errors.public_message(info.value) == message

    async def test_a_failure_on_the_first_chunk_wrote_nothing(self, tmp_path, gated_call_spy):
        from privacyfence import safe_errors

        connector, client, path = csv_connector(tmp_path, "Name\nAda\n")
        client.add_records.side_effect = GristClientError("boom")
        with pytest.raises(grist_module.GristCsvWriteError) as info:
            await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        assert str(info.value) == (
            "Grist refused the first write; no rows were written. See the PrivacyFence log for "
            "Grist's reason."
        )
        assert safe_errors.public_message(info.value) == str(info.value)

    async def test_nothing_is_written_when_the_gate_denies(self, tmp_path, raising_gated_call):
        connector, client, path = csv_connector(tmp_path, "Name\nAda\n")
        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        client.add_records.assert_not_called()

    async def test_a_client_without_ids_still_succeeds(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name\nAda\n")
        client.add_records.return_value = []
        result = await connector.call("grist_import_csv", {**CSV_ARGS, "local_path": path})
        assert result["first_added_id"] is None and result["last_added_id"] is None

    async def test_upload_id_claims_the_staged_bytes_and_consumes_the_slot(self, gated_call_spy):
        from privacyfence import local_files

        connector, client = write_connector()
        client.list_columns.return_value = CSV_COLUMNS
        client.add_records.return_value = [3]
        store, token, upload_id, principal = new_slot(b"Name\nAda\n")

        with local_files.call_context(bridge_available=False, uploads={}):
            result = await connector.call("grist_import_csv", {**CSV_ARGS, "upload_id": upload_id})

        assert result["added"] == 1
        client.add_records.assert_called_once_with(DOC_ID, "Contacts", [{"Name": "Ada"}])
        assert gated_call_spy[0]["preview"]["File"] == "uploaded via privacyfence_create_upload_slot"
        assert store.peek(token, principal.id) is None

    async def test_upload_id_survives_a_pending_approval(self, monkeypatch):
        from privacyfence import local_files
        from privacyfence.approvals import ApprovalPending

        decisions = iter(["pending", "approved"])

        async def fake_gated_call(**kwargs):
            if next(decisions) == "pending":
                raise ApprovalPending({"status": "approval_pending", "approval_id": "a1"})
            return kwargs["filtered_data"]

        monkeypatch.setattr(grist_module, "gated_call", fake_gated_call)
        connector, client = write_connector()
        connector.download_mode = "org"
        client.list_columns.return_value = CSV_COLUMNS
        client.add_records.return_value = [3]
        store, token, upload_id, principal = new_slot(b"Name\nAda\n")
        args = {**CSV_ARGS, "upload_id": upload_id}

        with local_files.call_context(bridge_available=False, uploads={}), pytest.raises(ApprovalPending):
            await connector.call("grist_import_csv", args)
        client.add_records.assert_not_called()
        assert store.peek(token, principal.id) is not None

        with local_files.call_context(bridge_available=False, uploads={}):
            result = await connector.call("grist_import_csv", args)

        assert result["added"] == 1
        client.add_records.assert_called_once_with(DOC_ID, "Contacts", [{"Name": "Ada"}])
        assert store.peek(token, principal.id) is None

    async def test_one_upload_id_backs_one_approved_write(self, gated_call_spy):
        from privacyfence import local_files
        from privacyfence.local_files import LocalFileAccessError

        connector, client = write_connector()
        client.list_columns.return_value = CSV_COLUMNS
        client.add_records.return_value = [3]
        _, _, upload_id, _ = new_slot(b"Name\nAda\n")
        args = {**CSV_ARGS, "upload_id": upload_id}

        with local_files.call_context(bridge_available=False, uploads={}):
            await connector.call("grist_import_csv", args)
        with local_files.call_context(bridge_available=False, uploads={}), pytest.raises(
            LocalFileAccessError, match="expired or was already used",
        ):
            await connector.call("grist_import_csv", args)
        client.add_records.assert_called_once()


def table_server(client, records):
    """get_records answers from ``records`` ({id: fields}) with Grist's exact-value filter."""
    def get_records(doc_id, table_id, *, filters=None, sort="", limit=100, after_id=0):
        (column, wanted), = filters.items()
        found = [
            GristRecord(rec_id, dict(fields)) for rec_id, fields in records.items()
            if fields.get(column) in wanted
        ]
        return GristRecordPage(records=found[:limit], truncated=len(found) > limit)

    client.get_records.side_effect = get_records


class TestUpdateCsv:
    ARGS = {**CSV_ARGS, "key_column": "Name"}

    async def test_changes_only_the_cells_that_differ(self, tmp_path, gated_call_spy):
        text = "Name,Score,Born\nAda,37,1815-12-10\nGrace,50,\nLin,1,\nNew,9,\n"
        connector, client, path = csv_connector(tmp_path, text)
        table_server(client, {
            1: {"Name": "Ada", "Score": 36, "Born": 5},
            2: {"Name": "Grace", "Score": 50.0},
            3: {"Name": "Lin", "Score": 2},
        })

        result = await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})

        assert result == {
            "doc_id": DOC_ID, "table_id": "Contacts", "updated": 2, "added": 0,
            "unchanged": 1, "skipped": 1,
        }
        updates = [c.args[2] for c in client.update_records.call_args_list]
        assert updates == [[(1, {"Score": 37, "Born": calendar_seconds("1815-12-10")}), (3, {"Score": 1})]]
        client.add_records.assert_not_called()

    async def test_the_card_shows_counts_changes_and_unmatched_rows(self, tmp_path, gated_call_spy):
        text = "Name,Score\nAda,37\nGrace,50\nNew,9\n"
        connector, client, path = csv_connector(tmp_path, text)
        table_server(client, {1: {"Name": "Ada", "Score": 36}, 2: {"Name": "Grace", "Score": 50}})

        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})

        kwargs = gated_call_spy[0]
        assert kwargs["tool"] == "grist_update_csv" and kwargs["gate"] == "popup"
        assert kwargs["tool_name"] == "Update Grist from CSV"
        assert kwargs["summary"] == "Update 1 and add 0 record(s) from people.csv in Budget / Contacts"
        assert kwargs["preview"] == {
            "Server": "docs.getgrist.com", "Document": "Budget", "Table": "Contacts",
            "File": "people.csv", "SHA-256": sha_of(text), "Rows in file": "3",
            "Key column": "Name", "Records to update": "1", "Cells to change": "1",
            "Unchanged rows": "1", "Rows with no match": "1 (skipped)",
        }
        assert list(kwargs["preview"])[:6] == [
            "Server", "Document", "Table", "File", "SHA-256", "Rows in file",
        ]
        assert kwargs["preview_tables"] == [
            {
                "caption": "Changes", "headers": ["Record", "Key", "Column", "Current", "New"],
                "rows": [["#1", "Ada", "Score", "36", "37"]],
                "footer": "Showing 1 of 1 changed cells.",
            },
            {
                "caption": "Rows with no match", "headers": ["Line", "Name"],
                "rows": [["4", "New"]], "footer": "Showing 1 of 1 rows.",
            },
        ]
        assert kwargs["details_text"] == "\n".join(f"{k}: {v}" for k, v in kwargs["preview"].items())
        assert kwargs["raw_data"] == {
            "doc_id": DOC_ID, "table_id": "Contacts", "sha256": sha_of(text), "rows": 3,
            "columns": ["Name", "Score"],
        }
        assert kwargs["write_content_scan_text"] == text
        assert kwargs["args"] == {
            "doc_id": DOC_ID, "table_id": "Contacts", "local_path": path, "upload_id": "",
            "sha256": sha_of(text), "key_column": "Name", "create_missing": False,
        }

    async def test_dates_render_as_in_the_file_in_both_columns(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name,Born\nAda,2024-02-29\n")
        table_server(client, {1: {"Name": "Ada", "Born": calendar_seconds("2024-01-31")}})
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        assert gated_call_spy[0]["preview_tables"][0]["rows"] == [
            ["#1", "Ada", "Born", "2024-01-31", "2024-02-29"],
        ]

    async def test_the_changes_sample_is_capped(self, tmp_path, gated_call_spy):
        text = "Name,Score\n" + "".join(f"n{i},2\n" for i in range(30))
        connector, client, path = csv_connector(tmp_path, text)
        table_server(client, {i: {"Name": f"n{i}", "Score": 1} for i in range(30)})
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        table = gated_call_spy[0]["preview_tables"][0]
        assert len(table["rows"]) == 20 and table["footer"] == "Showing 20 of 30 changed cells."

    async def test_the_unmatched_sample_is_capped(self, tmp_path, gated_call_spy):
        text = "Name\n" + "".join(f"n{i}\n" for i in range(25))
        connector, client, path = csv_connector(tmp_path, text)
        table_server(client, {})
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        table = gated_call_spy[0]["preview_tables"][1]
        assert len(table["rows"]) == 20 and table["footer"] == "Showing 20 of 25 rows."

    async def test_create_missing_adds_after_the_updates(self, tmp_path, gated_call_spy):
        text = "Name,Score\nAda,37\nNew,9\n"
        connector, client, path = csv_connector(tmp_path, text)
        table_server(client, {1: {"Name": "Ada", "Score": 36}})
        order = []
        client.update_records.side_effect = lambda *a: order.append("update")
        client.add_records.side_effect = lambda *a: order.append("add") or [5]

        result = await connector.call(
            "grist_update_csv", {**self.ARGS, "local_path": path, "create_missing": True},
        )

        assert order == ["update", "add"]
        assert result == {
            "doc_id": DOC_ID, "table_id": "Contacts", "updated": 1, "added": 1,
            "unchanged": 0, "skipped": 0,
        }
        client.add_records.assert_called_once_with(DOC_ID, "Contacts", [{"Name": "New", "Score": 9}])
        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Rows with no match"] == "1 (will be added)"
        assert kwargs["args"]["create_missing"] is True
        assert kwargs["summary"].startswith("Update 1 and add 1 record(s)")

    async def test_an_ambiguous_key_is_refused(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name\nAda\n")
        table_server(client, {1: {"Name": "Ada"}, 2: {"Name": "Ada"}})
        with pytest.raises(ValueError, match="matches more than one record in Contacts"):
            await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        assert gated_call_spy == []

    async def test_a_repeated_key_inside_the_page_is_refused(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name\nAda\nGrace\n")
        table_server(client, {1: {"Name": "Ada"}, 2: {"Name": "Ada"}, 3: {"Name": "Grace"}})
        with pytest.raises(ValueError, match="matches more than one record in Contacts"):
            await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})

    async def test_a_duplicate_hidden_by_the_page_limit_is_still_refused(self, tmp_path, gated_call_spy):
        # ids order a, b, a: the page cut at two records shows no repeated key, only truncated
        connector, client, path = csv_connector(tmp_path, "Name\nAda\nGrace\n")
        table_server(client, {1: {"Name": "Ada"}, 2: {"Name": "Grace"}, 3: {"Name": "Ada"}})
        with pytest.raises(ValueError, match="matches more than one record in Contacts"):
            await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        assert gated_call_spy == []

    async def test_integral_numeric_keys_match_whatever_their_form(self, tmp_path, gated_call_spy):
        connector, client = write_connector()
        client.list_columns.return_value = [
            GristColumn("Score", "Score", "Numeric", False), GristColumn("Name", "Name", "Text", False),
        ]
        path = tmp_path / "k.csv"
        path.write_text("Score,Name\n3,Ada\n", encoding="utf-8")
        table_server(client, {1: {"Score": 3.0, "Name": "old"}})
        await connector.call(
            "grist_update_csv", {**CSV_ARGS, "key_column": "Score", "local_path": str(path)},
        )
        client.update_records.assert_called_once_with(DOC_ID, "Contacts", [(1, {"Name": "Ada"})])

    async def test_a_no_op_still_reaches_the_gate(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name,Score\nAda,36\n")
        table_server(client, {1: {"Name": "Ada", "Score": 36}})

        result = await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})

        assert len(gated_call_spy) == 1
        assert gated_call_spy[0]["summary"].startswith("Update 0 and add 0 record(s)")
        assert gated_call_spy[0]["preview_tables"][0]["rows"] == []
        assert len(gated_call_spy[0]["preview_tables"]) == 1
        assert result["updated"] == 0 and result["unchanged"] == 1
        client.update_records.assert_not_called()
        client.add_records.assert_not_called()

    async def test_the_approval_is_bound_to_the_file_and_the_options(self, tmp_path, gated_call_spy):
        from privacyfence.approvals import canonical_key

        connector, client, path = csv_connector(tmp_path, "Name\nAda\n")
        table_server(client, {})
        client.add_records.return_value = [1]
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path, "create_missing": True})
        (tmp_path / "people.csv").write_text("Name\nGrace\n", encoding="utf-8")
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})

        keys = [canonical_key("grist", "grist_update_csv", c["args"]) for c in gated_call_spy]
        assert len(set(keys)) == 3

    async def test_the_key_column_must_be_in_the_file(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Score\n1\n")
        with pytest.raises(ValueError, match="key_column Name is not a column of the CSV file"):
            await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})

    async def test_the_key_column_needs_a_key_type(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Born\n2024-01-01\n")
        with pytest.raises(ValueError, match="key_column Born has type Date; use a Text, Choice"):
            await connector.call(
                "grist_update_csv", {**CSV_ARGS, "key_column": "Born", "local_path": path},
            )

    async def test_a_duplicate_key_in_the_file_is_refused(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name\nAda\nAda\n")
        with pytest.raises(ValueError, match="Lines 2, 3 have the same key"):
            await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        client.get_records.assert_not_called()

    async def test_lookups_are_chunked_by_count(self, tmp_path, gated_call_spy, monkeypatch):
        monkeypatch.setattr(grist_module, "_CSV_LOOKUP_KEYS", 3)
        connector, client, path = csv_connector(tmp_path, "Name\n" + "".join(f"n{i}\n" for i in range(7)))
        table_server(client, {})
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        calls = client.get_records.call_args_list
        assert [c.kwargs["filters"]["Name"] for c in calls] == [
            ["n0", "n1", "n2"], ["n3", "n4", "n5"], ["n6"],
        ]
        assert [c.kwargs["limit"] for c in calls] == [3, 3, 1]

    async def test_lookups_are_chunked_by_size(self, tmp_path, gated_call_spy, monkeypatch):
        monkeypatch.setattr(grist_module, "_CSV_LOOKUP_JSON_CHARS", 40)
        connector, client, path = csv_connector(tmp_path, "Name\n" + "".join(f"name-{i}\n" for i in range(8)))
        table_server(client, {})
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        calls = client.get_records.call_args_list
        assert len(calls) > 1
        assert sum(len(c.kwargs["filters"]["Name"]) for c in calls) == 8
        for call in calls:
            assert len(json.dumps({"Name": call.kwargs["filters"]["Name"]})) <= 40

    async def test_updates_are_chunked(self, tmp_path, gated_call_spy, monkeypatch):
        monkeypatch.setattr(grist_module, "_CSV_CHUNK_ROWS", 2)
        connector, client, path = csv_connector(
            tmp_path, "Name,Score\n" + "".join(f"n{i},2\n" for i in range(5)),
        )
        table_server(client, {i: {"Name": f"n{i - 1}", "Score": 1} for i in range(1, 6)})
        await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        assert [len(c.args[2]) for c in client.update_records.call_args_list] == [2, 2, 1]

    async def test_a_failed_update_says_how_far_it_got(self, tmp_path, gated_call_spy, monkeypatch):
        from privacyfence import safe_errors

        monkeypatch.setattr(grist_module, "_CSV_CHUNK_ROWS", 2)
        text = "Name,Score\n" + "".join(f"n{i},2\n" for i in range(5)) + "extra,1\n"
        connector, client, path = csv_connector(tmp_path, text)
        table_server(client, {i: {"Name": f"n{i - 1}", "Score": 1} for i in range(1, 6)})
        client.update_records.side_effect = [None, GristClientError("SECRET grist text")]

        with pytest.raises(grist_module.GristCsvWriteError) as info:
            await connector.call(
                "grist_update_csv", {**self.ARGS, "local_path": path, "create_missing": True},
            )

        assert str(info.value) == (
            "Grist refused a write after 2 of 5 update(s) and 0 of 1 add(s); see the PrivacyFence "
            "log for Grist's reason. Running grist_update_csv again with the same file finishes "
            "the rest."
        )
        assert safe_errors.public_message(info.value) == str(info.value)
        client.add_records.assert_not_called()

    async def test_a_failed_add_counts_the_finished_updates(self, tmp_path, gated_call_spy):
        connector, client, path = csv_connector(tmp_path, "Name,Score\nAda,2\nNew,1\n")
        table_server(client, {1: {"Name": "Ada", "Score": 1}})
        client.add_records.side_effect = GristClientError("boom")
        with pytest.raises(grist_module.GristCsvWriteError, match="1 of 1 update.*0 of 1 add"):
            await connector.call(
                "grist_update_csv", {**self.ARGS, "local_path": path, "create_missing": True},
            )

    async def test_nothing_is_written_when_the_gate_denies(self, tmp_path, raising_gated_call):
        connector, client, path = csv_connector(tmp_path, "Name,Score\nAda,2\n")
        table_server(client, {1: {"Name": "Ada", "Score": 1}})
        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("grist_update_csv", {**self.ARGS, "local_path": path})
        client.update_records.assert_not_called()

    async def test_upload_id_survives_a_pending_approval(self, monkeypatch):
        from privacyfence import local_files
        from privacyfence.approvals import ApprovalPending

        decisions = iter(["pending", "approved"])

        async def fake_gated_call(**kwargs):
            if next(decisions) == "pending":
                raise ApprovalPending({"status": "approval_pending", "approval_id": "a1"})
            return kwargs["filtered_data"]

        monkeypatch.setattr(grist_module, "gated_call", fake_gated_call)
        connector, client = write_connector()
        client.list_columns.return_value = CSV_COLUMNS
        table_server(client, {1: {"Name": "Ada", "Score": 1}})
        store, token, upload_id, principal = new_slot(b"Name,Score\nAda,2\n")
        args = {**self.ARGS, "upload_id": upload_id}

        with local_files.call_context(bridge_available=False, uploads={}), pytest.raises(ApprovalPending):
            await connector.call("grist_update_csv", args)
        client.update_records.assert_not_called()
        assert store.peek(token, principal.id) is not None

        with local_files.call_context(bridge_available=False, uploads={}):
            result = await connector.call("grist_update_csv", args)

        assert result["updated"] == 1
        client.update_records.assert_called_once_with(DOC_ID, "Contacts", [(1, {"Score": 2})])
        assert store.peek(token, principal.id) is None

    async def test_an_upload_id_backs_one_approved_write_even_for_a_no_op(self, gated_call_spy):
        from privacyfence import local_files
        from privacyfence.local_files import LocalFileAccessError

        connector, client = write_connector()
        client.list_columns.return_value = CSV_COLUMNS
        table_server(client, {1: {"Name": "Ada", "Score": 2}})
        store, token, upload_id, principal = new_slot(b"Name,Score\nAda,2\n")
        args = {**self.ARGS, "upload_id": upload_id}

        with local_files.call_context(bridge_available=False, uploads={}):
            await connector.call("grist_update_csv", args)
        assert store.peek(token, principal.id) is None
        with local_files.call_context(bridge_available=False, uploads={}), pytest.raises(
            LocalFileAccessError, match="expired or was already used",
        ):
            await connector.call("grist_update_csv", args)
