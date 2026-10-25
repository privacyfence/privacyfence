"""Unit tests for privacyfence.connectors.miro.MiroConnector's listing tools.

Invariant: the three listing tools never reach the approval gate, each leaves an audit entry, and
none of them hands the model more than the board's name, team, dates and link (no description, no
owner)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import miro as miro_module
from privacyfence.connectors.miro import MiroConnector
from privacyfence.miro_client import MiroBoard, MiroClientError, MiroTag

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_tool_definitions_complete

pytestmark = pytest.mark.unit

BOARD_ID = "uXjVOD6LSME="
BOARD = MiroBoard(
    id=BOARD_ID, name="QA board", description="secret notes", team="QA team",
    created_at="2026-01-01T00:00:00Z", modified_at="2026-02-02T00:00:00Z",
    view_link="https://miro.com/app/board/uXjVOD6LSME=/",
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


MIRO_SIBLINGS: dict[str, list[str]] = {
    "miro_list_boards": ["miro_get_board"],
    "miro_get_board": ["miro_list_boards"],
    "miro_list_tags": [],
}


class TestToolDefinitions:
    """What an AI client reads to choose and call these tools (ADR 0115)."""

    def test_every_tool_definition_is_complete(self):
        assert_tool_definitions_complete(MiroConnector(MagicMock()), MIRO_SIBLINGS)

    def test_no_tool_deletes_and_all_three_are_reads(self):
        specs = MiroConnector(MagicMock()).tool_specs()
        assert [s.name for s in specs] == ["miro_list_boards", "miro_get_board", "miro_list_tags"]
        assert all(s.read_only and not s.destructive for s in specs)
        assert all(s.params[-1].name == "reason" and s.params[-1].required for s in specs)


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        client.team_name = "QA team"
        client.list_boards.return_value = [BOARD]
        client.get_board.return_value = BOARD
        client.list_tags.return_value = []

        await assert_all_tools_leave_an_audit_trail(
            connector, miro_module, monkeypatch, tmp_path,
            arg_overrides={
                "miro_get_board": {"board_id": BOARD_ID},
                "miro_list_tags": {"board_id": BOARD_ID},
            },
        )
