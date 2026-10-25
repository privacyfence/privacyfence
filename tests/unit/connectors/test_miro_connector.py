"""Unit tests for privacyfence.connectors.miro.MiroConnector.

Invariant: the three listing tools never reach the approval gate, each leaves an audit entry, and
none of them hands the model more than the board's name, team, dates and link (no description, no
owner); the three item and connector reads always go through a review card whose preview shows no
item text; the two create tools validate everything first, write nothing until the approval card
is accepted, and show the approver exactly what will be sent."""
from __future__ import annotations

import json
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


FRAME = MiroItem(
    FRAME_ID, "frame", "", "<b>Plan</b>", "", "", "", "", 0.0, 0.0, 800.0, 600.0, "", "",
)
NEW_FRAME_ID = "3458764549483493031"
NEW_FRAME = MiroItem(NEW_FRAME_ID, "frame", "", "F", "", "", "", "", 0.0, 0.0, 1.0, 1.0, "", "")
STICKY_2 = MiroItem(
    ITEM_ID_2, "sticky_note", "b", "", "", "", "", "", 0.0, 0.0, 1.0, 1.0, "", "",
)


def items_json(*items):
    return json.dumps(list(items))


def make_writer(*known: MiroItem):
    """A connector whose client knows the given items and whose writes succeed."""
    connector, client = make_connector()
    client.team_name = "QA team"
    client.get_board.return_value = BOARD
    by_id = {i.id: i for i in known}

    def get_item(_board, item_id):
        if item_id not in by_id:
            raise MiroNotFound("Miro found no such board or item (HTTP 404).")
        return by_id[item_id]

    client.get_item.side_effect = get_item
    client.create_items.return_value = [STICKY]
    client.create_frame.return_value = NEW_FRAME
    client.create_connector.return_value = "3458764549483493040"
    return connector, client


