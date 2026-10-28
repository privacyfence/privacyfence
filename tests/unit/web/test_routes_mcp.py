"""Tests for the /mcp Streamable HTTP endpoint (web/routes_mcp.py) --
the wire-protocol/auth layer sitting on top of McpDispatcher (see
test_mcp_dispatch.py for the dispatch logic itself).

Drives the real ASGI app with the official `mcp` Python client over an
in-process ASGI transport (httpx2.ASGITransport -- mcp 2.x's client
transports are written against httpx2, see pyproject.toml's test extra) --
no real socket. This is
the in-process counterpart of tests/integration/test_mcp_daemon_contract.py,
without spawning a real process.
"""
from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import anyio
import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.connection import Connection

from privacyfence import local_files, scoped_tokens
from privacyfence.agent_identity import UNKNOWN_AGENT
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.principal import LOCAL_PRINCIPAL, current_principal
from privacyfence.policy import scope_rows
from privacyfence.policy.engine import PolicyRule
from privacyfence.scoped_tokens import ScopedToken, ScopedTokenStore
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.mcp_tools import META_TOOL_NAMES
from privacyfence.web import routes_mcp as rm
from privacyfence.web.routes_mcp import SERVER_INSTRUCTIONS, build_mcp_asgi_app, mcp_lifespan


class EchoConnector(Connector):
    """A minimal real connector -- exercises manifest -> tool listing ->
    tool-call dispatch end to end, mirroring
    test_bridge_daemon_contract.py's own EchoConnector."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "echo"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="echo_say",
                description="Echoes its arguments back.",
                params=[ToolParam("message", "str", required=True)],
                read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        return {"echoed": args}


TOKEN = "mcp-test-token"


class _OrgVerifier(TokenVerifier):
    """Stands in for web/oauth_provider.py's OrgOAuthProvider: mints a token
    carrying a real ``subject``, which is what makes
    mcp_auth.principal_from_access_token resolve a signed-in human rather
    than LOCAL_PRINCIPAL. Local mode's PerUserTokenVerifier sets it to LOCAL_PRINCIPAL.id by default."""

    def __init__(self, principal_id: str) -> None:
        self._principal_id = principal_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != TOKEN:
            return None
        return AccessToken(
            token=token, client_id="claude-desktop", scopes=[], subject=self._principal_id,
        )


@contextlib.asynccontextmanager
async def _connected_session(
    dispatcher: McpDispatcher, *, token: str = TOKEN, verifier: TokenVerifier | None = None,
    headers: dict[str, str] | None = None,
):
    """Builds the /mcp app for ``dispatcher`` and yields a live, initialized
    ClientSession against it -- the happy-path fixture every wire-level test
    below starts from.

    ``verifier`` swaps local mode's PerUserTokenVerifier for an org-mode one
    (see _OrgVerifier), so a test can drive this surface as a signed-in
    human rather than as LOCAL_PRINCIPAL."""
    if verifier is not None:
        app, session_manager = build_mcp_asgi_app(dispatcher, verifier=verifier)
    else:
        app, session_manager = build_mcp_asgi_app(dispatcher, token=token)
    transport = httpx2.ASGITransport(app=app)

    async with mcp_lifespan(session_manager):
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}", **(headers or {})},
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


def _raw_client(dispatcher: McpDispatcher, *, token: str = TOKEN) -> httpx.AsyncClient:
    app, _session_manager = build_mcp_asgi_app(dispatcher, token=token)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@contextlib.asynccontextmanager
async def _raw_client_on_a_running_app(dispatcher: McpDispatcher, *, token: str = TOKEN):
    """Like ``_raw_client``, but with the session manager's own lifespan
    running -- needed by anything that drives a request far enough for a
    session to actually be opened for it (``_raw_client`` alone is only good
    for requests rejected before that, like the auth tests above)."""
    app, session_manager = build_mcp_asgi_app(dispatcher, token=token)
    async with mcp_lifespan(session_manager):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            yield client


class ReadWriteDeleteConnector(Connector):
    """One tool of each kind ADR 0086's truthful annotations tell apart."""

    @property
    def name(self) -> str:
        return "rwd"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="rwd_read", description="Reads.", read_only=True),
            ToolSpec(name="rwd_write", description="Writes."),
            ToolSpec(name="rwd_delete", description="Deletes.", destructive=True),
        ]

    async def call(self, tool: str, args: dict) -> object:
        return {}


def _dispatcher(connectors: dict[str, Connector] | None = None, **kwargs) -> McpDispatcher:
    store = dict(connectors or {})
    kwargs.setdefault("scoped_token_policy", scoped_tokens.policy_from_org_config({}, org_mode=False))
    return McpDispatcher(lambda: store, **kwargs)


_INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
_PING_BODY = {"jsonrpc": "2.0", "id": 0, "method": "ping"}
# What a Streamable HTTP client sends on every POST; without them the
# transport stops at a 406/415 long before session handling is reached.
_WIRE_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


# --------------------------------------------------------------------------- #
# Auth -- audience separation (ADR 0061) starts here: no credential but the
# right bearer token gets past this layer at all.
# --------------------------------------------------------------------------- #

