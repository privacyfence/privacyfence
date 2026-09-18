"""The Streamable HTTP MCP endpoint -- what took over the original
``privacyfence-bridge``'s four jobs (find/launch the daemon, fetch the
manifest, register one MCP tool per ``ToolSpec``, forward calls) for a
client that talks to PrivacyFence directly, no intermediate process
required. The bridge itself was retired at P5, once this transport had
shipped a stable release.

P2 scope only: this is a hosting change for the *transport*, not the
approval protocol. A gated call reaching a connector here still blocks on
whichever ``ApprovalUI`` ``approval_ui.init_approval_ui()`` currently
resolves to (the web approval UI, unconditionally since P10 -- through P9
this could also be the native one, selected by ``web.approval_ui``, a
config key P10 removed along with the native implementation itself),
exactly like a call arriving over the bridge's IPC socket used to before P5
retired it. Deferred approvals, concurrent pending approvals, and
``privacyfence_await_approval`` are P3's ``_popup_lock`` retirement, not
this module's ("P2 before P3" is deliberate: the deferred protocol is
written once, on the transport it ships on, instead of being added to the
bridge/IPC protocol first and thrown away one phase later).

Built on the official MCP Python SDK's low-level ``Server`` (dynamic tool
registration -- the tool set depends on which connectors are currently
built, so it can't be the decorator-per-tool ``FastMCP`` surface) plus
``StreamableHTTPSessionManager`` (D2/D10).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from mcp import types
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.connection import Connection
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from .. import __version__ as PRIVACYFENCE_VERSION
from ..connector import Connector
from ..principal import principal_scope
from ..safe_errors import public_message
from . import mcp_tools
from .mcp_auth import StaticTokenVerifier, principal_from_access_token
from .mcp_dispatch import McpDispatcher
from .oauth_provider import IDP_CALLBACK_PATH, OrgOAuthProvider

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"

# Part A of issue #396: server instructions returned in the `initialize`
# result (Server.instructions -> InitializationOptions.instructions,
# confirmed against a real mcp==1.30.0 install -- no Server subclass needed
# for this part, unlike the NotificationOptions(tools_changed=True) override
# Part C's tools/list_changed support needs at this same construction site).
# Deliberately short and factual, not "call privacyfence_status at the start
# of every conversation": most conversations have nothing to do with
# PrivacyFence, and every meta-tool call is a round trip a client pays for.
SERVER_INSTRUCTIONS = (
    "PrivacyFence is a privacy/approval gateway between this client and the user's real "
    "business systems (Gmail, Drive, Slack, and similar) -- it does not provide those services "
    "itself, it governs access to connectors that do, applying policy and approval gates before "
    "data moves.\n\n"
    "An empty tool list, or one with only privacyfence_-prefixed meta-tools and no connector "
    "tools (gmail_*, drive_*, slack_*, ...), means this install's connectors aren't set up or "
    "authenticated yet -- it does NOT mean PrivacyFence has nothing to do with the current "
    "request. Call privacyfence_status before the first PrivacyFence-governed action in a "
    "conversation, or whenever the user asks why a connector isn't available: it reports the "
    "real setup state and, if nothing is authenticated yet, a link the user can open to finish "
    "setup. It is the one tool guaranteed to exist even when every other tool is missing.\n\n"
    "Most conversations have nothing to do with PrivacyFence and should not call any "
    "privacyfence_* tool at all."
)


# The header a Streamable HTTP client stamps on every request that belongs
# to an established session -- the string form of ``_MCP_SESSION_ID_HEADER``
# further down, which the ASGI middleware below matches on raw ASGI headers.
_MCP_SESSION_ID = "mcp-session-id"

# The session key a request that carries no session id at all falls back to.
# Unreachable on this daemon's own wiring (``mount_mcp`` runs the session
# manager stateful, so every request after ``initialize`` carries the header
# the manager itself minted), but a transport without session ids -- stdio, or
# a stateless manager -- must still land somewhere stable rather than raise.
_SESSIONLESS_KEY = "sessionless"


def _connection_of(ctx: ServerRequestContext) -> Connection | None:
    """The per-connection ``Connection`` behind this request, or ``None`` when
    this SDK build doesn't expose it where we expect it.

    mcp 2.x hands a request handler a ``ServerRequestContext``, whose
    ``session`` is built fresh per *request* (see
    ``mcp.server.session.ServerSession``'s own docstring) -- the
    connection-scoped object, with the per-connection ``state`` dict and the
    ``exit_stack`` unwound when the session ends, is the ``Connection`` behind
    it. ``mcp.server.context.Context`` exposes that as a public ``.connection``
    property, but ``ServerRunner`` doesn't build a ``Context`` yet (that
    module's own ``TODO(L54)``), so a handler's only route to it today is
    through the session that holds it.

    Same "degrade to doing nothing rather than break /mcp" posture as
    ``_RehomeStaleInitialize._is_live`` below, and for the same reason: the pin
    is a range (``mcp>=1.28,<3.0``), so an internal rename must cost this
    module its tools/list_changed notifications and its session cleanup, not
    its ability to serve a tool call.
    """
    connection = getattr(ctx.session, "_connection", None)
    return connection if isinstance(connection, Connection) else None


def _session_key(ctx: ServerRequestContext) -> str:
    """The current request's session key -- stable for one Streamable HTTP
    session and nothing more, exactly like ``id(writer)`` was in ipc_server.py.

    Under mcp 1.x this was a ``uuid4`` minted by the server's own ``lifespan``,
    which that SDK entered once per session. mcp 2.x enters the lifespan once
    per session *manager* instead (``StreamableHTTPSessionManager.run`` enters
    it before any session exists and hands the one ``lifespan_context`` to
    every session it later starts -- see its own "the manager's already-entered
    lifespan is reused rather than re-entered per session" comment), so a
    lifespan-minted id would now be one id for the whole daemon: every client
    would share one unattended-session flag and one dedupe window.

    The transport's own session id is the per-session identity now. It is the
    same value the manager mints, stamps on the ``initialize`` response and
    keys ``_server_instances`` by, and the client echoes it on every
    subsequent request -- which is every request that reaches a handler here,
    since ``initialize`` is the runner's own and never dispatched to one.
    """
    request = ctx.request
    header = request.headers.get(_MCP_SESSION_ID) if request is not None else None
    if header:
        return header
    connection = _connection_of(ctx)
    return getattr(connection, "session_id", None) or _SESSIONLESS_KEY


class _PrivacyFenceServer(MCPServer):
    """Overrides ``create_initialization_options()`` to always advertise
    ``tools.list_changed = True`` (issue #396 Part C).

    ``StreamableHTTPSessionManager`` (``mcp/server/streamable_http_manager.py``,
    what this module's ``mount_mcp`` actually uses) drives each session with
    ``serve_loop(..., init_options=None)``, so the runner answering
    ``initialize`` falls back to ``self.server.create_initialization_options()``
    -- called with zero arguments, whose own ``notification_options or
    NotificationOptions()`` then falls through to the default
    ``tools_changed=False``. There is no argument for a caller to pass here
    that this class would need to respect instead; overriding the method
    itself is the only lever available without patching the manager.

    Scope note (mcp 2.x): this is the handshake-era capability, which is what
    every client this daemon serves negotiates -- ``initialize`` is answered
    with a handshake protocol version whatever the client asks for (see
    ``ServerRunner._negotiate_initialize``). A 2026-07-28-era client that skips
    the handshake entirely gets its change notifications from
    ``subscriptions/listen`` streams instead, a method this server doesn't
    serve (and ``get_capabilities`` correctly won't claim ``listChanged`` for);
    adding it is its own piece of work, not part of the 2.x migration.
    """

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        return super().create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True),
            experimental_capabilities=experimental_capabilities,
            extensions=extensions,
        )


def build_mcp_server(dispatcher: McpDispatcher) -> MCPServer:
    """Builds the low-level MCP ``Server``, wired to ``dispatcher`` for both
    tool listing and tool calls. A fresh ``Server`` per daemon process
    (there's exactly one dispatcher, and its connector set can change live --
    see ``McpDispatcher.connectors``), not a handler per connector tool:
    the tool set is only known at request time.

    Handlers are constructor arguments (``on_list_tools``/``on_call_tool``),
    not ``@server.list_tools()`` decorators: mcp 2.0 replaced the decorator
    registry with constructor-based registration, and a handler is now
    ``(ctx, params) -> Result`` -- the per-request context carries what
    ``Server.request_context`` used to, and the handler returns the whole
    ``ListToolsResult``/``CallToolResult`` rather than the SDK wrapping a
    bare list for it.
    """

    # issue #396 Part C: every currently-open Streamable HTTP session's own
    # live ``Connection`` -- the SDK object ``send_tool_list_changed()``
    # lives on, and the one that outlives a single request (unlike
    # ``ServerSession``, which mcp 2.x rebuilds per request) -- keyed by the
    # same session_key dispatcher.end_session() already uses. This dict, not
    # McpDispatcher, is the right owner: it's routes_mcp.py-specific
    # transport state with no meaning outside one running MCPServer, whereas
    # McpDispatcher's own session-scoped state (unattended flags, dedupe) is
    # protocol-level and already has its own home. Populated from inside
    # handle_list_tools/handle_call_tool below, and emptied by the
    # per-connection cleanup each entry registers as it goes in.
    live_connections: dict[str, Connection] = {}

    async def _forget_session(session_key: str) -> None:
        # Runs from ``Connection.exit_stack`` when the session ends, however
        # it ends (client DELETE, idle timeout, crash -- see
        # StreamableHTTPSessionManager._serve_opening_request's own
        # ``finally``). The direct counterpart of ipc_server.py's
        # ``_handle_connection`` ``finally`` clearing ``id(writer)`` from
        # ``_unattended_connections`` when a bridge connection dropped, and of
        # the per-session ``lifespan`` this module used under mcp 1.x.
        live_connections.pop(session_key, None)
        dispatcher.end_session(session_key)

    def _track_session(ctx: ServerRequestContext, session_key: str) -> None:
        if session_key in live_connections or session_key == _SESSIONLESS_KEY:
            return
        connection = _connection_of(ctx)
        if connection is None:
            return
        live_connections[session_key] = connection
        connection.exit_stack.push_async_callback(_forget_session, session_key)

    async def _send_tool_list_changed(connection: Connection) -> None:
        try:
            await connection.send_tool_list_changed()
        except Exception as exc:  # noqa: BLE001 -- one dead/closing session must not
            # stop the others in the same broadcast from being notified.
            logger.info("tools/list_changed notification failed for one session: %s", exc)

    def _broadcast_tools_changed() -> None:
        # Called via McpDispatcher.notify_tools_changed(), itself called
        # from SettingsController.refresh_connectors()'s on-main-thread
        # `done()` callback (daemon_main.py wires
        # controller.set_connectors_changed_listener(mcp_dispatcher.
        # notify_tools_changed)) -- that callback already runs on this
        # server's own asyncio loop (settings_controller.call_on_main ->
        # web/state_stream.call_soon_threadsafe), so a running loop is
        # always available here in production; a test that calls this
        # directly with no loop running (e.g. exercising the dispatcher in
        # isolation) simply notifies nobody, since live_connections is empty
        # in that case anyway.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for connection in list(live_connections.values()):
            loop.create_task(_send_tool_list_changed(connection))

    dispatcher.set_tools_changed_broadcaster(_broadcast_tools_changed)

    async def handle_list_tools(
        ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        # Principal-scoped for the same reason handle_call_tool below is:
        # ``dispatcher.connectors`` is
        # ``connector_registry.get(current_principal()).connectors`` in org
        # mode, so *which* connector set this enumerates depends entirely on
        # the scope it runs in. Without this, current_principal() fell back
        # to LOCAL_PRINCIPAL and the manifest was built from the local
        # principal's connectors -- on an org server nobody authorizes
        # services as "local", so build_connectors() skipped every one of
        # them and the advertised manifest was META_TOOLS and nothing else.
        # Every connector tool was invisible to org-mode clients, while
        # handle_call_tool (correctly scoped) could resolve those same
        # connectors perfectly well -- a client simply had no way to learn
        # the tools existed to call them.
        _track_session(ctx, _session_key(ctx))
        principal = principal_from_access_token(get_access_token())
        with principal_scope(principal):
            tools = [
                mcp_tools.to_mcp_tool(spec)
                for connector in dispatcher.connectors.values()
                for spec in connector.tool_specs()
            ]
        tools.extend(mcp_tools.META_TOOLS)
        return types.ListToolsResult(tools=tools)

    async def handle_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        session_key = _session_key(ctx)
        _track_session(ctx, session_key)
        name = params.name
        arguments = dict(params.arguments or {})
        # Entered once per tool call, in the one place this surface
        # dispatches one (P6) --
        # every per-principal registry downstream (auto_accept.py,
        # audit_log.py, pii_detector.py, privacy_filter.py,
        # resource_names.py) resolves against whatever this sets for the
        # rest of the call, including everything gate.py's gated_call()
        # does. LOCAL_PRINCIPAL in local mode; in org mode (P7 onwards) the
        # real signed-in human -- see
        # mcp_auth.principal_from_access_token's own docstring for how each
        # is resolved.
        principal = principal_from_access_token(get_access_token())
        with principal_scope(principal):
            try:
                if name in mcp_tools.META_TOOL_NAMES:
                    result = await _dispatch_meta_tool(dispatcher, session_key, name, arguments)
                else:
                    result = await _dispatch_connector_tool(dispatcher, session_key, name, arguments)
            except Exception as exc:  # noqa: BLE001 -- surfaced to the client as a tool error, not a
                # transport-level failure, exactly like ipc_server.py's own
                # `{"id": ..., "error": str(exc)}` response to a "call" request.
                # SEC-10: the full exception (redacted for local logging by
                # SecretRedactingFormatter, installed on the root logger by
                # daemon_main.setup_logging) goes to the log; the client
                # only ever sees safe_errors.public_message(exc) -- a fixed
                # generic message unless exc's type is on the reviewed
                # allowlist, since a connector or OAuth failure can wrap a
                # third-party exception carrying a token or auth code.
                #
                # Still this module's job under mcp 2.x, not the SDK's: 2.0
                # made an escaping handler exception a generic "Error
                # executing tool <name>" *protocol* error, which is neither
                # the tool-level error result the bridge protocol promised
                # nor something the model can act on.
                logger.info("Tool call %s failed: %s", name, exc)
                return mcp_tools.error_result(public_message(exc))
        if name == mcp_tools.GET_SIGN_IN_LINK_TOOL.name:
            # This tool's whole point is handing a human a link to click --
            # the generic JSON-dump text every other meta tool gets isn't
            # clickable in most clients, so it gets its own result shape.
            return mcp_tools.sign_in_link_result(result)
        return mcp_tools.to_call_tool_result(result)

    return _PrivacyFenceServer(
        "privacyfence", version=PRIVACYFENCE_VERSION, instructions=SERVER_INSTRUCTIONS,
        on_list_tools=handle_list_tools, on_call_tool=handle_call_tool,
    )


async def _dispatch_connector_tool(
    dispatcher: McpDispatcher, session_key: str, tool: str, arguments: dict[str, Any],
) -> Any:
    connector_name = _connector_for_tool(dispatcher.connectors, tool)
    if connector_name is None:
        raise ValueError(f"Unknown tool: {tool!r}")
    return await dispatcher.call(session_key, connector_name, tool, dict(arguments))


def _connector_for_tool(connectors: dict[str, Connector], tool: str) -> str | None:
    for connector in connectors.values():
        for spec in connector.tool_specs():
            if spec.name == tool:
                return connector.name
    return None


async def _dispatch_meta_tool(
    dispatcher: McpDispatcher, session_key: str, name: str, arguments: dict[str, Any],
) -> Any:
    reason = arguments.get("reason", "")
    if name == mcp_tools.CHECK_POLICY_TOOL.name:
        return dispatcher.check_policy(
            arguments["connector"], arguments["tool"], arguments.get("args") or {}, reason,
        )
    if name == mcp_tools.LIST_RULES_TOOL.name:
        return dispatcher.list_rules(reason)
    if name == mcp_tools.PROPOSE_RULE_CHANGE_TOOL.name:
        return await dispatcher.propose_rule_change(session_key, arguments)
    if name == mcp_tools.BEGIN_UNATTENDED_SESSION_TOOL.name:
        return dispatcher.begin_unattended_session(session_key, reason)
    if name == mcp_tools.END_UNATTENDED_SESSION_TOOL.name:
        return dispatcher.end_unattended_session(session_key, reason)
    if name == mcp_tools.AWAIT_APPROVAL_TOOL.name:
        return await dispatcher.await_approval(
            arguments.get("approval_ids") or [], arguments.get("timeout_seconds", 30),
        )
    if name == mcp_tools.GET_SIGN_IN_LINK_TOOL.name:
        return dispatcher.get_sign_in_link(arguments.get("page", "approvals"), reason)
    if name == mcp_tools.PRIVACYFENCE_STATUS_TOOL.name:
        return dispatcher.status(reason)
    raise ValueError(f"Unknown tool: {name!r}")  # pragma: no cover -- unreachable, META_TOOL_NAMES gates this


@contextlib.asynccontextmanager
async def mcp_lifespan(session_manager: StreamableHTTPSessionManager) -> AsyncIterator[None]:
    """The session manager's own ``run()`` task-group lifespan -- must stay
    open for as long as the ASGI app serving ``/mcp`` does (see
    ``StreamableHTTPSessionManager.run``'s own docstring). server.py folds
    this into the combined app's Starlette ``lifespan``."""
    async with session_manager.run():
        yield


class _StreamableHTTPASGIApp:
    """Thin class wrapper around ``session_manager.handle_request`` -- a
    plain async function would make Starlette's ``Route`` treat this
    endpoint as a ``func(request) -> response`` handler (defaulting to
    GET-only) instead of passing it the raw ASGI ``(scope, receive, send)``
    Streamable HTTP needs for GET/POST/DELETE alike; a class instance takes
    the raw-ASGI branch instead. Same reason the official SDK's own
    ``mcp.server.fastmcp.server.StreamableHTTPASGIApp`` exists.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


_MCP_SESSION_ID_HEADER = b"mcp-session-id"


class _SessionIdOnlyOnSuccess:
    """Strips ``Mcp-Session-Id`` from any non-2xx response, so a refused
    request cannot hand a client the id of a session that no longer exists.

    Streamable HTTP requires the request that opens a session to be
    ``initialize``. When a client opens with anything else, the SDK's session
    manager still admits a session -- it allocates the id before it has parsed
    the body far enough to know better -- answers 400, and discards that
    session again (``_serve_opening_request``'s own
    ``established = status < 400``). All correct, except that the 400 goes out
    carrying the discarded session's id anyway:
    ``StreamableHTTPServerTransport._create_error_response`` stamps
    ``self.mcp_session_id`` on every error it builds, and by then the
    transport has one.

    That id is dead on arrival and a client has no way to know it. The
    official client transport reads the header off *every* response before it
    checks whether the response succeeded, adopts it, and stamps it on
    everything it sends next -- all of which this daemon then answers,
    ``initialize`` very much included, with 404 "Session not found". One
    rejected frame becomes a connection that can never recover, for as long as
    the client process lives.

    ``mcpb/shim/src/sessionFetch.ts`` defends the bundled shim against the
    same cascade from the client side, which is what reaches a user whose
    daemon is older than this. This is the other half of it: a client pointed
    straight at ``/mcp`` -- a documented setup, ``claude mcp add --transport
    http privacyfence ...``, with no shim anywhere in it -- is protected here
    or not at all.

    A session id is only ever meaningful on a response that established or
    used a session; on a failure it names, at best, something already gone.
    Successful responses pass through untouched, so ordinary session handling
    -- including the ``initialize`` response that legitimately carries a brand
    new id -- is unaffected.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        async def send_without_dead_session_id(message) -> None:
            if message["type"] == "http.response.start" and not 200 <= message["status"] < 300:
                message = {**message, "headers": [
                    (name, value) for name, value in message.get("headers", [])
                    if name.lower() != _MCP_SESSION_ID_HEADER
                ]}
            await send(message)

        await self._app(scope, receive, send_without_dead_session_id)


# One JSON-RPC ``initialize`` frame is a few hundred bytes; this leaves room
# for a client that sends generous capability metadata while bounding what a
# stale-session POST can make this middleware hold in memory before it
# decides. Over the cap, the request is passed through untouched -- it cannot
# be an ``initialize`` worth re-homing at that size, and guessing is worse
# than leaving today's behavior alone.
_MAX_BUFFERED_OPENING_BODY_BYTES = 256 * 1024


def _header_value(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _names_initialize(frame: object) -> bool:
    return isinstance(frame, dict) and frame.get("method") == "initialize"


def _is_initialize(body: bytes | None) -> bool:
    """Whether ``body`` is (or, for a batch, contains) an ``initialize``
    request. Deliberately the *only* thing this module reads out of a /mcp
    request body: which method is being called is JSON-RPC envelope framing,
    not knowledge of what the method does -- the same line ``mcpb/shim/src/
    proxy.ts`` draws for the same reason."""
    if not body:
        return False
    try:
        frame = json.loads(body)
    except ValueError:
        return False
    if isinstance(frame, list):
        return any(_names_initialize(item) for item in frame)
    return _names_initialize(frame)


async def _buffer_request_body(receive: Any) -> tuple[bytes | None, Any]:
    """Reads the request body so it can be inspected, and returns it
    alongside a ``receive`` that replays every consumed ASGI message before
    falling back to the real one -- so whatever runs next sees an untouched
    request either way.

    ``None`` as the body means "don't inspect this": either it exceeded
    ``_MAX_BUFFERED_OPENING_BODY_BYTES`` or the client disconnected
    mid-stream. The replay is still correct in both cases.
    """
    consumed: list[dict[str, Any]] = []
    body = bytearray()
    unusable = False
    while True:
        message = await receive()
        consumed.append(message)
        if message["type"] != "http.request":
            unusable = True  # http.disconnect: a partial body is not worth parsing
            break
        body += message.get("body", b"")
        if len(body) > _MAX_BUFFERED_OPENING_BODY_BYTES:
            unusable = True
            break
        if not message.get("more_body", False):
            break

    pending = list(consumed)

    async def replay() -> dict[str, Any]:
        if pending:
            return pending.pop(0)
        return await receive()

    return (None if unusable else bytes(body)), replay


class _RehomeStaleInitialize:
    """Lets an ``initialize`` that arrives carrying a dead ``Mcp-Session-Id``
    open a fresh session, instead of being refused forever.

    The SDK's session manager answers any request naming a session it doesn't
    hold with 404 "Session not found" (``streamable_http_manager.py``'s final
    ``else``), which is right by the spec and fatal in practice, because
    **neither** official client transport recovers from it:

    - the Python client (``client/streamable_http.py``) turns a 404 into a
      "Session terminated" JSON-RPC error and returns -- it never clears its
      own ``session_id``;
    - the TypeScript client (``client/streamableHttp.js``) throws
      ``StreamableHTTPError``. It special-cases 401 and 403; 404 is not one of
      them, and ``_sessionId`` is only ever cleared by an explicit
      ``terminateSession()`` -- which itself throws on the 404 that a dead
      session's DELETE earns, before reaching the line that would clear it.

    So a client holding a stale id keeps stamping it on everything it sends,
    ``initialize`` very much included, and every one of those is refused on
    account of the id rather than judged on its own merits. The connection
    cannot recover for the life of the client process. That happens after any
    daemon restart, and after any session this daemon evicts or lets lapse.

    ``_SessionIdOnlyOnSuccess`` above stops a client *adopting* an id it was
    never given; this handles the case where the id was legitimately issued
    and has since died. Together they make the failure recoverable from both
    ends.

    Only a POST whose body actually is an ``initialize`` is re-homed. A
    request naming a *live* session is untouched, so the session manager's own
    "a session can only be used with the credential that created it" check
    still decides those. Everything else -- a GET reopening an SSE stream, a
    DELETE terminating a session, any non-``initialize`` POST -- keeps today's
    404: those genuinely need the session they name, and a fresh one would not
    serve them (a server session that never saw ``initialize`` refuses the
    frame anyway, so silently re-homing them would trade a clear 404 for a
    confusing one).
    """

    def __init__(self, app: ASGIApp, session_manager: StreamableHTTPSessionManager) -> None:
        self._app = app
        self._session_manager = session_manager

    def _is_live(self, session_id: str) -> bool | None:
        """``None`` when this SDK build doesn't expose its session map where
        we expect it -- the pin is a range (``mcp>=1.28,<3.0``), so an
        internal rename must degrade to "do nothing" rather than break /mcp.
        """
        instances = getattr(self._session_manager, "_server_instances", None)
        if not isinstance(instances, dict):
            return None
        return session_id in instances

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return
        session_id = _header_value(scope, _MCP_SESSION_ID_HEADER)
        if session_id is None or self._is_live(session_id) is not False:
            await self._app(scope, receive, send)
            return
        body, replay = await _buffer_request_body(receive)
        if not _is_initialize(body):
            await self._app(scope, replay, send)
            return
        logger.info(
            "Re-homing an initialize request that named a session this daemon no longer holds",
        )
        rehomed = {**scope, "headers": [
            (name, value) for name, value in scope.get("headers", [])
            if name.lower() != _MCP_SESSION_ID_HEADER
        ]}
        await self._app(rehomed, replay, send)


def build_mcp_asgi_app(
    dispatcher: McpDispatcher, *, token: str | None = None, verifier: TokenVerifier | None = None,
    resource_metadata_url: AnyHttpUrl | None = None,
) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """Builds the ``/mcp`` endpoint app -- bearer-token authenticated,
    audience-separated from the approval surface's session cookie (§10.3).
    Returns the app alongside its session manager so server.py can fold
    ``mcp_lifespan`` into the combined app's own lifespan.

    ``verifier`` is the seam P7 plugs org mode into: pass
    ``web/oauth_provider.py``'s ``OrgOAuthProvider`` (which satisfies
    ``TokenVerifier`` via its own ``verify_token``) instead of building
    ``StaticTokenVerifier(token)`` for local mode's single shared secret --
    exactly one of ``token``/``verifier`` should be given.
    ``resource_metadata_url`` (RFC 9728, org mode only) is threaded into a
    401 response's ``WWW-Authenticate`` header so a client that gets one
    knows where to discover this server's authorization server; local
    mode has no such document to point to, so it stays ``None`` there.
    """
    server = build_mcp_server(dispatcher)
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)

    if verifier is None:
        if token is None:
            raise ValueError("build_mcp_asgi_app needs either token or verifier")
        verifier = StaticTokenVerifier(token)
    protected = RequireAuthMiddleware(
        _SessionIdOnlyOnSuccess(
            _RehomeStaleInitialize(_StreamableHTTPASGIApp(session_manager), session_manager),
        ),
        required_scopes=[], resource_metadata_url=resource_metadata_url,
    )
    authenticated = AuthContextMiddleware(protected)
    app: ASGIApp = AuthenticationMiddleware(authenticated, backend=BearerAuthBackend(verifier))
    return app, session_manager


def mount_mcp(
    dispatcher: McpDispatcher, *, token: str | None = None, verifier: TokenVerifier | None = None,
    resource_metadata_url: AnyHttpUrl | None = None,
) -> tuple[Route, StreamableHTTPSessionManager]:
    """The ``/mcp`` route -- an exact-path ``Route`` with no ``methods``
    restriction (matches GET/POST/DELETE alike, exactly like the official
    SDK's own FastMCP wiring does for the same endpoint), not a ``Mount``:
    Streamable HTTP clients address this one path directly, with no
    sub-path routing underneath it.
    """
    app, session_manager = build_mcp_asgi_app(
        dispatcher, token=token, verifier=verifier, resource_metadata_url=resource_metadata_url,
    )
    return Route(MCP_PATH, endpoint=app), session_manager


def mount_org_oauth(provider: OrgOAuthProvider, *, issuer_url: str) -> list[Route]:
    """Org mode's OAuth 2.1 authorization-server + resource-metadata
    surface (P7): the SDK's own
    ``create_auth_routes`` builds ``/.well-known/oauth-authorization-
    server``, ``/authorize``, ``/token``, ``/register`` (DCR) and
    ``/revoke`` against ``provider`` -- see that function's own module for
    why none of that protocol machinery is hand-rolled here (same D2
    reasoning as the MCP SDK itself). ``create_protected_resource_routes``
    builds the RFC 9728 ``/.well-known/oauth-protected-resource/mcp``
    document pointing at this same issuer. The one route the SDK has no
    opinion on -- ``provider``'s own IdP-facing callback -- is added
    alongside them; see ``OrgOAuthProvider.handle_idp_callback``'s own
    docstring for what it does.
    """
    issuer = AnyHttpUrl(issuer_url)
    resource_url = AnyHttpUrl(f"{issuer_url.rstrip('/')}{MCP_PATH}")
    routes = create_auth_routes(
        provider, issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes.extend(create_protected_resource_routes(
        resource_url=resource_url, authorization_servers=[issuer], resource_name="PrivacyFence",
    ))

    async def idp_callback(request: Request) -> Response:
        idp_error = request.query_params.get("error")
        if idp_error:
            logger.info("MCP client authorization declined by IdP: %s", idp_error)
            return PlainTextResponse("Authorization was not completed.", status_code=400)
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if not state or not code:
            return PlainTextResponse("Invalid IdP callback.", status_code=400)
        try:
            redirect_url = await provider.handle_idp_callback(state=state, code=code)
        except Exception as exc:  # noqa: BLE001 -- any failure here ends the same way: authorization didn't complete
            logger.warning("MCP client authorization failed: %s", exc)
            return PlainTextResponse("Authorization failed. Please try again.", status_code=400)
        return RedirectResponse(redirect_url, status_code=302, headers={"Cache-Control": "no-store"})

    routes.append(Route(IDP_CALLBACK_PATH, idp_callback))
    return routes


def protected_resource_metadata_url(issuer_url: str) -> AnyHttpUrl:
    """The URL ``build_mcp_asgi_app``'s ``resource_metadata_url`` needs --
    factored out so server.py doesn't have to import ``mcp.server.auth.
    routes`` itself just to compute it."""
    return build_resource_metadata_url(AnyHttpUrl(f"{issuer_url.rstrip('/')}{MCP_PATH}"))


__all__ = [
    "MCP_PATH",
    "SERVER_INSTRUCTIONS",
    "build_mcp_server",
    "build_mcp_asgi_app",
    "mount_mcp",
    "mcp_lifespan",
]