class TestCreateItems:
    @pytest.mark.parametrize("item, body", [
        (
            {"type": "sticky_note", "content": "hi", "shape": "square", "width": 200,
             "color": "light_blue", "x": 5, "y": -6.5},
            {"type": "sticky_note", "data": {"content": "hi", "shape": "square"},
             "style": {"fillColor": "light_blue"}, "position": {"x": 5, "y": -6.5},
             "geometry": {"width": 200}},
        ),
        (
            {"type": "card", "title": "T", "description": "D", "due_date": "2026-03-04",
             "color": "#2D9BF0", "width": 300, "height": 120, "frame_id": FRAME_ID},
            {"type": "card",
             "data": {"title": "T", "description": "D", "dueDate": "2026-03-04T12:00:00.000Z"},
             "style": {"cardTheme": "#2D9BF0"}, "position": {"x": 0, "y": 0},
             "geometry": {"width": 300, "height": 120}, "parent": {"id": FRAME_ID}},
        ),
        (
            {"type": "text", "content": "<p>x</p>", "color": "#112233", "width": 50},
            {"type": "text", "data": {"content": "<p>x</p>"}, "style": {"color": "#112233"},
             "position": {"x": 0, "y": 0}, "geometry": {"width": 50}},
        ),
        (
            {"type": "shape", "content": "s", "shape": "circle", "color": "#FFF9B1",
             "width": 10, "height": 20},
            {"type": "shape", "data": {"content": "s", "shape": "circle"},
             "style": {"fillColor": "#fff9b1"}, "position": {"x": 0, "y": 0},
             "geometry": {"width": 10, "height": 20}},
        ),
        (
            {"type": "sticky_note"},
            {"type": "sticky_note", "position": {"x": 0, "y": 0}},
        ),
    ])
    async def test_each_type_builds_its_body(self, gated_call_spy, item, body):
        connector, client = make_writer(FRAME)

        await connector.call("miro_create_items", {"board_id": BOARD_ID, "items": items_json(item)})

        client.create_items.assert_called_once_with(BOARD_ID, [body])
        client.create_frame.assert_not_called()

    async def test_a_frame_body_has_no_type_or_parent_key(self, gated_call_spy):
        connector, client = make_writer()
        item = {"type": "frame", "title": "Plan", "width": 800, "height": 600, "x": 1,
                "y": 2, "color": "#A6CCF5"}

        result = await connector.call(
            "miro_create_items", {"board_id": BOARD_ID, "items": items_json(item)},
        )

        client.create_frame.assert_called_once_with(BOARD_ID, {
            "data": {"title": "Plan", "format": "custom", "type": "freeform"},
            "style": {"fillColor": "#a6ccf5"}, "position": {"x": 1, "y": 2},
            "geometry": {"width": 800, "height": 600},
        })
        client.create_items.assert_not_called()
        assert result == {"board_id": BOARD_ID, "created": [{"id": NEW_FRAME_ID, "type": "frame"}]}

    async def test_frames_are_created_first_then_the_rest_in_one_call(self, gated_call_spy):
        connector, client = make_writer()
        order = []
        client.create_frame.side_effect = lambda *a: order.append("frame") or NEW_FRAME
        client.create_items.side_effect = lambda *a: order.append("bulk") or [STICKY]

        result = await connector.call("miro_create_items", {
            "board_id": BOARD_ID,
            "items": items_json(
                {"type": "sticky_note", "content": "a"},
                {"type": "frame", "title": "F"},
                {"type": "frame", "title": "G"},
            ),
        })

        assert order == ["frame", "frame", "bulk"]
        assert client.create_items.call_args.args[1] == [
            {"type": "sticky_note", "data": {"content": "a"}, "position": {"x": 0, "y": 0}},
        ]
        assert [c["type"] for c in result["created"]] == ["sticky_note", "frame", "frame"]

    async def test_a_failure_on_the_second_frame_names_the_first_and_skips_the_bulk_call(
        self, gated_call_spy,
    ):
        connector, client = make_writer()
        client.create_frame.side_effect = [NEW_FRAME, MiroClientError("Miro refused.")]

        with pytest.raises(RuntimeError, match=f"Miro refused. Frames created before the error: {NEW_FRAME_ID}."):
            await connector.call("miro_create_items", {
                "board_id": BOARD_ID,
                "items": items_json(
                    {"type": "frame"}, {"type": "frame"}, {"type": "sticky_note"},
                ),
            })

        client.create_items.assert_not_called()

    async def test_a_failed_first_frame_says_none_were_created(self, gated_call_spy):
        connector, client = make_writer()
        client.create_frame.side_effect = MiroClientError("Miro refused.")

        with pytest.raises(RuntimeError, match="Frames created before the error: none."):
            await connector.call("miro_create_items", {
                "board_id": BOARD_ID, "items": items_json({"type": "frame"}),
            })

    async def test_a_bulk_failure_after_frames_names_them(self, gated_call_spy):
        connector, client = make_writer()
        client.create_items.side_effect = MiroClientError("Miro refused.")

        with pytest.raises(RuntimeError, match=f"Frames created before the error: {NEW_FRAME_ID}."):
            await connector.call("miro_create_items", {
                "board_id": BOARD_ID,
                "items": items_json({"type": "frame"}, {"type": "sticky_note"}),
            })

    async def test_a_bulk_failure_without_frames_is_a_plain_error(self, gated_call_spy):
        connector, client = make_writer()
        client.create_items.side_effect = MiroClientError("Miro refused.")

        with pytest.raises(RuntimeError) as excinfo:
            await connector.call("miro_create_items", {
                "board_id": BOARD_ID, "items": items_json({"type": "sticky_note"}),
            })

        assert str(excinfo.value) == "Miro refused."

    async def test_the_card_shows_what_will_be_added(self, gated_call_spy):
        connector, _client = make_writer(FRAME)

        await connector.call("miro_create_items", {
            "board_id": f" {BOARD_ID} ",
            "items": items_json(
                {"type": "sticky_note", "content": "<p>Idea &amp; more</p>", "x": 3, "y": 4.5,
                 "width": 100, "color": "yellow", "frame_id": FRAME_ID},
                {"type": "card", "title": "Card", "width": 10, "height": 20},
                {"type": "frame", "title": "F"},
            ),
        })

        call = gated_call_spy[0]
        assert call["gate"] == "popup"
        assert call["tool"] == "miro_create_items"
        assert call["tool_name"] == "Add Miro Items"
        assert call["filtered_data"] is None
        assert call["args"] == {"board_id": BOARD_ID}
        assert call["connector"] == "miro" and call["my_email"] == ""
        assert call["summary"] == "Add 3 item(s) to QA board"
        assert call["sender"] == "QA board"
        assert list(call["preview"]) == ["Board", "Team", "Items"]
        assert call["preview"] == {"Board": "QA board", "Team": "QA team", "Items": "3"}
        table = call["preview_tables"][0]
        assert table["headers"] == ["Type", "Text", "Position", "Size", "Frame", "Color"]
        assert table["rows"] == [
            ["sticky_note", "Idea & more", "3, 4.5", "100 × -", "Plan", "yellow"],
            ["card", "Card", "0, 0", "10 × 20", "", ""],
            ["frame", "F", "0, 0", "", "", ""],
        ]
        assert call["raw_data"]["board_id"] == BOARD_ID
        assert json.loads(call["details_text"]) == call["raw_data"]["items"]
        assert call["details_text"] == json.dumps(
            call["raw_data"]["items"], indent=2, ensure_ascii=False,
        )

    async def test_a_size_with_only_a_height_shows_a_dash_for_the_width(self, gated_call_spy):
        connector, _client = make_writer()

        await connector.call("miro_create_items", {
            "board_id": BOARD_ID, "items": items_json({"type": "shape", "height": 7}),
        })

        assert gated_call_spy[0]["preview_tables"][0]["rows"][0][3] == "- × 7"

    async def test_the_team_falls_back_when_miro_does_not_show_it(self, gated_call_spy):
        connector, client = make_writer()
        client.team_name = ""

        await connector.call("miro_create_items", {
            "board_id": BOARD_ID, "items": items_json({"type": "text", "content": "x"}),
        })

        assert gated_call_spy[0]["preview"]["Team"] == "(not shown by Miro)"

    async def test_the_return_value(self, gated_call_spy):
        connector, _client = make_writer()

        result = await connector.call("miro_create_items", {
            "board_id": BOARD_ID, "items": items_json({"type": "sticky_note"}),
        })

        assert result == {"board_id": BOARD_ID, "created": [{"id": ITEM_ID, "type": "sticky_note"}]}

    async def test_nothing_is_written_when_the_gate_raises(self, monkeypatch):
        connector, client = make_writer()

        async def refuse(**_kwargs):
            raise RuntimeError("denied")

        monkeypatch.setattr(miro_module, "gated_call", refuse, raising=False)

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("miro_create_items", {
                "board_id": BOARD_ID,
                "items": items_json({"type": "frame"}, {"type": "sticky_note"}),
            })

        client.create_frame.assert_not_called()
        client.create_items.assert_not_called()

    @pytest.mark.parametrize("frame_known", [False, True])
    async def test_a_frame_id_that_is_not_a_frame_is_refused_before_the_gate(
        self, gated_call_spy, frame_known,
    ):
        connector, client = make_writer(STICKY) if frame_known else make_writer()
        target = ITEM_ID if frame_known else FRAME_ID

        with pytest.raises(ValueError, match=f"Item {target} is not a frame on this board."):
            await connector.call("miro_create_items", {
                "board_id": BOARD_ID,
                "items": items_json({"type": "sticky_note", "frame_id": target}),
            })

        assert gated_call_spy == []
        client.create_items.assert_not_called()

    async def test_each_distinct_frame_is_looked_up_once(self, gated_call_spy):
        connector, client = make_writer(FRAME)

        await connector.call("miro_create_items", {
            "board_id": BOARD_ID,
            "items": items_json(
                {"type": "text", "content": "a", "frame_id": FRAME_ID},
                {"type": "text", "content": "b", "frame_id": FRAME_ID},
            ),
        })

        assert client.get_item.call_count == 1

    async def test_a_client_error_while_checking_the_frame_becomes_a_runtime_error(
        self, gated_call_spy,
    ):
        connector, client = make_writer()
        client.get_item.side_effect = MiroClientError("Miro refused the request (HTTP 403).")

        with pytest.raises(RuntimeError, match="HTTP 403"):
            await connector.call("miro_create_items", {
                "board_id": BOARD_ID,
                "items": items_json({"type": "text", "content": "a", "frame_id": FRAME_ID}),
            })

        assert gated_call_spy == []

    async def test_a_bad_board_id_is_refused_before_the_client_is_called(self, gated_call_spy):
        connector, client = make_writer()

        with pytest.raises(ValueError, match="Not a Miro board id"):
            await connector.call("miro_create_items", {
                "board_id": "https://miro.com/app/board/uXjVOD6LSME=/",
                "items": items_json({"type": "sticky_note"}),
            })

        client.get_board.assert_not_called()

    @pytest.mark.parametrize("items, message", [
        ("not json", "items must be a JSON array of 1 to 20 objects"),
        ('{"type": "text"}', "items must be a JSON array of 1 to 20 objects"),
        ("[]", "items must be a JSON array of 1 to 20 objects"),
        (json.dumps([{"type": "text", "content": "x"}] * 21),
         "items must be a JSON array of 1 to 20 objects"),
        (None, "items must be a JSON array of 1 to 20 objects"),
        ('["text"]', "items must be a JSON array of 1 to 20 objects"),
        ('[{"type": "image"}]', "items must be a JSON array of 1 to 20 objects"),
        ('[{"type": ["text"]}]', "items must be a JSON array of 1 to 20 objects"),
        ('[{"type": "frame", "frame_id": "3458764549483493030"}]',
         "A frame takes only these keys: color, height, title, type, width, x, y."),
        ('[{"type": "text", "content": "x", "title": "y"}]',
         "A text takes only these keys: color, content, frame_id, type, width, x, y."),
        ('[{"type": "sticky_note", "width": 1, "height": 1}]',
         "A sticky_note takes width or height, not both."),
        ('[{"type": "text"}]', "A text needs non-empty content."),
        ('[{"type": "text", "content": "  "}]', "A text needs non-empty content."),
        ('[{"type": "sticky_note", "color": "mauve"}]',
         "A sticky_note color must be one of gray, light_yellow"),
        ('[{"type": "sticky_note", "color": 3}]',
         "A sticky_note color must be one of gray, light_yellow"),
        ('[{"type": "shape", "color": "#123456"}]',
         "A shape color must be one of #f5f6f8, #d5f692"),
        ('[{"type": "frame", "color": 7}]', "A frame color must be one of #f5f6f8"),
        ('[{"type": "card", "color": "#12345"}]',
         "A card color must be a hex color such as #2D9BF0."),
        ('[{"type": "text", "content": "x", "color": "red"}]',
         "A text color must be a hex color such as #2D9BF0."),
        ('[{"type": "shape", "shape": "blob"}]', "Unsupported shape 'blob'. Use one of rectangle,"),
        ('[{"type": "sticky_note", "shape": "circle"}]',
         "A sticky_note shape must be square or rectangle."),
        ('[{"type": "shape", "width": true}]', "width must be a number from 1 to 100000."),
        ('[{"type": "shape", "height": 0}]', "height must be a number from 1 to 100000."),
        ('[{"type": "shape", "width": 100001}]', "width must be a number from 1 to 100000."),
        ('[{"type": "shape", "width": "9"}]', "width must be a number from 1 to 100000."),
        ('[{"type": "shape", "x": true}]', "x and y must be numbers."),
        ('[{"type": "shape", "y": "1"}]', "x and y must be numbers."),
        ('[{"type": "shape", "x": 1e999}]', "x and y must be numbers."),
        ('[{"type": "card", "due_date": "2026-13-40"}]',
         "due_date must be a date written YYYY-MM-DD."),
        ('[{"type": "card", "due_date": "tomorrow"}]',
         "due_date must be a date written YYYY-MM-DD."),
        ('[{"type": "card", "due_date": 20260101}]',
         "due_date must be a date written YYYY-MM-DD."),
        (json.dumps([{"type": "text", "content": "x" * 6001}]),
         "content is longer than 6000 characters."),
        (json.dumps([{"type": "card", "title": "x" * 501}]),
         "title is longer than 500 characters."),
        (json.dumps([{"type": "card", "description": "x" * 6001}]),
         "description is longer than 6000 characters."),
        ('[{"type": "text", "content": 5}]', "content must be text."),
        ('[{"type": "text", "content": "x", "frame_id": "abc"}]', "Not a Miro item id: 'abc'"),
        ('[{"type": "text", "content": "x", "frame_id": 5}]', "Not a Miro item id: 5"),
    ])
    async def test_validation_messages_are_raised_before_the_gate(
        self, gated_call_spy, items, message,
    ):
        connector, client = make_writer()

        with pytest.raises(ValueError) as excinfo:
            await connector.call("miro_create_items", {"board_id": BOARD_ID, "items": items})

        assert str(excinfo.value).startswith(message)
        assert gated_call_spy == []
        client.create_items.assert_not_called()
        client.create_frame.assert_not_called()

    async def test_the_boundary_values_are_accepted(self, gated_call_spy):
        connector, _client = make_writer()

        await connector.call("miro_create_items", {
            "board_id": BOARD_ID,
            "items": items_json(
                {"type": "shape", "width": 1, "height": 100000, "x": -1e6, "y": 0.5,
                 "content": "x" * 6000, "shape": "right_brace"},
                {"type": "card", "title": "x" * 500, "due_date": "2024-02-29"},
            ),
        })

        assert len(gated_call_spy) == 1


