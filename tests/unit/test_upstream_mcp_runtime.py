"""upstream_mcp_runtime: the caller claim, held audiences, status and the startup tool-list fetch."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import types

from privacyfence import agent_identity, scoped_tokens, upstream_mcp_runtime
from privacyfence.agent_identity import AgentIdentity, AgentSource
from privacyfence.identity_claim import CallerNotAllowedError, ClaimSigner
from privacyfence.principal import Principal, principal_scope
from privacyfence.upstream_claim_keys import KeySet
from privacyfence.upstream_mcp_client import UpstreamMcpClientError
from privacyfence import group_membership
from privacyfence.upstream_mcp_config import GroupMapStore, parse_upstreams

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
_GROUPS = {
    "upstream_mcp": {
        "kh": {"groups": {
            "everyone@co.example": ["general"],
            "hr@co.example": ["hr"],
            "finance@co.example": ["finance"],
        }},
    },
}
_MEMBERS = {
    "version": 1,
    "groups": {
        "everyone@co.example": ["bob@x.example", "ann@hr.example", "Zed@x.example"],
        "hr@co.example": ["ann@hr.example"],
        "finance@co.example": ["ann@hr.example"],
    },
}

def _tool(name: str) -> types.Tool:
    return types.Tool(name=name, description=f"{name}.", input_schema={"type": "object", "properties": {}})


def _signer(tmp_path):
    keys = KeySet(tmp_path / "keys.json")
    keys.ensure()
    return ClaimSigner(issuer="https://pf.example", keys=keys)


@pytest.fixture
def upstreams():
    return parse_upstreams(_FILE, source="test")


@pytest.fixture
def configured(tmp_path, upstreams):
    path = tmp_path / "admin_config.json"
    path.write_text(json.dumps(_GROUPS), encoding="utf-8")
    members = tmp_path / "members.json"
    members.write_text(json.dumps(_MEMBERS), encoding="utf-8")
    signer = _signer(tmp_path)
    upstream_mcp_runtime.configure(
        upstreams=upstreams, signer=signer, groups=GroupMapStore(path, frozenset({"kh", "wiki"})),
        resolver=group_membership.StaticFileResolver(members),
    )
    return upstreams


def _caller(upstream, principal, scope=None):
    with principal_scope(principal), scoped_tokens.token_scope(scope):
        return upstream_mcp_runtime.caller_for(upstream)


def _scope(**fields):
    base = {
        "id": "st-1", "label": "Nightly", "principal_email": "ann@hr.example", "principal_id": "u1",
        "tools": frozenset({"kh_search"}),
    }
    return SimpleNamespace(**{**base, **fields})


class TestGroupMembershipHelpers:
    def test_unconfigured_defaults(self):
        assert upstream_mcp_runtime.all_group_audiences() == {}
        assert upstream_mcp_runtime.max_additions() == group_membership.DEFAULT_MAX_ADDITIONS

    def test_all_group_audiences_merges_upstreams(self, tmp_path, upstreams):
        path = tmp_path / "admin_config.json"
        path.write_text(json.dumps({"upstream_mcp": {
            "kh": {"groups": {"hr@co.example": ["hr"], "everyone@co.example": ["general"]}},
            "wiki": {"groups": {"hr@co.example": ["wiki-hr"]}},
        }}), encoding="utf-8")
        upstream_mcp_runtime.configure(
            upstreams=upstreams, signer=_signer(tmp_path), groups=GroupMapStore(path, frozenset({"kh", "wiki"})),
            resolver=group_membership.StaticFileResolver(tmp_path / "none.json"), membership_max_additions=7,
        )
        assert upstream_mcp_runtime.all_group_audiences() == {
            "hr@co.example": frozenset({"hr", "wiki-hr"}),
            "everyone@co.example": frozenset({"general"}),
        }
        assert upstream_mcp_runtime.max_additions() == 7


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
        assert claim.audiences == ("general",)


class TestCallerFor:
    def test_verified_person_gets_the_audiences_their_groups_give(self, configured):
        principal = Principal(id="u1", email="Ann@HR.example", email_verified=True)
        claim = _caller(configured[0], principal)
        assert claim.sub == "u1"
        assert claim.principal_type == "claude_client"
        assert claim.email == "Ann@HR.example"
        assert claim.audiences == ("finance", "general", "hr")
        assert claim.tools == ()  # nothing listed yet
        assert claim.act is None and claim.approved_by is None and claim.token_id is None

    def test_tools_are_the_upstream_native_names_of_the_exposed_tools(self, configured):
        runtime_entry = upstream_mcp_runtime._runtime.entries["kh"]
        runtime_entry.catalog.update([_tool("search"), _tool("get_doc")])
        principal = Principal(id="u1", email="bob@x.example", email_verified=True)
        assert _caller(configured[0], principal).tools == ("get_doc", "search")
        scope = _scope(principal_email_verified=True)
        assert _caller(configured[0], principal, scope).tools == ("search",)

    def test_act_carries_the_client_id_and_only_an_attested_agent(self, configured):
        principal = Principal(id="u1", email="bob@x.example", email_verified=True)
        attested = AgentIdentity(id="claude-desktop", name="Claude", version="1", source=AgentSource.OAUTH_CLIENT)
        claimed = AgentIdentity(id="x", name="X", version="1", source=AgentSource.NONE)
        with agent_identity.client_id_scope("client-1"), agent_identity.agent_scope(attested):
            assert _caller(configured[0], principal).act == {"sub": "client-1", "agent": "claude-desktop"}
        with agent_identity.client_id_scope("client-1"), agent_identity.agent_scope(claimed):
            assert _caller(configured[0], principal).act == {"sub": "client-1"}
        with agent_identity.agent_scope(attested):
            assert _caller(configured[0], principal).act == {"agent": "claude-desktop"}

    def test_group_maps_are_per_upstream(self, configured):
        principal = Principal(id="u1", email="ann@hr.example", email_verified=True)
        assert _caller(configured[1], principal).audiences == ()

    def test_unverified_person_still_gets_a_claim_with_no_audiences_and_no_email(self, configured):
        claim = _caller(configured[0], Principal(id="u2", email="ann@hr.example"))
        assert claim.audiences == ()
        assert claim.email is None

    def test_scoped_token_is_an_n8n_workflow_named_after_the_token(self, configured):
        claim = _caller(
            configured[0], Principal(id="u1", email="ann@hr.example", email_verified=True),
            _scope(principal_email_verified=True),
        )
        assert claim.sub == "n8n-token:st-1"
        assert claim.principal_type == "n8n_workflow"
        assert claim.token_id == "st-1" and claim.approved_by == "u1"
        assert claim.email is None and claim.act is None
        assert claim.audiences == ()

    def test_scoped_token_with_upstream_audiences_narrows(self, configured):
        scope = _scope(principal_email_verified=True, upstream_audiences={"kh": frozenset({"hr", "legal"})})
        claim = _caller(configured[0], Principal(id="u1", email="ann@hr.example", email_verified=True), scope)
        # "legal" was ticked but the owner does not hold it: a token never widens.
        assert claim.audiences == ("hr",)

    def test_scoped_token_of_an_unverified_owner_gets_none(self, configured):
        scope = _scope(upstream_audiences={"kh": frozenset({"general"})})
        claim = _caller(configured[0], Principal(id="u1", email="ann@hr.example"), scope)
        assert claim.audiences == ()
        assert claim.email is None

    def test_scoped_token_audiences_for_another_upstream_do_not_apply(self, configured):
        scope = _scope(principal_email_verified=True, upstream_audiences={"wiki": frozenset({"general"})})
        claim = _caller(configured[0], Principal(id="u1", email="ann@hr.example", email_verified=True), scope)
        assert claim.audiences == ()

    @pytest.mark.parametrize(("allowed", "scoped"), [(("n8n_workflow",), False), (("claude_client",), True)])
    def test_a_principal_type_the_upstream_does_not_allow_is_refused(self, configured, allowed, scoped):
        from dataclasses import replace

        upstream = replace(configured[0], allowed_principal_types=allowed)
        principal = Principal(id="u1", email="ann@hr.example", email_verified=True)
        scope = _scope(principal_email_verified=True) if scoped else None
        with pytest.raises(CallerNotAllowedError):
            _caller(upstream, principal, scope)


class TestHeldAudiences:
    def test_one_entry_per_upstream(self, configured):
        held = upstream_mcp_runtime.held_audiences(Principal(id="u1", email="ann@hr.example"), True)
        assert held == {"kh": frozenset({"general", "hr", "finance"}), "wiki": frozenset()}

    def test_an_unverified_person_holds_nothing(self, configured):
        held = upstream_mcp_runtime.held_audiences(Principal(id="u1", email="ann@hr.example"), False)
        assert held == {"kh": frozenset(), "wiki": frozenset()}


class TestAudiencesForEmail:
    def test_static_resolver_gives_the_union_over_groups(self, configured):
        assert upstream_mcp_runtime.audiences_for_email("kh", " Ann@HR.example ", True) == {
            "general", "hr", "finance",
        }

    def test_unverified_or_empty_email_gets_nothing(self, configured):
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", False) == frozenset()
        assert upstream_mcp_runtime.audiences_for_email("kh", "", True) == frozenset()

    def test_a_person_in_no_group_or_an_unmapped_upstream_gets_nothing(self, configured):
        assert upstream_mcp_runtime.audiences_for_email("kh", "nobody@x.example", True) == frozenset()
        assert upstream_mcp_runtime.audiences_for_email("wiki", "ann@hr.example", True) == frozenset()

    def test_snapshot_resolver_grants_all_at_once_and_restricted_after_approval(self, tmp_path, upstreams):
        path = tmp_path / "admin_config.json"
        everyone = {"upstream_mcp": {"kh": {"groups": {
            "everyone@co.example": ["all"], "hr@co.example": ["hr"],
        }}}}
        path.write_text(json.dumps(everyone), encoding="utf-8")
        store = group_membership.MembershipStore(tmp_path / "group_membership.json")
        groups = GroupMapStore(path, frozenset({"kh", "wiki"}))
        upstream_mcp_runtime.configure(
            upstreams=upstreams, signer=_signer(tmp_path),
            groups=groups, resolver=group_membership.SnapshotResolver(store),
        )
        mapping = groups.group_audiences("kh")
        result = store.apply_snapshot(
            {"everyone@co.example": ["ann@hr.example"], "hr@co.example": ["ann@hr.example"]},
            group_audiences=mapping, now=1000.0, max_additions=20,
        )
        assert result.status == "applied"
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == {"all"}
        store.approve("hr@co.example", "hr", "ann@hr.example", group_audiences=mapping, now=1001.0)
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == {"all", "hr"}
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", False) == frozenset()

    def test_a_scoped_token_gets_owner_now_intersected_with_chosen(self, configured):
        scope = _scope(principal_email_verified=True, upstream_audiences={"kh": frozenset({"general", "legal"})})
        claim = _caller(configured[0], Principal(id="u1", email="ann@hr.example", email_verified=True), scope)
        assert claim.audiences == ("general",)

    def test_a_token_loses_an_audience_its_creator_loses_and_gets_it_back_on_approval(self, tmp_path, upstreams):
        path = tmp_path / "admin_config.json"
        path.write_text(json.dumps({"upstream_mcp": {"kh": {"groups": {"mgmt@co.example": ["mgmt"]}}}}), "utf-8")
        store = group_membership.MembershipStore(tmp_path / "group_membership.json")
        groups = GroupMapStore(path, frozenset({"kh", "wiki"}))
        upstream_mcp_runtime.configure(
            upstreams=upstreams, signer=_signer(tmp_path), groups=groups,
            resolver=group_membership.SnapshotResolver(store),
        )
        mapping = groups.group_audiences("kh")
        scope = _scope(principal_email_verified=True, upstream_audiences={"kh": frozenset({"mgmt"})})
        owner = Principal(id="u1", email="ann@hr.example", email_verified=True)
        member = {"mgmt@co.example": ["ann@hr.example"]}
        store.apply_snapshot(member, group_audiences=mapping, now=1000.0, max_additions=20)
        store.approve("mgmt@co.example", "mgmt", "ann@hr.example", group_audiences=mapping, now=1001.0)
        assert _caller(upstreams[0], owner, scope).audiences == ("mgmt",)
        store.apply_snapshot({"mgmt@co.example": []}, group_audiences=mapping, now=2000.0, max_additions=20)
        assert _caller(upstreams[0], owner, scope).audiences == ()
        store.apply_snapshot(member, group_audiences=mapping, now=3000.0, max_additions=20)
        assert _caller(upstreams[0], owner, scope).audiences == ()  # re-added: waits for an approval again
        store.approve("mgmt@co.example", "mgmt", "ann@hr.example", group_audiences=mapping, now=3001.0)
        assert _caller(upstreams[0], owner, scope).audiences == ("mgmt",)


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
