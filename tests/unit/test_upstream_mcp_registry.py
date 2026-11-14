"""Tests for upstream_mcp_registry: declared upstream tools land in every policy table (ADR 0278)."""
from __future__ import annotations

import copy
import time

import pytest

from privacyfence import auto_accept, gate, scoped_tokens, upstream_mcp_registry, write_effects
from privacyfence.approval_window_html import WIDE
from privacyfence.connector import Connector, ToolSpec
from privacyfence.org_mode import ConfigurationError
from privacyfence.policy import propose, registry
from privacyfence.policy.registry import TOOL_REGISTRY, Verb
from privacyfence.principal import LOCAL_PRINCIPAL
from privacyfence.upstream_mcp_config import DeclaredTool, UpstreamServer
from privacyfence.web.mcp_dispatch import McpDispatcher

from ..helpers import make_ctx


def _tool(upstream: str, name: str, gate_: str, verb: str, effect: str = "") -> DeclaredTool:
    return DeclaredTool(
        name=name, exposed_name=f"{upstream}_{name}", operation=f"{upstream}.{name}",
        gate=gate_, verb=verb, effect=effect,
    )


def _kh() -> UpstreamServer:
    return UpstreamServer(
        id="kh", label="Knowledge hub", url="https://kh.example.com/mcp", audience="kh",
        tools=(
            _tool("kh", "search", "review", "search"),
            _tool("kh", "list_topics", "auto", "read"),
            _tool("kh", "propose_change", "popup", "update", "Proposes a change to a page."),
        ),
    )


def _snapshot():
    return (
        dict(auto_accept.TOOL_TO_GATE), dict(auto_accept.TOOL_TO_OPERATION), dict(registry.TOOL_TO_VERB),
        dict(write_effects.EFFECT_BY_TOOL), dict(gate._TOOL_LAYOUT), dict(TOOL_REGISTRY),
        propose.ALL_OPERATIONS,
    )


class _Fake(Connector):
    name = "kh"

    def tool_specs(self) -> list[ToolSpec]:
        return []

    async def call(self, tool: str, args: dict) -> object:
        return None


def test_register_writes_every_table():
    upstream_mcp_registry.register([_kh()])
    assert auto_accept.TOOL_TO_GATE["kh_search"] == "review"
    assert auto_accept.TOOL_TO_GATE["kh_list_topics"] == "auto"
    assert auto_accept.TOOL_TO_GATE["kh_propose_change"] == "popup"
    assert auto_accept.TOOL_TO_OPERATION["kh_search"] == "kh.search"
    assert registry.TOOL_TO_VERB["kh_search"] is Verb.SEARCH
    assert registry.TOOL_TO_VERB["kh_propose_change"] is Verb.UPDATE
    assert write_effects.EFFECT_BY_TOOL["kh_propose_change"] == "Proposes a change to a page."
    assert "kh_search" not in write_effects.EFFECT_BY_TOOL
    assert gate._TOOL_LAYOUT["kh_search"] == WIDE
    assert gate._TOOL_LAYOUT["kh_propose_change"] == WIDE
    assert "kh_list_topics" not in gate._TOOL_LAYOUT


def test_registry_and_all_operations_pick_up_the_tools():
    upstream_mcp_registry.register([_kh()])
    entry = TOOL_REGISTRY["kh_search"]
    assert (entry.gate, entry.operation, entry.verb) == ("review", "kh.search", Verb.SEARCH)
    assert "kh.search" in propose.ALL_OPERATIONS


def test_dispatcher_check_policy_accepts_the_tool():
    upstream_mcp_registry.register([_kh()])
    dispatcher = McpDispatcher(lambda: {"kh": _Fake()})
    result = dispatcher.check_policy("kh", "kh_search", {})
    assert result["gate"] == "review"
    assert dispatcher.check_policy("kh", "kh_list_topics", {})["gate"] == "auto"


def test_scoped_token_may_name_the_tool(tmp_path, monkeypatch):
    from privacyfence import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    policy = scoped_tokens.ScopedTokenPolicy(enabled=True, max_days=365)
    store = scoped_tokens.ScopedTokenStore(lambda: [LOCAL_PRINCIPAL])

    def create():
        return store.create(
            LOCAL_PRINCIPAL, label="kh-sync", days=30, rules=[], tools=["kh_search"], policy=policy,
            now=time.time(),
        )

    with pytest.raises(scoped_tokens.ScopedTokenError, match="not a PrivacyFence tool"):
        create()
    upstream_mcp_registry.register([_kh()])
    create()


def test_no_always_allow_offer_for_an_upstream_tool():
    upstream_mcp_registry.register([_kh()])
    ctx = make_ctx(connector="kh", tool="kh_propose_change", args={})
    assert propose.proposals_for("kh_propose_change", ctx) == []


def test_collision_refuses_with_nothing_written():
    clash = UpstreamServer(
        id="kh", label="x", url="https://kh.example.com/mcp", audience="kh",
        tools=(_tool("kh", "search", "review", "search"), _tool("kh", "list_topics", "auto", "read")),
    )
    other = UpstreamServer(
        id="gmail", label="x", url="https://g.example.com/mcp", audience="g",
        tools=(_tool("gmail", "get_message", "auto", "read"),),
    )
    before = _snapshot()
    with pytest.raises(ConfigurationError, match='tool "gmail_get_message" collides with an existing tool'):
        upstream_mcp_registry.register([clash, other])
    assert _snapshot() == before


def test_duplicate_exposed_name_within_the_call_refuses():
    twin = UpstreamServer(
        id="kh", label="x", url="https://kh.example.com/mcp", audience="kh",
        tools=(_tool("kh", "search", "review", "search"), _tool("kh", "search", "auto", "read")),
    )
    before = _snapshot()
    with pytest.raises(ConfigurationError, match="collides"):
        upstream_mcp_registry.register([twin])
    assert _snapshot() == before


def test_unregister_all_restores_everything():
    before = copy.deepcopy(_snapshot())
    registry_object = TOOL_REGISTRY
    upstream_mcp_registry.register([_kh()])
    assert _snapshot() != before
    upstream_mcp_registry.unregister_all()
    assert _snapshot() == before
    assert TOOL_REGISTRY is registry_object
    assert "kh.search" not in propose.ALL_OPERATIONS
