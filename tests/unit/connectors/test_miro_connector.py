"""Unit tests for privacyfence.connectors.miro.MiroConnector's read tools.

Invariant: the three listing tools never reach the approval gate, each leaves an audit entry, and
none of them hands the model more than the board's name, team, dates and link (no description, no
owner); the three item and connector reads always go through a review card whose preview shows no
item text."""
from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import miro as miro_module
from privacyfence.connectors.miro import MiroConnector
from privacyfence.miro_client import (
    MiroBoard,
    MiroClient,
    MiroClientError,
    MiroConnectorLine,
    MiroItem,
    MiroNotFound,
    MiroTag,
)

from ...helpers import (
    assert_all_tools_leave_an_audit_trail,
    assert_no_placeholder_fields,
    assert_tool_definitions_complete,
)

pytestmark = pytest.mark.unit

BOARD_ID = "uXjVOD6LSME="
BOARD = MiroBoard(
    id=BOARD_ID, name="QA board", description="secret notes", team="QA team",
    created_at="2026-01-01T00:00:00Z", modified_at="2026-02-02T00:00:00Z",
    view_link="https://miro.com/app/board/uXjVOD6LSME=/",
)


ITEM_ID = "3458764549483493025"
ITEM_ID_2 = "3458764549483493026"
FRAME_ID = "3458764549483493030"
STICKY = MiroItem(
    ITEM_ID, "sticky_note", "<p>[QATEST]</p>", "", "", "", "square", "yellow",
    0.0, 0.0, 199.0, 228.0, "", "",
)
LINE = MiroConnectorLine(
    "3458764549483493027", ITEM_ID, ITEM_ID_2, "curved", ["[QATEST]"],
)


def make_connector():
    client = MagicMock()
    return MiroConnector(client), client


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(miro_module, "gated_call", fake_gated_call, raising=False)
    return calls