class TestAuth:
    async def test_missing_bearer_token_is_rejected(self):
        async with _raw_client(_dispatcher()) as client:
            resp = await client.post("/mcp", json=_INIT_BODY)
        assert resp.status_code == 401
        assert "Bearer" in resp.headers.get("www-authenticate", "")

    async def test_wrong_bearer_token_is_rejected(self):
        async with _raw_client(_dispatcher()) as client:
            resp = await client.post("/mcp", json=_INIT_BODY, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    async def test_correct_bearer_token_is_accepted(self):
        async with _connected_session(_dispatcher()):
            pass  # ClientSession.initialize() succeeding is the assertion.

    def test_build_mcp_asgi_app_requires_a_token_or_a_verifier(self):
        # build_mcp_asgi_app takes an alternative to `token` (`verifier`,
        # for web/oauth_provider.py's OrgOAuthProvider) -- calling it with
        # neither is a caller bug, not a runtime condition to silently
        # tolerate.
        with pytest.raises(ValueError):
            build_mcp_asgi_app(_dispatcher())


# --------------------------------------------------------------------------- #
# Server instructions -- the wire-level counterpart of
# tests/integration/test_mcp_daemon_contract.py's real-socket assertion.
# --------------------------------------------------------------------------- #

class TestServerInstructions:
    async def test_initialize_result_carries_the_instructions(self):
        dispatcher = _dispatcher()
        app, session_manager = build_mcp_asgi_app(dispatcher, token=TOKEN)
        async with mcp_lifespan(session_manager):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http_client,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        result = await session.initialize()
        assert result.instructions == SERVER_INSTRUCTIONS
        # Named concretely, per the status tool's own docstring -- the
        # instructions are what tells a client the tool exists and why to
        # call it, not just that an empty tool list means "not set up".
        assert "privacyfence_status" in result.instructions


class TestToolsListChangedCapability:
    """StreamableHTTPSessionManager drives every session
    with ``init_options=None``, so the runner answering initialize falls back
    to Server.create_initialization_options() with no arguments, and
    NotificationOptions()'s own tools_changed=False default is what a real
    client would see without _PrivacyFenceServer's override -- this is what
    proves that override actually reaches a real initialize() response."""

    async def test_initialize_result_advertises_tools_list_changed(self):
        dispatcher = _dispatcher()
        app, session_manager = build_mcp_asgi_app(dispatcher, token=TOKEN)
        async with mcp_lifespan(session_manager):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http_client,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        result = await session.initialize()
        assert result.capabilities.tools is not None
        assert result.capabilities.tools.list_changed is True

    async def test_build_mcp_server_wires_a_tools_changed_broadcaster(self):
        # build_mcp_server (called by build_mcp_asgi_app above) is the one
        # place that actually owns the live-Connection registry
        # notify_tools_changed() needs -- confirm it registers itself on
        # the dispatcher rather than leaving notify_tools_changed() a
        # permanent no-op.
        dispatcher = _dispatcher()
        assert dispatcher._tools_changed_broadcaster is None
        build_mcp_asgi_app(dispatcher, token=TOKEN)
        assert dispatcher._tools_changed_broadcaster is not None

    def test_broadcast_with_no_running_loop_is_a_silent_no_op(self):
        # notify_tools_changed() can, in principle, be called from a
        # background thread with no asyncio loop of its own (see
        # _broadcast_tools_changed's own comment) -- this is a plain, non-
        # async test specifically so there is no running loop here either.
        dispatcher = _dispatcher()
        build_mcp_asgi_app(dispatcher, token=TOKEN)
        dispatcher.notify_tools_changed()  # must not raise

    async def test_broadcast_calls_send_tool_list_changed_on_the_captured_session(self, monkeypatch):
        # A full send-to-a-real-client round trip (message actually
        # observed via ClientSession's own message_handler) is what
        # tests/integration/test_mcp_daemon_contract.py proves, over a real
        # socket where a persistent server-push stream is unambiguous. This
        # unit-level test instead confirms the piece that's actually this
        # module's own responsibility: the captured Connection's
        # send_tool_list_changed() is awaited at all once
        # notify_tools_changed() fires.
        calls = []

        async def _record(self, **_kwargs):
            calls.append(self)

        monkeypatch.setattr(Connection, "send_tool_list_changed", _record)

        dispatcher = _dispatcher()
        app, session_manager = build_mcp_asgi_app(dispatcher, token=TOKEN)
        async with mcp_lifespan(session_manager):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http_client,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        # Captures this session's live Connection
                        # server-side (see build_mcp_server's own comment).
                        await session.list_tools()

                        dispatcher.notify_tools_changed()

                        for _ in range(50):
                            if calls:
                                break
                            await anyio.sleep(0.02)

        assert len(calls) == 1

    async def test_one_sessions_send_failure_does_not_stop_the_broadcast(self, monkeypatch):
        # A session that's gone stale/closing must not take the whole
        # broadcast down with it -- _send_tool_list_changed's own try/except
        # is what this proves.
        async def _raise(self, **_kwargs):
            raise RuntimeError("session is closing")

        monkeypatch.setattr(Connection, "send_tool_list_changed", _raise)

        dispatcher = _dispatcher()
        app, session_manager = build_mcp_asgi_app(dispatcher, token=TOKEN)
        async with mcp_lifespan(session_manager):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http_client,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await session.list_tools()

                        dispatcher.notify_tools_changed()  # must not raise
                        await anyio.sleep(0.05)  # let the scheduled task actually run


# --------------------------------------------------------------------------- #
# Tool listing
# --------------------------------------------------------------------------- #

class TestListTools:
    async def test_lists_every_connector_tool_and_every_meta_tool(self):
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.list_tools()
        names = {t.name for t in result.tools}
        assert "echo_say" in names
        assert META_TOOL_NAMES <= names

    async def test_reflects_a_live_connector_set_change_between_calls(self):
        store: dict[str, Connector] = {}
        dispatcher = McpDispatcher(
            lambda: store, scoped_token_policy=scoped_tokens.policy_from_org_config({}, org_mode=False),
        )
        async with _connected_session(dispatcher) as session:
            first = await session.list_tools()
            assert "echo_say" not in {t.name for t in first.tools}
            store["echo"] = EchoConnector()
            second = await session.list_tools()
            assert "echo_say" in {t.name for t in second.tools}

    async def test_org_mode_lists_the_signed_in_principals_own_connectors(self):
        """Regression: handle_list_tools ran outside principal_scope, so
        ``dispatcher.connectors`` -- which in org mode is
        ``connector_registry.get(current_principal()).connectors`` --
        resolved against LOCAL_PRINCIPAL instead of the signed-in human.

        On a real org server nobody authorizes services as "local", so
        build_connectors() skipped every connector for that principal and
        the advertised manifest collapsed to META_TOOLS alone: every
        connector tool was invisible to org-mode clients, even though
        handle_call_tool (correctly scoped all along) could resolve those
        same connectors fine.

        The existing tests above could not catch this -- their provider is
        a plain ``lambda: store``, identical for every principal. This one
        makes the provider principal-sensitive, the way org mode's really
        is.
        """
        per_principal: dict[str, dict[str, Connector]] = {
            "alice": {"echo": EchoConnector()},
            LOCAL_PRINCIPAL.id: {},  # an org server's local principal: nothing authorized
        }
        dispatcher = McpDispatcher(
            lambda: per_principal.get(current_principal().id, {}),
            scoped_token_policy=scoped_tokens.policy_from_org_config({}, org_mode=False),
        )

        async with _connected_session(dispatcher, verifier=_OrgVerifier("alice")) as session:
            result = await session.list_tools()

        names = {t.name for t in result.tools}
        assert "echo_say" in names, "signed-in principal's connector tools must be advertised"
        assert META_TOOL_NAMES <= names

    async def test_org_mode_does_not_advertise_the_local_principals_connectors(self):
        """The same scoping, in the other direction, and the sharper half of
        the regression: the local principal here *does* own a connector, so
        an unscoped handle_list_tools would advertise it to bob. Bob has
        none of his own and must be shown none -- a signed-in human must
        never be offered tools backed by someone else's credentials."""
        per_principal: dict[str, dict[str, Connector]] = {
            LOCAL_PRINCIPAL.id: {"echo": EchoConnector()},
            "bob": {},
        }
        dispatcher = McpDispatcher(
            lambda: per_principal.get(current_principal().id, {}),
            scoped_token_policy=scoped_tokens.policy_from_org_config({}, org_mode=False),
        )

        async with _connected_session(dispatcher, verifier=_OrgVerifier("bob")) as session:
            result = await session.list_tools()

        names = {t.name for t in result.tools}
        assert "echo_say" not in names, "must not advertise another principal's connectors"
        assert META_TOOL_NAMES <= names


# --------------------------------------------------------------------------- #
# Calling a connector tool
# --------------------------------------------------------------------------- #

class TestCallConnectorTool:
    async def test_dispatches_to_the_connector_and_returns_structured_content(self):
        connector = EchoConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("echo_say", {"message": "hi"})
        assert result.is_error is False
        assert result.structured_content == {"echoed": {"message": "hi"}}
        assert connector.calls == [("echo_say", {"message": "hi"})]

    async def test_reason_is_popped_before_reaching_the_connector(self):
        connector = EchoConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with _connected_session(dispatcher) as session:
            await session.call_tool("echo_say", {"message": "hi", "reason": "because"})
        assert connector.calls == [("echo_say", {"message": "hi"})]

    async def test_unknown_tool_is_a_tool_error_not_a_transport_error(self):
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("not_a_real_tool", {})
        assert result.is_error is True

    async def test_connector_exception_is_a_tool_error(self):
        class BoomConnector(EchoConnector):
            async def call(self, tool: str, args: dict) -> object:
                raise ValueError("boom")

        dispatcher = _dispatcher({"echo": BoomConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("echo_say", {"message": "hi"})
        assert result.is_error is True
        assert "boom" in result.content[0].text

    async def test_two_calls_in_one_session_share_dedupe_state(self):
        # Same MCP session -> same session_key -> the second identical call
        # is served from the completed-result cache instead of re-running
        # the connector (retry coalescing, keyed on session_key).
        connector = EchoConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with _connected_session(dispatcher) as session:
            await session.call_tool("echo_say", {"message": "hi"})
            await session.call_tool("echo_say", {"message": "hi"})
        assert len(connector.calls) == 1


class TestFileBridgeOverTheWire:
    """ADR 0007 SS1.1's wire protocol, driven in process. The Node shim contract test
    (tests/integration/test_shim_mcp_contract.py) covers it end to end, but it skips
    itself when the shim cannot be built, and this module is under a 100% floor -- so
    the Python half must not depend on it."""

    async def test_a_connector_needing_local_files_gets_a_need_uploads_result(self):
        class NeedsFilesConnector(EchoConnector):
            async def call(self, tool: str, args: dict) -> object:
                raise local_files.LocalFilesNeeded(["~/a.pdf", "~/b.pdf"], max_bytes=1000)

        dispatcher = _dispatcher({"echo": NeedsFilesConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("echo_say", {"message": "hi"})
        assert result.is_error is False
        assert "~/a.pdf, ~/b.pdf" in result.content[0].text
        bridge = result.meta[local_files.META_KEY]
        assert bridge["v"] == 1
        assert bridge["op"] == "need_uploads"
        assert [f["path"] for f in bridge["files"]] == ["~/a.pdf", "~/b.pdf"]
        assert all(f["upload_path"] == f"/mcp-files/uploads/{f['slot']}" for f in bridge["files"])

    async def test_a_staged_delivery_rides_back_in_the_result_meta(self):
        delivery = {"dest_dir": "~/Downloads", "name": "report.pdf", "download_path": "/mcp-files/downloads/x"}

        class DeliversConnector(EchoConnector):
            async def call(self, tool: str, args: dict) -> object:
                local_files._current_state().pending_deliveries.append(delivery)
                return {"ok": True}

        dispatcher = _dispatcher({"echo": DeliversConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("echo_say", {"message": "hi"})
        assert result.structured_content == {"ok": True}
        assert result.meta[local_files.META_KEY] == {"v": 1, "op": "deliver", "files": [delivery]}

    async def test_the_second_round_uploads_map_reaches_the_connector(self):
        seen: list[dict[str, str]] = []

        class ReadsUploadsConnector(EchoConnector):
            async def call(self, tool: str, args: dict) -> object:
                seen.append(dict(local_files._current_state().uploads))
                return {}

        dispatcher = _dispatcher({"echo": ReadsUploadsConnector()})
        async with _connected_session(dispatcher) as session:
            await session.call_tool(
                "echo_say", {"message": "one"}, meta={local_files.META_KEY: {"uploads": {"~/a.pdf": "slot-a"}}},
            )
            # Malformed bridge metadata is ignored, never an error: a first-round call, or
            # a client that does not do the handshake at all, behaves as if it were absent.
            await session.call_tool("echo_say", {"message": "two"}, meta={local_files.META_KEY: "junk"})
            await session.call_tool(
                "echo_say", {"message": "three"}, meta={local_files.META_KEY: {"uploads": ["not", "a", "map"]}},
            )
        assert seen == [{"~/a.pdf": "slot-a"}, {}, {}]


# --------------------------------------------------------------------------- #
# Meta-tools -- one representative round trip per tool is enough here;
# test_mcp_dispatch.py already covers each one's own branch logic in depth.
# --------------------------------------------------------------------------- #

class TestMetaTools:
    async def test_check_policy_round_trips(self):
        # "gmail_list_messages" -- a real, globally-recognized (auto-gated)
        # tool name from auto_accept.TOOL_TO_GATE; check_policy looks that
        # up directly rather than through the connector's own tool_specs
        # (see mcp_dispatch.check_policy), so the connector fixture just has
        # to exist under this name, not actually expose that tool.
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool(
                "privacyfence_check_policy",
                {"connector": "echo", "tool": "gmail_list_messages", "reason": "planning"},
            )
        assert result.is_error is False
        assert result.structured_content["gate"] == "auto"

    async def test_begin_and_end_unattended_session_round_trip(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        async with _connected_session(dispatcher) as session:
            begin = await session.call_tool("privacyfence_begin_unattended_session", {"reason": "scheduled"})
            assert begin.structured_content == {"unattended": True}
            end = await session.call_tool("privacyfence_end_unattended_session", {"reason": "done"})
            assert end.structured_content == {"unattended": False}

    async def test_begin_unattended_session_disabled_is_a_tool_error(self):
        dispatcher = _dispatcher({})  # unattended_sessions_enabled defaults False
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_begin_unattended_session", {"reason": "x"})
        assert result.is_error is True
        assert "disabled" in result.content[0].text

    async def test_status_round_trips(self):
        # No provider wired here, so status() falls
        # back to reporting the one connector this dispatcher can actually
        # see (test_mcp_dispatch.py's TestStatus covers the provider-backed
        # shape in detail) -- this just proves the wire round trip reaches
        # McpDispatcher.status() at all.
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_status", {"reason": "planning"})
        assert result.is_error is False
        assert result.structured_content["mode"] == "local"
        assert result.structured_content["setup_complete"] is True

    async def test_create_upload_slot_round_trips(self, tmp_path, monkeypatch):
        # privacyfence_create_upload_slot mints a real
        # UploadStagingStore slot for the principal this session resolved
        # to (LOCAL_PRINCIPAL, same as every other meta-tool test here) and
        # returns a capability URL built from *this request's* own base
        # URL -- see local_files.build_upload_slot.
        from privacyfence import paths
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool(
                "privacyfence_create_upload_slot", {"filename": "report.pdf", "reason": "attach a file"},
            )
        assert result.is_error is False
        content = result.structured_content
        assert content["method"] == "PUT"
        assert content["upload_url"] == f"http://testserver/mcp-files/slots/{content['upload_id']}"
        assert content["upload_id"] in content["example"]

    async def test_create_upload_slot_is_listed_in_the_tool_manifest(self):
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            tools = await session.list_tools()
        assert "privacyfence_create_upload_slot" in {t.name for t in tools.tools}

    async def test_await_approval_round_trips_to_the_registry(self):
        # privacyfence_await_approval, reaching the same registry a real
        # deferred approval would have registered into. No registry wired
        # here (no WebApprovalUI in this fixture), so every id comes back
        # "unknown" -- the wire round trip is what this test proves, the
        # status vocabulary itself is test_mcp_dispatch.py's job.
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool(
                "privacyfence_await_approval", {"approval_ids": ["a1"], "timeout_seconds": 1},
            )
        assert result.is_error is False
        assert result.structured_content == {"a1": "unknown"}


# --------------------------------------------------------------------------- #
# Scoped tokens (ADR 0164): what a client holding one sees and may call
# --------------------------------------------------------------------------- #

class TwoToolConnector(EchoConnector):
    """``echo_say`` is in the test token's scope, ``echo_shout`` is not."""

    def tool_specs(self) -> list[ToolSpec]:
        return [*super().tool_specs(), ToolSpec(name="echo_shout", description="Shouts.", read_only=True)]


def _scoped_token(*, days: float = 42.5) -> ScopedToken:
    return ScopedToken(
        id="st-0123456789ab", label="sales-sync", digest="", principal_id=LOCAL_PRINCIPAL.id,
        principal_email="", principal_display_name="", created_at=time.time(),
        expires_at=time.time() + days * 86400,
        rules=tuple(
            row for rid in ("r-1", "r-2")
            for row in scope_rows.rows_for_rules([PolicyRule(
                id=rid, predicate="gmail.anything", value=None, operations=frozenset({"gmail.read_message"}),
            )], rid)
        ),
        tools=frozenset({"echo_say", "gmail_list_messages"}),
    )


class _OneTokenStore(ScopedTokenStore):
    """A store holding one token -- or none, once ``revoked`` is set."""

    def __init__(self, token: ScopedToken) -> None:
        super().__init__(lambda: [])
        self.token = token
        self.revoked = False

    def get(self, principal_id: str, token_id: str) -> ScopedToken | None:
        token = self.token
        if self.revoked or (principal_id, token_id) != (token.principal_id, token.id):
            return None
        return token


class _ScopedVerifier(TokenVerifier):
    """Like ``_OrgVerifier``, but mints the AccessToken mcp_auth.ScopedTokenVerifier gives a
    scoped token."""

    def __init__(self, token: ScopedToken) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != TOKEN:
            return None
        t = self._token
        return AccessToken(
            token=token, client_id=f"scoped:{t.id}", scopes=["scoped"], expires_at=int(t.expires_at),
            subject=t.principal_id,
            claims={"email": "", "display_name": "", "is_admin": False, "scoped_token_id": t.id},
        )


class TestProposeScopedTokenOverTheWire:
    @pytest.fixture(autouse=True)
    def _audit(self, tmp_path, monkeypatch):
        from privacyfence import paths
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path
        scoped_tokens.set_store(ScopedTokenStore(lambda: [LOCAL_PRINCIPAL]))

    def _audit_entries(self) -> list[dict]:
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    async def test_propose_scoped_token_is_listed_only_when_enabled(self):
        enabled = _dispatcher({"echo": EchoConnector()})
        disabled = _dispatcher(
            {"echo": EchoConnector()}, scoped_token_policy=scoped_tokens.ScopedTokenPolicy(True, 90, False),
        )
        unset = McpDispatcher(lambda: {})
        names = {}
        for key, dispatcher in (("enabled", enabled), ("disabled", disabled), ("unset", unset)):
            async with _connected_session(dispatcher) as session:
                names[key] = {t.name for t in (await session.list_tools()).tools}

        assert "privacyfence_propose_scoped_token" in names["enabled"]
        assert "privacyfence_propose_scoped_token" not in names["disabled"]
        assert "privacyfence_propose_scoped_token" not in names["unset"]
        assert names["enabled"] - {"privacyfence_propose_scoped_token"} == names["disabled"]

    async def test_propose_scoped_token_is_attributed_to_the_client(self):
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_propose_scoped_token", {
                "reason": "nightly", "label": "nightly", "tools": ["gmail_list_messages"],
            })

        assert result.is_error is False
        assert result.structured_content["review_url"].startswith("http://testserver/settings/scoped_tokens/proposals/stp-")
        [entry] = [e for e in self._audit_entries() if e["decision"] == "scoped_token_proposed"]
        assert entry["agent_id"] != UNKNOWN_AGENT.id and entry["agent_source"] == "client_info"

    async def test_other_meta_tools_stay_unattributed(self):
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            await session.call_tool(
                "privacyfence_check_policy",
                {"connector": "echo", "tool": "gmail_list_messages", "reason": "planning"},
            )

        [entry] = [e for e in self._audit_entries() if e["decision"] == "policy_check"]
        assert entry["agent_id"] == "" and entry["agent_source"] == ""


class TestScopedTokens:
    @pytest.fixture(autouse=True)
    def _audit(self, tmp_path, monkeypatch):
        from privacyfence import paths
        # Scoped calls note their activity under authority_dir; keep it out of the checkout.
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path

    def _audit_entries(self) -> list[dict]:
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    @contextlib.asynccontextmanager
    async def _scoped_session(self, dispatcher: McpDispatcher, *, days: float = 42.5):
        store = _OneTokenStore(_scoped_token(days=days))
        scoped_tokens.set_store(store)
        async with _connected_session(dispatcher, verifier=_ScopedVerifier(store.token)) as session:
            yield session, store

    async def test_tools_list_shows_only_scoped_tools_and_meta_tools(self):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, _store):
            result = await session.list_tools()
        assert {t.name for t in result.tools} == {"echo_say"} | rm.SCOPED_META_TOOLS

    async def test_an_out_of_scope_tool_is_refused_and_audited(self):
        connector = TwoToolConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with self._scoped_session(dispatcher) as (session, _store):
            refused = await session.call_tool("echo_shout", {"reason": "loud"})
            allowed = await session.call_tool("echo_say", {"message": "hi"})

        assert refused.is_error is True
        assert refused.content[0].text == "Request denied: this token may not call echo_shout."
        assert allowed.is_error is False
        assert connector.calls == [("echo_say", {"message": "hi"})]
        [entry] = self._audit_entries()
        assert (entry["decision"], entry["connector"], entry["tool"]) == ("denied_scoped_token", "echo", "echo_shout")
        assert entry["summary"] == "Tool not in scoped token 'sales-sync'"
        assert entry["claude_reason"] == "loud"
        assert (entry["agent_id"], entry["agent_source"]) == ("scoped-token:st-0123456789ab", "override")

    async def test_an_allowed_scoped_call_records_the_tokens_last_use(self):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, store):
            before = time.time()
            await session.call_tool("echo_say", {"message": "hi"})
        activity = store._activity[LOCAL_PRINCIPAL.id][store.token.id]
        assert activity.last_used_at is not None and activity.last_used_at >= before
        assert activity.last_denied_at is None

    async def test_a_refused_scoped_call_is_a_denial_not_a_use(self):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, store):
            await session.call_tool("echo_shout", {"reason": "loud"})
        activity = store._activity[LOCAL_PRINCIPAL.id][store.token.id]
        assert activity.last_used_at is None
        assert activity.last_denied_tool == "echo_shout"

    async def test_a_scoped_call_without_a_store_still_runs(self, monkeypatch):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, store):
            # The store is swapped out after the scope was resolved: current_store() is None.
            monkeypatch.setattr(rm, "scope_from_access_token", lambda token: store.token)
            scoped_tokens.set_store(None)
            result = await session.call_tool("echo_say", {"message": "hi"})
        assert result.is_error is False
        assert store._activity == {}

    async def test_a_revoked_scope_records_nothing(self):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, store):
            store.revoked = True
            await session.call_tool("echo_say", {"message": "hi"})
        assert store._activity == {}

    async def test_status_reports_the_token_and_its_expiry(self):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, store):
            result = await session.call_tool("privacyfence_status", {"reason": "start of run"})

        assert result.is_error is False
        content = result.structured_content
        assert content["token"] == {
            "label": "sales-sync",
            "expires_at": datetime.fromtimestamp(store.token.expires_at, tz=timezone.utc).isoformat(),
            "days_left": 43, "expiring_soon": False, "rules": 2, "tools": 2,
        }
        assert "expires in" not in content["message"]

    async def test_status_warns_when_the_token_expires_soon(self):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher, days=4.5) as (session, _store):
            result = await session.call_tool("privacyfence_status", {"reason": "start of run"})

        content = result.structured_content
        assert (content["token"]["days_left"], content["token"]["expiring_soon"]) == (5, True)
        assert content["message"].endswith(" This token expires in 5 day(s); ask its owner for a new one.")

    async def test_begin_unattended_needs_no_admin_opt_in(self):
        dispatcher = _dispatcher({})  # unattended_sessions_enabled defaults False
        async with self._scoped_session(dispatcher) as (session, _store):
            begin = await session.call_tool("privacyfence_begin_unattended_session", {"reason": "scheduled"})
            end = await session.call_tool("privacyfence_end_unattended_session", {"reason": "done"})

        assert begin.structured_content == {"unattended": True, "scoped_token": True}
        # Ending one does not leave deny-only: a scoped call always is.
        assert end.structured_content == {"unattended": True, "scoped_token": True}

    @pytest.mark.parametrize("name", [
        "privacyfence_list_policy", "privacyfence_propose_policy_change", "privacyfence_await_approval",
        "privacyfence_propose_scoped_token",
    ])
    async def test_list_policy_and_propose_are_not_offered_and_are_refused(self, name):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, _store):
            listed = {t.name for t in (await session.list_tools()).tools}
            result = await session.call_tool(name, {"reason": "curious", "operation": "remove", "rule_id": "r-1"})

        assert name not in listed
        assert result.is_error is True
        assert result.content[0].text == f"Request denied: this token may not call {name}."
        [entry] = self._audit_entries()
        assert (entry["decision"], entry["connector"], entry["tool"]) == ("denied_scoped_token", "mcp", name)

    async def test_a_revoked_scope_lists_nothing_and_refuses_every_call(self):
        connector = TwoToolConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with self._scoped_session(dispatcher) as (session, store):
            store.revoked = True
            listed = await session.list_tools()
            results = [
                await session.call_tool("echo_say", {"message": "hi"}),
                await session.call_tool("privacyfence_status", {"reason": "start of run"}),
            ]

        assert listed.tools == []
        assert [r.is_error for r in results] == [True, True]
        assert {r.content[0].text for r in results} == {"Request denied: this token is no longer valid."}
        assert connector.calls == []
        entries = self._audit_entries()
        assert [(e["decision"], e["connector"], e["tool"]) for e in entries] == [
            ("denied_scoped_token", "echo", "echo_say"), ("denied_scoped_token", "mcp", "privacyfence_status"),
        ]
        assert {e["agent_id"] for e in entries} == {"scoped-token:revoked"}

    async def test_create_upload_slot_works_as_usual(self, tmp_path, monkeypatch):
        from privacyfence import paths
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        dispatcher = _dispatcher({})
        async with self._scoped_session(dispatcher) as (session, _store):
            result = await session.call_tool(
                "privacyfence_create_upload_slot", {"filename": "report.pdf", "reason": "attach a file"},
            )
        assert result.is_error is False
        assert result.structured_content["method"] == "PUT"

    async def test_check_policy_refuses_a_tool_outside_the_scope(self):
        dispatcher = _dispatcher({"echo": TwoToolConnector()})
        async with self._scoped_session(dispatcher) as (session, _store):
            refused = await session.call_tool(
                "privacyfence_check_policy", {"connector": "echo", "tool": "gmail_send_message", "reason": "plan"},
            )
            allowed = await session.call_tool(
                "privacyfence_check_policy", {"connector": "echo", "tool": "gmail_list_messages", "reason": "plan"},
            )

        assert refused.is_error is True
        assert refused.content[0].text == "This token may not call gmail_send_message."
        assert allowed.is_error is False
        assert allowed.structured_content["gate"] == "auto"
        decisions = [(e["decision"], e["tool"], e["claude_reason"]) for e in self._audit_entries()]
        assert decisions == [
            ("denied_scoped_token", "gmail_send_message", "plan"), ("policy_check", "gmail_list_messages", "plan"),
        ]


