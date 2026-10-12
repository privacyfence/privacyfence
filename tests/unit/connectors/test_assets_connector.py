"""Tests for the Jira Assets connector's auto-approved schema tools."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from privacyfence import auto_accept
from privacyfence.assets_client import (
    AssetsAttributeDef,
    AssetsClientError,
    AssetsObjectType,
    AssetsSchema,
)
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import assets as assets_module
from privacyfence.connectors.assets import AssetsConnector

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_tool_definitions_complete


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


@pytest.fixture(autouse=True)
def gated_call_never(monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("auto tools must not reach gated_call")

    monkeypatch.setattr(assets_module, "gated_call", boom, raising=False)


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
        assert names == ["assets_list_schemas", "assets_list_object_types", "assets_list_attributes"]

    def test_every_tool_is_auto_gated(self):
        for spec in AssetsConnector(None).tool_specs():  # type: ignore[arg-type]
            assert auto_accept.TOOL_TO_GATE[spec.name] == "auto"
            assert spec.read_only


class TestListSchemas:
    async def test_shape_and_audit(self, tmp_path):
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
    async def test_max_results_is_clamped(self, tmp_path, given, expected):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_schemas.return_value = []

        await connector.call("assets_list_schemas", {"max_results": given})

        client.list_schemas.assert_called_once_with(expected)

    async def test_client_error_becomes_runtime_error(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_schemas.side_effect = AssetsClientError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            await connector.call("assets_list_schemas", {})


class TestListObjectTypes:
    async def test_shape_and_audit(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_object_types.return_value = [AssetsObjectType("7", "Contract", "2", "", "c", 4)]

        result = await connector.call("assets_list_object_types", {"schema_id": "2"})

        assert result == [{"id": "7", "name": "Contract", "parent_id": "", "description": "c",
                           "object_count": 4}]
        client.list_object_types.assert_called_once_with("2")
        assert audit_lines(tmp_path)[-1]["decision"] == "auto_accepted"

    async def test_client_error_becomes_runtime_error(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_object_types.side_effect = AssetsClientError("bad id")
        with pytest.raises(RuntimeError, match="bad id"):
            await connector.call("assets_list_object_types", {"schema_id": "x"})


class TestListAttributes:
    async def test_system_attributes_are_left_out(self, tmp_path):
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

    async def test_client_error_becomes_runtime_error(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_attributes.side_effect = AssetsClientError("nope")
        with pytest.raises(RuntimeError, match="nope"):
            await connector.call("assets_list_attributes", {"object_type_id": "7"})


class TestAuditFailure:
    async def test_audit_write_failure_does_not_break_the_call(self, tmp_path, monkeypatch):
        connector, client = make_connector()
        client.list_schemas.return_value = []

        def broken():
            raise OSError("disk")

        monkeypatch.setattr(assets_module, "get_audit_logger", broken)
        assert await connector.call("assets_list_schemas", {}) == []


ASSETS_SIBLINGS: dict[str, tuple[str, ...]] = {
    "assets_list_schemas": ("assets_list_object_types",),
    "assets_list_object_types": ("assets_list_schemas", "assets_list_attributes"),
    "assets_list_attributes": ("assets_list_object_types",),
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
        await assert_all_tools_leave_an_audit_trail(connector, assets_module, monkeypatch, tmp_path)
