"""An organization server in front of an OAuth upstream, over real loopback sockets (ADR 0329-0331).

The server is a real ``WebServer`` in organization mode; the upstream is
``scripts/upstream_mcp_oauth_reference_server.py`` on another port. A person signs in to the upstream
through the server's own Connect route (``/upstream-mcp/oauthref/connect`` and its callback), with
``httpx2`` playing the browser. The Admin actions need a passkey step-up that a test cannot answer;
p16's unit tests cover them, so this test calls ``upstream_mcp_runtime.refresh``, ``save`` and
``set_gates`` in process: everything else goes over HTTP.
The AI client is the official ``mcp`` client with a real access token for the person.
"""
from __future__ import annotations

import asyncio
import importlib.util
import itertools
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
import uvicorn
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from privacyfence import approval_ui, audit_log, auto_accept, paths, scoped_tokens
from privacyfence import org_identity as oi
from privacyfence import upstream_mcp_registry, upstream_mcp_runtime
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.policy import propose as policy_propose
from privacyfence.policy import scopes
from privacyfence.principal import Principal, current_principal
from privacyfence.upstream_mcp_config import parse_upstreams
from privacyfence.upstream_oauth_tokens import token_path
from privacyfence.upstream_tools import ToolStore
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.org_session import SESSION_COOKIE, OrgSessionStore
from privacyfence.web.server import OrgAuth, WebServer
from privacyfence.web_approval_ui import WebApprovalUI

pytestmark = [pytest.mark.timeout(60), pytest.mark.integration]

_SPEC = importlib.util.spec_from_file_location(
    "upstream_mcp_oauth_reference_server",
    Path(__file__).parents[2] / "scripts" / "upstream_mcp_oauth_reference_server.py",
)
assert _SPEC and _SPEC.loader
reference = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = reference
_SPEC.loader.exec_module(reference)

ALICE = Principal(id="alice-id", email="alice@example.com", email_verified=True, is_admin=True)
BOB = Principal(id="bob-id", email="bob@example.com", email_verified=True)

# How long the registry replays a decision to an identical request. A test that needs the next call to
# be judged afresh (a rule, or its absence) waits this long first.
LEDGER_TTL = 1.5

_TOOLS = ("echo", "rename_thing", "whoami", "wipe_thing")
_POLICY = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_connectable(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"localhost:{port} never became connectable")


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


class Stack:
    def __init__(self, base_url, provider, sessions, registry, reference_app, web_ui, listed_tools) -> None:
        self.base_url = base_url
        self.provider = provider
        self.sessions = sessions
        self.registry = registry
        self.reference_app = reference_app
        self.web_ui = web_ui
        self.listed_tools = listed_tools

    def bearer(self, principal: Principal) -> str:
        return self.provider._mint_tokens(client_id="claude", scopes=[], resource=None, principal=principal).access_token

    def cookie(self, principal: Principal) -> str:
        return self.sessions.create(principal, email_verified=True)

    async def connect(self, principal: Principal, account: str) -> None:
        """What a person's browser does on the Connect page: the server's Connect route, the reference
        server's authorize endpoint (signed in as ``account``) and the server's callback."""
        session_id = self.cookie(principal)
        async with httpx2.AsyncClient(
            base_url=self.base_url, cookies={SESSION_COOKIE: session_id}, follow_redirects=False,
        ) as org:
            started = await org.get("/upstream-mcp/oauthref/connect")
            assert started.status_code == 302, started.text
            authorize = started.headers["location"] + f"&login_hint={account}"
            async with httpx2.AsyncClient(follow_redirects=False) as upstream:
                granted = await upstream.get(authorize)
            assert granted.status_code == 302, granted.text
            back = urlsplit(granted.headers["location"])
            assert back.path == "/upstream-mcp/oauthref/callback"
            done = await org.get(f"{back.path}?{back.query}")
            assert done.status_code == 302, done.text
            assert done.headers["location"] == "/connect?upstream_connected=oauthref"
            assert dict(parse_qsl(back.query)).get("iss")

    async def decide(self, principal: Principal, approval_id: str, result: str, **extra) -> httpx2.Response:
        """What a person's browser does when they press a button on a card at /approvals/{id}."""
        session_id = self.cookie(principal)
        async with httpx2.AsyncClient(base_url=self.base_url, cookies={SESSION_COOKIE: session_id}) as http:
            return await http.post(
                f"/api/approvals/{approval_id}/decide", json={"result": result, "csrf": session_id, **extra},
            )

    async def pending(self, principal: Principal, *, wait: float = 5.0) -> list:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            cards = self.web_ui.deferred_registry.list_pending(principal.id)
            if cards:
                return cards
            await asyncio.sleep(0.05)
        return []