# --------------------------------------------------------------------------- #
# Session lifecycle -- unattended-session cleanup on session end. Exercised
# at the McpDispatcher.end_session level in test_mcp_dispatch.py; this just
# confirms routes_mcp.py's own per-session lifespan actually calls it once
# the session ends, not just that end_session works in isolation.
# --------------------------------------------------------------------------- #

class TestSessionCleanup:
    async def test_ending_the_session_clears_its_unattended_flag(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        app, session_manager = build_mcp_asgi_app(dispatcher, token=TOKEN)
        transport = httpx2.ASGITransport(app=app)

        async with mcp_lifespan(session_manager):
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://testserver", headers={"Authorization": f"Bearer {TOKEN}"},
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http_client,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await session.call_tool("privacyfence_begin_unattended_session", {"reason": "x"})
                        assert dispatcher.unattended_session_count() == 1
                # streamable_http_client's own __aexit__ sends the DELETE
                # that terminates this Streamable HTTP session -- awaited
                # above, so by the time we're back here the per-connection
                # cleanup routes_mcp.py's build_mcp_server pushes onto
                # Connection.exit_stack has already run.
        assert dispatcher.unattended_session_count() == 0


class TestSessionIdentityDegradedPaths:
    """Where a session's identity comes from under mcp 2.x, on the paths this
    daemon's own wiring never takes: a request that carries no
    ``Mcp-Session-Id`` header, and a request whose ``Connection`` this module
    can't reach (see ``_connection_of``'s own docstring -- a future SDK rename
    must cost /mcp its notifications and its cleanup, not its ability to
    answer). Every other test here covers the normal path, where the header
    is present and the connection is reachable."""

    @staticmethod
    def _ctx(*, headers: dict | None, connection):
        request = SimpleNamespace(headers=headers) if headers is not None else None
        return SimpleNamespace(request=request, session=SimpleNamespace(_connection=connection))

    def test_the_transports_session_id_header_is_the_key(self):
        ctx = self._ctx(headers={"mcp-session-id": "from-the-header"}, connection=None)
        assert rm._session_key(ctx) == "from-the-header"

    def test_falls_back_to_the_connections_own_session_id(self):
        # No HTTP request attached to the message at all (stdio's shape),
        # but the connection the SDK built for it knows its own id.
        connection = Connection(object(), protocol_version="2025-06-18", session_id="from-the-connection")
        assert rm._session_key(self._ctx(headers=None, connection=connection)) == "from-the-connection"

    def test_a_request_with_no_session_id_anywhere_still_gets_one_stable_key(self):
        assert rm._session_key(self._ctx(headers={}, connection=None)) == rm._SESSIONLESS_KEY

    async def test_an_unreachable_connection_costs_notifications_not_the_tool_call(self, monkeypatch):
        # The degradation _connection_of promises, driven end to end: with no
        # Connection to register, nothing is tracked for the broadcast and
        # nothing is pushed onto an exit stack -- and a real client still
        # lists and calls tools over the same session.
        monkeypatch.setattr(rm, "_connection_of", lambda ctx: None)
        connector = EchoConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with _connected_session(dispatcher) as session:
            tools = await session.list_tools()
            result = await session.call_tool("echo_say", {"message": "hi", "reason": "test"})
        assert "echo_say" in {t.name for t in tools.tools}
        assert result.is_error is False
        dispatcher.notify_tools_changed()  # nothing registered -- must not raise


# --------------------------------------------------------------------------- #
# A refused request must not leave the client pinned to a dead session --
# routes_mcp.py's _SessionIdOnlyOnSuccess. See its docstring for the full
# cascade; mcpb/shim/src/sessionFetch.ts is the same guard on the client side,
# for a shim talking to a daemon older than this.
# --------------------------------------------------------------------------- #

class TestDeadSessionIdIsNotHandedOut:
    async def test_a_refused_opening_request_carries_no_session_id(self):
        # Opening with anything but `initialize` is a 400 -- that part is
        # correct and unchanged. What must not come back with it is the id of
        # the session the manager admitted and has already discarded again.
        async with _raw_client_on_a_running_app(_dispatcher()) as client:
            resp = await client.post("/mcp", json=_PING_BODY, headers=_WIRE_HEADERS)
        assert resp.status_code == 400
        assert "mcp-session-id" not in resp.headers

    async def test_initialize_still_works_after_a_refused_opening_request(self):
        # The regression itself, replayed the way a real client produces it:
        # adopt whatever session id the response offers -- without checking
        # whether it succeeded, which is exactly what the official client
        # transport does -- and send it on the next request. With the id
        # withheld there is nothing to adopt, so the `initialize` that
        # follows a rejected probe opens a session normally instead of being
        # refused 404 "Session not found" on account of it.
        async with _raw_client_on_a_running_app(_dispatcher()) as client:
            probe = await client.post("/mcp", json=_PING_BODY, headers=_WIRE_HEADERS)
            assert probe.status_code == 400
            adopted = {"mcp-session-id": probe.headers["mcp-session-id"]} if "mcp-session-id" in probe.headers else {}
            resp = await client.post("/mcp", json=_INIT_BODY, headers={**_WIRE_HEADERS, **adopted})
        assert resp.status_code == 200
        assert resp.headers["mcp-session-id"]

    async def test_a_successful_response_keeps_its_session_id(self):
        # The other side of the rule: a 2xx is where a session id is
        # meaningful, and it passes through untouched.
        async with _raw_client_on_a_running_app(_dispatcher()) as client:
            resp = await client.post("/mcp", json=_INIT_BODY, headers=_WIRE_HEADERS)
        assert resp.status_code == 200
        assert resp.headers["mcp-session-id"]


# --------------------------------------------------------------------------- #
# ...and a session id that *was* real must not pin the client to a dead
# session either -- routes_mcp.py's _RehomeStaleInitialize. The other half of
# the same problem: above, the id was never valid; here it was, and the
# session behind it is gone (a restart, an eviction). Neither official client
# transport clears a session id on a 404, so without this the connection can
# never recover. See that class's docstring.
# --------------------------------------------------------------------------- #

class _SpyApp:
    """Records the scope it was called with and the body it could read, so a
    test can assert both that the header was (or wasn't) stripped and that the
    request survived being buffered."""

    def __init__(self) -> None:
        self.scopes: list[dict] = []
        self.bodies: list[bytes] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        self.bodies.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    @property
    def saw_session_id(self) -> bool:
        return any(name.lower() == b"mcp-session-id" for name, _ in self.scopes[-1]["headers"])


class _FakeSessionManager:
    def __init__(self, *live: str) -> None:
        self._server_instances = {session_id: object() for session_id in live}


class _ManagerWithoutTheMapWeExpect:
    """A hypothetical future SDK build that renamed its session map. The pin
    is a range, so this must degrade to "change nothing", not to a crash."""


def _scope(*, method="POST", session_id="stale-session", scope_type="http"):
    headers = [(b"content-type", b"application/json")]
    if session_id is not None:
        headers.append((b"mcp-session-id", session_id.encode()))
    return {"type": scope_type, "method": method, "headers": headers}


def _receive_of(*messages):
    queued = list(messages)

    async def receive():
        return queued.pop(0)

    return receive


def _body(raw: bytes, *, more=False):
    return {"type": "http.request", "body": raw, "more_body": more}


_INIT_RAW = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
_PING_RAW = b'{"jsonrpc":"2.0","id":0,"method":"ping"}'


async def _drive(app, scope, *messages):
    sent = []

    async def send(message):
        sent.append(message)

    await app(scope, _receive_of(*messages), send)
    return sent


class TestRehomingAStaleSessionId:
    async def test_initialize_naming_a_dead_session_is_re_homed(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(), _body(_INIT_RAW))
        assert not spy.saw_session_id
        # Buffering the body to decide must not consume it.
        assert spy.bodies[-1] == _INIT_RAW

    async def test_a_batch_containing_an_initialize_is_re_homed(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        batch = b'[{"jsonrpc":"2.0","id":0,"method":"ping"},' + _INIT_RAW + b"]"
        await _drive(middleware, _scope(), _body(batch))
        assert not spy.saw_session_id

    async def test_a_body_split_across_chunks_is_reassembled_and_replayed(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(
            middleware, _scope(),
            _body(_INIT_RAW[:20], more=True), _body(_INIT_RAW[20:]),
        )
        assert not spy.saw_session_id
        assert spy.bodies[-1] == _INIT_RAW

    async def test_a_live_session_id_is_left_alone(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager("stale-session"))
        await _drive(middleware, _scope(), _body(_INIT_RAW))
        assert spy.saw_session_id

    async def test_anything_but_initialize_keeps_todays_404(self):
        # A server session that never saw `initialize` refuses these anyway,
        # so re-homing one would trade a clear 404 for a confusing 400.
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(), _body(_PING_RAW))
        assert spy.saw_session_id
        assert spy.bodies[-1] == _PING_RAW

    async def test_an_unparseable_body_is_passed_through(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(), _body(b"not json"))
        assert spy.saw_session_id

    async def test_an_empty_body_is_passed_through(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(), _body(b""))
        assert spy.saw_session_id

    async def test_an_oversized_body_is_passed_through_intact(self, monkeypatch):
        monkeypatch.setattr(rm, "_MAX_BUFFERED_OPENING_BODY_BYTES", 8)
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(), _body(_INIT_RAW))
        assert spy.saw_session_id
        assert spy.bodies[-1] == _INIT_RAW

    async def test_a_client_that_disconnects_mid_body_is_passed_through(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(
            middleware, _scope(),
            _body(_INIT_RAW[:10], more=True), {"type": "http.disconnect"},
        )
        assert spy.saw_session_id

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    async def test_only_a_post_is_ever_re_homed(self, method):
        # A GET reopens an SSE stream and a DELETE terminates a session: both
        # genuinely need the session they name, and a fresh one cannot serve
        # them.
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(method=method), _body(b""))
        assert spy.saw_session_id

    async def test_a_request_with_no_session_id_is_passed_through(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(session_id=None), _body(_INIT_RAW))
        assert not spy.saw_session_id

    async def test_a_non_http_scope_is_passed_through(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _FakeSessionManager())
        await _drive(middleware, _scope(scope_type="lifespan"), _body(b""))
        assert spy.scopes[-1]["type"] == "lifespan"

    async def test_an_sdk_that_moved_its_session_map_changes_nothing(self):
        spy = _SpyApp()
        middleware = rm._RehomeStaleInitialize(spy, _ManagerWithoutTheMapWeExpect())
        await _drive(middleware, _scope(), _body(_INIT_RAW))
        assert spy.saw_session_id

    async def test_end_to_end_a_dead_session_id_no_longer_refuses_initialize(self):
        """What a client actually experiences after the daemon restarts: it
        still holds the id of a session this process has never heard of."""
        async with _raw_client_on_a_running_app(_dispatcher()) as client:
            resp = await client.post(
                "/mcp", json=_INIT_BODY,
                headers={**_WIRE_HEADERS, "mcp-session-id": "a-session-from-before-the-restart"},
            )
        assert resp.status_code == 200
        assert resp.headers["mcp-session-id"] != "a-session-from-before-the-restart"

    async def test_end_to_end_a_live_session_still_serves_its_own_requests(self):
        """The liveness check must not re-home a session that is perfectly
        alive -- that would silently drop the session's state."""
        async with _raw_client_on_a_running_app(_dispatcher()) as client:
            opened = await client.post("/mcp", json=_INIT_BODY, headers=_WIRE_HEADERS)
            session_id = opened.headers["mcp-session-id"]
            again = await client.post(
                "/mcp", json=_INIT_BODY, headers={**_WIRE_HEADERS, "mcp-session-id": session_id},
            )
        assert again.headers.get("mcp-session-id", session_id) == session_id


# (readOnlyHint, destructiveHint, idempotentHint) per tool (ADR 0089).
_EXPECTED = {
    "rwd_read": (True, False, True), "rwd_write": (False, False, False), "rwd_delete": (False, True, False),
}
# The header 5.0.0a1's second Claude Desktop extension sent to ask for every tool read-only. Removed
# by ADR 0089: a client still sending it is neither refused nor obeyed.
_REMOVED_HEADER = "X-PrivacyFence-Tool-Annotations"


def _triples(tools) -> dict[str, tuple]:
    return {
        t.name: (t.annotations.read_only_hint, t.annotations.destructive_hint, t.annotations.idempotent_hint)
        for t in tools
    }


class TestToolAnnotationsOverTheWire:
    """ADR 0089, checked on a live ``/mcp`` ``list_tools``: every connector tool is annotated
    truthfully in both modes, whatever the client sends, and the meta-tools keep their own."""

    @pytest.mark.parametrize("dispatcher_mode", ["local", "org"])
    @pytest.mark.parametrize("header", [None, "all-read-only", "truthful", "read-only"])
    async def test_connector_tools_are_always_truthful(self, dispatcher_mode, header):
        dispatcher = _dispatcher({"rwd": ReadWriteDeleteConnector()}, mode=dispatcher_mode)
        verifier = _OrgVerifier("alice@example.com") if dispatcher_mode == "org" else None
        headers = {_REMOVED_HEADER: header} if header is not None else None
        async with _connected_session(dispatcher, verifier=verifier, headers=headers) as session:
            result = await session.list_tools()
        triples = _triples(result.tools)
        assert {name: triples[name] for name in _EXPECTED} == _EXPECTED

    @pytest.mark.parametrize("header", [None, "all-read-only"])
    async def test_meta_tools_keep_their_own_annotations(self, header):
        from privacyfence.web.mcp_tools import META_TOOLS

        dispatcher = _dispatcher({"rwd": ReadWriteDeleteConnector()})
        headers = {_REMOVED_HEADER: header} if header is not None else None
        async with _connected_session(dispatcher, headers=headers) as session:
            result = await session.list_tools()
        advertised = {t.name: t.annotations for t in result.tools if t.name in META_TOOL_NAMES}
        assert advertised == {t.name: t.annotations for t in META_TOOLS}

    async def test_the_removed_header_is_not_refused(self):
        # A 5.0.0a1 no-prompts extension left installed keeps working, with truthful annotations,
        # rather than failing every request -- the 400 it used to get for a bad value is gone too.
        dispatcher = _dispatcher({"rwd": ReadWriteDeleteConnector()})
        async with _raw_client_on_a_running_app(dispatcher) as client:
            response = await client.post(
                "/mcp", json=_INIT_BODY, headers={**_WIRE_HEADERS, _REMOVED_HEADER: "read-only"},
            )
        assert response.status_code == 200
