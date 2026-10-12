"""Tests for the Jira Assets connector: auto-approved schema tools and reviewed object reads."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from privacyfence import auto_accept
from privacyfence.assets_client import (
    AssetsAttributeDef,
    AssetsClient,
    AssetsClientError,
    AssetsObject,
    AssetsObjectType,
    AssetsReference,
    AssetsSchema,
)
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import assets as assets_module
from privacyfence.connectors.assets import AssetsConnector

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_no_placeholder_fields,
    assert_tool_definitions_complete,
)


def make_connector():
    client = MagicMock()
    return AssetsConnector(client), client


def audit_lines(tmp_path):
    f = tmp_path / f"{current_week()}.jsonl"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]


def attr(**overrides):
    defaults = dict(
        id="1", name="Name", kind="default", value_type="Text", reference_type_id="",
        reference_type_name="", reference_kind="", min_cardinality=1, max_cardinality=1,
        is_label=True, is_system=False,
    )
    defaults.update(overrides)
    return AssetsAttributeDef(**defaults)


@pytest.fixture
def gated_call_never(monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("auto tools must not reach gated_call")

    monkeypatch.setattr(assets_module, "gated_call", boom, raising=False)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(assets_module, "gated_call", fake_gated_call)
    return calls


@pytest.fixture
def denying_gate(monkeypatch):
    calls: list[dict[str, Any]] = []

    async def deny(**kwargs):
        calls.append(kwargs)
        raise PermissionError("denied by user")

    monkeypatch.setattr(assets_module, "gated_call", deny)
    return calls


def make_object(**overrides):
    defaults = dict(
        id="123", key="PFQA-123", label="ACME support contract", type_id="7",
        type_name="Contract", schema_id="2", created="2026-01-05", updated="2026-02-01",
        attributes={"Name": ["ACME support contract"], "Tags": ["a", "b"],
                    "Account": ["ACME Ltd (PFQA-45)"]},
        references={"Account": [AssetsReference("45", "PFQA-45", "ACME Ltd")]},
    )
    defaults.update(overrides)
    return AssetsObject(**defaults)


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Assets tool"):
            await connector.call("assets_does_not_exist", {})

    def test_name_client_and_specs_work_without_a_client(self):
        connector, client = make_connector()
        assert connector.name == "assets"
        assert connector.client is client
        names = [s.name for s in AssetsConnector(None).tool_specs()]  # type: ignore[arg-type]
        assert names == [
            "assets_list_schemas", "assets_list_object_types", "assets_list_attributes",
            "assets_search_objects", "assets_get_object",
        ]

    def test_gates_and_operations(self):
        auto = {"assets_list_schemas", "assets_list_object_types", "assets_list_attributes"}
        for spec in AssetsConnector(None).tool_specs():  # type: ignore[arg-type]
            assert spec.read_only
            if spec.name in auto:
                assert auto_accept.TOOL_TO_GATE[spec.name] == "auto"
            else:
                assert auto_accept.TOOL_TO_GATE[spec.name] == "review"
                assert auto_accept.TOOL_TO_OPERATION[spec.name] == "assets.read_object"


class TestListSchemas:
    async def test_shape_and_audit(self, gated_call_never, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_schemas.return_value = [AssetsSchema("2", "QA", "PFQA", "d", 7, 3)]

        result = await connector.call("assets_list_schemas", {})

        assert result == [{"id": "2", "name": "QA", "key": "PFQA", "description": "d",
                           "object_count": 7, "object_type_count": 3}]
        client.list_schemas.assert_called_once_with(50)
        entry = audit_lines(tmp_path)[-1]
        assert entry["tool"] == "assets_list_schemas"
        assert entry["decision"] == "auto_accepted"

    @pytest.mark.parametrize("given,expected", [(0, 1), (-5, 1), (500, 100), (25, 25)])
    async def test_max_results_is_clamped(self, gated_call_never, tmp_path, given, expected):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_schemas.return_value = []

        await connector.call("assets_list_schemas", {"max_results": given})

        client.list_schemas.assert_called_once_with(expected)

    async def test_client_error_becomes_runtime_error(self, gated_call_never, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_schemas.side_effect = AssetsClientError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            await connector.call("assets_list_schemas", {})


class TestListObjectTypes:
    async def test_shape_and_audit(self, gated_call_never, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_object_types.return_value = [AssetsObjectType("7", "Contract", "2", "", "c", 4)]

        result = await connector.call("assets_list_object_types", {"schema_id": "2"})

        assert result == [{"id": "7", "name": "Contract", "parent_id": "", "description": "c",
                           "object_count": 4}]
        client.list_object_types.assert_called_once_with("2")
        assert audit_lines(tmp_path)[-1]["decision"] == "auto_accepted"

    async def test_client_error_becomes_runtime_error(self, gated_call_never, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_object_types.side_effect = AssetsClientError("bad id")
        with pytest.raises(RuntimeError, match="bad id"):
            await connector.call("assets_list_object_types", {"schema_id": "x"})


class TestListAttributes:
    async def test_system_attributes_are_left_out(self, gated_call_never, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_attributes.return_value = [
            attr(id="1", name="Name"),
            attr(id="2", name="Account", kind="reference", reference_type_id="9",
                 reference_type_name="Account", reference_kind="Reference", is_label=False),
            attr(id="3", name="Created", is_system=True, is_label=False),
        ]

        result = await connector.call("assets_list_attributes", {"object_type_id": "7"})

        assert [a["name"] for a in result] == ["Name", "Account"]
        assert "is_system" not in result[0]
        assert result[1]["reference_type_name"] == "Account"
        assert result[0]["is_label"] is True
        assert audit_lines(tmp_path)[-1]["tool"] == "assets_list_attributes"

    async def test_client_error_becomes_runtime_error(self, gated_call_never, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_attributes.side_effect = AssetsClientError("nope")
        with pytest.raises(RuntimeError, match="nope"):
            await connector.call("assets_list_attributes", {"object_type_id": "7"})


class TestSearchObjects:
    async def test_approved_search_returns_dicts_and_builds_the_card(self, gated_call_spy):
        connector, client = make_connector()
        connector.my_email = "me@example.com"
        client.search_objects.return_value = [make_object(), make_object(id="124", key="PFQA-124",
                                                                          label="Other", attributes={"Name": ["Other"]})]

        result = await connector.call("assets_search_objects", {"aql": 'objectType = "Contract"'})

        client.search_objects.assert_called_once_with('objectType = "Contract"', 25)
        assert [r["key"] for r in result] == ["PFQA-123", "PFQA-124"]
        kw = gated_call_spy[0]
        assert kw["gate"] == "review"
        assert kw["tool"] == "assets_search_objects"
        assert kw["tool_name"] == "Search Jira Assets"
        assert kw["sender"] == "Jira Assets"
        assert kw["summary"] == 'AQL: objectType = "Contract"'
        assert kw["raw_data"] == client.search_objects.return_value
        assert kw["table_only"] is True
        assert kw["my_email"] == "me@example.com"
        assert kw["args"] == {"aql": 'objectType = "Contract"', "max_results": 25}
        assert kw["preview"] == {"Query": 'objectType = "Contract"', "Objects": "2"}
        table = kw["preview_tables"][0]
        assert table["headers"] == ["Key", "Label", "Type", "Name", "Tags", "Account"]
        assert table["rows"][0] == ["PFQA-123", "ACME support contract", "Contract",
                                    "ACME support contract", "a; b", "ACME Ltd (PFQA-45)"]
        assert table["rows"][1][4] == ""
        assert "PFQA-123 — ACME support contract: Name: ACME support contract; Tags: a; b" in kw["details_text"]

    async def test_preview_holds_only_metadata_keys(self, gated_call_spy):
        connector, client = make_connector()
        client.search_objects.return_value = [make_object()]
        await connector.call("assets_search_objects", {"aql": "x" * 300})
        preview = gated_call_spy[0]["preview"]
        assert set(preview) == {"Query", "Objects"}
        assert len(preview["Query"]) == 200

    async def test_pii_scan_text_holds_the_attribute_values(self, gated_call_spy):
        connector, client = make_connector()
        client.search_objects.return_value = [make_object()]
        await connector.call("assets_search_objects", {"aql": "q"})
        scan = gated_call_spy[0]["pii_scan_text"]
        assert scan.split("\n") == ["ACME support contract", "a", "b", "ACME Ltd (PFQA-45)"]

    async def test_columns_and_cells_are_capped(self, gated_call_spy):
        connector, client = make_connector()
        attrs = {f"A{i}": ["v"] for i in range(12)}
        attrs["A0"] = ["x" * 300]
        client.search_objects.return_value = [make_object(attributes=attrs)]
        await connector.call("assets_search_objects", {"aql": "q"})
        table = gated_call_spy[0]["preview_tables"][0]
        assert len(table["headers"]) == 3 + 8
        assert len(table["rows"][0][3]) == 120
        assert table["rows"][0][3].endswith("…")

    async def test_empty_search_still_shows_a_card(self, gated_call_spy):
        connector, client = make_connector()
        client.search_objects.return_value = []
        result = await connector.call("assets_search_objects", {"aql": "q"})
        assert result == []
        kw = gated_call_spy[0]
        assert kw["preview_tables"] == []
        assert kw["details_text"] == "(no matches)"
        assert kw["preview"]["Objects"] == "0"

    @pytest.mark.parametrize("given,expected", [(0, 1), (500, 100), (40, 40)])
    async def test_max_results_is_clamped(self, gated_call_spy, given, expected):
        connector, client = make_connector()
        client.search_objects.return_value = []
        await connector.call("assets_search_objects", {"aql": "q", "max_results": given})
        client.search_objects.assert_called_once_with("q", expected)

    async def test_denied_search_propagates(self, denying_gate):
        connector, client = make_connector()
        client.search_objects.return_value = [make_object()]
        with pytest.raises(PermissionError):
            await connector.call("assets_search_objects", {"aql": "q"})
        client.search_objects.assert_called_once()
        assert denying_gate[0]["gate"] == "review"

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.search_objects.side_effect = AssetsClientError("aql must be 1-4000 characters")
        with pytest.raises(RuntimeError, match="aql must be"):
            await connector.call("assets_search_objects", {"aql": ""})
        assert gated_call_spy == []


class TestGetObject:
    async def test_approved_read_returns_the_object_dict(self, gated_call_spy):
        connector, client = make_connector()
        client.get_object.return_value = make_object()

        result = await connector.call("assets_get_object", {"object_id": "123"})

        client.get_object.assert_called_once_with("123")
        assert result == make_object().to_dict()
        kw = gated_call_spy[0]
        assert kw["gate"] == "review"
        assert kw["tool_name"] == "Read Jira Assets Object"
        assert kw["summary"] == "PFQA-123: ACME support contract"
        assert kw["sender"] == "Jira Assets"
        assert kw["raw_data"] is client.get_object.return_value
        assert kw["args"] == {"object_id": "123"}
        assert kw["preview"] == {"Key": "PFQA-123", "Label": "ACME support contract",
                                 "Type": "Contract", "Schema": "2"}
        assert kw["preview_tables"] == [{
            "headers": ["Attribute", "Value"],
            "rows": [["Name", "ACME support contract"], ["Tags", "a; b"],
                     ["Account", "ACME Ltd (PFQA-45)"]],
        }]
        assert kw["pii_scan_text"].split("\n") == [
            "ACME support contract", "a", "b", "ACME Ltd (PFQA-45)"]

    async def test_object_without_attributes_has_no_table(self, gated_call_spy):
        connector, client = make_connector()
        client.get_object.return_value = make_object(attributes={}, references={})
        await connector.call("assets_get_object", {"object_id": "123"})
        assert gated_call_spy[0]["preview_tables"] == []

    async def test_denied_read_propagates(self, denying_gate):
        connector, client = make_connector()
        client.get_object.return_value = make_object()
        with pytest.raises(PermissionError):
            await connector.call("assets_get_object", {"object_id": "123"})
        client.get_object.assert_called_once_with("123")

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_object.side_effect = AssetsClientError("object id must be a numeric Assets id")
        with pytest.raises(RuntimeError, match="numeric Assets id"):
            await connector.call("assets_get_object", {"object_id": "x"})
        assert gated_call_spy == []


FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "assets"


class TestFieldCompleteness:
    """Fixture JSON -> the real AssetsClient.get_object parser -> the real card building."""

    async def test_get_object_preview_has_no_placeholder_fields(self, gated_call_spy, monkeypatch):
        raw = json.loads((FIXTURES / "object_contract.json").read_text(encoding="utf-8"))
        attrs = json.loads((FIXTURES / "object_contract_attributes.json").read_text(encoding="utf-8"))
        client = AssetsClient({
            "access_token": "tok", "refresh_token": "rt", "cloud_id": "cloud-1",
            "scopes": ["read:cmdb-object:jira"],
        })
        monkeypatch.setattr(
            client, "_get",
            lambda path, params=None: attrs if path.endswith("/attributes") else raw,
        )

        connector = AssetsConnector(client)
        connector.my_email = "me@example.com"
        await connector.call("assets_get_object", {"object_id": "123"})

        kw = gated_call_spy[0]
        assert_no_placeholder_fields(kw["preview"])
        assert kw["preview"]["Key"] == "PFQA-123"
        assert kw["preview_tables"][0]["rows"], "attribute rows should come from the fixture"


class TestAuditFailure:
    async def test_audit_write_failure_does_not_break_the_call(self, gated_call_never, tmp_path, monkeypatch):
        connector, client = make_connector()
        client.list_schemas.return_value = []

        def broken():
            raise OSError("disk")

        monkeypatch.setattr(assets_module, "get_audit_logger", broken)
        assert await connector.call("assets_list_schemas", {}) == []


ASSETS_SIBLINGS: dict[str, tuple[str, ...]] = {
    "assets_list_schemas": ("assets_list_object_types",),
    "assets_list_object_types": ("assets_list_schemas", "assets_list_attributes"),
    "assets_list_attributes": ("assets_list_object_types", "assets_search_objects"),
    "assets_search_objects": ("assets_get_object",),
    "assets_get_object": ("assets_search_objects",),
}


class TestToolDefinitions:
    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(AssetsConnector(MagicMock()), ASSETS_SIBLINGS)


class TestAuditTrail:
    async def test_every_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.list_schemas.return_value = []
        client.list_object_types.return_value = []
        client.list_attributes.return_value = []
        client.search_objects.return_value = []
        client.get_object.return_value = make_object()
        await assert_all_tools_leave_an_audit_trail(connector, assets_module, monkeypatch, tmp_path)
