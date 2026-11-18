"""upstream_mcp_runtime: the caller claim, held audiences, status and the startup tool-list fetch."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from types import SimpleNamespace

import pytest
from mcp import types

from privacyfence import agent_identity, scoped_tokens, upstream_mcp_runtime
from privacyfence.agent_identity import AgentIdentity, AgentSource
from privacyfence.identity_claim import CallerNotAllowedError, ClaimSigner
from privacyfence.principal import Principal, principal_scope
from privacyfence.upstream_claim_keys import KeySet
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.upstream_audiences import UpstreamAudienceSource
from privacyfence.upstream_mcp_client import CallResult, UpstreamMcpClientError
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


_SOURCE_FILE = {
    **_FILE,
    "upstreams": [{**_FILE["upstreams"][0], "audiences_tool": "list_audiences"}, _FILE["upstreams"][1]],
}


def _table(**audiences: list[str]) -> dict:
    return {"version": 1, "audiences": audiences}


class _AudiencesClient:
    """Answers the audiences tool from a script of tables or errors, and records each call."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple] = []

    async def call_tool(self, tool, arguments, caller):
        self.calls.append((tool, arguments, caller))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return CallResult(text=answer, structured=None, dropped_blocks=0, jti="jti-1")
        return CallResult(text=json.dumps(answer), structured=answer, dropped_blocks=0, jti="jti-1")


@pytest.fixture
def configured_with_source(tmp_path):
    path = tmp_path / "admin_config.json"
    path.write_text(json.dumps(_GROUPS), encoding="utf-8")
    members = tmp_path / "members.json"
    members.write_text(json.dumps(_MEMBERS), encoding="utf-8")
    upstreams = parse_upstreams(_SOURCE_FILE, source="test")
    upstream_mcp_runtime.configure(
        upstreams=upstreams, signer=_signer(tmp_path), groups=GroupMapStore(path, frozenset({"kh", "wiki"})),
        resolver=group_membership.StaticFileResolver(members),
    )
    return upstreams


@pytest.fixture
def audit_dir(tmp_path):
    directory = tmp_path / "audit"
    directory.mkdir()
    init_audit_logger(str(directory))
    return directory


def _audit_rows(audit_dir):
    path = audit_dir / f"{current_week()}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _entry(upstream_id="kh"):
    return upstream_mcp_runtime._runtime.entries[upstream_id]


def _refresh(upstream_id="kh"):
    return asyncio.run(upstream_mcp_runtime.refresh_audiences(upstream_id))


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

    def test_opted_in_a_verified_email_is_lowercased(self, configured):
        upstream = dataclasses.replace(configured[0], email_for_visibility=True)
        claim = _caller(upstream, Principal(id="u1", email="Ann@HR.example", email_verified=True))
        assert claim.email == "ann@hr.example"

    def test_opted_in_a_non_ascii_email_gives_none(self, configured):
        upstream = dataclasses.replace(configured[0], email_for_visibility=True)
        claim = _caller(upstream, Principal(id="u1", email="\u212Aate@hr.example", email_verified=True))
        assert claim.email is None

    def test_opted_in_an_unverified_email_gives_none(self, configured):
        upstream = dataclasses.replace(configured[0], email_for_visibility=True)
        claim = _caller(upstream, Principal(id="u2", email="ann@hr.example"))
        assert claim.email is None

    def test_opted_in_a_scoped_token_never_carries_email(self, configured):
        upstream = dataclasses.replace(configured[0], email_for_visibility=True)
        claim = _caller(
            upstream, Principal(id="u1", email="ann@hr.example", email_verified=True),
            _scope(principal_email_verified=True),
        )
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

    def test_a_look_alike_address_holds_no_audience(self, configured):
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann\u200b@hr.example", True) == frozenset()

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
            "missing_tools": [], "audiences_source": "groups",
        }

    def test_an_upstream_source_shows_its_state(self, configured_with_source):
        rows = upstream_mcp_runtime.status()
        assert rows[0]["audiences_source"] == "upstream" and rows[0]["audiences_state"] == "waiting"
        assert rows[1]["audiences_source"] == "groups" and "audiences_state" not in rows[1]

    def test_ready_names_the_declared_tools_the_upstream_lacks(self, configured):
        entry = upstream_mcp_runtime._runtime.entries["kh"]
        entry.catalog.update([_tool("search")])
        entry.listed = True
        assert upstream_mcp_runtime.status()[0] == {
            "id": "kh", "label": "Knowledge hub", "tools_exposed": 1, "state": "ready",
            "missing_tools": ["kh_get_doc"], "audiences_source": "groups",
        }


