"""The upstream MCP runtime: configure, who sees the tools, refresh, save, status and Admin rows (ADR 0329, 0331)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from mcp import types

from privacyfence import auto_accept, paths, upstream_mcp_registry, upstream_mcp_runtime as runtime
from privacyfence.org_mode import ConfigurationError
from privacyfence.policy import scopes
from privacyfence.principal import Principal, principal_scope
from privacyfence.scoped_tokens import token_scope
from privacyfence.upstream_mcp_client import CallerNotAllowedError, UpstreamMcpClientError
from privacyfence.upstream_mcp_config import UpstreamOAuthSettings, UpstreamServer
from privacyfence.upstream_oauth_tokens import (
    OAuthClient,
    TokenRecord,
    save_token,
    token_path,
)
from privacyfence.upstream_tools import ToolEntry, ToolStore, classify, tool_digest

pytestmark = pytest.mark.unit

URL = "https://mcp.example.com/mcp"
ALICE = Principal(id="alice", email="alice@example.com")
BOB = Principal(id="bob", email="bob@example.com")
SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def upstream(upstream_id="canva", label="Canva", url=URL, **oauth) -> UpstreamServer:
    return UpstreamServer(
        id=upstream_id, label=label, url=url, oauth=UpstreamOAuthSettings(scopes=("mcp:read",), **oauth),
    )


def tool(name, description="", read_only=None, destructive=None, schema=None) -> types.Tool:
    annotations = None
    if read_only is not None or destructive is not None:
        annotations = types.ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive)
    return types.Tool(
        name=name, description=description, input_schema=schema if schema is not None else SCHEMA,
        annotations=annotations,
    )


class FakeClient:
    def __init__(self, tools=(), error=None):
        self.tools = list(tools)
        self.error = error
        self.tokens: list[str] = []
        self.calls = 0

    async def list_tools(self, *, bearer):
        self.calls += 1
        if self.error is not None:
            raise UpstreamMcpClientError(self.error)
        self.tokens.append(await bearer(False))
        return list(self.tools)


class FakeAudit:
    def __init__(self):
        self.entries = []

    def record(self, entry):
        self.entries.append(entry)


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


@pytest.fixture
def audit(monkeypatch):
    fake = FakeAudit()
    monkeypatch.setattr(runtime, "get_audit_logger", lambda: fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(runtime, "UpstreamMcpClient", lambda upstream, transport=None: fake)
    return fake


@pytest.fixture
def store(tmp_path):
    return ToolStore(tmp_path / "org" / "upstream_tools")


def configure(store, tmp_path, *upstreams):
    runtime.configure(
        upstreams=upstreams or (upstream(),), tool_store=store, registrations_dir=tmp_path / "org" / "upstream_oauth",
    )


def connect(principal, upstream_id="canva", **over):
    base = dict(
        upstream_id=upstream_id, access_token=f"access-{principal.id}", refresh_token="refresh",
        expires_at=time.time() + 3600, scope="mcp:read", issuer="https://auth.example.com", client_id="cid",
        auth_method="none", token_endpoint="https://auth.example.com/token", resource=URL, connected_at=1.0,
    )
    base.update(over)
    save_token(token_path(principal, upstream_id), TokenRecord(**base))


def saved_tool(name, gate="review", read_only=True, destructive=None, description="d") -> object:
    entry = ToolEntry(name, description, SCHEMA, read_only, destructive)
    from privacyfence.upstream_tools import SavedTool

    _default, verb = classify(read_only, destructive)
    return SavedTool(name, description, SCHEMA, read_only, destructive, gate, verb, tool_digest(entry))


def seed(store, *tools, upstream_id="canva", url=URL):
    store.save(upstream_id, url, tuple(tools), datetime(2026, 1, 2, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- #
# configure
# --------------------------------------------------------------------------- #


class TestConfigure:
    def test_it_is_not_configured_by_default(self):
        assert not runtime.configured()
        assert runtime.upstream_ids() == frozenset()
        assert runtime.oauth_upstream("canva") is None
        assert runtime.connectors_for_principal() == []
        assert runtime.status() == []
        assert runtime.admin_rows() == []
        with pytest.raises(runtime.UpstreamAdminError, match="Unknown upstream."):
            runtime.registrations_for("canva")

    def test_it_registers_the_saved_list_with_no_network_call(self, store, tmp_path, client):
        seed(store, saved_tool("search"), saved_tool("make", "popup", False, True), saved_tool("peek", "auto"))
        configure(store, tmp_path)

        assert runtime.configured()
        assert runtime.upstream_ids() == frozenset({"canva"})
        assert runtime.oauth_upstream("canva") == upstream()
        assert auto_accept.TOOL_TO_GATE["canva_search"] == "review"
        assert auto_accept.TOOL_TO_GATE["canva_make"] == "popup"
        assert auto_accept.TOOL_TO_GATE["canva_peek"] == "review"  # auto is a ceiling, never a gate
        assert scopes.UPSTREAM_AUTO_TOOLS == {"canva_peek"}
        assert client.calls == 0
        assert runtime.registrations_for("canva") is not None
        assert runtime.tokens_for("canva") is not None

    def test_an_empty_secret_variable_refuses_the_start(self, store, tmp_path, monkeypatch):
        monkeypatch.delenv("CANVA_SECRET", raising=False)
        with pytest.raises(ConfigurationError) as raised:
            configure(store, tmp_path, upstream(client_id="cid", client_secret_env="CANVA_SECRET"))
        assert str(raised.value) == 'Upstream "canva": oauth.client_secret_env names CANVA_SECRET, which is not set.'
        monkeypatch.setenv("CANVA_SECRET", "")
        with pytest.raises(ConfigurationError):
            configure(store, tmp_path, upstream(client_id="cid", client_secret_env="CANVA_SECRET"))
        monkeypatch.setenv("CANVA_SECRET", "s3cret")
        configure(store, tmp_path, upstream(client_id="cid", client_secret_env="CANVA_SECRET"))
        assert runtime.configured()

    def test_a_list_made_for_another_address_is_ignored_with_a_warning(self, store, tmp_path, caplog):
        seed(store, saved_tool("search"), url="https://other.example.com/mcp")
        with caplog.at_level(logging.WARNING):
            configure(store, tmp_path)
        assert (
            "Upstream canva: the saved tool list was made for another address and is ignored; refresh the tools."
            in caplog.text
        )
        assert "canva_search" not in auto_accept.TOOL_TO_GATE
        assert runtime.status()[0]["state"] == "waiting_for_admin"

    def test_an_upstream_with_no_scopes_logs_the_warning(self, store, tmp_path, caplog):
        bare = UpstreamServer(id="canva", label="Canva", url=URL, oauth=UpstreamOAuthSettings())
        with caplog.at_level(logging.WARNING):
            configure(store, tmp_path, bare)
        assert (
            "Upstream canva declares no oauth.scopes; it will ask for every scope the server lists." in caplog.text
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            configure(store, tmp_path)
        assert "declares no oauth.scopes" not in caplog.text

    def test_a_restart_restores_the_same_tables(self, store, tmp_path):
        seed(store, saved_tool("search"), saved_tool("peek", "auto"))
        configure(store, tmp_path)
        before = (dict(auto_accept.TOOL_TO_GATE), set(scopes.UPSTREAM_AUTO_TOOLS), dict(scopes.UPSTREAM_DECLARATIONS))
        runtime.reset()
        upstream_mcp_registry.reset()
        assert "canva_search" not in auto_accept.TOOL_TO_GATE
        configure(store, tmp_path)
        assert (
            dict(auto_accept.TOOL_TO_GATE), set(scopes.UPSTREAM_AUTO_TOOLS), dict(scopes.UPSTREAM_DECLARATIONS),
        ) == before


class TestClientSource:
    """A declared client's secret method after a restart (before any Connect in this process)."""

    def _source(self, store, tmp_path, monkeypatch, **oauth):
        monkeypatch.setenv("CANVA_SECRET", "s3cret")
        configure(store, tmp_path, upstream(client_id="cid", client_secret_env="CANVA_SECRET", **oauth))
        return runtime.tokens_for("canva")._client_source

    def test_the_method_of_the_saved_sign_in_is_used(self, store, tmp_path, monkeypatch):
        source = self._source(store, tmp_path, monkeypatch)
        assert source("canva").auth_method == "client_secret_basic"  # what client_for alone would say
        connect(ALICE, auth_method="client_secret_post")
        assert source("canva") == OAuthClient("cid", "s3cret", "client_secret_post")

    def test_a_sign_in_of_another_client_or_without_a_secret_method_is_ignored(self, store, tmp_path, monkeypatch):
        source = self._source(store, tmp_path, monkeypatch)
        connect(ALICE, client_id="other", auth_method="client_secret_post")
        connect(BOB, auth_method="none")
        assert source("canva").auth_method == "client_secret_basic"
        (token_path(BOB, "canva")).write_text("not json")
        assert source("canva").auth_method == "client_secret_basic"

    def test_a_declared_client_without_a_secret_and_a_dynamic_client_are_returned_as_they_are(
        self, store, tmp_path, monkeypatch,
    ):
        configure(store, tmp_path, upstream(client_id="cid"))
        connect(ALICE, auth_method="client_secret_post")
        assert runtime.tokens_for("canva")._client_source("canva") == OAuthClient("cid", None, "none")
        configure(store, tmp_path)
        runtime.registrations_for("canva")._write({
            "version": 1, "upstream_id": "canva", "issuer": "https://auth.example.com", "redirect_uri": "r",
            "client_id": "dyn", "client_secret": "x", "client_secret_expires_at": 0, "auth_method": "client_secret_post",
            "registered_at": 1.0,
        })
        assert runtime.tokens_for("canva")._client_source("canva").client_id == "dyn"


