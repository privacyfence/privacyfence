"""Miro connector: lists the boards a Miro account can open and each board's tags without a
card. The client is built by the daemon from an OAuth sign-in (see ``miro_auth``)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason
from ..miro_client import (
    MiroBoard,
    MiroClient,
    MiroClientError,
    validate_board_id,
)

logger = logging.getLogger(__name__)

_REASON = ToolParam(
    "reason", "str", required=True,
    description="One sentence: why are you calling this tool right now?",
)

_BOARD_ID_DESCRIPTION = (
    "The board id from miro_list_boards (such as uXjVOD6LSME=), the part of the board's "
    "address after /app/board/. Not the full link."
)
_MAX_RESULTS_DESCRIPTION = "Most results to return. The result is cut to this many."
_QUERY_DESCRIPTION = (
    "Text to search for in board names and descriptions. Empty lists every board."
)
_LIST_BOARDS_MAX = 200


class MiroConnector(Connector):
    def __init__(self, client: MiroClient) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "miro"

    @property
    def client(self) -> MiroClient:
        return self._client

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="miro_list_boards",
                description=(
                    "List the Miro boards this account can open in the connected Miro team, "
                    "optionally only those matching a search text. Only boards in the Miro team "
                    "chosen when PrivacyFence was signed in are visible. Returns a list of "
                    "{id, name, team, modified_at, view_link}. Pass an id as board_id to "
                    "miro_get_board. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str", required=False, default="",
                              description=_QUERY_DESCRIPTION),
                    ToolParam("max_results", "int", required=False, default=50,
                              description=_MAX_RESULTS_DESCRIPTION),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="miro_get_board",
                description=(
                    "Get one Miro board's name, team, dates and link. Returns {id, name, team, "
                    "created_at, modified_at, view_link}. Use miro_list_boards first to find the "
                    "board_id, and miro_list_items to read what is on the board. Auto-approved."
                ),
                params=[
                    ToolParam("board_id", "str", description=_BOARD_ID_DESCRIPTION),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="miro_list_tags",
                description=(
                    "List the tags defined on one Miro board. Returns a list of {id, title, "
                    "color}. Use miro_list_items to read the board's items. Auto-approved."
                ),
                params=[
                    ToolParam("board_id", "str", description=_BOARD_ID_DESCRIPTION),
                    _REASON,
                ],
                read_only=True,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "miro_list_boards":
            return await self._list_boards(**args)
        if tool == "miro_get_board":
            return await self._get_board(**args)
        if tool == "miro_list_tags":
            return await self._list_tags(**args)
        raise ValueError(f"Unknown Miro tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_boards(self, query: str = "", max_results: int = 50) -> Any:
        t0 = time.time()
        _check_max_results(max_results, _LIST_BOARDS_MAX)
        boards = await self._fetch(self._client.list_boards, query, max_results)
        boards = boards[:max_results]
        self._auto_audit("miro_list_boards", "List Miro Boards", "List boards",
                         f"{len(boards)} board(s)", t0)
        return [
            {"id": b.id, "name": b.name, "team": b.team, "modified_at": b.modified_at,
             "view_link": b.view_link}
            for b in boards
        ]

    async def _get_board(self, board_id: str) -> Any:
        t0 = time.time()
        board_id = _board_id(board_id)
        board = await self._board_info(board_id)
        self._auto_audit("miro_get_board", "Get Miro Board", f"Get board {board_id}",
                         board.name, t0)
        return {"id": board.id, "name": board.name, "team": board.team,
                "created_at": board.created_at, "modified_at": board.modified_at,
                "view_link": board.view_link}

    async def _list_tags(self, board_id: str) -> Any:
        t0 = time.time()
        board_id = _board_id(board_id)
        tags = await self._fetch(self._client.list_tags, board_id)
        self._auto_audit("miro_list_tags", "List Miro Tags", f"List tags of {board_id}",
                         f"{len(tags)} tag(s)", t0)
        return [{"id": t.id, "title": t.title, "color": t.color} for t in tags]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _board_info(self, board_id: str) -> MiroBoard:
        return await self._fetch(self._client.get_board, board_id)

    async def _fetch(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except MiroClientError as exc:
            logger.warning("Miro request failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)


def _board_id(value: str) -> str:
    try:
        return validate_board_id(value)
    except MiroClientError as exc:
        raise ValueError(str(exc)) from exc


def _check_max_results(value: Any, upper: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise ValueError(f"max_results must be between 1 and {upper}.")
