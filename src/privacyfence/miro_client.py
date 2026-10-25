"""Miro REST v2 client.

The token comes from ``miro_auth``. The client never follows a redirect and
never retries a write (ADR 0105): the one repeat is after an HTTP 401, with a
refreshed token, and Miro ran nothing on a 401. It logs board ids and counts
only, never a token or item content, and calls only the endpoints PrivacyFence
needs.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from .miro_auth import MiroClientError, MiroTokenProvider

__all__ = [
    "BOARD_ID_RE",
    "MiroAccessDenied",
    "MiroBoard",
    "MiroClient",
    "MiroClientError",
    "MiroConnectorLine",
    "MiroItem",
    "MiroNotFound",
    "MiroTag",
    "validate_board_id",
    "validate_item_id",
]

logger = logging.getLogger(__name__)

API_BASE = "https://api.miro.com"
MAX_PAGES = 10
MAX_ITEMS = 500

BOARD_ID_RE = re.compile(r"[A-Za-z0-9_\-=+/]{4,64}")
ITEM_ID_RE = re.compile(r"[0-9]{1,20}")

_TIMEOUT = 30
_DETAIL_LIMIT = 200
_PAGE_SIZE = 50
_MAX_TAGS = 200

_TYPE_ENDPOINT = {
    "sticky_note": "sticky_notes",
    "card": "cards",
    "text": "texts",
    "shape": "shapes",
    "frame": "frames",
}

_UNAUTHORIZED = (
    "Miro refused the sign-in (HTTP 401). Use Authenticate… in PrivacyFence Settings to sign in again."
)
_FORBIDDEN = (
    "Miro refused the request (HTTP 403). Your Miro account, or the team PrivacyFence was signed in to, "
    "has no access to it."
)
_NOT_FOUND = "Miro found no such board or item (HTTP 404)."


class MiroNotFound(MiroClientError):
    """HTTP 404: no such board or item."""


class MiroAccessDenied(MiroClientError):
    """HTTP 403: the account or team has no access."""


@dataclass
class MiroBoard:
    id: str
    name: str
    description: str
    team: str
    created_at: str
    modified_at: str
    view_link: str


@dataclass
class MiroTag:
    id: str
    title: str
    color: str


@dataclass
class MiroItem:
    id: str
    type: str
    content: str
    title: str
    description: str
    due_date: str
    shape: str
    color: str
    x: float | None
    y: float | None
    width: float | None
    height: float | None
    frame_id: str
    modified_at: str


@dataclass
class MiroConnectorLine:
    id: str
    start_item_id: str
    end_item_id: str
    shape: str
    captions: list[str] = field(default_factory=list)


def validate_board_id(value: str) -> str:
    """The board id, or MiroClientError: ids go into the URL path."""
    text = value.strip() if isinstance(value, str) else ""
    if not BOARD_ID_RE.fullmatch(text):
        raise MiroClientError(
            f"Not a Miro board id: {value!r}. Use the id from miro_list_boards, "
            "or the part of the board's address after /app/board/."
        )
    return text


def validate_item_id(value: str) -> str:
    """The item id, or MiroClientError."""
    if not isinstance(value, str) or not ITEM_ID_RE.fullmatch(value):
        raise MiroClientError(f"Not a Miro item id: {value!r}")
    return value


def _board_path(board_id: str) -> str:
    return f"/v2/boards/{quote(validate_board_id(board_id), safe='')}"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_board(raw: Any) -> MiroBoard:
    body = _as_dict(raw)
    return MiroBoard(
        id=_str(body.get("id")),
        name=_str(body.get("name")),
        description=_str(body.get("description")),
        team=_str(_as_dict(body.get("team")).get("name")),
        created_at=_str(body.get("createdAt")),
        modified_at=_str(body.get("modifiedAt")),
        view_link=_str(body.get("viewLink")),
    )


def _parse_tag(raw: Any) -> MiroTag:
    body = _as_dict(raw)
    return MiroTag(id=_str(body.get("id")), title=_str(body.get("title")), color=_str(body.get("fillColor")))


def _parse_item(raw: Any) -> MiroItem:
    body = _as_dict(raw)
    data = _as_dict(body.get("data"))
    style = _as_dict(body.get("style"))
    position = _as_dict(body.get("position"))
    geometry = _as_dict(body.get("geometry"))
    item_type = _str(body.get("type"))
    if item_type == "card":
        color = style.get("cardTheme")
    elif item_type == "text":
        color = style.get("color")
    else:
        color = style.get("fillColor")
    return MiroItem(
        id=_str(body.get("id")),
        type=item_type,
        content=_str(data.get("content")),
        title=_str(data.get("title")),
        description=_str(data.get("description")),
        due_date=_str(data.get("dueDate"))[:10],
        shape=_str(data.get("shape")),
        color=_str(color),
        x=_number(position.get("x")),
        y=_number(position.get("y")),
        width=_number(geometry.get("width")),
        height=_number(geometry.get("height")),
        frame_id=_str(_as_dict(body.get("parent")).get("id")),
        modified_at=_str(body.get("modifiedAt")),
    )


def _parse_connector(raw: Any) -> MiroConnectorLine:
    body = _as_dict(raw)
    captions = [_str(_as_dict(c).get("content")) for c in _as_list(body.get("captions"))]
    return MiroConnectorLine(
        id=_str(body.get("id")),
        start_item_id=_str(_as_dict(body.get("startItem")).get("id")),
        end_item_id=_str(_as_dict(body.get("endItem")).get("id")),
        shape=_str(body.get("shape")),
        captions=captions,
    )


class MiroClient:
    """Miro REST calls for one signed-in person and team."""

    def __init__(self, provider: MiroTokenProvider) -> None:
        self._provider = provider
        self._session = requests.Session()
        self.team_name = ""

    def __repr__(self) -> str:
        return "MiroClient()"

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def _send(
        self, method: str, path: str, token: str, params: dict[str, str] | None, json_body: Any,
    ) -> requests.Response:
        try:
            return self._session.request(
                method,
                API_BASE + path,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise MiroClientError(f"Could not reach Miro: {type(exc).__name__}") from None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        resp = self._send(method, path, self._provider.access_token(), params, json_body)
        if resp.status_code == 401:
            resp = self._send(
                method, path, self._provider.access_token(force_refresh=True), params, json_body,
            )
        status = resp.status_code
        logger.debug("Miro %s -> HTTP %s", method, status)
        if 300 <= status < 400:
            raise MiroClientError(f"Miro answered with a redirect (HTTP {status}).")
        if status == 401:
            raise MiroClientError(_UNAUTHORIZED)
        if status == 403:
            raise MiroAccessDenied(_FORBIDDEN)
        if status == 404:
            raise MiroNotFound(_NOT_FOUND)
        if status == 429:
            raise MiroClientError(_rate_limit_message(resp))
        if status >= 400:
            detail = "no detail"
            try:
                body = resp.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("message"):
                detail = str(body["message"])[:_DETAIL_LIMIT]
            raise MiroClientError(f"Miro API error (HTTP {status}): {detail}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise MiroClientError(
                f"Miro answered with something other than JSON (HTTP {status})."
            ) from None

    def check_connection(self) -> str:
        """Prove the token works; returns the team name ("" when Miro will not say)."""
        try:
            body = _as_dict(self._request("GET", "/v1/oauth-token"))
        except MiroAccessDenied:
            self._request("GET", "/v2/boards", params={"limit": "1"})
            self.team_name = ""
        else:
            self.team_name = _str(_as_dict(body.get("team")).get("name"))
        return self.team_name

    # ------------------------------------------------------------------ #
    # Paging
    # ------------------------------------------------------------------ #

    def _offset_pages(self, path: str, base: dict[str, str], max_results: int) -> list[Any]:
        rows: list[Any] = []
        offset = 0
        for _ in range(MAX_PAGES):
            if len(rows) >= max_results:
                break
            body = _as_dict(self._request(
                "GET", path, params={**base, "limit": str(_PAGE_SIZE), "offset": str(offset)},
            ))
            page = _as_list(body.get("data"))
            rows.extend(page)
            offset += len(page)
            total = body.get("total")
            if not page or (isinstance(total, int) and offset >= total):
                break
        return rows[:max_results]

    def _cursor_pages(
        self, path: str, base: dict[str, str], max_results: int,
    ) -> tuple[list[Any], bool]:
        rows: list[Any] = []
        cursor = ""
        pages = 0
        while pages < MAX_PAGES and len(rows) < max_results:
            params = {**base, "limit": str(_PAGE_SIZE)}
            if cursor:
                params["cursor"] = cursor
            body = _as_dict(self._request("GET", path, params=params))
            pages += 1
            rows.extend(_as_list(body.get("data")))
            cursor = _str(body.get("cursor"))
            if not cursor:
                break
        truncated = bool(cursor) or len(rows) > max_results
        return rows[:max_results], truncated

    # ------------------------------------------------------------------ #
    # Boards and tags
    # ------------------------------------------------------------------ #

    def list_boards(self, query: str, max_results: int) -> list[MiroBoard]:
        base = {"query": query} if query else {}
        rows = self._offset_pages("/v2/boards", base, max_results)
        logger.info("Miro: %d board(s)", len(rows))
        return [_parse_board(row) for row in rows]

    def get_board(self, board_id: str) -> MiroBoard:
        return _parse_board(self._request("GET", _board_path(board_id)))

    def update_board(
        self, board_id: str, *, name: str | None, description: str | None,
    ) -> MiroBoard:
        path = _board_path(board_id)
        body: dict[str, str] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        return _parse_board(self._request("PATCH", path, json_body=body))

    def list_tags(self, board_id: str) -> list[MiroTag]:
        rows = self._offset_pages(f"{_board_path(board_id)}/tags", {}, _MAX_TAGS)
        return [_parse_tag(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Items
    # ------------------------------------------------------------------ #

    def list_items(
        self, board_id: str, *, item_type: str, frame_id: str, max_results: int,
    ) -> tuple[list[MiroItem], bool]:
        path = f"{_board_path(board_id)}/items"
        base: dict[str, str] = {}
        if item_type:
            base["type"] = item_type
        if frame_id:
            base["parent_item_id"] = validate_item_id(frame_id)
        rows, truncated = self._cursor_pages(path, base, min(max_results, MAX_ITEMS))
        logger.info("Miro: %d item(s) from board %s", len(rows), board_id)
        return [_parse_item(row) for row in rows], truncated

    def get_item(self, board_id: str, item_id: str) -> MiroItem:
        path = _board_path(board_id)
        validate_item_id(item_id)
        item = _parse_item(self._request("GET", f"{path}/items/{item_id}"))
        endpoint = _TYPE_ENDPOINT.get(item.type)
        if endpoint is None:
            return item
        return _parse_item(self._request("GET", f"{path}/{endpoint}/{item_id}"))

    def list_connectors(
        self, board_id: str, *, max_results: int,
    ) -> tuple[list[MiroConnectorLine], bool]:
        path = f"{_board_path(board_id)}/connectors"
        rows, truncated = self._cursor_pages(path, {}, min(max_results, MAX_ITEMS))
        return [_parse_connector(row) for row in rows], truncated

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def create_items(self, board_id: str, bodies: list[dict[str, Any]]) -> list[MiroItem]:
        path = _board_path(board_id)
        body = _as_dict(self._request("POST", f"{path}/items/bulk", json_body=bodies))
        return [_parse_item(row) for row in _as_list(body.get("data"))]

    def create_frame(self, board_id: str, body: dict[str, Any]) -> MiroItem:
        path = _board_path(board_id)
        return _parse_item(self._request("POST", f"{path}/frames", json_body=body))

    def create_connector(self, board_id: str, body: dict[str, Any]) -> str:
        path = _board_path(board_id)
        created = _as_dict(self._request("POST", f"{path}/connectors", json_body=body))
        connector_id = _str(created.get("id"))
        if not connector_id:
            raise MiroClientError("Miro answered without the id of the new connector line.")
        return connector_id

    def update_item(
        self, board_id: str, item_type: str, item_id: str, body: dict[str, Any],
    ) -> None:
        path = _board_path(board_id)
        validate_item_id(item_id)
        endpoint = _TYPE_ENDPOINT.get(item_type)
        if endpoint is None:
            raise MiroClientError(f"Miro items of type {item_type} cannot be changed here.")
        self._request("PATCH", f"{path}/{endpoint}/{item_id}", json_body=body)

    def set_parent(self, board_id: str, item_id: str, frame_id: str) -> None:
        path = _board_path(board_id)
        validate_item_id(item_id)
        if frame_id:
            validate_item_id(frame_id)
        self._request("PATCH", f"{path}/items/{item_id}", json_body={"parent": {"id": frame_id}})
        # Miro answers 200 to a parent change it ignored, so read the item back.
        if self.get_item(board_id, item_id).frame_id != frame_id:
            raise MiroClientError(f"Miro answered the move of item {item_id} but did not make it.")


def _rate_limit_message(resp: requests.Response) -> str:
    try:
        wait = max(1, int(resp.headers["X-RateLimit-Reset"]) - int(time.time()))
    except (KeyError, TypeError, ValueError):
        return "Miro's rate limit was reached. Try again in a minute."
    return f"Miro's rate limit was reached. Try again in {wait} seconds."
