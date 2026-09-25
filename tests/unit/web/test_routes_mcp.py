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
from types import SimpleNamespace

import anyio
import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.connection import Connection

from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.principal import LOCAL_PRINCIPAL, current_principal
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
            transport=transport, base_url="http://testserver", headers={"Authorization": f"Bearer {token}"},
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


def _dispatcher(connectors: dict[str, Connector] | None = None, **kwargs) -> McpDispatcher:
    store = dict(connectors or {})
    return McpDispatcher(lambda: store, **kwargs)


_INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
_PING_BODY = {"jsonrpc": "2.0", "id": 0, "method": "ping"}
# What a Streamable HTTP client sends on every POST; without them the
# transport stops at a 406/415 long before session handling is reached.
_WIRE_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


# --------------------------------------------------------------------------- #
# Auth -- audience separation starts here: no credential but the
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

    async def test_connector_tool_is_advertised_uniformly_read_only(self):
        # Every tool -- write tools included -- is advertised
        # read-only/non-destructive; the real gate is server-side.
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.list_tools()
        tool = next(t for t in result.tools if t.name == "echo_say")
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False

    async def test_reflects_a_live_connector_set_change_between_calls(self):
        store: dict[str, Connector] = {}
        dispatcher = McpDispatcher(lambda: store)
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
        dispatcher = McpDispatcher(lambda: per_principal.get(current_principal().id, {}))

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
        dispatcher = McpDispatcher(lambda: per_principal.get(current_principal().id, {}))

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