class TestAdminRows:
    def test_unconfigured_is_empty(self):
        upstream_mcp_runtime.reset()
        assert upstream_mcp_runtime.admin_rows() == []

    def test_waiting_until_the_tool_list_arrives(self, configured):
        row = upstream_mcp_runtime.admin_rows()[0]
        assert row["state"] == "waiting_for_tool_list"
        assert [tool["state"] for tool in row["tools"]] == ["waiting", "waiting"]

    def test_ready_marks_each_declared_tool_exposed_or_missing(self, configured):
        entry = upstream_mcp_runtime._runtime.entries["kh"]
        entry.catalog.update([_tool("search")])
        entry.listed = True
        row = upstream_mcp_runtime.admin_rows()[0]
        assert row["state"] == "ready"
        assert row["tools"] == [
            {"name": "kh_search", "gate": "auto", "verb": "search", "effect": "", "state": "exposed"},
            {"name": "kh_get_doc", "gate": "auto", "verb": "read", "effect": "", "state": "missing"},
        ]

    def test_a_groups_upstream_shows_the_map_and_no_source(self, configured):
        row = upstream_mcp_runtime.admin_rows()[0]
        assert (row["audiences_source"], row["audiences_tool"], row["audiences_refresh_seconds"]) == (
            "groups", None, None,
        )
        assert row["audiences"] is None and row["groups_ignored"] == 0 and row["groups"]

    def test_an_upstream_source_shows_its_summary_and_the_ignored_groups(self, configured_with_source, audit_dir):
        _entry().client = _AudiencesClient(_table(mgmt=["ann@hr.example", "bad\u00e9@x.example"]))
        _refresh()
        row = upstream_mcp_runtime.admin_rows()[0]
        assert (row["audiences_source"], row["audiences_tool"], row["audiences_refresh_seconds"]) == (
            "upstream", "list_audiences", 300,
        )
        summary = dict(row["audiences"])
        assert summary.pop("fetched_at") is not None
        assert summary == {
            "state": "ready", "audience_count": 1, "people_count": 1, "dropped": 1, "last_error": "",
        }
        assert row["groups"] == [] and row["groups_ignored"] == 3
        assert "@" not in json.dumps(row["audiences"])
        other = upstream_mcp_runtime.admin_rows()[1]
        assert other["audiences_source"] == "groups" and other["groups_ignored"] == 0

    def test_groups_are_the_map_in_force_sorted(self, tmp_path, configured):
        expected = [
            {"group": group, "audiences": sorted(audiences)}
            for group, audiences in sorted(_GROUPS["upstream_mcp"]["kh"]["groups"].items())
        ]
        assert upstream_mcp_runtime.admin_rows()[0]["groups"] == expected
        path = tmp_path / "admin_config.json"
        path.write_text(json.dumps({"upstream_mcp": {"kh": {"groups": {"b@co.example": ["y", "x"]}}}}), encoding="utf-8")
        later = path.stat().st_mtime + 10
        os.utime(path, (later, later))
        assert upstream_mcp_runtime.admin_rows()[0]["groups"] == [{"group": "b@co.example", "audiences": ["x", "y"]}]

    @pytest.mark.parametrize(("url", "shown"), [
        ("https://kh.example/mcp?key=s3cret", "https://kh.example/…"),
        ("https://kh.example/mcp/s3cret/sse", "https://kh.example/…"),
        ("https://kh.example:8443/mcp", "https://kh.example:8443/…"),
        ("http://knowledge-hub:8080", "http://knowledge-hub:8080"),
    ])
    def test_the_path_and_query_of_the_address_are_hidden(self, tmp_path, url, shown):
        declared = {**_FILE, "upstreams": [{**_FILE["upstreams"][0], "url": url}]}
        path = tmp_path / "admin_config.json"
        path.write_text(json.dumps(_GROUPS), encoding="utf-8")
        upstream_mcp_runtime.configure(
            upstreams=parse_upstreams(declared, source="test"), signer=_signer(tmp_path),
            groups=GroupMapStore(path, frozenset({"kh"})),
            resolver=group_membership.StaticFileResolver(tmp_path / "none.json"),
        )
        rows = upstream_mcp_runtime.admin_rows()
        assert rows[0]["url"] == shown
        assert "s3cret" not in json.dumps(rows)


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