_calls = itertools.count(1)


async def _call(stack: Stack, token: str, tool: str, args: dict | None = None):
    # A new reason for every call, so an identical request never replays an earlier decision.
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with http, streamable_http_client(f"{stack.base_url}/mcp", http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, {**(args or {}), "reason": f"integration test {next(_calls)}"})


async def _tool_names(stack: Stack, token: str) -> set[str]:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with http, streamable_http_client(f"{stack.base_url}/mcp", http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return {tool.name for tool in (await session.list_tools()).tools}


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setattr("privacyfence.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    (tmp_path / "audit").mkdir()
    audit_log.init_audit_logger(str(tmp_path / "audit"))
    org_port, ref_port = _free_port(), _free_port()
    issuer_url = f"http://localhost:{org_port}"
    ref_base = f"http://127.0.0.1:{ref_port}"

    upstream_mcp_registry.reset()
    upstreams = parse_upstreams(
        {"version": 2, "upstreams": [{"id": "oauthref", "label": "oauthref", "url": f"{ref_base}/mcp"}]},
        source="test",
    )
    upstream_mcp_runtime.configure(
        upstreams=upstreams, tool_store=ToolStore(tmp_path / "org" / "upstream_tools"),
        registrations_dir=tmp_path / "org" / "upstream_oauth",
    )

    # The reference server builds its four tools once; keep them so a test can rewrite one's description.
    listed_tools: dict[str, types.Tool] = {}
    build_tool = reference._tool

    def remember(name, *args, **kwargs):
        listed_tools[name] = build_tool(name, *args, **kwargs)
        return listed_tools[name]

    monkeypatch.setattr(reference, "_tool", remember)
    reference_app = reference.build_app(base_url=ref_base)
    upstream_server = uvicorn.Server(uvicorn.Config(reference_app, host="127.0.0.1", port=ref_port, log_level="warning"))
    upstream_thread = threading.Thread(target=upstream_server.run, daemon=True)
    upstream_thread.start()
    _wait_until_connectable(ref_port)

    # A hold window longer than a test needs: a card stays open until the test answers it.
    registry = PendingApprovalRegistry(hold_window=0.3, pending_ttl=60.0, ledger_ttl=LEDGER_TTL)
    web_ui = WebApprovalUI(registry=registry)
    approval_ui.init_approval_ui(web_ui)
    provider = OrgOAuthProvider(_idp(), idp_callback_url=f"{issuer_url}/oauth/idp/callback")

    def build_connectors(principal: Principal) -> list:
        # What daemon_main's per-principal factory does for a person's rules (ADR 0178): where an Always
        # allow rule is written, and the rules already stored.
        settings = paths.user_dir(principal) / "settings.yaml"
        settings.touch(exist_ok=True)
        auto_accept.init_config_path(str(settings))
        auto_accept.set_policy_v2_store_rules([])
        return upstream_mcp_runtime.connectors_for_principal()

    connectors = ConnectorRegistry(factory=build_connectors)
    dispatcher = McpDispatcher(lambda: connectors.get(current_principal()).connectors, registry=registry)
    sessions = OrgSessionStore()
    org = OrgAuth(
        provider=provider, sessions=sessions, idp=_idp(), issuer_url=issuer_url, connector_registry=connectors,
    )
    server = WebServer(web_ui, host="localhost", port=org_port, org=org, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable(org_port)
        yield Stack(issuer_url, provider, sessions, connectors, reference_app, web_ui, listed_tools)
    finally:
        # A card a test left open holds a worker thread that would keep the process from exiting.
        for card in registry.list_pending():
            web_ui.resolve(card.id, "deny", None, principal_id=card.principal_id)
        server.stop()
        upstream_server.should_exit = True
        upstream_thread.join(timeout=10)
        upstream_mcp_runtime.reset()
        upstream_mcp_registry.reset()


def _row(name: str) -> dict:
    rows = upstream_mcp_runtime.admin_rows(ALICE)
    return next(tool for tool in rows[0]["tools"] if tool["name"] == name)


async def _save_with_whoami_auto(gates: dict | None = None) -> None:
    pending = (await upstream_mcp_runtime.refresh("oauthref", ALICE))
    upstream_mcp_runtime.save("oauthref", gates or {"whoami": "auto"}, pending["fetched_at"])


async def _connect_both_and_save(stack: Stack) -> None:
    await stack.connect(ALICE, "alice")
    await stack.connect(BOB, "bob")
    await _save_with_whoami_auto()


async def _answer_card(stack: Stack, principal: Principal, result: str, **extra):
    cards = await stack.pending(principal)
    assert len(cards) == 1, cards
    response = await stack.decide(principal, cards[0].id, result, **extra)
    assert response.status_code == 200, response.text
    return cards[0]


async def _call_with_card(stack: Stack, principal: Principal, tool: str, result: str = "accept", **extra):
    """Call ``tool``; it must stop at a card (``approval_pending`` after the hold window); answer the card
    and call again, which finds the decision in the ledger."""
    token = stack.bearer(principal)
    first = await _call(stack, token, tool)
    assert (first.structured_content or {}).get("status") == "approval_pending", first.content
    await _answer_card(stack, principal, result, **extra)
    return await _call(stack, token, tool)


async def test_before_an_administrator_saves_a_list_the_status_says_waiting_for_admin_and_no_tool_is_listed(stack):
    await stack.connect(BOB, "bob")
    token = stack.bearer(BOB)
    assert not {name for name in await _tool_names(stack, token) if name.startswith("oauthref_")}
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with http, streamable_http_client(f"{stack.base_url}/mcp", http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            status = await session.call_tool("privacyfence_status", {})
    rows = status.structured_content["upstreams"]
    assert [(row["id"], row["state"], row["tools_exposed"]) for row in rows] == [("oauthref", "waiting_for_admin", 0)]
    assert rows[0]["admin_hint"] == "An administrator has to refresh the tool list on the Admin page."
    assert rows[0]["connected"] is True


async def test_refresh_lists_defaults_and_exposes_nothing_until_save(stack):
    await stack.connect(ALICE, "alice")
    token = stack.bearer(ALICE)
    pending = await upstream_mcp_runtime.refresh("oauthref", ALICE)
    assert {tool["name"]: tool["state"] for tool in pending["tools"]} == {name: "new" for name in _TOOLS}
    assert {tool["name"]: tool["default_gate"] for tool in pending["tools"]} == {
        "whoami": "review", "echo": "review", "rename_thing": "popup", "wipe_thing": "popup",
    }
    assert not {name for name in await _tool_names(stack, token) if name.startswith("oauthref_")}

    upstream_mcp_runtime.save("oauthref", {"whoami": "auto"}, pending["fetched_at"])
    assert {name for name in await _tool_names(stack, token) if name.startswith("oauthref_")} == {
        f"oauthref_{name}" for name in _TOOLS
    }
    assert _row("whoami")["gate"] == "auto"
    assert _row("echo")["gate"] == "review"
    assert _row("wipe_thing")["gate"] == "popup"


async def test_a_person_calls_a_review_tool_as_themselves(stack):
    await _connect_both_and_save(stack)
    token = stack.bearer(BOB)
    first = await _call(stack, token, "oauthref_echo", {"text": "hello"})
    assert first.structured_content["status"] == "approval_pending"
    await _answer_card(stack, BOB, "accept")
    echoed = await _call(stack, token, "oauthref_echo", {"text": "hello"})
    assert echoed.is_error is not True, echoed.content
    assert "hello" in echoed.content[0].text

    who = await _call_with_card(stack, BOB, "oauthref_whoami")
    assert who.is_error is not True, who.content
    assert "bob" in who.content[0].text
    assert "alice" not in who.content[0].text


async def _always_allow(stack: Stack, principal: Principal, tool: str) -> None:
    """Press Always allow on the card of ``tool`` and confirm the rule; return once the rule is stored and
    the registry no longer replays the decision of that card, so the next call is judged afresh."""
    first = await _call(stack, stack.bearer(principal), tool)
    assert first.structured_content["status"] == "approval_pending"
    card = (await stack.pending(principal))[0]
    proposals = policy_propose.proposals_for(tool, card.review_ctx)
    assert len(proposals) == 1
    assert (await stack.decide(principal, card.id, "accept_all", choice=0)).status_code == 200
    # The rule asks for its own confirmation, as every Always allow does.
    confirm = (await stack.pending(principal))[0]
    assert (await stack.decide(principal, confirm.id, "confirm")).status_code == 200
    settings = paths.user_dir(principal) / "settings.yaml"
    deadline = time.monotonic() + 5
    while "upstream.always_allow" not in settings.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, "the Always allow rule was never stored"
        await asyncio.sleep(0.05)
    await asyncio.sleep(LEDGER_TTL + 0.2)


async def test_always_allow_for_an_auto_tool_removes_the_card(stack):
    await _connect_both_and_save(stack)
    token = stack.bearer(BOB)
    await _always_allow(stack, BOB, "oauthref_whoami")
    again = await _call(stack, token, "oauthref_whoami")
    assert again.is_error is not True, again.content
    assert "bob" in again.content[0].text
    assert not stack.web_ui.deferred_registry.list_pending(BOB.id)

    # echo is configured review, not auto: its card offers no Always allow.
    shown = await _call(stack, token, "oauthref_echo", {"text": "again"})
    assert shown.structured_content["status"] == "approval_pending"
    echo_card = (await stack.pending(BOB))[0]
    assert policy_propose.proposals_for("oauthref_echo", echo_card.review_ctx) == []


async def test_lowering_the_ceiling_makes_the_stored_rule_inert(stack):
    await _connect_both_and_save(stack)
    token = stack.bearer(BOB)
    await _always_allow(stack, BOB, "oauthref_whoami")
    assert "bob" in (await _call(stack, token, "oauthref_whoami")).content[0].text

    upstream_mcp_runtime.set_gates("oauthref", {"whoami": "review"})
    after = await _call(stack, token, "oauthref_whoami")
    assert (after.structured_content or {}).get("status") == "approval_pending", after.content
    assert len(await stack.pending(BOB)) == 1


async def test_a_changed_tool_is_held_until_save_and_its_ceiling_is_reset(stack):
    await _connect_both_and_save(stack)
    token = stack.bearer(BOB)
    await _always_allow(stack, BOB, "oauthref_whoami")
    assert "bob" in (await _call(stack, token, "oauthref_whoami")).content[0].text

    stack.reference_app.state.extra_tools.append(types.Tool(
        name="new_thing", description="A tool that appeared later.",
        input_schema={"type": "object", "properties": {}}, annotations=types.ToolAnnotations(read_only_hint=True),
    ))
    first = await upstream_mcp_runtime.refresh("oauthref", ALICE)
    assert {tool["name"]: tool["state"] for tool in first["tools"]}["new_thing"] == "new"
    assert "oauthref_new_thing" not in await _tool_names(stack, token)

    # The server rewrites whoami's description: the next refresh marks it changed, at its class default.
    stack.listed_tools["whoami"].description = "Now it says something else."
    second = await upstream_mcp_runtime.refresh("oauthref", ALICE)
    changed = next(tool for tool in second["tools"] if tool["name"] == "whoami")
    assert (changed["state"], changed["default_gate"], changed["gate"]) == ("changed", "review", "review")

    # A save that carries the first refresh's stamp is refused, and the hold still stands.
    with pytest.raises(upstream_mcp_runtime.UpstreamAdminError) as refused:
        upstream_mcp_runtime.save("oauthref", {}, first["fetched_at"])
    assert refused.value.args[0] == "The tool list changed since you refreshed it. Refresh again."
    assert "oauthref_new_thing" not in await _tool_names(stack, token)

    # Saving without choosing auto again lowers whoami: Bob's stored rule no longer removes the card.
    upstream_mcp_runtime.save("oauthref", {}, second["fetched_at"])
    assert "oauthref_new_thing" in await _tool_names(stack, token)
    assert _row("whoami")["gate"] == "review"
    again = await _call(stack, token, "oauthref_whoami")
    assert (again.structured_content or {}).get("status") == "approval_pending", again.content


def _create_token(principal: Principal, tools, *, rules=(), approved=(), is_admin=False):
    return scoped_tokens.get_store().create(
        principal, label="n8n workflow", days=30, rules=list(rules), tools=list(tools), policy=_POLICY,
        principal_email_verified=True, is_admin=is_admin,
        upstream_approvals=[
            {"tool": tool, "digest": scopes.UPSTREAM_DECLARATIONS[auto_accept.TOOL_TO_OPERATION[tool]]}
            for tool in approved
        ],
    )[1]


_REFUSED = (
    "oauthref_wipe_thing changes data or releases results unseen; an administrator must approve it "
    "on the New token form, or the token needs an Always allow rule for it."
)
_WHOAMI_RULE = {"kind": "new", "group": "upstream.always_allow/oauthref_whoami", "value": "", "verbs": ["read"]}


async def test_a_scoped_token_needs_an_approval_or_a_rule(stack):
    await _connect_both_and_save(stack)

    for principal, is_admin in ((ALICE, True), (BOB, False)):
        with pytest.raises(scoped_tokens.ScopedTokenError) as refused:
            _create_token(principal, ["oauthref_wipe_thing"], is_admin=is_admin)
        assert str(refused.value) == _REFUSED

    # The administrator's tick approves the tool; the unattended call goes through.
    secret = _create_token(ALICE, ["oauthref_wipe_thing"], approved=["oauthref_wipe_thing"], is_admin=True)
    wiped = await _call(stack, secret, "oauthref_wipe_thing")
    assert wiped.is_error is not True, wiped.content
    assert "wiped" in wiped.content[0].text

    # A non-administrator's token for the auto tool carries the per-tool rule instead.
    ruled = _create_token(BOB, ["oauthref_whoami"], rules=[_WHOAMI_RULE])
    result = await _call(stack, ruled, "oauthref_whoami")
    assert result.is_error is not True, result.content
    assert "bob" in result.content[0].text
    assert not stack.web_ui.deferred_registry.list_pending(BOB.id)

    # Lowering the ceiling makes the rule inert; a token never gets a card, so the call is denied.
    upstream_mcp_runtime.set_gates("oauthref", {"whoami": "review"})
    denied = await _call(stack, ruled, "oauthref_whoami")
    assert denied.is_error is True
    assert not stack.web_ui.deferred_registry.list_pending(BOB.id)


async def test_a_revoked_connection_gives_the_expired_message(stack):
    await _connect_both_and_save(stack)
    token_file = token_path(BOB, "oauthref")
    assert token_file.exists()
    stack.reference_app.state.revoke_all()
    result = await _call(stack, stack.bearer(BOB), "oauthref_whoami")
    assert result.is_error is True
    assert result.content[0].text.startswith("The oauthref sign-in has expired or was revoked.")
    assert not token_file.exists()