CONNECTOR_ID = "3458764549483493040"


class TestCreateConnectors:
    def args(self, *lines):
        return {"board_id": BOARD_ID, "connectors": json.dumps(list(lines))}

    async def test_the_body_and_the_card(self, gated_call_spy):
        connector, client = make_writer(STICKY, STICKY_2)

        result = await connector.call("miro_create_connectors", self.args(
            {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2, "shape": "elbowed",
             "caption": "<b>leads</b> to", "color": "#2D9BF0"},
            {"start_item_id": ITEM_ID_2, "end_item_id": ITEM_ID},
        ))

        assert [c.args for c in client.create_connector.call_args_list] == [
            (BOARD_ID, {
                "startItem": {"id": ITEM_ID}, "endItem": {"id": ITEM_ID_2}, "shape": "elbowed",
                "captions": [{"content": "<b>leads</b> to"}],
                "style": {"strokeColor": "#2D9BF0"},
            }),
            (BOARD_ID, {"startItem": {"id": ITEM_ID_2}, "endItem": {"id": ITEM_ID}}),
        ]
        assert result == {"board_id": BOARD_ID, "created_ids": [CONNECTOR_ID, CONNECTOR_ID]}
        call = gated_call_spy[0]
        assert call["gate"] == "popup" and call["filtered_data"] is None
        assert call["tool"] == "miro_create_connectors"
        assert call["tool_name"] == "Add Miro Connectors"
        assert call["summary"] == "Draw 2 connector(s) on QA board"
        assert call["args"] == {"board_id": BOARD_ID}
        assert call["preview"] == {"Board": "QA board", "Team": "QA team", "Connectors": "2"}
        table = call["preview_tables"][0]
        assert table["headers"] == ["From", "To", "Shape", "Caption"]
        assert table["rows"] == [
            ["sticky_note [QATEST]", "sticky_note b", "elbowed", "leads to"],
            ["sticky_note b", "sticky_note [QATEST]", "", ""],
        ]
        assert json.loads(call["details_text"]) == call["raw_data"]["connectors"]

    async def test_the_team_falls_back_when_miro_does_not_show_it(self, gated_call_spy):
        connector, client = make_writer(STICKY, STICKY_2)
        client.team_name = ""

        await connector.call("miro_create_connectors", self.args(
            {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2},
        ))

        assert gated_call_spy[0]["preview"]["Team"] == "(not shown by Miro)"

    async def test_an_empty_caption_draws_no_caption(self, gated_call_spy):
        connector, client = make_writer(STICKY, STICKY_2)

        await connector.call("miro_create_connectors", self.args(
            {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2, "caption": ""},
        ))

        assert client.create_connector.call_args.args[1] == {
            "startItem": {"id": ITEM_ID}, "endItem": {"id": ITEM_ID_2},
        }

    async def test_each_end_is_looked_up_once(self, gated_call_spy):
        connector, client = make_writer(STICKY, STICKY_2)

        await connector.call("miro_create_connectors", self.args(
            {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2},
            {"start_item_id": ITEM_ID_2, "end_item_id": ITEM_ID},
        ))

        assert client.get_item.call_count == 2

    async def test_a_missing_end_is_refused_before_the_gate(self, gated_call_spy):
        connector, client = make_writer(STICKY)

        with pytest.raises(ValueError, match=f"Item\\(s\\) not found on this board: {ITEM_ID_2}."):
            await connector.call("miro_create_connectors", self.args(
                {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2},
            ))

        assert gated_call_spy == []
        client.create_connector.assert_not_called()

    async def test_a_frame_end_is_refused_before_the_gate(self, gated_call_spy):
        connector, client = make_writer(STICKY, FRAME)

        with pytest.raises(ValueError, match="A connector cannot start or end at a frame."):
            await connector.call("miro_create_connectors", self.args(
                {"start_item_id": ITEM_ID, "end_item_id": FRAME_ID},
            ))

        assert gated_call_spy == []
        client.create_connector.assert_not_called()

    async def test_a_client_error_while_fetching_an_end_becomes_a_runtime_error(
        self, gated_call_spy,
    ):
        connector, client = make_writer()
        client.get_item.side_effect = MiroClientError("Miro refused the request (HTTP 403).")

        with pytest.raises(RuntimeError, match="HTTP 403"):
            await connector.call("miro_create_connectors", self.args(
                {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2},
            ))

    @pytest.mark.parametrize("connectors, message", [
        ("not json", "connectors must be a JSON array of 1 to 20"),
        ("[]", "connectors must be a JSON array of 1 to 20"),
        ('["a"]', "connectors must be a JSON array of 1 to 20"),
        (None, "connectors must be a JSON array of 1 to 20"),
        (json.dumps([{"start_item_id": "3458764549483493025", "end_item_id": "3458764549483493026"}] * 21),
         "connectors must be a JSON array of 1 to 20"),
        ('[{"start_item_id": "3458764549483493025"}]', "connectors must be a JSON array of 1 to 20"),
        ('[{"start_item_id": 3458764549483493025, "end_item_id": "3458764549483493026"}]',
         "connectors must be a JSON array of 1 to 20"),
        ('[{"start_item_id": "3458764549483493025", "end_item_id": "x"}]',
         "Not a Miro item id: 'x'"),
        ('[{"start_item_id": "3458764549483493025", "end_item_id": "3458764549483493025"}]',
         "A connector must join two different items."),
        ('[{"start_item_id": "3458764549483493025", "end_item_id": "3458764549483493026", "label": "a"}]',
         "A connector takes only these keys: caption, color, end_item_id, shape, start_item_id."),
        ('[{"start_item_id": "3458764549483493025", "end_item_id": "3458764549483493026", "shape": "zigzag"}]',
         "A connector shape must be straight, elbowed or curved."),
        ('[{"start_item_id": "3458764549483493025", "end_item_id": "3458764549483493026", "color": "red"}]',
         "A connector color must be a hex color such as #2D9BF0."),
        ('[{"start_item_id": "3458764549483493025", "end_item_id": "3458764549483493026", "color": 5}]',
         "A connector color must be a hex color such as #2D9BF0."),
        (json.dumps([{"start_item_id": "3458764549483493025", "end_item_id": "3458764549483493026",
                      "caption": "x" * 201}]),
         "caption is longer than 200 characters."),
    ])
    async def test_validation_messages_are_raised_before_the_gate(
        self, gated_call_spy, connectors, message,
    ):
        connector, client = make_writer(STICKY, STICKY_2)

        with pytest.raises(ValueError) as excinfo:
            await connector.call(
                "miro_create_connectors", {"board_id": BOARD_ID, "connectors": connectors},
            )

        assert str(excinfo.value).startswith(message)
        assert gated_call_spy == []
        client.get_item.assert_not_called()
        client.create_connector.assert_not_called()

    async def test_a_failure_on_the_second_of_three_names_the_first_and_skips_the_third(
        self, gated_call_spy,
    ):
        connector, client = make_writer(STICKY, STICKY_2)
        client.create_connector.side_effect = [
            "3458764549483493041", MiroClientError("Miro refused."), "3458764549483493043",
        ]
        line = {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2}

        with pytest.raises(RuntimeError) as excinfo:
            await connector.call("miro_create_connectors", self.args(line, line, line))

        assert str(excinfo.value) == (
            "Miro refused. Connectors created before the error: 3458764549483493041."
        )
        assert client.create_connector.call_count == 2

    async def test_a_failure_on_the_first_says_none_were_created(self, gated_call_spy):
        connector, client = make_writer(STICKY, STICKY_2)
        client.create_connector.side_effect = MiroClientError("Miro refused.")

        with pytest.raises(RuntimeError, match="Connectors created before the error: none."):
            await connector.call("miro_create_connectors", self.args(
                {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2},
            ))

    async def test_nothing_is_written_when_the_gate_raises(self, monkeypatch):
        connector, client = make_writer(STICKY, STICKY_2)

        async def refuse(**_kwargs):
            raise RuntimeError("denied")

        monkeypatch.setattr(miro_module, "gated_call", refuse, raising=False)

        with pytest.raises(RuntimeError, match="denied"):
            await connector.call("miro_create_connectors", self.args(
                {"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2},
            ))

        client.create_connector.assert_not_called()


MIRO_SIBLINGS: dict[str, list[str]] = {
    "miro_list_boards": ["miro_get_board"],
    "miro_get_board": ["miro_list_boards", "miro_list_items"],
    "miro_list_tags": ["miro_list_items"],
    "miro_list_items": ["miro_get_items", "miro_create_items"],
    "miro_get_items": ["miro_list_items", "miro_update_items"],
    "miro_list_connectors": ["miro_create_connectors"],
    "miro_create_items": ["miro_update_items"],
    "miro_create_connectors": ["miro_list_connectors"],
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

    def test_no_tool_deletes_and_only_the_creates_write(self):
        specs = MiroConnector(MagicMock()).tool_specs()
        assert [s.name for s in specs] == [
            "miro_list_boards", "miro_get_board", "miro_list_tags",
            "miro_list_items", "miro_get_items", "miro_list_connectors",
            "miro_create_items", "miro_create_connectors",
        ]
        assert not any(s.destructive for s in specs)
        assert {s.name for s in specs if not s.read_only} == {
            "miro_create_items", "miro_create_connectors",
        }
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
        client.create_items.return_value = [STICKY]
        client.create_connector.return_value = LINE.id

        await assert_all_tools_leave_an_audit_trail(
            connector, miro_module, monkeypatch, tmp_path,
            arg_overrides={
                "miro_get_board": {"board_id": BOARD_ID},
                "miro_list_tags": {"board_id": BOARD_ID},
                "miro_list_items": {"board_id": BOARD_ID},
                "miro_get_items": {"board_id": BOARD_ID, "item_ids": ITEM_ID},
                "miro_list_connectors": {"board_id": BOARD_ID},
                "miro_create_items": {
                    "board_id": BOARD_ID, "items": '[{"type": "sticky_note", "content": "a"}]',
                },
                "miro_create_connectors": {
                    "board_id": BOARD_ID,
                    "connectors": json.dumps([{"start_item_id": ITEM_ID, "end_item_id": ITEM_ID_2}]),
                },
            },
        )