class TestAudiencesFromUpstream:
    def test_a_person_holds_the_audiences_the_upstream_lists(self, configured_with_source, audit_dir):
        _entry().client = _AudiencesClient(_table(mgmt=["Ann@HR.example"], finance=["bob@x.example"]))
        assert _refresh() is True
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == {"mgmt"}
        claim = _caller(configured_with_source[0], Principal(id="u1", email="ann@hr.example", email_verified=True))
        assert claim.audiences == ("mgmt",)
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", False) == frozenset()
        held = upstream_mcp_runtime.held_audiences(Principal(id="u1", email="bob@x.example"), True)
        assert held == {"kh": frozenset({"finance"}), "wiki": frozenset()}

    def test_nobody_holds_an_audience_before_the_first_answer(self, configured_with_source):
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == frozenset()

    def test_a_stale_snapshot_gives_no_audience(self, configured_with_source, audit_dir):
        now = [1000.0]
        _entry().audiences = UpstreamAudienceSource(300, clock=lambda: now[0])
        _entry().client = _AudiencesClient(_table(mgmt=["ann@hr.example"]))
        assert _refresh() is True
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == {"mgmt"}
        now[0] += 901
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == frozenset()
        assert upstream_mcp_runtime.status()[0]["audiences_state"] == "stale"

    def test_a_failed_refresh_keeps_the_last_snapshot(self, configured_with_source, audit_dir, caplog):
        _entry().client = _AudiencesClient(_table(mgmt=["ann@hr.example"]), UpstreamMcpClientError("unreachable"))
        assert _refresh() is True
        with caplog.at_level("WARNING", logger=upstream_mcp_runtime.logger.name):
            assert _refresh() is False
        assert "Upstream kh audiences refresh failed: unreachable" in caplog.text
        assert "@" not in caplog.text
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == {"mgmt"}
        assert _entry().audiences.summary()["last_error"] == "unreachable"

    def test_the_group_map_is_not_used_for_an_upstream_source(self, configured_with_source):
        # _GROUPS would give ann general, hr and finance on kh; with a source she holds nothing yet.
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == frozenset()
        assert _caller(configured_with_source[0], Principal(id="u1", email="ann@hr.example", email_verified=True)).audiences == ()

    def test_all_group_audiences_skips_an_upstream_source(self, configured_with_source):
        assert upstream_mcp_runtime.all_group_audiences() == {}

    def test_an_upstream_table_never_reaches_another_upstream(self, configured_with_source, audit_dir):
        wiki_before = upstream_mcp_runtime.held_audiences(Principal(id="u1", email="ann@hr.example"), True)["wiki"]
        _entry().client = _AudiencesClient(_table(mgmt=["ann@hr.example"], wiki=["ann@hr.example"]))
        _refresh()
        principal = Principal(id="u1", email="ann@hr.example", email_verified=True)
        assert _caller(configured_with_source[1], principal).audiences == ()
        held = upstream_mcp_runtime.held_audiences(principal, True)
        assert held["wiki"] == wiki_before == frozenset()
        assert held["kh"] == {"mgmt", "wiki"}

    def test_a_scoped_token_holds_the_intersection_with_the_upstream_source(self, configured_with_source, audit_dir):
        _entry().client = _AudiencesClient(_table(mgmt=["ann@hr.example"], finance=["ann@hr.example"]))
        _refresh()
        scope = _scope(principal_email_verified=True, upstream_audiences={"kh": frozenset({"mgmt", "legal"})})
        claim = _caller(configured_with_source[0], Principal(id="u1", email="ann@hr.example", email_verified=True), scope)
        assert claim.audiences == ("mgmt",)

    def test_the_service_claim_names_only_the_audiences_tool(self, configured_with_source, audit_dir):
        client = _AudiencesClient(_table())
        _entry().client = client
        _refresh()
        (tool, arguments, caller), = client.calls
        assert (tool, arguments) == ("list_audiences", {})
        assert (caller.sub, caller.principal_type, caller.tools) == ("privacyfence", "service", ("list_audiences",))

    @pytest.mark.parametrize("answer", [
        "not json", {"version": 2, "audiences": {}}, UpstreamMcpClientError("tool_error"),
    ])
    def test_a_failed_answer_is_recorded(self, configured_with_source, audit_dir, answer):
        _entry().client = _AudiencesClient(answer)
        assert _refresh() is False
        expected = "tool_error" if isinstance(answer, Exception) else "invalid"
        assert _entry().audiences.summary()["last_error"] == expected

    def test_an_invalid_answer_is_recorded_as_invalid(self, configured_with_source, audit_dir, caplog):
        _entry().client = _AudiencesClient({"version": 1})
        with caplog.at_level(logging.WARNING):
            assert _refresh() is False
        assert _entry().audiences.summary()["last_error"] == "invalid"
        assert "unusable result: the answer must hold exactly version and audiences" in caplog.text

    def test_an_unexpected_error_is_recorded_as_invalid(self, configured_with_source, audit_dir):
        _entry().client = _AudiencesClient(RuntimeError("boom"))
        assert _refresh() is False
        assert _entry().audiences.summary()["last_error"] == "invalid"

    def test_an_older_attempt_that_finishes_last_changes_nothing(self, configured_with_source, audit_dir):
        source = _entry().audiences
        older = source.begin()

        class _Racing(_AudiencesClient):
            async def call_tool(self, tool, arguments, caller):
                source.apply(upstream_mcp_runtime.upstream_audiences.parse_result(_table(mgmt=["a@x.example"]), ""), 99)
                return await super().call_tool(tool, arguments, caller)

        assert older == 1
        _entry().client = _Racing(_table(finance=["b@x.example"]))
        assert _refresh() is False  # seq 2 is below the applied 99
        _entry().client = _Racing(UpstreamMcpClientError("unreachable"))
        assert _refresh() is False
        assert _entry().audiences_failing is False
        assert _audit_rows(audit_dir) == []

    def test_audit_entries_on_first_success_change_failure_and_recovery(self, configured_with_source, audit_dir):
        client = _AudiencesClient(
            _table(mgmt=["ann@hr.example"]),
            _table(mgmt=["ann@hr.example"]),  # unchanged
            _table(mgmt=["bob@x.example"]),   # a swap with equal counts
            UpstreamMcpClientError("unreachable"),
            UpstreamMcpClientError("unreachable"),  # still failing: no second entry
            _table(mgmt=["bob@x.example"]),   # recovery with the same table
        )
        _entry().client = client
        outcomes = [_refresh() for _ in range(6)]
        assert outcomes == [True, True, True, False, False, True]
        rows = _audit_rows(audit_dir)
        assert [row["decision"] for row in rows] == [
            "upstream_audiences_applied", "upstream_audiences_applied",
            "upstream_audiences_failed", "upstream_audiences_applied",
        ]
        assert rows[0]["summary"] == "Audiences from kh applied: 1 audience(s), 1 people, 0 address(es) dropped"
        assert rows[2]["summary"] == "Audiences from kh not refreshed: unreachable"
        for row in rows:
            assert "@" not in row["summary"]
            assert (row["connector"], row["tool"], row["tool_name"], row["sender"]) == (
                "kh", "list_audiences", "Read audiences", "privacyfence",
            )
        assert rows[0]["claim_jti"] == "jti-1"

    def test_an_audit_failure_never_changes_the_outcome(self, configured_with_source, monkeypatch, caplog):
        def broken():
            raise OSError("disk full")

        monkeypatch.setattr(upstream_mcp_runtime, "get_audit_logger", broken)
        _entry().client = _AudiencesClient(_table(mgmt=["ann@hr.example"]))
        with caplog.at_level(logging.WARNING):
            assert _refresh() is True
        assert "Audit log write failed" in caplog.text
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == {"mgmt"}

    def test_the_ignored_group_map_is_logged_once(self, tmp_path, caplog):
        path = tmp_path / "admin_config.json"
        path.write_text(json.dumps(_GROUPS), encoding="utf-8")
        upstreams = parse_upstreams(_SOURCE_FILE, source="test")
        with caplog.at_level(logging.WARNING):
            upstream_mcp_runtime.configure(
                upstreams=upstreams, signer=_signer(tmp_path),
                groups=GroupMapStore(path, frozenset({"kh", "wiki"})),
                resolver=group_membership.StaticFileResolver(tmp_path / "none.json"),
            )
        messages = [r.getMessage() for r in caplog.records if "is ignored" in r.getMessage()]
        assert messages == [
            "admin_config.json: upstream_mcp.kh.groups is ignored: kh takes its audiences from its audiences tool",
        ]