# --------------------------------------------------------------------------- #
# who sees the tools, who may call
# --------------------------------------------------------------------------- #


class TestConnectors:
    def test_a_connector_needs_a_saved_list_and_the_persons_sign_in(self, store, tmp_path):
        configure(store, tmp_path)
        connect(ALICE)
        with principal_scope(ALICE):
            assert runtime.connectors_for_principal() == []  # no saved list
        seed(store, saved_tool("search"))
        configure(store, tmp_path)
        with principal_scope(BOB):
            assert runtime.connectors_for_principal() == []  # no sign-in
        with principal_scope(ALICE):
            connectors = runtime.connectors_for_principal()
        assert [c.name for c in connectors] == ["canva"]
        assert [spec.name for spec in connectors[0].tool_specs()] == ["canva_search"]

    @pytest.mark.parametrize(
        ("allowed", "scoped", "refused"),
        [
            (("claude_client", "n8n_workflow"), False, None),
            (("claude_client", "n8n_workflow"), True, None),
            (("claude_client",), False, None),
            (("claude_client",), True, "n8n_workflow"),
            (("n8n_workflow",), False, "claude_client"),
            (("n8n_workflow",), True, None),
        ],
    )
    def test_admit_caller(self, allowed, scoped, refused):
        server = UpstreamServer(
            id="canva", label="Canva", url=URL, oauth=UpstreamOAuthSettings(), allowed_principal_types=allowed,
        )
        if scoped:
            with token_scope(SimpleNamespace(id="st-1")):
                self._check(server, refused)
        else:
            self._check(server, refused)

    @staticmethod
    def _check(server, refused):
        if refused is None:
            runtime.admit_caller(server)
            return
        with pytest.raises(CallerNotAllowedError) as raised:
            runtime.admit_caller(server)
        assert raised.value.principal_type == refused

    async def test_the_connector_uses_the_persons_own_token_file(self, store, tmp_path, client):
        seed(store, saved_tool("search"))
        configure(store, tmp_path)
        connect(ALICE)
        connect(BOB)
        with principal_scope(BOB):
            connector = runtime.connectors_for_principal()[0]
        assert await connector._bearer(False) == "access-bob"


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #


def read_tool(name):
    return tool(name, f"{name} tool", read_only=True)


class TestRefresh:
    async def test_it_classifies_with_the_administrators_token_and_exposes_nothing(
        self, store, tmp_path, client, audit,
    ):
        client.tools = [
            read_tool("search"),
            tool("rename"),
            tool("both", read_only=True, destructive=True),
            tool("update", destructive=False),
            tool("bad name!"),
        ]
        configure(store, tmp_path)
        connect(ALICE)
        connect(BOB)
        pending = await runtime.refresh("canva", BOB)

        assert client.tokens == ["access-bob"]
        rows = {row["name"]: row for row in pending["tools"]}
        assert rows["search"]["default_gate"] == "review" and rows["search"]["verb"] == "read"
        assert rows["rename"]["default_gate"] == "popup" and rows["rename"]["verb"] == "delete"
        assert rows["both"]["default_gate"] == "popup" and rows["both"]["verb"] == "delete"
        assert rows["update"]["default_gate"] == "popup" and rows["update"]["verb"] == "update"
        assert rows["bad name!"]["state"] == "unsupported"
        assert rows["bad name!"]["reason"] == "its name is not 1-48 letters, digits, underscores or hyphens"
        assert pending["new"] == ["both", "rename", "search", "update"]
        assert pending["unsupported"] == [
            {"name": "bad name!", "reason": "its name is not 1-48 letters, digits, underscores or hyphens"},
        ]
        assert pending["changed"] == [] and pending["removed"] == []
        # nothing is exposed or registered
        assert "canva_search" not in auto_accept.TOOL_TO_GATE
        assert store.load("canva") is None
        with principal_scope(BOB):
            assert runtime.connectors_for_principal() == []
        assert runtime.status()[0]["tools_exposed"] == 0
        assert [entry.decision for entry in audit.entries] == ["upstream_tools_refreshed"]

    async def test_it_diffs_against_the_saved_list_and_offers_a_changed_tool_its_class_default(
        self, store, tmp_path, client,
    ):
        seed(
            store, saved_tool("search", "auto"), saved_tool("keep", "auto", description="same"),
            saved_tool("gone"), saved_tool("make", "review", False, True, "make tool"),
        )
        configure(store, tmp_path)
        connect(ALICE)
        client.tools = [
            tool("search", "new text", read_only=True),
            tool("keep", "same", read_only=True),
            tool("make", "make tool", read_only=False, destructive=True),
            tool("fresh", read_only=True),
        ]
        pending = await runtime.refresh("canva", ALICE)
        rows = {row["name"]: row for row in pending["tools"]}

        assert (pending["new"], pending["changed"], pending["removed"]) == (["fresh"], ["search"], ["gone"])
        assert rows["search"]["state"] == "changed" and rows["search"]["gate"] == "review"  # not its saved auto
        assert rows["keep"]["state"] == "unchanged" and rows["keep"]["gate"] == "auto"
        assert rows["make"]["state"] == "unchanged" and rows["make"]["gate"] == "review"
        assert rows["fresh"]["gate"] == "review"
        # the saved list is untouched until save
        assert auto_accept.TOOL_TO_GATE["canva_search"] == "review"
        assert "canva_search" in scopes.UPSTREAM_AUTO_TOOLS
        assert runtime.admin_rows()[0]["pending"]["changed"] == ["search"]

    async def test_a_second_refresh_replaces_the_pending_list(self, store, tmp_path, client):
        configure(store, tmp_path)
        connect(ALICE)
        client.tools = [read_tool("one")]
        first = await runtime.refresh("canva", ALICE)
        client.tools = [read_tool("two")]
        second = await runtime.refresh("canva", ALICE)
        assert second["fetched_at"] > first["fetched_at"]
        assert second["new"] == ["two"]
        assert runtime.admin_rows()[0]["pending"]["new"] == ["two"]

    async def test_descriptions_are_cleaned_and_cut(self, store, tmp_path, client):
        client.tools = [tool("long", "a\x00b\n" + "x" * 500, read_only=True)]
        configure(store, tmp_path)
        connect(ALICE)
        pending = await runtime.refresh("canva", ALICE)
        assert pending["tools"][0]["description"] == "ab" + "x" * 198

    @pytest.mark.parametrize(
        ("kind", "message", "status"),
        [
            ("not_connected", "Connect Canva on the Connect page (/connect) first, then refresh.", 409),
            ("unreachable", "The Canva server could not be reached.", 502),
            ("auth_expired", "The Canva sign-in has expired or was revoked. Connect Canva on the Connect page (/connect) to sign in again.", 502),
            ("tool_error", "The Canva server could not be reached.", 502),
        ],
    )
    async def test_failures_use_the_connector_messages(self, store, tmp_path, client, audit, kind, message, status):
        client.error = kind
        configure(store, tmp_path)
        with pytest.raises(runtime.UpstreamRefreshError) as raised:
            await runtime.refresh("canva", ALICE)
        assert str(raised.value) == message
        assert raised.value.status == status
        assert audit.entries == []
        assert runtime.admin_rows()[0]["pending"] is None

    async def test_an_unknown_upstream_is_refused(self, store, tmp_path, client):
        configure(store, tmp_path)
        with pytest.raises(runtime.UpstreamAdminError, match="Unknown upstream."):
            await runtime.refresh("nope", ALICE)

    async def test_an_unconnected_administrator_gets_the_real_client_message(self, store, tmp_path):
        """No fake client: the real one maps the missing token file to ``not_connected``."""
        configure(store, tmp_path)
        with pytest.raises(runtime.UpstreamRefreshError) as raised:
            await runtime.refresh("canva", ALICE)
        assert str(raised.value) == "Connect Canva on the Connect page (/connect) first, then refresh."


