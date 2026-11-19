"""Live registration of saved upstream tool lists into the policy tables (ADR 0331)."""
from __future__ import annotations

import threading

import pytest

from privacyfence import auto_accept, gate, upstream_mcp_registry, write_effects
from privacyfence.org_mode import ConfigurationError
from privacyfence.policy import describe, propose, scopes
from privacyfence.policy import registry as policy_registry
from privacyfence.policy.registry import TOOL_REGISTRY, Verb
from privacyfence.upstream_mcp_config import UpstreamOAuthSettings, UpstreamServer
from privacyfence.upstream_mcp_registry import UpstreamRegistry, get_registry, reset
from privacyfence.upstream_tools import SavedTool, ToolEntry, classify, declaration_digest, tool_digest

URL = "https://mcp.example.com/mcp"


def upstream(upstream_id="kh", label="Knowledge Hub", url=URL) -> UpstreamServer:
    return UpstreamServer(id=upstream_id, label=label, url=url, oauth=UpstreamOAuthSettings())


def saved_tool(name, gate_="review", read_only=True, destructive=None) -> SavedTool:
    entry = ToolEntry(name, f"{name} tool", {"type": "object", "properties": {}}, read_only, destructive)
    _default, verb = classify(read_only, destructive)
    return SavedTool(
        name=name, description=entry.description, input_schema=entry.input_schema, read_only=read_only,
        destructive=destructive, gate=gate_, verb=verb, digest=tool_digest(entry),
    )


REVIEW = saved_tool("search")
POPUP = saved_tool("write", "popup", read_only=False, destructive=True)
AUTO = saved_tool("fetch", "auto", read_only=True)
AUTO_WRITE = saved_tool("update_note", "auto", read_only=False, destructive=False)


def _tables():
    return {
        "gate": dict(auto_accept.TOOL_TO_GATE),
        "operation": dict(auto_accept.TOOL_TO_OPERATION),
        "verb": dict(policy_registry.TOOL_TO_VERB),
        "effects": dict(write_effects.EFFECT_BY_TOOL),
        "layout": dict(gate._TOOL_LAYOUT),
        "declarations": dict(scopes.UPSTREAM_DECLARATIONS),
        "auto": set(scopes.UPSTREAM_AUTO_TOOLS),
        "registry": dict(TOOL_REGISTRY),
        "operations": propose.ALL_OPERATIONS,
        "scopes": propose.PROPOSABLE_SCOPES,
        "groups": dict(propose.SCOPES_BY_GROUP),
        "label": describe.connector_label("kh"),
    }


