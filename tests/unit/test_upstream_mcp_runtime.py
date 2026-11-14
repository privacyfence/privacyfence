"""upstream_mcp_runtime: the caller claim, held audiences, status and the startup tool-list fetch."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import types

from privacyfence import oidc_provider, scoped_tokens, upstream_mcp_runtime
from privacyfence.principal import Principal, principal_scope
from privacyfence.upstream_claims import ClaimSigner
from privacyfence.upstream_mcp_client import UpstreamMcpClientError
from privacyfence.upstream_mcp_config import GrantStore, parse_upstreams

_FILE = {
    "version": 1,
    "upstreams": [
        {
            "id": "kh", "label": "Knowledge hub", "url": "https://kh.example/mcp", "audience": "kh-aud",
            "tools": {
                "search": {"gate": "auto", "verb": "search"},
                "get_doc": {"gate": "auto", "verb": "read"},
            },
        },
        {
            "id": "wiki", "label": "Wiki", "url": "https://wiki.example/mcp", "audience": "wiki-aud",
            "tools": {"search": {"gate": "auto", "verb": "search"}},
        },
    ],
}
_GRANTS = {
    "upstream_mcp": {
        "kh": {"grants": [
            {"match": "*", "domains": ["general"]},
            {"match": "@hr.example", "domains": ["hr"]},
            {"match": "ann@hr.example", "domains": ["finance"]},
        ]},
    },
}

_AI_CLIENT_ACT = {"type": "ai_client", "attested": False, "name": "", "claimed_name": ""}


def _tool(name: str) -> types.Tool:
    return types.Tool(name=name, description=f"{name}.", input_schema={"type": "object", "properties": {}})


@pytest.fixture
def upstreams():
    return parse_upstreams(_FILE, source="test")


@pytest.fixture
def configured(tmp_path, upstreams):
    path = tmp_path / "admin_config.json"
    path.write_text(json.dumps(_GRANTS), encoding="utf-8")
    key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
    signer = ClaimSigner(issuer="https://pf.example/upstream-mcp", key=key)
    upstream_mcp_runtime.configure(
        upstreams=upstreams, signer=signer, grants=GrantStore(path, frozenset({"kh", "wiki"})),
    )
    return upstreams


def _caller(upstream, principal, scope=None):
    with principal_scope(principal), scoped_tokens.token_scope(scope):
        return upstream_mcp_runtime.caller_for(upstream)


def _scope(**fields):
    base = {"id": "st-1", "label": "Nightly", "principal_email": "ann@hr.example"}
    return SimpleNamespace(**{**base, **fields})


class TestUnconfigured:
    def test_nothing_is_configured(self):
        assert upstream_mcp_runtime.configured() is False
        assert upstream_mcp_runtime.upstream_ids() == frozenset()
        assert upstream_mcp_runtime.connectors_for_principal() == []
        assert upstream_mcp_runtime.status() == []
        assert upstream_mcp_runtime.signer() is None
        assert upstream_mcp_runtime.held_audiences(Principal(id="u", email="a@b.example"), True) == {}

    def test_lifespan_with_nothing_configured_does_nothing(self):
        async def run():
            async with upstream_mcp_runtime.lifespan(lambda: None):
                pass

        asyncio.run(run())


class TestConfigured:
    def test_reset_forgets_the_configuration(self, configured):
        assert upstream_mcp_runtime.configured() is True
        upstream_mcp_runtime.reset()
        assert upstream_mcp_runtime.configured() is False

    def test_ids_and_connectors(self, configured):
        assert upstream_mcp_runtime.upstream_ids() == {"kh", "wiki"}
        connectors = upstream_mcp_runtime.connectors_for_principal()
        assert [c.name for c in connectors] == ["kh", "wiki"]
        assert upstream_mcp_runtime.signer() is not None

    def test_the_connector_caller_resolves_the_current_principal(self, configured):
        connector = upstream_mcp_runtime.connectors_for_principal()[0]
        with principal_scope(Principal(id="u1", email="bob@x.example", email_verified=True)):
            claim = connector._caller()
        assert claim.sub == "u1"
        assert claim.domains == ("general",)


class TestCallerFor:
    def test_verified_person_gets_the_audiences_their_grants_give(self, configured):
        principal = Principal(id="u1", email="Ann@HR.example", email_verified=True)
        claim = _caller(configured[0], principal)
        assert claim.sub == "u1"
        assert claim.email == "Ann@HR.example"
        assert claim.email_verified is True
        assert claim.domains == ("finance", "general", "hr")
        assert claim.act == _AI_CLIENT_ACT

    def test_grants_are_per_upstream(self, configured):
        principal = Principal(id="u1", email="ann@hr.example", email_verified=True)
        assert _caller(configured[1], principal).domains == ()

    def test_unverified_person_gets_no_audiences_and_no_email(self, configured):
        claim = _caller(configured[0], Principal(id="u2", email="ann@hr.example"))
        assert claim.domains == ()
        assert claim.email == ""
        assert claim.email_verified is False

    def test_scoped_token_without_upstream_audiences_gets_none(self, configured):
        claim = _caller(
            configured[0], Principal(id="u1", email="ann@hr.example", email_verified=True),
            _scope(principal_email_verified=True),
        )
        assert claim.domains == ()
        assert claim.email_verified is True
        assert claim.act == {"type": "workflow", "token_id": "st-1", "label": "Nightly"}

    def test_scoped_token_with_upstream_audiences_narrows(self, configured):
        scope = _scope(principal_email_verified=True, upstream_audiences={"kh": frozenset({"hr", "legal"})})
        claim = _caller(configured[0], Principal(id="u1", email="ann@hr.example", email_verified=True), scope)
        # "legal" was ticked but the owner does not hold it: a token never widens.
        assert claim.domains == ("hr",)

    def test_scoped_token_of_an_unverified_owner_gets_none(self, configured):
        scope = _scope(upstream_audiences={"kh": frozenset({"general"})})
        claim = _caller(configured[0], Principal(id="u1", email="ann@hr.example"), scope)
        assert claim.domains == ()
        assert claim.email == ""
        assert claim.email_verified is False

    def test_scoped_token_audiences_for_another_upstream_do_not_apply(self, configured):
        scope = _scope(principal_email_verified=True, upstream_audiences={"wiki": frozenset({"general"})})
        claim = _caller(configured[0], Principal(id="u1", email="ann@hr.example", email_verified=True), scope)
        assert claim.domains == ()


class TestHeldDomains:
    def test_one_entry_per_upstream(self, configured):
        held = upstream_mcp_runtime.held_audiences(Principal(id="u1", email="ann@hr.example"), True)
        assert held == {"kh": frozenset({"general", "hr", "finance"}), "wiki": frozenset()}

    def test_an_unverified_person_holds_nothing(self, configured):
        held = upstream_mcp_runtime.held_audiences(Principal(id="u1", email="ann@hr.example"), False)
        assert held == {"kh": frozenset(), "wiki": frozenset()}


class TestStatus:
    def test_waiting_until_the_tool_list_arrives(self, configured):
        assert upstream_mcp_runtime.status()[0] == {
            "id": "kh", "label": "Knowledge hub", "tools_exposed": 0, "state": "waiting_for_tool_list",
            "missing_tools": [],
        }

    def test_ready_names_the_declared_tools_the_upstream_lacks(self, configured):
        entry = upstream_mcp_runtime._runtime.entries["kh"]
        entry.catalog.update([_tool("search")])
        entry.listed = True
        assert upstream_mcp_runtime.status()[0] == {
            "id": "kh", "label": "Knowledge hub", "tools_exposed": 1, "state": "ready",
            "missing_tools": ["kh_get_doc"],
        }


class _FlakyClient:
    def __init__(self, failures: int, tools: list[types.Tool]) -> None:
        self.failures = failures
        self.tools = tools
        self.attempts = 0

    async def list_tools(self):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise UpstreamMcpClientError("unreachable")
        return self.tools


class TestLifespan:
    def test_retries_with_doubling_backoff_then_notifies_once(self, configured, monkeypatch):
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        monkeypatch.setattr(upstream_mcp_runtime, "_sleep", fake_sleep)
        entries = upstream_mcp_runtime._runtime.entries
        entries["kh"].client = _FlakyClient(2, [_tool("search"), _tool("get_doc")])
        entries["wiki"].client = _FlakyClient(0, [_tool("search")])
        notified: list[int] = []

        async def run() -> None:
            async with upstream_mcp_runtime.lifespan(lambda: notified.append(1)):
                for _ in range(50):
                    await asyncio.sleep(0)
                    if all(entry.listed for entry in entries.values()):
                        break
                else:
                    raise AssertionError("the tool lists never arrived")

        asyncio.run(run())
        assert delays == [5.0, 10.0]
        assert len(notified) == 2  # once per upstream, none for the failed attempts
        assert entries["kh"].client.attempts == 3
        assert [row["state"] for row in upstream_mcp_runtime.status()] == ["ready", "ready"]

    def test_the_delay_stops_doubling_at_the_cap(self, configured, monkeypatch):
        delays: list[float] = []
        monkeypatch.setattr(upstream_mcp_runtime, "BACKOFF_START_SECONDS", 200.0)

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        monkeypatch.setattr(upstream_mcp_runtime, "_sleep", fake_sleep)
        entry = upstream_mcp_runtime._runtime.entries["kh"]
        entry.client = _FlakyClient(3, [_tool("search")])
        asyncio.run(upstream_mcp_runtime._fetch_tool_list(entry, lambda: None))
        assert delays == [200.0, 300.0, 300.0]

    def test_shutdown_cancels_an_upstream_that_never_answers(self, configured, monkeypatch):
        async def fake_sleep(delay: float) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(upstream_mcp_runtime, "_sleep", fake_sleep)
        for entry in upstream_mcp_runtime._runtime.entries.values():
            entry.client = _FlakyClient(10**6, [])
        notified: list[int] = []

        async def run() -> None:
            async with upstream_mcp_runtime.lifespan(lambda: notified.append(1)):
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        asyncio.run(run())
        assert notified == []
        assert [row["state"] for row in upstream_mcp_runtime.status()] == ["waiting_for_tool_list"] * 2

    def test_a_missing_declared_tool_is_still_ready(self, configured):
        entry = upstream_mcp_runtime._runtime.entries["kh"]
        entry.client = _FlakyClient(0, [_tool("search")])
        asyncio.run(upstream_mcp_runtime._fetch_tool_list(entry, lambda: None))
        assert entry.listed is True
        assert entry.catalog.missing() == ["kh_get_doc"]


class TestOrgModeLookups:
    def test_an_upstream_id_is_never_enabled_by_the_organization_config(self, configured):
        from privacyfence import org_mode

        # "kh" is not in CONNECTOR_ORG_SECTION: without the skip this would raise KeyError.
        assert org_mode.connector_enabled_by_org({"kh": {"x": 1}}, "kh") is False
        assert org_mode.connector_enabled_by_org({"grist": {"enabled": True}}, "grist") is True

    def test_an_unknown_name_that_is_not_an_upstream_still_raises(self):
        from privacyfence import org_mode

        with pytest.raises(KeyError):
            org_mode.connector_enabled_by_org({}, "kh")