# --------------------------------------------------------------------------- #
# save and set_gates
# --------------------------------------------------------------------------- #


class TestSave:
    async def _pending(self, store, tmp_path, client, *tools):
        client.tools = list(tools)
        configure(store, tmp_path)
        connect(ALICE)
        return await runtime.refresh("canva", ALICE)

    async def test_save_without_a_pending_list_is_a_409(self, store, tmp_path, client):
        configure(store, tmp_path)
        with pytest.raises(runtime.UpstreamAdminError) as raised:
            runtime.save("canva", {}, 1.0)
        assert str(raised.value) == "Refresh the tool list first."
        assert raised.value.status == 409

    async def test_save_with_another_fetched_at_is_a_409(self, store, tmp_path, client):
        pending = await self._pending(store, tmp_path, client, read_tool("search"))
        with pytest.raises(runtime.UpstreamAdminError) as raised:
            runtime.save("canva", {}, pending["fetched_at"] + 1)
        assert str(raised.value) == "The tool list changed since you refreshed it. Refresh again."
        assert raised.value.status == 409
        assert store.load("canva") is None

    async def test_save_refuses_a_bad_gate_and_an_unknown_upstream(self, store, tmp_path, client):
        pending = await self._pending(store, tmp_path, client, read_tool("search"))
        with pytest.raises(runtime.UpstreamAdminError, match="Gate must be review, popup or auto."):
            runtime.save("canva", {"search": "never"}, pending["fetched_at"])
        with pytest.raises(runtime.UpstreamAdminError, match="Unknown upstream."):
            runtime.save("nope", {}, 1.0)

    async def test_save_applies_the_gates_defaults_and_registers_everything(self, store, tmp_path, client, audit):
        callbacks = []
        runtime.set_tools_changed_callback(lambda: callbacks.append(1))
        pending = await self._pending(
            store, tmp_path, client,
            read_tool("search"), read_tool("peek"), tool("rename"), tool("bad name!"), tool("also", read_only=True),
        )
        runtime.save("canva", {"peek": "auto", "search": "popup", "rename": "review"}, pending["fetched_at"])

        saved = {item.name: item for item in store.load("canva").tools}
        assert set(saved) == {"search", "peek", "rename", "also"}  # never the unsupported tool
        assert {name: item.gate for name, item in saved.items()} == {
            "search": "popup", "peek": "auto", "rename": "review", "also": "review",
        }
        assert store.load("canva").url == URL
        assert auto_accept.TOOL_TO_GATE["canva_peek"] == "review"  # never auto
        assert auto_accept.TOOL_TO_GATE["canva_search"] == "popup"
        assert auto_accept.TOOL_TO_GATE["canva_rename"] == "review"
        assert scopes.UPSTREAM_AUTO_TOOLS == {"canva_peek"}
        assert callbacks == [1]
        assert runtime.admin_rows()[0]["pending"] is None
        assert runtime.status()[0]["state"] == "ready"
        with principal_scope(ALICE):
            specs = runtime.connectors_for_principal()[0].tool_specs()
        assert {spec.name for spec in specs} == {"canva_search", "canva_peek", "canva_rename", "canva_also"}
        assert [entry.decision for entry in audit.entries] == ["upstream_tools_refreshed", "upstream_tools_saved"]
        assert audit.entries[-1].summary == "Saved 4 tool(s) of canva: 2 review, 1 popup, 1 auto"
        # a second save of the same pending list is refused: it was cleared
        with pytest.raises(runtime.UpstreamAdminError, match="Refresh the tool list first."):
            runtime.save("canva", {}, pending["fetched_at"])

    async def test_a_removed_tool_disappears_and_a_changed_tool_resets_to_its_class_default(
        self, store, tmp_path, client,
    ):
        seed(store, saved_tool("search", "auto"), saved_tool("gone"), saved_tool("keep", "auto", description="same"))
        configure(store, tmp_path)
        connect(ALICE)
        client.tools = [tool("search", "changed", read_only=True), tool("keep", "same", read_only=True)]
        pending = await runtime.refresh("canva", ALICE)
        runtime.save("canva", {}, pending["fetched_at"])
        assert {item.name: item.gate for item in store.load("canva").tools} == {"search": "review", "keep": "auto"}
        assert "canva_gone" not in auto_accept.TOOL_TO_GATE
        assert scopes.UPSTREAM_AUTO_TOOLS == {"canva_keep"}

    async def test_a_name_that_collides_with_a_built_in_tool_is_refused_and_nothing_changes(
        self, store, tmp_path, client,
    ):
        pending = await self._pending(store, tmp_path, client, read_tool("search"))
        auto_accept.TOOL_TO_GATE["canva_search"] = "review"
        try:
            with pytest.raises(runtime.UpstreamAdminError) as raised:
                runtime.save("canva", {}, pending["fetched_at"])
        finally:
            del auto_accept.TOOL_TO_GATE["canva_search"]
        assert raised.value.status == 409
        assert "collides" in str(raised.value)
        assert store.load("canva") is None
        assert runtime.admin_rows()[0]["pending"] is not None

    async def test_a_failing_store_write_puts_the_previous_registration_back(
        self, store, tmp_path, client, monkeypatch,
    ):
        seed(store, saved_tool("old"))
        configure(store, tmp_path)
        connect(ALICE)
        client.tools = [read_tool("fresh")]
        pending = await runtime.refresh("canva", ALICE)

        def fail(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(store, "save", fail)
        with pytest.raises(OSError):
            runtime.save("canva", {}, pending["fetched_at"])
        assert "canva_old" in auto_accept.TOOL_TO_GATE
        assert "canva_fresh" not in auto_accept.TOOL_TO_GATE
        assert runtime.admin_rows()[0]["tools"][0]["name"] == "old"
        assert runtime.admin_rows()[0]["pending"] is not None

    async def test_an_audit_failure_changes_nothing(self, store, tmp_path, client, monkeypatch, caplog):
        def broken():
            raise RuntimeError("no audit log")

        monkeypatch.setattr(runtime, "get_audit_logger", broken)
        pending = await self._pending(store, tmp_path, client, read_tool("search"))
        with caplog.at_level(logging.WARNING):
            runtime.save("canva", {}, pending["fetched_at"])
        assert "Audit log write failed" in caplog.text
        assert store.load("canva") is not None


class TestSetGates:
    def _seeded(self, store, tmp_path, callbacks=None):
        seed(store, saved_tool("search"), saved_tool("make", "popup", False, True))
        configure(store, tmp_path)
        if callbacks is not None:
            runtime.set_tools_changed_callback(lambda: callbacks.append(1))

    def test_it_changes_a_saved_tools_gate(self, store, tmp_path, audit):
        callbacks: list[int] = []
        self._seeded(store, tmp_path, callbacks)
        runtime.set_gates("canva", {"search": "auto"})
        assert {item.name: item.gate for item in store.load("canva").tools} == {"search": "auto", "make": "popup"}
        assert scopes.UPSTREAM_AUTO_TOOLS == {"canva_search"}
        assert auto_accept.TOOL_TO_GATE["canva_search"] == "review"
        assert callbacks == [1]
        assert audit.entries[0].decision == "upstream_tool_gates_changed"
        assert audit.entries[0].summary == "Changed the gates of 1 tool(s) of canva"
        runtime.set_gates("canva", {"search": "popup"})
        assert scopes.UPSTREAM_AUTO_TOOLS == set()

    def test_it_refuses_an_unknown_tool_a_bad_gate_and_an_unknown_upstream(self, store, tmp_path):
        self._seeded(store, tmp_path)
        with pytest.raises(runtime.UpstreamAdminError) as raised:
            runtime.set_gates("canva", {"nope": "auto"})
        assert str(raised.value) == "That tool is not in the saved list."
        with pytest.raises(runtime.UpstreamAdminError, match="Gate must be review, popup or auto."):
            runtime.set_gates("canva", {"search": "always"})
        with pytest.raises(runtime.UpstreamAdminError, match="Unknown upstream."):
            runtime.set_gates("nope", {})
        assert {item.gate for item in store.load("canva").tools} == {"review", "popup"}

    async def test_a_pending_list_shows_the_new_saved_gate_of_an_unchanged_tool(self, store, tmp_path, client):
        self._seeded(store, tmp_path)
        connect(ALICE)
        client.tools = [tool("search", "d", read_only=True)]
        await runtime.refresh("canva", ALICE)
        runtime.set_gates("canva", {"search": "auto"})
        assert runtime.admin_rows()[0]["pending"]["tools"][0]["gate"] == "auto"


# --------------------------------------------------------------------------- #
# status and the Admin rows
# --------------------------------------------------------------------------- #


class TestStatus:
    def test_waiting_for_admin_and_ready_with_the_hints(self, store, tmp_path):
        configure(store, tmp_path, upstream(), upstream("framer", "Framer", "https://mcp.framer.com/mcp"))
        seed(store, saved_tool("search"), upstream_id="framer", url="https://mcp.framer.com/mcp")
        configure(store, tmp_path, upstream(), upstream("framer", "Framer", "https://mcp.framer.com/mcp"))
        connect(ALICE, "framer")
        with principal_scope(ALICE):
            rows = {row["id"]: row for row in runtime.status()}
        assert rows["canva"] == {
            "id": "canva", "label": "Canva", "tools_exposed": 0, "state": "waiting_for_admin", "connected": False,
            "admin_hint": "An administrator has to refresh the tool list on the Admin page.",
            "connect_hint": "Connect Canva on the Connect page (/connect).",
        }
        assert rows["framer"] == {
            "id": "framer", "label": "Framer", "tools_exposed": 1, "state": "ready", "connected": True,
        }
        with principal_scope(BOB):
            ready = {row["id"]: row for row in runtime.status()}["framer"]
        assert ready["connected"] is False
        assert ready["connect_hint"] == "Connect Framer on the Connect page (/connect)."

    def test_admin_rows_carry_the_fields_and_no_secret(self, store, tmp_path):
        seed(store, saved_tool("search", "auto", description="a\x07" + "y" * 300), saved_tool("make", "popup", False, True))
        configure(store, tmp_path, UpstreamServer(
            id="canva", label="Canva", url="https://mcp.example.com/mcp?key=SECRET", oauth=UpstreamOAuthSettings(
                scopes=("a", "b"), client_id="DECLARED-CLIENT-ID", issuer="https://auth.example.com/tenant",
            ),
        ))
        # a saved list is only used for the address it was made for
        seed(store, saved_tool("search", "auto", description="a\x07" + "y" * 300), saved_tool("make", "popup", False, True),
             url="https://mcp.example.com/mcp?key=SECRET")
        configure(store, tmp_path, UpstreamServer(
            id="canva", label="Canva", url="https://mcp.example.com/mcp?key=SECRET", oauth=UpstreamOAuthSettings(
                scopes=("a", "b"), client_id="DECLARED-CLIENT-ID", issuer="https://auth.example.com/tenant",
            ),
        ))
        connect(ALICE, access_token="TOP-SECRET-ACCESS")
        connect(BOB)
        row = runtime.admin_rows(ALICE)[0]
        assert row["url"] == "https://mcp.example.com/…"
        assert row["issuer_host"] == "auth.example.com"
        assert row["registration"] == "declared"
        assert row["scopes"] == ["a", "b"]
        assert row["connected_people"] == 2
        assert row["admin_connected"] is True
        assert row["allowed_principal_types"] == ["claude_client", "n8n_workflow"]
        assert row["saved_at"] == "2026-01-02T00:00:00+00:00"
        assert row["pending"] is None
        assert row["tools"][0] == {
            "name": "search", "exposed_name": "canva_search", "verb": "read", "read_only": True,
            "default_gate": "review", "gate": "auto", "description": "a" + "y" * 199,
        }
        assert row["tools"][1]["default_gate"] == "popup" and row["tools"][1]["gate"] == "popup"
        dumped = repr(row)
        for forbidden in ("TOP-SECRET-ACCESS", "DECLARED-CLIENT-ID", "SECRET", "alice", "bob", "refresh"):
            assert forbidden not in dumped
        assert runtime.admin_rows(Principal(id="carol"))[0]["admin_connected"] is False

    def test_the_issuer_host_comes_from_a_sign_in_when_not_pinned_and_is_none_before(self, store, tmp_path):
        configure(store, tmp_path)
        assert runtime.admin_rows(ALICE)[0]["issuer_host"] is None
        assert runtime.admin_rows(ALICE)[0]["registration"] == "dynamic"
        token_path(ALICE, "canva").parent.mkdir(parents=True, exist_ok=True)
        token_path(ALICE, "canva").write_text("not json")
        assert runtime.admin_rows(ALICE)[0]["issuer_host"] is None
        connect(BOB)
        assert runtime.admin_rows(ALICE)[0]["issuer_host"] == "auth.example.com"

    def test_the_admin_row_defaults_to_the_current_principal(self, store, tmp_path):
        configure(store, tmp_path)
        connect(ALICE)
        with principal_scope(ALICE):
            assert runtime.admin_rows()[0]["admin_connected"] is True
        assert runtime.admin_rows()[0]["admin_connected"] is False

    def test_an_upstream_url_with_a_plain_origin_is_shown_whole(self, store, tmp_path):
        configure(store, tmp_path, upstream(url="https://mcp.example.com/"))
        assert runtime.admin_rows(ALICE)[0]["url"] == "https://mcp.example.com"


class TestCallback:
    def test_the_callback_can_be_set_and_cleared(self, store, tmp_path):
        seed(store, saved_tool("search"))
        configure(store, tmp_path)
        calls: list[int] = []
        runtime.set_tools_changed_callback(lambda: calls.append(1))
        runtime.set_gates("canva", {"search": "auto"})
        runtime.set_tools_changed_callback(None)
        runtime.set_gates("canva", {"search": "popup"})
        assert calls == [1]
        runtime.set_tools_changed_callback(lambda: calls.append(2))
        runtime.reset()
        seed(store, saved_tool("search"))
        configure(store, tmp_path)
        runtime.set_gates("canva", {"search": "auto"})
        assert calls == [1]
