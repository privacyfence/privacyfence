"""Miro connector: lists the boards a Miro account can open and each board's tags without a
card, reads a board's items and connector lines behind a review card, and adds items and connector
lines behind an approval card. The client is built by the daemon from an OAuth sign-in (see
``miro_auth``)."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
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


_CREATE_MAX = 20
_CONTENT_LIMIT = 6000
_TITLE_LIMIT = 500
_CAPTION_LIMIT = 200
_SIZE_MAX = 100000
STICKY_COLORS = (
    "gray", "light_yellow", "yellow", "orange", "light_green", "green", "dark_green", "cyan",
    "light_pink", "pink", "violet", "red", "light_blue", "blue", "dark_blue", "black",
)
FILL_COLORS = (
    "#f5f6f8", "#d5f692", "#d0e17a", "#93d275", "#67c6c0", "#23bfe7", "#a6ccf5", "#7b92ff",
    "#fff9b1", "#f5d128", "#ff9d48", "#f16c7f", "#ea94bb", "#ffcee0", "#b384bb", "#000000",
)
MIRO_SHAPES = (
    "rectangle", "round_rectangle", "circle", "triangle", "rhombus", "parallelogram", "trapezoid",
    "pentagon", "hexagon", "octagon", "wedge_round_rectangle_callout", "star",
    "flow_chart_predefined_process", "cloud", "cross", "can", "right_arrow", "left_arrow",
    "left_right_arrow", "left_brace", "right_brace",
)
_STICKY_SHAPES = ("square", "rectangle")
_CONNECTOR_SHAPES = ("straight", "elbowed", "curved")
_CREATE_KEYS: dict[str, frozenset[str]] = {
    "sticky_note": frozenset({"type", "x", "y", "color", "content", "shape", "width", "height",
                              "frame_id"}),
    "card": frozenset({"type", "x", "y", "color", "title", "description", "due_date", "width",
                       "height", "frame_id"}),
    "text": frozenset({"type", "x", "y", "color", "content", "width", "frame_id"}),
    "shape": frozenset({"type", "x", "y", "color", "content", "shape", "width", "height",
                        "frame_id"}),
    "frame": frozenset({"type", "x", "y", "color", "title", "width", "height"}),
}
_CONNECTOR_KEYS = frozenset({"start_item_id", "end_item_id", "shape", "caption", "color"})
_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ITEMS_MESSAGE = (
    "items must be a JSON array of 1 to 20 objects, each with a type of sticky_note, card, text, "
    "shape or frame."
)
_CONNECTORS_MESSAGE = (
    'connectors must be a JSON array of 1 to 20 {"start_item_id": ..., "end_item_id": ...} '
    "objects."
)
_ITEMS_DESCRIPTION = (
    "A JSON array of 1 to 20 items, each {\"type\": sticky_note|card|text|shape|frame, ...}. Keys "
    "by type: sticky_note content, shape (square or rectangle), width or height, frame_id; card "
    "title, description, due_date (YYYY-MM-DD), width, height, frame_id; text content (required), "
    "width, frame_id; shape content, shape (rectangle, round_rectangle, circle, triangle, "
    "rhombus, star, cloud, ...), width, height, frame_id; frame title, width, height. Every type "
    "takes x, y and color: a sticky_note color is a name such as yellow, light_blue or green; a "
    "shape or frame color is one of Miro's 16 fill colors such as #fff9b1; a card or text color "
    "is any #RRGGBB. Inside a frame x and y are measured from the frame's top-left corner. "
    "Content may use simple HTML such as <p> and <strong>. Example: [{\"type\": \"sticky_note\", "
    "\"content\": \"Idea\", \"color\": \"yellow\", \"x\": 0, \"y\": 0}]."
)
_CONNECTORS_DESCRIPTION = (
    "A JSON array of 1 to 20 lines, each {\"start_item_id\": ..., \"end_item_id\": ...} with "
    "optional shape (straight, elbowed or curved), caption (at most 200 characters) and color "
    "(#RRGGBB). The two ends are different existing items that are not frames. Example: "
    "[{\"start_item_id\": \"3458764549483493025\", \"end_item_id\": \"3458764549483493026\", "
    "\"caption\": \"leads to\"}]."
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
            ToolSpec(
                name="miro_create_items",
                description=(
                    "Add sticky notes, cards, texts, shapes or frames to one Miro board, up to "
                    "20 in one request. Returns {board_id, created: [{id, type}]}. Use "
                    "miro_update_items to change items that already exist. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("board_id", "str", description=_BOARD_ID_DESCRIPTION),
                    ToolParam("items", "str", description=_ITEMS_DESCRIPTION),
                    _REASON,
                ],
                read_only=False,
            ),
            ToolSpec(
                name="miro_create_connectors",
                description=(
                    "Draw connector lines between existing items on one Miro board, up to 20 in "
                    "one request. Returns {board_id, created_ids}. Use miro_list_connectors to "
                    "read the lines already there. Requires user approval."
                ),
                params=[
                    ToolParam("board_id", "str", description=_BOARD_ID_DESCRIPTION),
                    ToolParam("connectors", "str", description=_CONNECTORS_DESCRIPTION),
                    _REASON,
                ],
                read_only=False,
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
        if tool == "miro_create_items":
            return await self._create_items(**args)
        if tool == "miro_create_connectors":
            return await self._create_connectors(**args)
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
            item = await self._get_item(board_id, item_id)
            if item is None:
                missing.append(item_id)
            else:
                items.append(item)
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
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_items(self, board_id: str, items: str) -> Any:
        board_id = _board_id(board_id)
        specs = _parse_items(items)
        board = await self._board_info(board_id)
        frames: dict[str, MiroItem] = {}
        for frame_id in dict.fromkeys(s["frame_id"] for s in specs if s.get("frame_id")):
            frame = await self._get_item(board_id, frame_id)
            if frame is None or frame.type != "frame":
                raise ValueError(f"Item {frame_id} is not a frame on this board.")
            frames[frame_id] = frame
        bodies = [_item_body(s) for s in specs]
        rows = [
            [
                s["type"], _cell(s.get("content") or s.get("title") or ""),
                f"{s.get('x', 0):g}, {s.get('y', 0):g}",
                _size_cell(s.get("width"), s.get("height")),
                _cell(frames[s["frame_id"]].title) if s.get("frame_id") else "",
                s.get("color", ""),
            ]
            for s in specs
        ]
        await gated_call(
            connector=self.name,
            tool="miro_create_items",
            tool_name="Add Miro Items",
            summary=f"Add {len(specs)} item(s) to {board.name}",
            sender=board.name,
            raw_data={"board_id": board_id, "items": bodies},
            filtered_data=None,
            gate="popup",
            preview={
                "Board": board.name,
                "Team": self._client.team_name or _NO_TEAM,
                "Items": str(len(specs)),
            },
            preview_tables=[{
                "headers": ["Type", "Text", "Position", "Size", "Frame", "Color"],
                "rows": rows,
            }],
            details_text=json.dumps(bodies, indent=2, ensure_ascii=False),
            my_email="",
            args={"board_id": board_id},
        )
        has_frames = any(s["type"] == "frame" for s in specs)
        frame_ids: list[str] = []
        created_frames: list[MiroItem] = []
        created_rest: list[MiroItem] = []
        try:
            for spec, body in zip(specs, bodies, strict=True):
                if spec["type"] == "frame":
                    frame = await asyncio.to_thread(self._client.create_frame, board_id, body)
                    created_frames.append(frame)
                    frame_ids.append(frame.id)
            rest = [b for s, b in zip(specs, bodies, strict=True) if s["type"] != "frame"]
            if rest:
                created_rest = await asyncio.to_thread(self._client.create_items, board_id, rest)
        except MiroClientError as exc:
            logger.warning("Miro request failed: %s", exc)
            if not has_frames:
                raise RuntimeError(str(exc)) from exc
            raise RuntimeError(
                f"{exc} Frames created before the error: {', '.join(frame_ids) or 'none'}."
            ) from exc
        frame_iter, rest_iter = iter(created_frames), iter(created_rest)
        created = [next(frame_iter if s["type"] == "frame" else rest_iter, None) for s in specs]
        logger.info("Miro items added to board %s: %d", board_id, len(specs))
        return {
            "board_id": board_id,
            "created": [{"id": i.id, "type": i.type} for i in created if i is not None],
        }

    async def _create_connectors(self, board_id: str, connectors: str) -> Any:
        board_id = _board_id(board_id)
        specs = _parse_connectors(connectors)
        board = await self._board_info(board_id)
        ends: dict[str, MiroItem] = {}
        missing: list[str] = []
        for end_id in dict.fromkeys(e for s in specs for e in (s["start_item_id"], s["end_item_id"])):
            end = await self._get_item(board_id, end_id)
            if end is None:
                missing.append(end_id)
            else:
                ends[end_id] = end
        if missing:
            raise ValueError(f"Item(s) not found on this board: {', '.join(sorted(missing))}.")
        if any(item.type == "frame" for item in ends.values()):
            raise ValueError("A connector cannot start or end at a frame.")
        bodies = [_connector_body(s) for s in specs]

        def end_cell(item_id: str) -> str:
            return f"{ends[item_id].type} {_cell(_item_text(ends[item_id]))[:60]}"

        await gated_call(
            connector=self.name,
            tool="miro_create_connectors",
            tool_name="Add Miro Connectors",
            summary=f"Draw {len(specs)} connector(s) on {board.name}",
            sender=board.name,
            raw_data={"board_id": board_id, "connectors": bodies},
            filtered_data=None,
            gate="popup",
            preview={
                "Board": board.name,
                "Team": self._client.team_name or _NO_TEAM,
                "Connectors": str(len(specs)),
            },
            preview_tables=[{
                "headers": ["From", "To", "Shape", "Caption"],
                "rows": [
                    [end_cell(s["start_item_id"]), end_cell(s["end_item_id"]),
                     s.get("shape", ""), _cell(s.get("caption", ""))]
                    for s in specs
                ],
            }],
            details_text=json.dumps(bodies, indent=2, ensure_ascii=False),
            my_email="",
            args={"board_id": board_id},
        )
        created_ids: list[str] = []
        try:
            for body in bodies:
                created_ids.append(
                    await asyncio.to_thread(self._client.create_connector, board_id, body)
                )
        except MiroClientError as exc:
            logger.warning("Miro request failed: %s", exc)
            raise RuntimeError(
                f"{exc} Connectors created before the error: {', '.join(created_ids) or 'none'}."
            ) from exc
        logger.info("Miro connectors added to board %s: %d", board_id, len(created_ids))
        return {"board_id": board_id, "created_ids": created_ids}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _get_item(self, board_id: str, item_id: str) -> MiroItem | None:
        try:
            return await asyncio.to_thread(self._client.get_item, board_id, item_id)
        except MiroNotFound:
            return None
        except MiroClientError as exc:
            logger.warning("Miro request failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

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


def _json_list(value: Any) -> list[Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= _CREATE_MAX:
        return None
    return parsed


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    )


def _text(raw: dict[str, Any], key: str, limit: int) -> None:
    if key not in raw:
        return
    if not isinstance(raw[key], str):
        raise ValueError(f"{key} must be text.")
    if len(raw[key]) > limit:
        raise ValueError(f"{key} is longer than {limit} characters.")


def _parse_items(value: Any) -> list[dict[str, Any]]:
    raw_items = _json_list(value)
    if raw_items is None:
        raise ValueError(_ITEMS_MESSAGE)
    return [_check_item(raw) for raw in raw_items]


def _check_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) \
            or raw["type"] not in _CREATE_KEYS:
        raise ValueError(_ITEMS_MESSAGE)
    kind = raw["type"]
    allowed = _CREATE_KEYS[kind]
    if set(raw) - allowed:
        raise ValueError(f"A {kind} takes only these keys: {', '.join(sorted(allowed))}.")
    if kind == "sticky_note" and "width" in raw and "height" in raw:
        raise ValueError("A sticky_note takes width or height, not both.")
    for key in ("x", "y"):
        if key in raw and not _number(raw[key]):
            raise ValueError("x and y must be numbers.")
    for key in ("width", "height"):
        if key in raw and not (_number(raw[key]) and 1 <= raw[key] <= _SIZE_MAX):
            raise ValueError(f"{key} must be a number from 1 to {_SIZE_MAX}.")
    _text(raw, "content", _CONTENT_LIMIT)
    _text(raw, "title", _TITLE_LIMIT)
    _text(raw, "description", _CONTENT_LIMIT)
    if kind == "text" and not str(raw.get("content", "")).strip():
        raise ValueError("A text needs non-empty content.")
    if "shape" in raw:
        if kind == "sticky_note":
            if raw["shape"] not in _STICKY_SHAPES:
                raise ValueError("A sticky_note shape must be square or rectangle.")
        elif raw["shape"] not in MIRO_SHAPES:
            raise ValueError(
                f"Unsupported shape {raw['shape']!r}. Use one of {', '.join(MIRO_SHAPES)}."
            )
    if "due_date" in raw:
        try:
            if not (isinstance(raw["due_date"], str) and _DATE_RE.fullmatch(raw["due_date"])):
                raise ValueError
            datetime.strptime(raw["due_date"], "%Y-%m-%d")
        except ValueError:
            raise ValueError("due_date must be a date written YYYY-MM-DD.") from None
    spec = dict(raw)
    if "color" in raw:
        spec["color"] = _check_color(kind, raw["color"])
    if "frame_id" in raw:
        spec["frame_id"] = _item_id(raw["frame_id"])
    return spec


def _check_color(kind: str, color: Any) -> str:
    if kind == "sticky_note":
        if color not in STICKY_COLORS:
            raise ValueError(f"A sticky_note color must be one of {', '.join(STICKY_COLORS)}.")
        return color
    if kind in ("shape", "frame"):
        if not isinstance(color, str) or color.lower() not in FILL_COLORS:
            raise ValueError(f"A {kind} color must be one of {', '.join(FILL_COLORS)}.")
        return color.lower()
    if not isinstance(color, str) or not _HEX_COLOR_RE.fullmatch(color):
        raise ValueError(f"A {kind} color must be a hex color such as #2D9BF0.")
    return color


def _item_body(spec: dict[str, Any]) -> dict[str, Any]:
    kind = spec["type"]
    data: dict[str, Any] = {}
    for key in ("content", "title", "description", "shape"):
        if key in spec:
            data[key] = spec[key]
    if "due_date" in spec:
        data["dueDate"] = f"{spec['due_date']}T12:00:00.000Z"
    if kind == "frame":
        data.update({"format": "custom", "type": "freeform"})
    style: dict[str, Any] = {}
    if "color" in spec:
        style[{"card": "cardTheme", "text": "color"}.get(kind, "fillColor")] = spec["color"]
    geometry = {k: spec[k] for k in ("width", "height") if k in spec}
    body: dict[str, Any] = {"type": kind}
    for name, group in (("data", data), ("style", style)):
        if group:
            body[name] = group
    body["position"] = {"x": spec.get("x", 0), "y": spec.get("y", 0)}
    if geometry:
        body["geometry"] = geometry
    if spec.get("frame_id"):
        body["parent"] = {"id": spec["frame_id"]}
    if kind == "frame":
        body.pop("type")
        body.pop("parent", None)
    return body


def _size_cell(width: Any, height: Any) -> str:
    if width is None and height is None:
        return ""
    side = lambda v: "-" if v is None else f"{v:g}"  # noqa: E731
    return f"{side(width)} × {side(height)}"


def _parse_connectors(value: Any) -> list[dict[str, Any]]:
    raw_lines = _json_list(value)
    if raw_lines is None or not all(isinstance(r, dict) for r in raw_lines):
        raise ValueError(_CONNECTORS_MESSAGE)
    return [_check_connector(raw) for raw in raw_lines]


def _check_connector(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) - _CONNECTOR_KEYS:
        raise ValueError(
            f"A connector takes only these keys: {', '.join(sorted(_CONNECTOR_KEYS))}."
        )
    if not all(isinstance(raw.get(k), str) for k in ("start_item_id", "end_item_id")):
        raise ValueError(_CONNECTORS_MESSAGE)
    spec = dict(raw)
    spec["start_item_id"] = _item_id(raw["start_item_id"])
    spec["end_item_id"] = _item_id(raw["end_item_id"])
    if spec["start_item_id"] == spec["end_item_id"]:
        raise ValueError("A connector must join two different items.")
    if "shape" in raw and raw["shape"] not in _CONNECTOR_SHAPES:
        raise ValueError("A connector shape must be straight, elbowed or curved.")
    if "color" in raw and not (
        isinstance(raw["color"], str) and _HEX_COLOR_RE.fullmatch(raw["color"])
    ):
        raise ValueError("A connector color must be a hex color such as #2D9BF0.")
    _text(raw, "caption", _CAPTION_LIMIT)
    return spec


def _connector_body(spec: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "startItem": {"id": spec["start_item_id"]}, "endItem": {"id": spec["end_item_id"]},
    }
    if "shape" in spec:
        body["shape"] = spec["shape"]
    if spec.get("caption"):
        body["captions"] = [{"content": spec["caption"]}]
    if "color" in spec:
        body["style"] = {"strokeColor": spec["color"]}
    return body
