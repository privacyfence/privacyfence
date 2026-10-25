"""Tests for the Miro REST client.

``requests.Session.request`` is replaced (no network), so every test sees the
exact method, URL, parameters, body and headers the client sends. The
invariants: redirects are never followed, a 401 triggers one forced refresh,
each status has its own message, an id is validated before any request, a
parent change is read back, and no token reaches a message or a repr.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
import requests

from privacyfence.miro_client import (
    BOARD_ID_RE,
    MAX_PAGES,
    MiroAccessDenied,
    MiroClient,
    MiroClientError,
    MiroNotFound,
    validate_board_id,
    validate_item_id,
)

pytestmark = pytest.mark.unit

BOARD = "uXjVOD6LSME="
BOARD_Q = "uXjVOD6LSME%3D"
ITEM = "3458764549483493025"
FRAME = "3458764549483493026"
OTHER = "3458764549483493027"
BASE = "https://api.miro.com"


class FakeProvider:
    def __init__(self) -> None:
        self.forced = 0

    def access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.forced += 1
        return "at-2" if self.forced else "at-1"


class FakeResponse:
    def __init__(
        self, status: int = 200, body: Any = None, *,
        content: bytes | None = None, headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        if content is not None:
            self.content = content
        else:
            self.content = b"" if body is None else json.dumps(body).encode()

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body


class Net:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: list[Any] = []
        monkeypatch.setattr(
            requests.Session, "request",
            lambda _session, method, url, **kw: self._request(method, url, **kw),
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def reply(self, *answers: Any) -> None:
        self.answers = list(answers)


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    return Net(monkeypatch)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(net: Net, provider: FakeProvider) -> MiroClient:
    return MiroClient(provider)  # type: ignore[arg-type]


def ok(body: Any) -> FakeResponse:
    return FakeResponse(200, body)


def sticky(item_id: str = ITEM, **extra: Any) -> dict[str, Any]:
    return {"id": item_id, "type": "sticky_note", "data": {"content": "hi", "shape": "square"}, **extra}


class TestIds:
    def test_board_id_with_equals_accepted(self) -> None:
        assert validate_board_id(f" {BOARD} ") == BOARD
        assert BOARD_ID_RE.fullmatch("a+b/c_d-e=")

    def test_board_link_rejected_naming_app_board(self) -> None:
        with pytest.raises(MiroClientError, match="/app/board/"):
            validate_board_id("https://miro.com/app/board/uXjVOD6LSME=/")

    @pytest.mark.parametrize("value", ["../x", "a b", "", "abc", "x" * 65, "a?b"])
    def test_board_id_rejected(self, value: str) -> None:
        with pytest.raises(MiroClientError, match="Not a Miro board id"):
            validate_board_id(value)

    def test_non_string_board_id_rejected(self) -> None:
        with pytest.raises(MiroClientError):
            validate_board_id(5)  # type: ignore[arg-type]

    def test_path_quoting_turns_slash_into_percent_2f(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}))
        client.get_board("ab/cd=")
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/ab%2Fcd%3D"

    def test_item_id_digits_only(self) -> None:
        assert validate_item_id(ITEM) == ITEM
        for bad in ("", "12a", "-1", "1" * 21, "../1"):
            with pytest.raises(MiroClientError, match="Not a Miro item id"):
                validate_item_id(bad)
        with pytest.raises(MiroClientError):
            validate_item_id(7)  # type: ignore[arg-type]


class TestRequestErrors:
    def test_request_shape(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"id": BOARD}))
        client.get_board(BOARD)
        call = net.calls[0]
        assert call["method"] == "GET"
        assert call["allow_redirects"] is False
        assert call["headers"] == {"Authorization": "Bearer at-1"}
        assert call["timeout"] == 30

    def test_network_error(self, client: MiroClient, net: Net) -> None:
        net.reply(requests.ConnectionError("secret-host at-1"))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == "Could not reach Miro: ConnectionError"

    def test_redirect(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(302, None))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == "Miro answered with a redirect (HTTP 302)."

    def test_401_then_success_after_one_forced_refresh(
        self, client: MiroClient, net: Net, provider: FakeProvider,
    ) -> None:
        net.reply(FakeResponse(401, {}), ok({"id": BOARD}))
        assert client.get_board(BOARD).id == BOARD
        assert provider.forced == 1
        assert [c["headers"]["Authorization"] for c in net.calls] == ["Bearer at-1", "Bearer at-2"]

    def test_401_twice(self, client: MiroClient, net: Net, provider: FakeProvider) -> None:
        net.reply(FakeResponse(401, {}))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == (
            "Miro refused the sign-in (HTTP 401). Use Authenticate… in PrivacyFence Settings to sign in again."
        )
        assert len(net.calls) == 2
        assert provider.forced == 1

    def test_403(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(403, {}))
        with pytest.raises(MiroAccessDenied) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == (
            "Miro refused the request (HTTP 403). Your Miro account, or the team PrivacyFence was signed in to, "
            "has no access to it."
        )

    def test_404(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(404, {}))
        with pytest.raises(MiroNotFound) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == "Miro found no such board or item (HTTP 404)."

    def test_429_with_reset_header(self, client: MiroClient, net: Net) -> None:
        reset = str(int(time.time()) + 30)
        net.reply(FakeResponse(429, {}, headers={"X-RateLimit-Reset": reset}))
        with pytest.raises(MiroClientError, match=r"^Miro's rate limit was reached\. Try again in (29|30|31) seconds\.$"):
            client.get_board(BOARD)

    def test_429_reset_in_the_past_waits_one_second(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(429, {}, headers={"X-RateLimit-Reset": "1"}))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == "Miro's rate limit was reached. Try again in 1 seconds."

    @pytest.mark.parametrize("headers", [{}, {"X-RateLimit-Reset": "soon"}])
    def test_429_without_usable_header(self, client: MiroClient, net: Net, headers: dict[str, str]) -> None:
        net.reply(FakeResponse(429, {}, headers=headers))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == "Miro's rate limit was reached. Try again in a minute."

    def test_other_error_with_detail_cut(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(400, {"message": "x" * 300}))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == f"Miro API error (HTTP 400): {'x' * 200}"

    @pytest.mark.parametrize("body", [None, {"other": 1}])
    def test_other_error_without_detail(self, client: MiroClient, net: Net, body: Any) -> None:
        net.reply(FakeResponse(500, body))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == "Miro API error (HTTP 500): no detail"

    def test_non_json_success(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(200, None, content=b"<html>"))
        with pytest.raises(MiroClientError) as exc:
            client.get_board(BOARD)
        assert str(exc.value) == "Miro answered with something other than JSON (HTTP 200)."

    def test_empty_success_body_is_none(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(204, None))
        assert client.update_item(BOARD, "text", ITEM, {}) is None

    def test_no_token_in_any_message_or_repr(self, client: MiroClient, net: Net) -> None:
        for response in (FakeResponse(401, {}), FakeResponse(403, {}), FakeResponse(500, {"message": "boom"})):
            net.reply(response)
            with pytest.raises(MiroClientError) as exc:
                client.get_board(BOARD)
            assert "at-1" not in str(exc.value)
            assert "at-2" not in str(exc.value)
        assert "at-" not in repr(client)


class TestCheckConnection:
    def test_team_name(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"team": {"name": "Acme"}}))
        assert client.check_connection() == "Acme"
        assert client.team_name == "Acme"
        assert net.calls[0]["method"] == "GET"
        assert net.calls[0]["url"] == f"{BASE}/v1/oauth-token"

    def test_403_falls_back_to_boards(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(403, {}), ok({"data": []}))
        assert client.check_connection() == ""
        assert client.team_name == ""
        assert net.calls[1]["url"] == f"{BASE}/v2/boards"
        assert net.calls[1]["params"] == {"limit": "1"}

    def test_other_failure_propagates(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(500, {"message": "down"}))
        with pytest.raises(MiroClientError, match="down"):
            client.check_connection()


class TestListBoards:
    def test_parses_and_sends_query(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{
            "id": BOARD, "name": "Plan", "description": "d", "team": {"name": "Acme"},
            "owner": {"name": "Pat"}, "createdAt": "c", "modifiedAt": "m", "viewLink": "https://miro.com/x",
        }], "total": 1}))
        boards = client.list_boards("plan", 10)
        assert net.calls[0]["params"] == {"query": "plan", "limit": "50", "offset": "0"}
        assert boards[0].name == "Plan"
        assert boards[0].team == "Acme"
        assert boards[0].view_link == "https://miro.com/x"
        assert not hasattr(boards[0], "owner")

    def test_empty_query_not_sent(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [], "total": 0}))
        assert client.list_boards("", 10) == []
        assert "query" not in net.calls[0]["params"]

    def test_stops_at_max_results(self, client: MiroClient, net: Net) -> None:
        page = [{"id": f"id{i:04d}"} for i in range(50)]
        net.reply(ok({"data": page, "total": 500}))
        assert len(client.list_boards("", 60)) == 60
        assert [c["params"]["offset"] for c in net.calls] == ["0", "50"]

    def test_stops_at_max_pages(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{"id": "abcd"}] * 50, "total": 100000}))
        assert len(client.list_boards("", 10000)) == 50 * MAX_PAGES
        assert len(net.calls) == MAX_PAGES

    def test_stops_when_total_reached(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{"id": "abcd"}] * 3, "total": 3}))
        assert len(client.list_boards("", 100)) == 3
        assert len(net.calls) == 1

    def test_stops_on_empty_page(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": []}))
        assert client.list_boards("", 100) == []
        assert len(net.calls) == 1


class TestGetBoard:
    def test_parses(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"id": BOARD, "name": "Plan", "owner": {"name": "Pat"}, "team": {"name": "Acme"}}))
        board = client.get_board(BOARD)
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}"
        assert (board.name, board.team) == ("Plan", "Acme")
        assert not hasattr(board, "owner")

    def test_missing_keys_give_empty_strings(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}))
        board = client.get_board(BOARD)
        assert (board.id, board.name, board.description, board.team) == ("", "", "", "")
        assert (board.created_at, board.modified_at, board.view_link) == ("", "", "")

    def test_non_object_answer(self, client: MiroClient, net: Net) -> None:
        net.reply(ok([1]))
        assert client.get_board(BOARD).id == ""

    def test_bad_id_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.get_board("a b")
        assert net.calls == []


class TestUpdateBoard:
    def test_only_given_keys(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"id": BOARD, "name": "New"}))
        board = client.update_board(BOARD, name="New", description=None)
        call = net.calls[0]
        assert call["method"] == "PATCH"
        assert call["url"] == f"{BASE}/v2/boards/{BOARD_Q}"
        assert call["json"] == {"name": "New"}
        assert board.name == "New"

    def test_description_only(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}))
        client.update_board(BOARD, name=None, description="")
        assert net.calls[0]["json"] == {"description": ""}

    def test_never_sends_policy_team_or_project(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}))
        client.update_board(BOARD, name="a", description="b")
        assert set(net.calls[0]["json"]) == {"name", "description"}

    def test_bad_id_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.update_board("..", name="a", description=None)
        assert net.calls == []


class TestListTags:
    def test_parses(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{"id": "1", "title": "Urgent", "fillColor": "red"}, {"id": "2"}], "total": 2}))
        tags = client.list_tags(BOARD)
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}/tags"
        assert net.calls[0]["params"] == {"limit": "50", "offset": "0"}
        assert (tags[0].id, tags[0].title, tags[0].color) == ("1", "Urgent", "red")
        assert (tags[1].title, tags[1].color) == ("", "")

    def test_capped_at_200(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{"id": "t"}] * 50, "total": 1000}))
        assert len(client.list_tags(BOARD)) == 200

    def test_bad_id_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.list_tags("")
        assert net.calls == []


class TestListItems:
    def test_params_only_when_non_empty(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": []}))
        client.list_items(BOARD, item_type="", frame_id="", max_results=10)
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}/items"
        assert net.calls[0]["params"] == {"limit": "50"}
        client.list_items(BOARD, item_type="card", frame_id=FRAME, max_results=10)
        assert net.calls[1]["params"] == {"type": "card", "parent_item_id": FRAME, "limit": "50"}

    def test_cursor_paging(self, client: MiroClient, net: Net) -> None:
        net.reply(
            ok({"data": [sticky("1")] * 50, "cursor": "c1"}),
            ok({"data": [sticky("2")] * 5}),
        )
        items, truncated = client.list_items(BOARD, item_type="", frame_id="", max_results=100)
        assert len(items) == 55
        assert truncated is False
        assert "cursor" not in net.calls[0]["params"]
        assert net.calls[1]["params"]["cursor"] == "c1"

    def test_truncated_when_cursor_remains_at_max_results(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [sticky()] * 50, "cursor": "c1"}))
        items, truncated = client.list_items(BOARD, item_type="", frame_id="", max_results=50)
        assert (len(items), truncated) == (50, True)
        assert len(net.calls) == 1

    def test_truncated_when_page_holds_more_than_max_results(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [sticky()] * 50}))
        items, truncated = client.list_items(BOARD, item_type="", frame_id="", max_results=20)
        assert (len(items), truncated) == (20, True)

    def test_truncated_at_max_pages(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [sticky()] * 50, "cursor": "more"}))
        items, truncated = client.list_items(BOARD, item_type="", frame_id="", max_results=100000)
        assert len(net.calls) == MAX_PAGES
        assert truncated is True
        assert len(items) == 500

    def test_item_field_mapping_sticky_note(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{
            "id": ITEM, "type": "sticky_note", "data": {"content": "<p>hi</p>", "shape": "square"},
            "position": {"x": 1, "y": 2.5}, "geometry": {"width": 199, "height": 228},
            "parent": {"id": FRAME}, "modifiedAt": "2026-01-01T00:00:00Z",
            "style": {"fillColor": "yellow"},
        }]}))
        (item,), _ = client.list_items(BOARD, item_type="", frame_id="", max_results=10)
        assert item.id == ITEM
        assert (item.type, item.content, item.shape) == ("sticky_note", "<p>hi</p>", "square")
        assert (item.x, item.y, item.width, item.height) == (1.0, 2.5, 199.0, 228.0)
        assert item.frame_id == FRAME
        assert item.modified_at == "2026-01-01T00:00:00Z"

    def test_generic_answer_has_no_color(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [sticky()]}))
        (item,), _ = client.list_items(BOARD, item_type="", frame_id="", max_results=10)
        assert item.color == ""

    def test_card_fields_and_due_date_cut(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{
            "id": ITEM, "type": "card",
            "data": {"title": "T", "description": "D", "dueDate": "2026-03-04T10:00:00.000Z"},
        }]}))
        (item,), _ = client.list_items(BOARD, item_type="card", frame_id="", max_results=10)
        assert (item.title, item.description, item.due_date) == ("T", "D", "2026-03-04")

    def test_missing_keys_and_odd_numbers(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{"id": ITEM, "position": {"x": True, "y": "3"}}]}))
        (item,), _ = client.list_items(BOARD, item_type="", frame_id="", max_results=10)
        assert (item.type, item.content, item.frame_id, item.due_date) == ("", "", "", "")
        assert (item.x, item.y, item.width, item.height) == (None, None, None, None)

    def test_bad_ids_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.list_items("a b", item_type="", frame_id="", max_results=10)
        with pytest.raises(MiroClientError):
            client.list_items(BOARD, item_type="", frame_id="../x", max_results=10)
        assert net.calls == []


class TestGetItem:
    @pytest.mark.parametrize(("item_type", "endpoint", "style", "color"), [
        ("sticky_note", "sticky_notes", {"fillColor": "yellow"}, "yellow"),
        ("card", "cards", {"cardTheme": "#2fa3f7"}, "#2fa3f7"),
        ("text", "texts", {"color": "#1a1a1a"}, "#1a1a1a"),
        ("shape", "shapes", {"fillColor": "#ff0000"}, "#ff0000"),
        ("frame", "frames", {"fillColor": "#ffffff"}, "#ffffff"),
    ])
    def test_per_type_get_carries_the_color(
        self, client: MiroClient, net: Net, item_type: str, endpoint: str, style: dict[str, str], color: str,
    ) -> None:
        net.reply(
            ok({"id": ITEM, "type": item_type, "data": {}}),
            ok({"id": ITEM, "type": item_type, "data": {}, "style": style}),
        )
        item = client.get_item(BOARD, ITEM)
        assert [c["url"] for c in net.calls] == [
            f"{BASE}/v2/boards/{BOARD_Q}/items/{ITEM}",
            f"{BASE}/v2/boards/{BOARD_Q}/{endpoint}/{ITEM}",
        ]
        assert item.color == color

    def test_image_makes_only_the_generic_get(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"id": ITEM, "type": "image", "data": {}}))
        item = client.get_item(BOARD, ITEM)
        assert len(net.calls) == 1
        assert (item.type, item.color) == ("image", "")

    def test_404(self, client: MiroClient, net: Net) -> None:
        net.reply(FakeResponse(404, {}))
        with pytest.raises(MiroNotFound):
            client.get_item(BOARD, ITEM)

    def test_bad_ids_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.get_item("a b", ITEM)
        with pytest.raises(MiroClientError):
            client.get_item(BOARD, "x")
        assert net.calls == []


class TestListConnectors:
    def test_parses_and_pages(self, client: MiroClient, net: Net) -> None:
        net.reply(
            ok({"data": [{
                "id": "9", "startItem": {"id": ITEM}, "endItem": {"id": FRAME}, "shape": "curved",
                "captions": [{"content": "yes"}, {"content": "no"}],
            }] * 50, "cursor": "c"}),
            ok({"data": [{"id": "10"}]}),
        )
        lines, truncated = client.list_connectors(BOARD, max_results=100)
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}/connectors"
        assert net.calls[1]["params"]["cursor"] == "c"
        assert truncated is False
        assert len(lines) == 51
        assert (lines[0].start_item_id, lines[0].end_item_id) == (ITEM, FRAME)
        assert (lines[0].shape, lines[0].captions) == ("curved", ["yes", "no"])
        assert (lines[50].start_item_id, lines[50].shape, lines[50].captions) == ("", "", [])

    def test_truncated(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({"data": [{"id": "9"}] * 50, "cursor": "c"}))
        lines, truncated = client.list_connectors(BOARD, max_results=50)
        assert (len(lines), truncated) == (50, True)

    def test_bad_id_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.list_connectors("", max_results=5)
        assert net.calls == []


class TestCreateItems:
    def test_one_bulk_post(self, client: MiroClient, net: Net) -> None:
        bodies = [{"type": "sticky_note", "data": {"content": "a"}}, {"type": "text", "data": {"content": "b"}}]
        net.reply(ok({"data": [sticky("1"), sticky("2")]}))
        items = client.create_items(BOARD, bodies)
        assert len(net.calls) == 1
        assert net.calls[0]["method"] == "POST"
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}/items/bulk"
        assert net.calls[0]["json"] == bodies
        assert [i.id for i in items] == ["1", "2"]

    def test_bad_id_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.create_items("a b", [{}])
        assert net.calls == []


class TestCreateFrame:
    def test_one_post(self, client: MiroClient, net: Net) -> None:
        body = {"data": {"title": "F", "format": "custom", "type": "freeform"}}
        net.reply(ok({"id": FRAME, "type": "frame", "data": {"title": "F"}}))
        item = client.create_frame(BOARD, body)
        assert net.calls[0]["method"] == "POST"
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}/frames"
        assert net.calls[0]["json"] == body
        assert (item.id, item.title) == (FRAME, "F")

    def test_bad_id_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.create_frame("..", {})
        assert net.calls == []


class TestCreateConnector:
    def test_returns_id(self, client: MiroClient, net: Net) -> None:
        body = {"startItem": {"id": ITEM}, "endItem": {"id": OTHER}}
        net.reply(ok({"id": "777"}))
        assert client.create_connector(BOARD, body) == "777"
        assert net.calls[0]["method"] == "POST"
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}/connectors"
        assert net.calls[0]["json"] == body

    def test_answer_without_id(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}))
        with pytest.raises(MiroClientError, match="without the id"):
            client.create_connector(BOARD, {})

    def test_bad_id_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.create_connector("", {})
        assert net.calls == []


class TestUpdateItem:
    @pytest.mark.parametrize(("item_type", "endpoint"), [
        ("sticky_note", "sticky_notes"), ("card", "cards"), ("text", "texts"),
        ("shape", "shapes"), ("frame", "frames"),
    ])
    def test_endpoint_per_type(self, client: MiroClient, net: Net, item_type: str, endpoint: str) -> None:
        net.reply(ok({}))
        client.update_item(BOARD, item_type, ITEM, {"data": {"content": "x"}})
        assert net.calls[0]["method"] == "PATCH"
        assert net.calls[0]["url"] == f"{BASE}/v2/boards/{BOARD_Q}/{endpoint}/{ITEM}"
        assert net.calls[0]["json"] == {"data": {"content": "x"}}

    def test_image_type_refused_before_any_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError) as exc:
            client.update_item(BOARD, "image", ITEM, {})
        assert str(exc.value) == "Miro items of type image cannot be changed here."
        assert net.calls == []

    def test_bad_ids_before_request(self, client: MiroClient, net: Net) -> None:
        with pytest.raises(MiroClientError):
            client.update_item("a b", "text", ITEM, {})
        with pytest.raises(MiroClientError):
            client.update_item(BOARD, "text", "x", {})
        assert net.calls == []


class TestSetParent:
    def test_move_in(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}), ok(sticky(parent={"id": FRAME})), ok({**sticky(parent={"id": FRAME})}))
        client.set_parent(BOARD, ITEM, FRAME)
        patch = net.calls[0]
        assert patch["method"] == "PATCH"
        assert patch["url"] == f"{BASE}/v2/boards/{BOARD_Q}/items/{ITEM}"
        assert patch["json"] == {"parent": {"id": FRAME}}
        assert net.calls[1]["method"] == "GET"

    def test_move_out_sends_empty_id_never_null(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}), ok(sticky()))
        client.set_parent(BOARD, ITEM, "")
        assert net.calls[0]["json"] == {"parent": {"id": ""}}

    def test_move_in_that_did_not_happen(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}), ok(sticky(parent={"id": OTHER})))
        with pytest.raises(MiroClientError) as exc:
            client.set_parent(BOARD, ITEM, FRAME)
        assert str(exc.value) == f"Miro answered the move of item {ITEM} but did not make it."

    def test_move_out_that_did_not_happen(self, client: MiroClient, net: Net) -> None:
        net.reply(ok({}), ok(sticky(parent={"id": FRAME})))
        with pytest.raises(MiroClientError, match="did not make it"):
            client.set_parent(BOARD, ITEM, "")

    def test_bad_ids_before_request(self, client: MiroClient, net: Net) -> None:
        for args in (("a b", ITEM, FRAME), (BOARD, "x", FRAME), (BOARD, ITEM, "../x")):
            with pytest.raises(MiroClientError):
                client.set_parent(*args)
        assert net.calls == []