class TestAudiencesLoop:
    def test_the_audiences_loop_sleeps_the_interval_after_success_and_backs_off_after_failure(
        self, configured_with_source, audit_dir, monkeypatch,
    ):
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 4:
                raise asyncio.CancelledError

        monkeypatch.setattr(upstream_mcp_runtime, "_audiences_sleep", fake_sleep)
        entry = _entry()
        entry.client = _AudiencesClient(
            UpstreamMcpClientError("unreachable"), UpstreamMcpClientError("unreachable"), _table(mgmt=["a@x.example"]),
        )
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(upstream_mcp_runtime._refresh_audiences_loop(entry))
        assert delays == [5.0, 10.0, 300, 300]

    def test_the_audiences_loop_does_not_wait_for_the_tool_list(self, configured_with_source, audit_dir, monkeypatch):
        async def never(delay: float) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(upstream_mcp_runtime, "_audiences_sleep", never)
        monkeypatch.setattr(upstream_mcp_runtime, "_sleep", never)
        entries = upstream_mcp_runtime._runtime.entries
        entries["kh"].client = _AudiencesClient(_table(mgmt=["ann@hr.example"]))
        entries["wiki"].client = _FlakyClient(10**6, [])

        async def run() -> None:
            async with upstream_mcp_runtime.lifespan(lambda: None):
                for _ in range(50):
                    await asyncio.sleep(0)
                    if entries["kh"].audiences.state() == "ready":
                        break
                else:
                    raise AssertionError("the audiences never arrived")

        asyncio.run(run())
        assert entries["kh"].listed is False
        assert upstream_mcp_runtime.audiences_for_email("kh", "ann@hr.example", True) == {"mgmt"}


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
