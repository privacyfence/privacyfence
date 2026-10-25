"""Miro connector: lists the boards a Miro account can open and each board's tags without a
card, and reads a board's items and connector lines behind a review card. The client is built by
the daemon from an OAuth sign-in (see ``miro_auth``)."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..miro_client import (
    MiroBoard,
    MiroClient,
    MiroClientError,
    MiroItem,
    MiroNotFound,
    validate_board_id,
    validate_item_id,
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
_READ_MAX = 500
_NO_TEAM = "(not shown by Miro)"
_ITEM_TYPES = (
    "sticky_note", "card", "text", "shape", "frame", "image", "document", "embed", "app_card",
)
_MAX_ITEM_IDS = 50
_CELL_LIMIT = 200
_ITEM_ID_LIST_DESCRIPTION = "Item ids from miro_list_items, separated by commas, 1 to 50."
_TYPE_DESCRIPTION = (
    "Only items of this type: sticky_note, card, text, shape, frame, image, document, embed or "
    "app_card. Empty means every type; use frame to list the board's frames."
)
_FRAME_ID_DESCRIPTION = (
    "Only the items inside this frame (an item id of type frame from miro_list_items). Empty "
    "means the whole board."
)


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
            ToolSpec(
                name="miro_list_items",
                description=(
                    "Read the items on one Miro board, optionally only one type of item or only "
                    "the items inside one frame. Returns a list of items with their id, type, "
                    "text, color, position, size and frame. Use miro_get_items to read specific "
                    "items by id, and miro_create_items to add to the board. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("board_id", "str", description=_BOARD_ID_DESCRIPTION),
                    ToolParam("type", "str", required=False, default="",
                              description=_TYPE_DESCRIPTION),
                    ToolParam("frame_id", "str", required=False, default="",
                              description=_FRAME_ID_DESCRIPTION),
                    ToolParam("max_results", "int", required=False, default=100,
                              description=_MAX_RESULTS_DESCRIPTION),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="miro_get_items",
                description=(
                    "Read specific items of one Miro board by their ids. Returns a list of items "
                    "with their id, type, text, color, position, size and frame. Use "
                    "miro_list_items to find the ids, and miro_update_items to change items. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("board_id", "str", description=_BOARD_ID_DESCRIPTION),
                    ToolParam("item_ids", "str", description=_ITEM_ID_LIST_DESCRIPTION),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="miro_list_connectors",
                description=(
                    "Read the connector lines on one Miro board and the ids of the items each "
                    "one joins. Returns a list of {id, start_item_id, end_item_id, shape, "
                    "captions}. Use miro_create_connectors to draw new lines. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("board_id", "str", description=_BOARD_ID_DESCRIPTION),
                    ToolParam("max_results", "int", required=False, default=100,
                              description=_MAX_RESULTS_DESCRIPTION),
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
        if tool == "miro_list_items":
            return await self._list_items(**args)
        if tool == "miro_get_items":
            return await self._get_items(**args)
        if tool == "miro_list_connectors":
            return await self._list_connectors(**args)
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
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _list_items(
        self, board_id: str, type: str = "", frame_id: str = "", max_results: int = 100,
    ) -> Any:
        board_id = _board_id(board_id)
        if not isinstance(type, str) or (type and type not in _ITEM_TYPES):
            raise ValueError(f"type must be empty or one of {', '.join(_ITEM_TYPES)}.")
        frame_id = _item_id(frame_id) if frame_id else ""
        _check_max_results(max_results, _READ_MAX)
        board = await self._board_info(board_id)
        items, truncated = await self._fetch(
            self._client.list_items, board_id,
            item_type=type, frame_id=frame_id, max_results=max_results,
        )
        items = items[:max_results]
        details, tables = _items_text(items, truncated, max_results)
        return await gated_call(
            connector=self.name,
            tool="miro_list_items",
            tool_name="Read Miro Items",
            summary=f"Read {len(items)} item(s) from {board.name}",
            sender=board.name,
            raw_data=[asdict(i) for i in items],
            filtered_data=[asdict(i) for i in items],
            gate="review",
            preview={
                "Board": board.name,
                "Team": self._client.team_name or _NO_TEAM,
                "Type": type or "(all)",
                "Frame": frame_id or "(whole board)",
            },
            new_info={"Items": str(len(items)), "Item content": "the text of every listed item"},
            details_text=details,
            pii_scan_text=details,
            preview_tables=tables,
            table_only=True,
            my_email="",
            args={"board_id": board_id},
        )

    async def _get_items(self, board_id: str, item_ids: str) -> Any:
        board_id = _board_id(board_id)
        ids = _parse_item_ids(item_ids)
        board = await self._board_info(board_id)
        items: list[MiroItem] = []
        missing: list[str] = []
        for item_id in ids:
            try:
                items.append(await asyncio.to_thread(self._client.get_item, board_id, item_id))
            except MiroNotFound:
                missing.append(item_id)
            except MiroClientError as exc:
                logger.warning("Miro request failed: %s", exc)
                raise RuntimeError(str(exc)) from exc
        if missing:
            raise ValueError(f"Item(s) not found on this board: {', '.join(sorted(missing))}.")
        details, tables = _items_text(items, False, len(items))
        return await gated_call(
            connector=self.name,
            tool="miro_get_items",
            tool_name="Read Miro Items by Id",
            summary=f"Read {len(items)} item(s) from {board.name}",
            sender=board.name,
            raw_data=[asdict(i) for i in items],
            filtered_data=[asdict(i) for i in items],
            gate="review",
            preview={
                "Board": board.name,
                "Team": self._client.team_name or _NO_TEAM,
            },
            new_info={"Items": str(len(items)), "Item content": "the text of every listed item"},
            details_text=details,
            pii_scan_text=details,
            preview_tables=tables,
            table_only=True,
            my_email="",
            args={"board_id": board_id},
        )

    async def _list_connectors(self, board_id: str, max_results: int = 100) -> Any:
        board_id = _board_id(board_id)
        _check_max_results(max_results, _READ_MAX)
        board = await self._board_info(board_id)
        lines, truncated = await self._fetch(
            self._client.list_connectors, board_id, max_results=max_results,
        )
        lines = lines[:max_results]
        rows = [asdict(line) for line in lines]
        if lines:
            details = "\n".join(
                f"#{c.id}: {c.start_item_id} → {c.end_item_id} "
                f"{' / '.join(_plain(x) for x in c.captions)}"
                for c in lines
            )
        else:
            details = "(no connectors)"
        if truncated:
            details = f"Showing the first {max_results} connectors; the board has more.\n\n{details}"
        tables = [{
            "headers": ["id", "from", "to", "captions"],
            "rows": [
                [c.id, c.start_item_id, c.end_item_id, _cell(" / ".join(c.captions))]
                for c in lines
            ],
        }] if lines else None
        return await gated_call(
            connector=self.name,
            tool="miro_list_connectors",
            tool_name="Read Miro Connectors",
            summary=f"Read {len(lines)} connector(s) from {board.name}",
            sender=board.name,
            raw_data=rows,
            filtered_data=rows,
            gate="review",
            preview={"Board": board.name, "Team": self._client.team_name or _NO_TEAM},
            new_info={
                "Connectors": str(len(lines)),
                "Captions": "the caption text of every listed connector",
            },
            details_text=details,
            pii_scan_text=details,
            preview_tables=tables,
            table_only=True,
            my_email="",
            args={"board_id": board_id},
        )

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


def _items_text(
    items: list[MiroItem], truncated: bool, max_results: int,
) -> tuple[str, list[dict[str, Any]] | None]:
    if items:
        details = "\n".join(f"{i.type} #{i.id}: {_item_text(i)}" for i in items)
    else:
        details = "(no items)"
    if truncated:
        details = f"Showing the first {max_results} items; the board has more.\n\n{details}"
    tables = [{
        "headers": ["id", "type", "text", "frame"],
        "rows": [[i.id, i.type, _cell(_item_text(i)), i.frame_id] for i in items],
    }] if items else None
    return details, tables


def _item_id(value: str) -> str:
    try:
        return validate_item_id(value.strip() if isinstance(value, str) else value)
    except MiroClientError as exc:
        raise ValueError(str(exc)) from exc


def _parse_item_ids(value: Any) -> list[str]:
    message = f"item_ids must be 1 to {_MAX_ITEM_IDS} Miro item ids separated by commas."
    if not isinstance(value, str):
        raise ValueError(message)
    ids = [part.strip() for part in value.split(",")]
    if not 1 <= len(ids) <= _MAX_ITEM_IDS or not all(i.isascii() and i.isdigit() for i in ids):
        raise ValueError(message)
    return ids


def _plain(html_text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html_text)).split())


def _cell(value: Any) -> str:
    if value is None:
        text = ""
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = f"{value:g}"
    else:
        text = _plain(str(value))
    return text if len(text) <= _CELL_LIMIT else text[:_CELL_LIMIT] + "…"


def _item_text(item: MiroItem) -> str:
    return _plain(item.content or item.title)