class TestApplyAndClear:
    def test_apply_registers_every_table_and_clear_restores_them(self):
        before = _tables()
        registry = get_registry()
        registry.apply(upstream(), (REVIEW, POPUP, AUTO))

        assert auto_accept.TOOL_TO_GATE["kh_search"] == "review"
        assert auto_accept.TOOL_TO_GATE["kh_write"] == "popup"
        assert auto_accept.TOOL_TO_GATE["kh_fetch"] == "review"  # the class default of a read-only tool
        assert auto_accept.TOOL_TO_OPERATION["kh_write"] == "kh.write"
        assert policy_registry.TOOL_TO_VERB["kh_write"] is Verb.DELETE
        assert policy_registry.TOOL_TO_VERB["kh_search"] is Verb.READ
        assert TOOL_REGISTRY["kh_write"].operation == "kh.write"
        assert {"kh.search", "kh.write", "kh.fetch"} <= propose.ALL_OPERATIONS
        assert gate._TOOL_LAYOUT["kh_search"] == "wide"
        assert scopes.UPSTREAM_DECLARATIONS["kh.search"] == declaration_digest(
            URL, "kh.search", "read", "review", REVIEW.digest,
        )
        assert scopes.UPSTREAM_DECLARATIONS["kh.fetch"] == declaration_digest(
            URL, "kh.fetch", "read", "auto", AUTO.digest,
        )
        assert describe.connector_label("kh") == "Knowledge Hub"
        assert "upstream.always_allow/kh_fetch" in propose.SCOPES_BY_GROUP

        registry.clear("kh")
        after = _tables()
        assert after == before

    def test_popup_tools_get_the_effect_template_and_review_tools_do_not(self):
        get_registry().apply(upstream(), (REVIEW, POPUP))
        assert write_effects.EFFECT_BY_TOOL["kh_write"] == "Calls write on Knowledge Hub; the server may change data."
        assert "kh_search" not in write_effects.EFFECT_BY_TOOL

    def test_the_gate_table_never_holds_auto(self):
        get_registry().apply(upstream(), (REVIEW, POPUP, AUTO, AUTO_WRITE))
        assert auto_accept.TOOL_TO_GATE["kh_update_note"] == "popup"
        upstream_names = [name for name in auto_accept.TOOL_TO_GATE if name.startswith("kh_")]
        assert len(upstream_names) == 4
        assert "auto" not in {auto_accept.TOOL_TO_GATE[name] for name in upstream_names}

    def test_the_auto_set_holds_only_auto_tools(self):
        get_registry().apply(upstream(), (REVIEW, POPUP, AUTO, AUTO_WRITE))
        assert scopes.UPSTREAM_AUTO_TOOLS == {"kh_fetch", "kh_update_note"}

    def test_a_second_apply_replaces_the_first(self):
        registry = get_registry()
        registry.apply(upstream(), (REVIEW, POPUP, AUTO))
        registry.apply(upstream(label="KH 2"), (saved_tool("search", "popup", read_only=True), saved_tool("other")))

        assert "kh_write" not in auto_accept.TOOL_TO_GATE and "kh_fetch" not in auto_accept.TOOL_TO_GATE
        assert "kh.write" not in scopes.UPSTREAM_DECLARATIONS and "kh_write" not in TOOL_REGISTRY
        assert auto_accept.TOOL_TO_GATE["kh_search"] == "popup"
        assert "kh_search" in write_effects.EFFECT_BY_TOOL
        assert auto_accept.TOOL_TO_GATE["kh_other"] == "review"
        assert scopes.UPSTREAM_AUTO_TOOLS == set()
        assert not [g for g in propose.SCOPES_BY_GROUP if g.startswith("upstream.always_allow/")]
        assert describe.connector_label("kh") == "KH 2"

    def test_a_tool_that_loses_auto_leaves_the_auto_set(self):
        registry = get_registry()
        registry.apply(upstream(), (AUTO,))
        registry.apply(upstream(), (saved_tool("fetch", "review"),))
        assert scopes.UPSTREAM_AUTO_TOOLS == set()

    def test_a_popup_tool_lowered_to_review_loses_its_effect_text(self):
        registry = get_registry()
        registry.apply(upstream(), (POPUP,))
        registry.apply(upstream(), (saved_tool("write", "review", read_only=False, destructive=True),))
        assert "kh_write" not in write_effects.EFFECT_BY_TOOL

    def test_a_collision_raises_and_changes_nothing(self):
        before = _tables()
        with pytest.raises(ConfigurationError, match='Tool "gmail_get_message" collides with an existing tool'):
            get_registry().apply(upstream("gmail", "Gmail"), (saved_tool("get_message"),))
        assert _tables() == before

    def test_two_upstreams_coexist_and_each_clears_alone(self):
        registry = get_registry()
        registry.apply(upstream("kh"), (AUTO,))
        registry.apply(upstream("wiki", "Wiki"), (saved_tool("page", "auto"),))
        assert scopes.UPSTREAM_AUTO_TOOLS == {"kh_fetch", "wiki_page"}
        assert describe.connector_label("wiki") == "Wiki"
        registry.clear("kh")
        assert scopes.UPSTREAM_AUTO_TOOLS == {"wiki_page"}
        assert describe.connector_label("kh") == "Kh"
        assert "upstream.always_allow/wiki_page" in propose.SCOPES_BY_GROUP

    def test_clear_of_an_unknown_upstream_is_a_no_op(self):
        before = _tables()
        get_registry().clear("nothing")
        assert _tables() == before

    def test_unregister_all_removes_every_upstream(self):
        before = _tables()
        registry = get_registry()
        registry.apply(upstream("kh"), (REVIEW, AUTO))
        registry.apply(upstream("wiki", "Wiki"), (POPUP,))
        registry.unregister_all()
        assert _tables() == before

    def test_the_auto_scope_excludes_every_other_operation_of_its_connector(self):
        get_registry().apply(upstream(), (REVIEW, POPUP, AUTO))
        (scope,) = propose.SCOPES_BY_GROUP["upstream.always_allow/kh_fetch"]
        assert scope.excludes == {"kh.search", "kh.write"}
        assert scope.fixed_value == "kh_fetch" and scope.verbs == {Verb.READ} and not scope.widenable
        assert propose.operations_for(scope, Verb.READ) == {"kh.fetch"}
        assert scope.value_of(None) == "kh_fetch"


def test_the_registry_is_never_empty_while_apply_runs():
    registry = get_registry()
    registry.apply(upstream(), (REVIEW,))
    stop = threading.Event()
    smallest = [len(TOOL_REGISTRY)]

    def reader():
        while not stop.is_set():
            smallest[0] = min(smallest[0], len(TOOL_REGISTRY))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(200):
            registry.apply(upstream(), (REVIEW, saved_tool(f"extra{index % 3}")))
    finally:
        stop.set()
        thread.join()
    assert smallest[0] > 0


class TestProcessWideInstance:
    def test_get_registry_is_created_once(self):
        first = get_registry()
        assert isinstance(first, UpstreamRegistry) and get_registry() is first

    def test_reset_unregisters_and_drops_the_instance(self):
        first = get_registry()
        first.apply(upstream(), (REVIEW,))
        reset()
        assert "kh_search" not in auto_accept.TOOL_TO_GATE
        assert get_registry() is not first

    def test_reset_with_no_instance_is_harmless(self):
        reset()
        reset()
        assert upstream_mcp_registry._registry is None
