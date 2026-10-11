"""OAuth sign-in for organization servers: token storage, the loopback callback and the provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .client import SignInError, SyncError

# The redirect URI must match the registered one exactly (PrivacyFence does no port-agnostic
# loopback matching), so the port is fixed and stored with the client registration.
DEFAULT_CALLBACK_PORT = 33418
CALLBACK_TIMEOUT_SECONDS = 300.0
_SIGNED_IN_PAGE = b"Signed in to PrivacyFence. You can close this tab."
_FAILED_PAGE = b"Sign-in failed. Return to the terminal."


def login_hint(server_url: str) -> str:
    return f"Not signed in to {server_url}, or the sign-in expired. Run: privacyfence-sync login --server {server_url}"


def storage_path(server_url: str) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    digest = hashlib.sha256(server_url.encode("utf-8")).hexdigest()[:16]
    return Path(base) / "privacyfence-sync" / f"{digest}.json"


class FileTokenStorage:
    """The SDK's TokenStorage over one JSON file, written atomically with mode 0o600."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


class _CallbackServer(HTTPServer):
    result: AuthorizationCodeResult | None = None
    error: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802  (the http.server hook name)
        url = urlparse(self.path)
        if url.path != "/callback":
            self._reply(404, b"Not found")
            return
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        if "code" not in query:
            self.server.error = query.get("error_description") or query.get("error") or "no code in the callback"
            self._reply(400, _FAILED_PAGE)
            return
        self.server.result = AuthorizationCodeResult(code=query["code"], state=query.get("state"), iss=query.get("iss"))
        self._reply(200, _SIGNED_IN_PAGE)

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None


def _bind_callback_server(port: int) -> _CallbackServer:
    try:
        return _CallbackServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        raise SyncError(f"cannot listen on 127.0.0.1:{port} for the sign-in ({exc}); pass --callback-port") from exc


def _wait_for_callback(server: _CallbackServer, timeout: float) -> AuthorizationCodeResult:
    deadline = time.monotonic() + timeout
    try:
        while server.result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SyncError("No sign-in arrived within 5 minutes.")
            server.timeout = remaining
            server.handle_request()
            if server.error is not None:
                raise SyncError(f"Sign-in failed: {server.error}")
        return server.result
    finally:
        server.server_close()


class _InteractiveSignIn:
    def __init__(self, callback_port: int, timeout: float = CALLBACK_TIMEOUT_SECONDS) -> None:
        self._port = callback_port
        self._timeout = timeout
        self._server: _CallbackServer | None = None

    async def redirect_handler(self, url: str) -> None:
        # Listen before the browser opens, so a fast redirect cannot beat the server.
        self._server = _bind_callback_server(self._port)
        print(f"Open this page to sign in: {url}", flush=True)
        webbrowser.open(url)

    async def callback_handler(self) -> AuthorizationCodeResult:
        server, self._server = self._server, None
        if server is None:
            raise SyncError("the sign-in callback was requested before the sign-in page")
        return await asyncio.to_thread(_wait_for_callback, server, self._timeout)


def oauth_provider(
    server_url: str, storage: FileTokenStorage, *, interactive: bool, callback_port: int
) -> OAuthClientProvider:
    metadata = OAuthClientMetadata(
        client_name="privacyfence-sync",
        redirect_uris=[f"http://127.0.0.1:{callback_port}/callback"],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # nosec B106  # a public loopback client holds no secret
    )
    if interactive:
        flow = _InteractiveSignIn(callback_port)
        return OAuthClientProvider(
            server_url,
            metadata,
            storage,
            redirect_handler=flow.redirect_handler,
            callback_handler=flow.callback_handler,
        )

    async def refuse_redirect(url: str) -> None:
        raise SignInError(login_hint(server_url))

    async def refuse_callback() -> AuthorizationCodeResult:
        raise SignInError(login_hint(server_url))

    return OAuthClientProvider(
        server_url, metadata, storage, redirect_handler=refuse_redirect, callback_handler=refuse_callback
    )