def audit_lines(tmp_path):
    return (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Miro tool: 'miro_does_not_exist'"):
            await connector.call("miro_does_not_exist", {})

    def test_the_client_property_returns_the_client(self):
        connector, client = make_connector()
        assert connector.client is client
        assert connector.name == "miro"


class TestListBoards:
    async def test_returns_boards_without_a_card(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_boards.return_value = [BOARD]

        result = await connector.call("miro_list_boards", {"query": "QA", "max_results": 10})

        assert result == [{
            "id": BOARD_ID, "name": "QA board", "team": "QA team",
            "modified_at": "2026-02-02T00:00:00Z", "view_link": BOARD.view_link,
        }]
        client.list_boards.assert_called_once_with("QA", 10)
        assert gated_call_spy == []
        entries = audit_lines(tmp_path)
        assert '"decision": "auto_accepted"' in entries[0]
        assert '"tool": "miro_list_boards"' in entries[0]

    async def test_the_result_is_cut_to_max_results(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_boards.return_value = [BOARD, BOARD, BOARD]

        result = await connector.call("miro_list_boards", {"max_results": 2})

        assert len(result) == 2

    @pytest.mark.parametrize("value", [0, 201, -1, True, "5"])
    async def test_max_results_out_of_range_is_refused(self, value):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="max_results must be between 1 and 200."):
            await connector.call("miro_list_boards", {"max_results": value})
        client.list_boards.assert_not_called()

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_boards.side_effect = MiroClientError("Could not reach Miro: ConnectionError")
        with pytest.raises(RuntimeError, match="Could not reach Miro"):
            await connector.call("miro_list_boards", {})


class TestGetBoard:
    async def test_returns_the_board_without_description_or_owner(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_board.return_value = BOARD

        result = await connector.call("miro_get_board", {"board_id": f" {BOARD_ID} "})

        assert result == {
            "id": BOARD_ID, "name": "QA board", "team": "QA team",
            "created_at": "2026-01-01T00:00:00Z", "modified_at": "2026-02-02T00:00:00Z",
            "view_link": BOARD.view_link,
        }
        client.get_board.assert_called_once_with(BOARD_ID)
        assert gated_call_spy == []
        entries = audit_lines(tmp_path)
        assert '"tool": "miro_get_board"' in entries[0]
        assert "secret notes" not in entries[0]

    async def test_bad_board_id_is_refused_before_the_client_is_called(self):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Miro board id"):
            await connector.call("miro_get_board", {"board_id": "../x"})
        client.get_board.assert_not_called()

    async def test_a_board_link_is_refused(self):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="/app/board/"):
            await connector.call(
                "miro_get_board", {"board_id": "https://miro.com/app/board/uXjVOD6LSME=/"},
            )
        client.get_board.assert_not_called()

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_board.side_effect = MiroClientError("Miro found no such board or item (HTTP 404).")
        with pytest.raises(RuntimeError, match="HTTP 404"):
            await connector.call("miro_get_board", {"board_id": BOARD_ID})


class TestListTags:
    async def test_returns_tags_without_a_card(self, tmp_path, gated_call_spy):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_tags.return_value = [MiroTag(id="3458764549483493025", title="QATEST", color="red")]

        result = await connector.call("miro_list_tags", {"board_id": BOARD_ID})

        assert result == [{"id": "3458764549483493025", "title": "QATEST", "color": "red"}]
        client.list_tags.assert_called_once_with(BOARD_ID)
        assert gated_call_spy == []
        entries = audit_lines(tmp_path)
        assert '"tool": "miro_list_tags"' in entries[0]

    async def test_bad_board_id_is_refused_before_the_client_is_called(self):
        connector, client = make_connector()
        with pytest.raises(ValueError, match="Not a Miro board id"):
            await connector.call("miro_list_tags", {"board_id": "a b"})
        client.list_tags.assert_not_called()

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_tags.side_effect = MiroClientError("Miro refused the request (HTTP 403).")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            await connector.call("miro_list_tags", {"board_id": BOARD_ID})


class TestAuditFailure:
    async def test_a_failing_audit_log_does_not_fail_the_call(self, monkeypatch):
        connector, client = make_connector()
        client.list_tags.return_value = []

        def broken():
            raise OSError("disk full")

        monkeypatch.setattr(miro_module, "get_audit_logger", broken)

        assert await connector.call("miro_list_tags", {"board_id": BOARD_ID}) == []


class TestListItems:
    def _connector(self, items=(STICKY,), truncated=False, team="QA team"):
        connector, client = make_connector()
        client.team_name = team
        client.get_board.return_value = BOARD
        client.list_items.return_value = (list(items), truncated)
        return connector, client

    async def test_the_card_preview_shows_no_item_text(self, gated_call_spy):
        connector, client = self._connector()

        result = await connector.call(
            "miro_list_items",
            {"board_id": f" {BOARD_ID} ", "type": "sticky_note", "frame_id": FRAME_ID},
        )

        call = gated_call_spy[0]
        assert list(call["preview"]) == ["Board", "Team", "Type", "Frame"]
        assert call["preview"] == {
            "Board": "QA board", "Team": "QA team", "Type": "sticky_note", "Frame": FRAME_ID,
        }
        assert "QATEST" not in str(call["preview"])
        assert call["gate"] == "review"
        assert call["tool"] == "miro_list_items"
        assert call["args"] == {"board_id": BOARD_ID}
        assert call["details_text"] == f"sticky_note #{ITEM_ID}: [QATEST]"
        assert call["pii_scan_text"] == call["details_text"]
        assert call["preview_tables"] == [{
            "headers": ["id", "type", "text", "frame"],
            "rows": [[ITEM_ID, "sticky_note", "[QATEST]", ""]],
        }]
        assert call["table_only"] is True
        assert result == call["filtered_data"] == [asdict(STICKY)]
        client.list_items.assert_called_once_with(
            BOARD_ID, item_type="sticky_note", frame_id=FRAME_ID, max_results=100,
        )

    async def test_whole_board_labels(self, gated_call_spy):
        connector, _client = self._connector()
        await connector.call("miro_list_items", {"board_id": BOARD_ID})
        assert gated_call_spy[0]["preview"]["Type"] == "(all)"
        assert gated_call_spy[0]["preview"]["Frame"] == "(whole board)"

    async def test_tags_are_stripped_and_entities_unescaped(self, gated_call_spy):
        item = MiroItem(
            ITEM_ID, "text", "<p>Fish &amp; <strong>chips</strong></p>", "", "", "", "", "",
            None, None, None, None, "", "",
        )
        connector, _client = self._connector(items=[item])
        await connector.call("miro_list_items", {"board_id": BOARD_ID})
        call = gated_call_spy[0]
        assert call["details_text"] == f"text #{ITEM_ID}: Fish & chips"
        assert call["preview_tables"][0]["rows"][0][2] == "Fish & chips"

    async def test_a_truncated_result_says_so(self, gated_call_spy):
        connector, _client = self._connector(truncated=True)
        await connector.call("miro_list_items", {"board_id": BOARD_ID, "max_results": 1})
        assert gated_call_spy[0]["details_text"].startswith(
            "Showing the first 1 items; the board has more.\n\n"
        )

    async def test_an_empty_result_has_no_table(self, gated_call_spy):
        connector, _client = self._connector(items=[])
        result = await connector.call("miro_list_items", {"board_id": BOARD_ID})
        assert result == []
        assert gated_call_spy[0]["details_text"] == "(no items)"
        assert gated_call_spy[0]["preview_tables"] is None

    async def test_a_missing_team_name_reads_not_shown(self, gated_call_spy):
        connector, _client = self._connector(team="")
        await connector.call("miro_list_items", {"board_id": BOARD_ID})
        assert gated_call_spy[0]["preview"]["Team"] == "(not shown by Miro)"

    @pytest.mark.parametrize("args, message", [
        ({"board_id": "../x"}, "Not a Miro board id"),
        ({"board_id": BOARD_ID, "type": "banana"}, "type must be empty or one of sticky_note"),
        ({"board_id": BOARD_ID, "frame_id": "abc"}, "Not a Miro item id"),
        ({"board_id": BOARD_ID, "max_results": 0}, "max_results must be between 1 and 500."),
        ({"board_id": BOARD_ID, "max_results": 501}, "max_results must be between 1 and 500."),
        ({"board_id": BOARD_ID, "max_results": True}, "max_results must be between 1 and 500."),
    ])
    async def test_validation_runs_before_anything_is_fetched(self, gated_call_spy, args, message):
        connector, client = self._connector()
        with pytest.raises(ValueError, match=message):
            await connector.call("miro_list_items", args)
        assert gated_call_spy == []
        client.get_board.assert_not_called()
        client.list_items.assert_not_called()

    async def test_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = self._connector()
        client.list_items.side_effect = MiroClientError("Miro refused the request (HTTP 403).")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            await connector.call("miro_list_items", {"board_id": BOARD_ID})
        assert gated_call_spy == []


class TestGetItems:
    def _connector(self):
        connector, client = make_connector()
        client.team_name = "QA team"
        client.get_board.return_value = BOARD
        client.get_item.return_value = STICKY
        return connector, client

    async def test_ids_are_parsed_and_each_item_is_read(self, gated_call_spy):
        connector, client = self._connector()

        result = await connector.call(
            "miro_get_items", {"board_id": BOARD_ID, "item_ids": f" {ITEM_ID} , {ITEM_ID_2}"},
        )

        assert [c.args for c in client.get_item.call_args_list] == [
            (BOARD_ID, ITEM_ID), (BOARD_ID, ITEM_ID_2),
        ]
        call = gated_call_spy[0]
        assert call["tool_name"] == "Read Miro Items by Id"
        assert call["summary"] == "Read 2 item(s) from QA board"
        assert list(call["preview"]) == ["Board", "Team"]
        assert call["args"] == {"board_id": BOARD_ID}
        assert call["pii_scan_text"] == call["details_text"]
        assert len(result) == 2

    @pytest.mark.parametrize("value", ["", "12,abc", "1,,2", "x", "١٢٣"])
    async def test_a_bad_id_list_is_refused(self, gated_call_spy, value):
        connector, client = self._connector()
        with pytest.raises(ValueError, match="item_ids must be 1 to 50 Miro item ids"):
            await connector.call("miro_get_items", {"board_id": BOARD_ID, "item_ids": value})
        assert gated_call_spy == []
        client.get_item.assert_not_called()

    async def test_more_than_fifty_ids_are_refused(self, gated_call_spy):
        connector, _client = self._connector()
        ids = ",".join(str(1000 + n) for n in range(51))
        with pytest.raises(ValueError, match="1 to 50"):
            await connector.call("miro_get_items", {"board_id": BOARD_ID, "item_ids": ids})
        assert gated_call_spy == []

    async def test_a_missing_item_is_named_and_nothing_is_gated(self, gated_call_spy):
        connector, client = self._connector()
        client.get_item.side_effect = [STICKY, MiroNotFound("Miro found no such board or item.")]
        with pytest.raises(ValueError, match=f"not found on this board: {ITEM_ID_2}"):
            await connector.call(
                "miro_get_items", {"board_id": BOARD_ID, "item_ids": f"{ITEM_ID},{ITEM_ID_2}"},
            )
        assert gated_call_spy == []

    async def test_another_client_error_becomes_runtime_error(self, gated_call_spy):
        connector, client = self._connector()
        client.get_item.side_effect = MiroClientError("Miro refused the request (HTTP 403).")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            await connector.call(
                "miro_get_items", {"board_id": BOARD_ID, "item_ids": ITEM_ID},
            )
        assert gated_call_spy == []


class TestDisplayHelpers:
    def test_cell_handles_none_numbers_and_long_text(self):
        assert miro_module._cell(None) == ""
        assert miro_module._cell(1.5) == "1.5"
        assert miro_module._cell(True) == "True"
        long = miro_module._cell("x" * 300)
        assert len(long) == 201 and long.endswith("…")

    async def test_item_ids_must_be_a_string(self, gated_call_spy):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="item_ids must be 1 to 50"):
            await connector.call("miro_get_items", {"board_id": BOARD_ID, "item_ids": 5})


class TestListConnectors:
    def _connector(self, lines=(LINE,), truncated=False):
        connector, client = make_connector()
        client.team_name = "QA team"
        client.get_board.return_value = BOARD
        client.list_connectors.return_value = (list(lines), truncated)
        return connector, client

    async def test_table_and_details_lines(self, gated_call_spy):
        connector, client = self._connector()

        result = await connector.call("miro_list_connectors", {"board_id": BOARD_ID})

        call = gated_call_spy[0]
        assert call["tool_name"] == "Read Miro Connectors"
        assert call["summary"] == "Read 1 connector(s) from QA board"
        assert call["gate"] == "review"
        assert call["preview"] == {"Board": "QA board", "Team": "QA team"}
        assert call["details_text"] == f"#{LINE.id}: {ITEM_ID} → {ITEM_ID_2} [QATEST]"
        assert call["pii_scan_text"] == call["details_text"]
        assert call["preview_tables"] == [{
            "headers": ["id", "from", "to", "captions"],
            "rows": [[LINE.id, ITEM_ID, ITEM_ID_2, "[QATEST]"]],
        }]
        assert call["table_only"] is True
        assert result == [asdict(LINE)]
        client.list_connectors.assert_called_once_with(BOARD_ID, max_results=100)

    async def test_a_truncated_result_says_so(self, gated_call_spy):
        connector, _client = self._connector(truncated=True)
        await connector.call("miro_list_connectors", {"board_id": BOARD_ID, "max_results": 1})
        assert gated_call_spy[0]["details_text"].startswith(
            "Showing the first 1 connectors; the board has more.\n\n"
        )

    async def test_no_connectors(self, gated_call_spy):
        connector, _client = self._connector(lines=[])
        assert await connector.call("miro_list_connectors", {"board_id": BOARD_ID}) == []
        assert gated_call_spy[0]["details_text"] == "(no connectors)"
        assert gated_call_spy[0]["preview_tables"] is None

    async def test_validation_and_client_errors(self, gated_call_spy):
        connector, client = self._connector()
        with pytest.raises(ValueError, match="max_results must be between 1 and 500."):
            await connector.call("miro_list_connectors", {"board_id": BOARD_ID, "max_results": 501})
        client.list_connectors.side_effect = MiroClientError("Miro refused the request (HTTP 403).")
        with pytest.raises(RuntimeError, match="HTTP 403"):
            await connector.call("miro_list_connectors", {"board_id": BOARD_ID})
        assert gated_call_spy == []


class TestFieldCompleteness:
    async def test_a_fully_populated_board_leaves_no_placeholder_in_the_preview(
        self, gated_call_spy, monkeypatch,
    ):
        client = MiroClient(MagicMock())
        client._team_name = "QA team"

        def fake_request(method, path, **kwargs):
            if path.endswith("/items"):
                return {"data": [{
                    "id": ITEM_ID, "type": "sticky_note", "data": {"content": "<p>hi</p>"},
                    "style": {"fillColor": "yellow"}, "position": {"x": 1, "y": 2},
                    "geometry": {"width": 3}, "parent": {"id": FRAME_ID},
                    "modifiedAt": "2026-02-02T00:00:00Z",
                }], "total": 1}
            return {
                "id": BOARD_ID, "name": "QA board", "description": "d",
                "team": {"name": "QA team"}, "createdAt": "2026-01-01T00:00:00Z",
                "modifiedAt": "2026-02-02T00:00:00Z", "viewLink": "https://miro.com/x",
            }

        monkeypatch.setattr(client, "_request", fake_request)
        connector = MiroConnector(client)

        result = await connector.call("miro_list_items", {"board_id": BOARD_ID})

        assert result[0]["content"] == "<p>hi</p>"
        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


MIRO_SIBLINGS: dict[str, list[str]] = {
    "miro_list_boards": ["miro_get_board"],
    "miro_get_board": ["miro_list_boards", "miro_list_items"],
    "miro_list_tags": ["miro_list_items"],
    "miro_list_items": ["miro_get_items", "miro_create_items"],
    "miro_get_items": ["miro_list_items", "miro_update_items"],
    "miro_list_connectors": ["miro_create_connectors"],
}


class TestToolDefinitions:
    """What an AI client reads to choose and call these tools (ADR 0115)."""

    def test_every_tool_definition_is_complete(self):
        connector = MiroConnector(MagicMock())
        registered = {s.name for s in connector.tool_specs()}
        siblings = {
            tool: [other for other in others if other in registered]
            for tool, others in MIRO_SIBLINGS.items() if tool in registered
        }
        assert_tool_definitions_complete(connector, siblings)

    def test_no_tool_deletes_and_all_are_reads(self):
        specs = MiroConnector(MagicMock()).tool_specs()
        assert [s.name for s in specs] == [
            "miro_list_boards", "miro_get_board", "miro_list_tags",
            "miro_list_items", "miro_get_items", "miro_list_connectors",
        ]
        assert all(s.read_only and not s.destructive for s in specs)
        assert all(s.params[-1].name == "reason" and s.params[-1].required for s in specs)


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.team_name = "QA team"
        client.list_boards.return_value = [BOARD]
        client.get_board.return_value = BOARD
        client.list_tags.return_value = []
        client.list_items.return_value = ([STICKY], False)
        client.get_item.return_value = STICKY
        client.list_connectors.return_value = ([LINE], False)

        await assert_all_tools_leave_an_audit_trail(
            connector, miro_module, monkeypatch, tmp_path,
            arg_overrides={
                "miro_get_board": {"board_id": BOARD_ID},
                "miro_list_tags": {"board_id": BOARD_ID},
                "miro_list_items": {"board_id": BOARD_ID},
                "miro_get_items": {"board_id": BOARD_ID, "item_ids": ITEM_ID},
                "miro_list_connectors": {"board_id": BOARD_ID},
            },
        )
