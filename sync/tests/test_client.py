from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx2
import pytest
from mcp.types import CallToolResult, TextContent

from privacyfence_sync import client as client_mod
from privacyfence_sync.client import (
    RecipeRuntimeError,
    SignInError,
    SyncClient,
    SyncError,
    ToolDenied,
    ToolFailed,
    connect_token,
)


def _result(text: str = "", *, structured: Any = None, is_error: bool = False) -> CallToolResult:
    content = [TextContent(type="text", text=text)] if text else []
    return CallToolResult(content=content, structuredContent=structured, isError=is_error)


def _client(*results: CallToolResult) -> tuple[SyncClient, AsyncMock]:
    client = SyncClient("http://x/mcp", None, {})
    session = AsyncMock()
    session.call_tool.side_effect = list(results)
    client._session = session
    return client, session.call_tool


def test_error_classes_share_a_base() -> None:
    for cls in (ToolFailed, ToolDenied, SignInError, RecipeRuntimeError):
        assert issubclass(cls, SyncError)


async def test_call_prefers_structured_content() -> None:
    client, call_tool = _client(_result('{"a": 1}', structured={"b": 2}))
    assert await client.call("t", {"x": 1}) == {"b": 2}
    call_tool.assert_awaited_once_with("t", {"x": 1})


async def test_call_parses_json() -> None:
    client, _ = _client(_result('{"a": 1}'))
    assert await client.call("t", {}) == {"a": 1}


async def test_call_returns_text_that_is_not_json() -> None:
    client, _ = _client(_result("plain words"))
    assert await client.call("t", {}) == "plain words"


async def test_call_raises_tool_denied_on_request_denied() -> None:
    client, _ = _client(_result("Request denied by policy", is_error=True))
    with pytest.raises(ToolDenied, match="Request denied"):
        await client.call("t", {})


async def test_call_raises_tool_failed_on_other_errors() -> None:
    client, _ = _client(_result("boom", is_error=True))
    with pytest.raises(ToolFailed, match="boom"):
        await client.call("t", {})


async def test_call_outside_async_with_is_a_programming_error() -> None:
    with pytest.raises(RuntimeError):
        await SyncClient("http://x/mcp", None, {}).call("t", {})


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real = httpx2.AsyncClient

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        return real(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(client_mod.httpx2, "AsyncClient", factory)


async def test_download_sends_no_authorization(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=b"data")

    _mock_http(monkeypatch, handler)
    client = connect_token("http://x/mcp", "secret")
    dest = tmp_path / "f.bin"
    await client.download("http://x/dl/abc", dest, hashlib.sha256(b"data").hexdigest())
    assert dest.read_bytes() == b"data"
    assert "authorization" not in seen[0].headers

    await client.download("http://x/dl/abc", dest, None)


async def test_download_checks_sha256(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mock_http(monkeypatch, lambda request: httpx2.Response(200, content=b"data"))
    dest = tmp_path / "f.bin"
    with pytest.raises(ToolFailed, match="sha256"):
        await SyncClient("http://x/mcp", None, {}).download("http://x/dl/abc", dest, "0" * 64)
    assert not dest.exists()


async def test_upload_puts_to_the_slot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    _mock_http(monkeypatch, handler)
    path = tmp_path / "out.csv"
    path.write_bytes(b"a,b\n")
    client, call_tool = _client(_result(structured={"upload_url": "http://x/up/1", "upload_id": "u1"}))
    assert await client.upload(path, "why") == "u1"
    call_tool.assert_awaited_once_with(
        "privacyfence_create_upload_slot", {"filename": "out.csv", "size_bytes": 4, "reason": "why"}
    )
    assert seen[0].method == "PUT"
    assert str(seen[0].url) == "http://x/up/1"
    assert seen[0].content == b"a,b\n"
    assert "authorization" not in seen[0].headers


class _Ctx:
    def __init__(self, value: Any, log: list[str], name: str) -> None:
        self.value, self.log, self.name = value, log, name

    async def __aenter__(self) -> Any:
        self.log.append(f"enter {self.name}")
        return self.value

    async def __aexit__(self, *exc: object) -> None:
        self.log.append(f"exit {self.name}")


def _patch_transport(monkeypatch: pytest.MonkeyPatch, log: list[str], *, init_fails: bool = False) -> None:
    session = AsyncMock()
    if init_fails:
        session.initialize.side_effect = ValueError("nope")
    seen: dict[str, Any] = {}

    def fake_stream(url: str, *, http_client: Any) -> _Ctx:
        seen["url"], seen["headers"] = url, http_client.headers
        return _Ctx(("r", "w"), log, "stream")

    monkeypatch.setattr(client_mod, "streamable_http_client", fake_stream)
    monkeypatch.setattr(client_mod, "ClientSession", lambda r, w: _Ctx(session, log, "session"))
    monkeypatch.setattr(client_mod, "_seen", seen, raising=False)


async def test_context_manager_opens_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _patch_transport(monkeypatch, log)
    async with connect_token("http://x/mcp", "tok") as client:
        assert client._session is not None
    assert client._session is None
    assert log == ["enter stream", "enter session", "exit session", "exit stream"]
    assert client_mod._seen["url"] == "http://x/mcp"  # type: ignore[attr-defined]
    assert client_mod._seen["headers"]["authorization"] == "Bearer tok"  # type: ignore[attr-defined]
    await client.__aexit__()


async def test_failed_initialize_closes_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    _patch_transport(monkeypatch, log, init_fails=True)
    with pytest.raises(ValueError):
        await connect_token("http://x/mcp", "tok").__aenter__()
    assert log[-2:] == ["exit session", "exit stream"]
