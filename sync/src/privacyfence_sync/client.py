"""MCP session, tool calls and file transfer: the only code that talks to the daemon."""

from __future__ import annotations

import hashlib
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
from mcp.types import TextContent

if TYPE_CHECKING:
    from .auth import FileTokenStorage

_TRANSFER_TIMEOUT = 120.0
_DENIED_PREFIX = "Request denied"


class SyncError(Exception):
    """Base of every error privacyfence-sync raises on purpose."""


class ToolFailed(SyncError):
    """A tool call or file transfer failed."""


class ToolDenied(SyncError):
    """The daemon denied the request (policy or the user said no)."""


class SignInError(SyncError):
    """Not signed in, sign-in expired, or no token."""


class RecipeRuntimeError(SyncError):
    """A reference in a recipe failed at run time."""


class SyncClient:
    def __init__(self, server_url: str, auth: httpx2.Auth | None, headers: dict[str, str]) -> None:
        self._server_url = server_url
        self._auth = auth
        self._headers = headers
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> SyncClient:
        stack = AsyncExitStack()
        try:
            # A client handed to the transport gets none of the MCP timeouts; httpx's own 5 s read
            # timeout would cut a tool call that is silent for longer (a 10 MB Drive download).
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    auth=self._auth,
                    headers=self._headers,
                    timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
                )
            )
            read, write = await stack.enter_async_context(streamable_http_client(self._server_url, http_client=http))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session
        return self

    async def __aexit__(self, *exc: object) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("SyncClient used outside `async with`")
        return self._session

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        result = await self._require_session().call_tool(tool, args)
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
        if result.is_error:
            if text.startswith(_DENIED_PREFIX):
                raise ToolDenied(text)
            raise ToolFailed(text)
        if result.structured_content is not None:
            return result.structured_content
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def download(self, url: str, dest: Path, expected_sha256: str | None) -> None:
        # The URL is the credential (ADR 0028), so this client carries no Authorization header.
        async with httpx2.AsyncClient(timeout=_TRANSFER_TIMEOUT) as http:
            response = await http.get(url)
            response.raise_for_status()
        data = response.content
        if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ToolFailed("downloaded file does not match the sha256 the tool reported")
        dest.write_bytes(data)

    async def upload(self, path: Path, reason: str) -> str:
        slot = await self.call(
            "privacyfence_create_upload_slot",
            {"filename": path.name, "size_bytes": path.stat().st_size, "reason": reason},
        )
        async with httpx2.AsyncClient(timeout=_TRANSFER_TIMEOUT) as http:
            response = await http.put(slot["upload_url"], content=path.read_bytes())
            response.raise_for_status()
        return str(slot["upload_id"])


def connect_token(server_url: str, token: str) -> SyncClient:
    return SyncClient(server_url, None, {"Authorization": f"Bearer {token}"})


def connect_oauth(
    server_url: str, storage: FileTokenStorage, *, interactive: bool = False, callback_port: int | None = None
) -> SyncClient:
    from .auth import DEFAULT_CALLBACK_PORT, oauth_provider  # auth.py imports this module's errors

    provider = oauth_provider(
        server_url,
        storage,
        interactive=interactive,
        callback_port=DEFAULT_CALLBACK_PORT if callback_port is None else callback_port,
    )
    return SyncClient(server_url, provider, {})
