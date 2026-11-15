"""A real single-user organization server for the daemon-level integration tests.

``single_user_org_server(tmp_path)`` starts the same in-process ``WebServer`` the organization
daemon starts with no identity provider (single-user mode): a real uvicorn server on a free
loopback port, host ``localhost``, its data directory under ``tmp_path``. Nothing here starts a
daemon process. ``.bearer()`` mints an access token in-process through ``OrgOAuthProvider``, with
no scopes, so every tool is visible to it.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from privacyfence import paths as paths_module
from privacyfence.connector import Connector
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.org_session import OrgSessionStore
from privacyfence.web.server import OrgAuth, WebServer
from privacyfence.web_approval_ui import WebApprovalUI

SINGLE_USER = Principal(id="single-user", email="single-user@localhost", email_verified=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_connectable(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05)
    raise TimeoutError(f"{host}:{port} never became connectable") from last_exc


@dataclass
class SingleUserOrgServer:
    server: WebServer
    provider: OrgOAuthProvider
    web_ui: WebApprovalUI
    dispatcher: McpDispatcher
    sessions: OrgSessionStore
    base_url: str
    mcp_url: str
    audit_dir: Path

    def bearer(self, scope: str = "full") -> str:
        """An access token for the one person, minted in-process. ``"full"`` carries no scope
        restriction, so every tool is visible; nothing else is supported here."""
        if scope != "full":
            raise ValueError(f"unsupported scope {scope!r}: the harness mints only full tokens")
        return self.provider._mint_tokens(
            client_id="integration-test", scopes=[], resource=None, principal=SINGLE_USER,
        ).access_token

    def session_cookie(self, principal: Principal = SINGLE_USER) -> str:
        """A signed-in browser session id (it doubles as the CSRF token)."""
        return self.sessions.create(principal, email_verified=True)


@contextmanager
def single_user_org_server(
    tmp_path: Path,
    *,
    connectors: Callable[[], dict[str, Connector]] | None = None,
    web_ui: WebApprovalUI | None = None,
    dispatcher: McpDispatcher | None = None,
) -> Iterator[SingleUserOrgServer]:
    """Run a single-user org server whose state lives under ``tmp_path``.

    Pass ``connectors`` (a provider returning ``{name: connector}``) to have the harness build
    the org-mode dispatcher, or a ready ``dispatcher``.
    """
    data = tmp_path / "pf-data"
    data.mkdir(parents=True, exist_ok=True)
    web_ui = web_ui or WebApprovalUI()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("NO_PROXY", "localhost,127.0.0.1")
        mp.setenv("no_proxy", "localhost,127.0.0.1")
        mp.setattr(paths_module, "data_dir", lambda: data)
        mp.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(data / "oauth_clients.json"))
        mp.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(data / "oauth_refresh.json"))
        port = free_port()
        issuer_url = f"http://localhost:{port}"
        provider = OrgOAuthProvider(
            None, idp_callback_url=f"{issuer_url}/oauth/idp/callback", issuer_url=issuer_url, single_user=True,
        )
        provided = connectors if connectors is not None else (lambda: {})
        if dispatcher is None:
            dispatcher = McpDispatcher(provided)
        registry = ConnectorRegistry(factory=lambda principal: [])
        sessions = OrgSessionStore()
        org = OrgAuth(
            provider=provider, sessions=sessions, idp=None, issuer_url=issuer_url,
            connector_registry=registry,
        )
        server = WebServer(web_ui, host="localhost", port=port, org=org, mcp_dispatcher=dispatcher)
        server.start()
        try:
            wait_until_connectable("localhost", port)
            assert server.mcp_url is not None
            yield SingleUserOrgServer(
                server=server, provider=provider, web_ui=web_ui, dispatcher=dispatcher, sessions=sessions,
                base_url=issuer_url, mcp_url=server.mcp_url,
                audit_dir=paths_module.user_dir(SINGLE_USER) / "logs" / "audit",
            )
        finally:
            server.stop()


__all__ = ["SINGLE_USER", "SingleUserOrgServer", "free_port", "single_user_org_server", "wait_until_connectable"]
