"""Tests for McpDispatcher -- the /mcp endpoint's connector-call dispatch
(web/mcp_dispatch.py). Ported test-for-test from the equivalent
IPCServer/_call_connector/_check_policy/_list_rules/_propose_rule_change/
unattended-session coverage in tests/unit/test_ipc_server.py, since
mcp_dispatch.py is itself a port of that logic onto a session key that isn't
id(writer) -- see that module's own docstring for why this is a separate
implementation rather than a shared refactor.

No socket, no ASGI, no MCP protocol framing here -- that's
tests/unit/web/test_routes_mcp.py's job. This file exercises McpDispatcher's
own methods directly, the same "hand-rolled client speaks the wire protocol
directly" vs. "test the dispatch logic itself" split test_ipc_server.py
already draws between itself and test_bridge_daemon_contract.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.auto_accept import init_auto_accept_evaluator
from privacyfence.connector import Connector, ToolSpec
from privacyfence.gate import is_unattended
from privacyfence.principal import Principal, principal_scope
from privacyfence.web.mcp_dispatch import McpDispatcher


class FakeConnector(Connector):
    def __init__(
        self, name: str, *, result=None, error: Exception | None = None, delay: float = 0.0, my_email: str = "",
    ):
        self._name = name
        self._result = result
        self._error = error
        self._delay = delay
        self.my_email = my_email
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name=f"{self._name}_tool", description="test read tool", read_only=True),
            ToolSpec(name=f"{self._name}_write_tool", description="test write tool", read_only=False),
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result


class UnattendedAwareConnector(Connector):
    def __init__(self, name: str):
        self._name = name
        self.observed_unattended: list[bool] = []

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=f"{self._name}_tool", description="t", read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        self.observed_unattended.append(is_unattended())
        return "ok"


def _dispatcher(connectors: dict[str, Connector] | None = None, **kwargs) -> McpDispatcher:
    store = dict(connectors or {})
    return McpDispatcher(lambda: store, **kwargs)


# --------------------------------------------------------------------------- #
# build_manifest / connectors
# --------------------------------------------------------------------------- #

def test_build_manifest_reflects_the_live_connector_set():
    connector = FakeConnector("gmail")
    dispatcher = _dispatcher({"gmail": connector})
    manifest = dispatcher.build_manifest()
    assert manifest["connectors"][0]["name"] == "gmail"
    assert {t["name"] for t in manifest["connectors"][0]["tools"]} == {"gmail_tool", "gmail_write_tool"}


def test_connectors_property_polls_the_provider_live():
    store: dict[str, Connector] = {}
    dispatcher = McpDispatcher(lambda: store)
    assert dispatcher.connectors == {}
    store["gmail"] = FakeConnector("gmail")
    assert list(dispatcher.connectors) == ["gmail"]


# --------------------------------------------------------------------------- #
# call() -- dedupe/staleness, ported from TestCallConnector in
# test_ipc_server.py
# --------------------------------------------------------------------------- #

class TestCall:
    async def test_calls_the_named_connector_tool(self):
        connector = FakeConnector("gmail", result={"ok": True})
        dispatcher = _dispatcher({"gmail": connector})
        result = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert result == {"ok": True}
        assert connector.calls == [("gmail_tool", {"x": 1})]

    async def test_reason_is_popped_before_reaching_the_connector(self):
        connector = FakeConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1, "reason": "because"})
        assert connector.calls == [("gmail_tool", {"x": 1})]

    async def test_unknown_connector_raises(self):
        dispatcher = _dispatcher({})
        with pytest.raises(ValueError, match="Unknown connector"):
            await dispatcher.call("s1", "nope", "nope_tool", {})

    async def test_concurrent_identical_calls_are_coalesced(self):
        connector = FakeConnector("gmail", result="r", delay=0.05)
        dispatcher = _dispatcher({"gmail": connector})
        results = await asyncio.gather(
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
        )
        assert results == ["r", "r"]
        assert len(connector.calls) == 1

    async def test_completed_read_result_is_reused_within_the_ttl(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_a_plain_write_tools_completed_result_is_still_reused_within_the_ttl(self):
        # Not in _DEDUPE_EXEMPT_TOOLS -- an ordinary write gets the same
        # completed-result reuse a read does (the mechanism this guards
        # against is a client-timeout retry double-firing the popup, not
        # "writes never dedupe"). See mcp_dispatch.py's module docstring
        # and _DEDUPE_EXEMPT_TOOLS.
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_write_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_write_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_dedupe_is_scoped_per_principal_not_shared_across_them(self):
        # P7: McpDispatcher is one shared instance for the whole process,
        # so its dedupe cache has to
        # key on the current principal too -- otherwise a second principal
        # calling the exact same tool with the exact same arguments within
        # the dedupe TTL would be handed the FIRST principal's actual
        # result instead of getting its own call dispatched. Regression
        # test for exactly that bug (caught by
        # web/test_org_mcp_e2e.py's own end-to-end version of this).
        connector = FakeConnector("gmail", result="depends-on-caller")
        dispatcher = _dispatcher({"gmail": connector})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with principal_scope(Principal(id="bob")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        # Both principals' calls actually reached the connector -- neither
        # was served the other's cached result.
        assert len(connector.calls) == 2

    async def test_dedupe_still_applies_within_the_same_principal(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_a_pending_approval_result_is_never_cached_for_reuse(self):
        # P3: a gated call that returned {"status": "approval_pending", ...}
        # must be
        # re-runnable immediately -- Claude re-issuing the identical call
        # is exactly how it collects the real decision from gate.py's
        # ledger, and that re-issue has to actually reach the connector
        # again, not be handed the same stale pending blob back by this
        # dispatcher's own (pre-P3) completed-result cache.
        class OnceThenRealDataConnector(FakeConnector):
            def __init__(self):
                super().__init__("gmail")

            async def call(self, tool, args):
                self.calls.append((tool, args))
                if len(self.calls) == 1:
                    return {"status": "approval_pending", "approval_id": "a1"}
                return "the real data"

        connector = OnceThenRealDataConnector()
        dispatcher = _dispatcher({"gmail": connector})

        first = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        second = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})

        assert first == {"status": "approval_pending", "approval_id": "a1"}
        assert second == "the real data"
        assert len(connector.calls) == 2  # the re-issue actually ran the connector again

    async def test_an_exempt_write_tool_always_reruns_after_completion(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_create_label", {"name": "x"})
        await dispatcher.call("s1", "gmail", "gmail_create_label", {"name": "x"})
        assert len(connector.calls) == 2

    async def test_read_result_older_than_a_write_to_the_same_connector_is_stale(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_write_tool", {"y": 1})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 3  # read, write, read again (not reused)

    async def test_a_write_by_one_principal_does_not_stale_another_principals_cached_read(self):
        # P7: the same staleness check above, but the write and the
        # cached read belong to two different principals -- one user's
        # write to their own connector account has nothing to say about
        # whether another user's already-cached read of theirs is stale.
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with principal_scope(Principal(id="bob")):
            await dispatcher.call("s1", "gmail", "gmail_write_tool", {"y": 1})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        # alice's second read is a cache hit -- bob's write never touched
        # her own staleness key. alice's read + bob's write = 2 calls
        # reaching the connector, not 3.
        assert len(connector.calls) == 2

    async def test_error_from_original_call_propagates_to_a_concurrent_deduped_retry(self):
        # Concurrent in-flight coalescing (both fired before either
        # completes) -- ported from
        # test_ipc_server.py::test_error_from_original_call_propagates_to_deduped_retry.
        connector = FakeConnector("gmail", error=ValueError("boom"), delay=0.05)
        dispatcher = _dispatcher({"gmail": connector})
        results = await asyncio.gather(
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
            return_exceptions=True,
        )
        assert all(isinstance(r, ValueError) and str(r) == "boom" for r in results)
        assert len(connector.calls) == 1

    async def test_a_failed_call_completed_result_is_also_reused_within_the_ttl(self):
        # Sequential, not concurrent: the failed future is still "done", so
        # the same completed-result reuse a successful call gets applies
        # here too (the exception propagates from the cached future).
        connector = FakeConnector("gmail", error=RuntimeError("boom"))
        dispatcher = _dispatcher({"gmail": connector})
        with pytest.raises(RuntimeError):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with pytest.raises(RuntimeError):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_different_args_are_not_deduped(self):
        connector = FakeConnector("gmail", result="ok")
        dispatcher = _dispatcher({"gmail": connector})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 2})
        assert len(connector.calls) == 2

    async def test_dedupe_window_expires_after_ttl(self):
        connector = FakeConnector("gmail", result="ok")
        dispatcher = _dispatcher({"gmail": connector})
        dispatcher._DEDUPE_TTL_SECONDS = 0.05
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        await asyncio.sleep(0.1)
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 2

    async def test_unattended_scope_reflects_this_sessions_flag(self):
        connector = UnattendedAwareConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector}, unattended_sessions_enabled=True)
        await dispatcher.call("s1", "gmail", "gmail_tool", {})
        dispatcher.begin_unattended_session("s1", "scheduled run")
        await dispatcher.call("s1", "gmail", "gmail_tool", {"z": 1})
        assert connector.observed_unattended == [False, True]

    async def test_a_different_sessions_unattended_flag_does_not_leak(self):
        connector = UnattendedAwareConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector}, unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "scheduled run")
        await dispatcher.call("s2", "gmail", "gmail_tool", {})
        assert connector.observed_unattended == [False]


# --------------------------------------------------------------------------- #
# check_policy -- ported from TestCheckPolicyDispatch
# --------------------------------------------------------------------------- #

class TestCheckPolicy:
    @pytest.fixture(autouse=True)
    def _audit_dir(self, tmp_path):
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def test_auto_gated_tool_is_always_auto_accept(self):
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        result = dispatcher.check_policy("gmail", "gmail_list_messages", {})
        assert result == {
            "gate": "auto", "verdict": "auto_accept", "matched_rule": None,
            "reason": "Unconditionally auto-accepted -- never reaches the review gate.",
            "pii_gate_may_apply": False,
        }

    def test_never_calls_the_connector(self):
        connector = FakeConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector})
        dispatcher.check_policy("gmail", "gmail_get_message", {})
        assert connector.calls == []

    def test_popup_tool_matching_args_only_rule_is_auto_accept(self):
        init_auto_accept_evaluator({"gmail.create_draft": [{"rule": "to_is_myself"}]})
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail", my_email="me@example.com")})
        result = dispatcher.check_policy(
            "gmail", "gmail_create_draft", {"to": "me@example.com", "subject": "x", "body": "y"},
        )
        assert result["gate"] == "popup"
        assert result["verdict"] == "auto_accept"
        assert result["matched_rule"] == "to_is_myself"

    def test_unknown_tool_raises(self):
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        with pytest.raises(ValueError, match="Unknown tool"):
            dispatcher.check_policy("gmail", "not_a_real_tool", {})

    def test_unknown_connector_raises(self):
        dispatcher = _dispatcher({})
        with pytest.raises(ValueError, match="Unknown connector"):
            dispatcher.check_policy("nope", "gmail_get_message", {})

    def test_records_a_policy_check_audit_entry_not_a_real_decision(self):
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        dispatcher.check_policy("gmail", "gmail_list_messages", {}, "Planning ahead.")
        entries = self._read_entries()
        assert len(entries) == 1
        assert entries[0]["decision"] == "policy_check"
        assert entries[0]["claude_reason"] == "Planning ahead."


# --------------------------------------------------------------------------- #
# list_rules -- ported from TestListRulesDispatch
# --------------------------------------------------------------------------- #

class TestListRules:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from privacyfence import auto_accept
        init_audit_logger(str(tmp_path / "audit"))
        self._audit_dir = tmp_path / "audit"
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept_rules: {gmail.read_message: [{rule: i_am_sender}]}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def test_returns_the_persisted_rules(self):
        # No longer a @staticmethod (the now-removed automated-test-strategy-plan.md
        # Phase 8): it now forces this principal's ConnectorRegistry entry
        # to exist first, the same way check_policy already did, so a
        # principal whose first-ever MCP call is this one still gets their
        # auto_accept config path initialized instead of raising -- an
        # empty connector set here is enough to exercise that, same as
        # _dispatcher()'s other callers above.
        result = _dispatcher({}).list_rules()
        assert result["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

    def test_records_a_rules_listed_audit_entry(self):
        _dispatcher({}).list_rules("checking before a scheduled run")
        entries = self._read_entries()
        assert entries[0]["decision"] == "rules_listed"
        assert entries[0]["claude_reason"] == "checking before a scheduled run"


# --------------------------------------------------------------------------- #
# get_sign_in_link -- privacyfence_get_sign_in_link's handler. No bridge-era
# equivalent (this tool is new, see mcp_tools.py's own module docstring).
# --------------------------------------------------------------------------- #

class TestGetSignInLink:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        init_audit_logger(str(tmp_path / "audit"))
        self._audit_dir = tmp_path / "audit"

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def test_no_provider_wired_raises(self):
        # The state every dispatcher starts in, and org mode's permanent
        # state (daemon_main.py's _start_org_web_server never wires one --
        # see McpDispatcher.set_bootstrap_link_provider's own docstring).
        with pytest.raises(ValueError, match="organization mode"):
            _dispatcher({}).get_sign_in_link("approvals")

    def test_delegates_to_the_wired_provider_with_a_leading_slash_path(self):
        calls = []
        dispatcher = _dispatcher({})
        dispatcher.set_bootstrap_link_provider(lambda path: calls.append(path) or f"http://x{path}?bootstrap=abc")

        result = dispatcher.get_sign_in_link("settings")

        assert calls == ["/settings"]
        assert result == {"url": "http://x/settings?bootstrap=abc"}

    def test_delegates_the_connectors_page_to_settings_connectors_path(self):
        calls = []
        dispatcher = _dispatcher({})
        dispatcher.set_bootstrap_link_provider(lambda path: calls.append(path) or f"http://x{path}?bootstrap=abc")

        result = dispatcher.get_sign_in_link("connectors")

        assert calls == ["/settings/connectors"]
        assert result == {"url": "http://x/settings/connectors?bootstrap=abc"}

    def test_empty_page_defaults_to_approvals(self):
        dispatcher = _dispatcher({})
        dispatcher.set_bootstrap_link_provider(lambda path: f"http://x{path}")
        assert dispatcher.get_sign_in_link("") == {"url": "http://x/approvals"}

    def test_invalid_page_is_rejected_before_the_provider_is_called(self):
        dispatcher = _dispatcher({})
        dispatcher.set_bootstrap_link_provider(lambda path: pytest.fail("must not be called"))
        with pytest.raises(ValueError, match="page must be"):
            dispatcher.get_sign_in_link("not-a-real-page")

    def test_provider_returning_none_raises_the_same_as_unwired(self):
        # WebServer.mint_bootstrap_url() itself returns None in org mode
        # (web/server.py) -- a provider wired to it, called through org
        # mode's own web server by mistake, must fail the same clear way
        # an unwired dispatcher already does, not hand back a None url.
        dispatcher = _dispatcher({})
        dispatcher.set_bootstrap_link_provider(lambda path: None)
        with pytest.raises(ValueError, match="organization mode"):
            dispatcher.get_sign_in_link("approvals")

    def test_records_a_sign_in_link_issued_audit_entry(self):
        dispatcher = _dispatcher({})
        dispatcher.set_bootstrap_link_provider(lambda path: f"http://x{path}")

        dispatcher.get_sign_in_link("approvals", "I'm locked out and need to check a pending approval")

        entries = self._read_entries()
        assert entries[0]["decision"] == "sign_in_link_issued"
        assert entries[0]["claude_reason"] == "I'm locked out and need to check a pending approval"

    def test_unset_provider_after_being_wired_raises_again(self):
        # set_bootstrap_link_provider(None) is a real, documented value --
        # the same "explicitly clear it" shape set_unattended_changed_
        # listener already accepts -- not just an unused default.
        dispatcher = _dispatcher({})
        dispatcher.set_bootstrap_link_provider(lambda path: f"http://x{path}")
        dispatcher.set_bootstrap_link_provider(None)
        with pytest.raises(ValueError, match="organization mode"):
            dispatcher.get_sign_in_link("approvals")


# --------------------------------------------------------------------------- #
# status -- privacyfence_status's handler (issue #396 Phase 2). No
# bridge-era equivalent, same as get_sign_in_link above.
# --------------------------------------------------------------------------- #

class TestStatus:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        init_audit_logger(str(tmp_path / "audit"))
        self._audit_dir = tmp_path / "audit"

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _row(name, *, enabled=True, authenticated=False, blocked_by=None):
        return {"name": name, "enabled": enabled, "authenticated": authenticated, "blocked_by": blocked_by}

    def test_defaults_to_local_mode(self):
        dispatcher = _dispatcher({})
        assert dispatcher.status("checking")["mode"] == "local"

    def test_reports_org_mode_when_constructed_that_way(self):
        dispatcher = _dispatcher({}, mode="org")
        assert dispatcher.status("checking")["mode"] == "org"

    def test_no_connectors_state_provider_falls_back_to_built_connectors_only(self):
        # org mode today, or a test that never wires one -- the only thing
        # knowable without SettingsController's own config/failure state is
        # which connectors this dispatcher can actually see built.
        dispatcher = _dispatcher({"gmail": object()})
        result = dispatcher.status("checking")
        assert result["connectors"] == [self._row("gmail", authenticated=True)]
        assert result["setup_complete"] is True

    def test_setup_incomplete_when_no_connector_is_authenticated(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [
            self._row("gmail", blocked_by="no_org_config"), self._row("slack", blocked_by="not_authenticated"),
        ])
        result = dispatcher.status("checking")
        assert result["setup_complete"] is False
        assert result["next_step"] == "authenticate_connectors"

    def test_setup_complete_when_at_least_one_connector_is_authenticated(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [
            self._row("gmail", authenticated=True), self._row("slack", blocked_by="not_authenticated"),
        ])
        result = dispatcher.status("checking")
        assert result["setup_complete"] is True
        assert result["next_step"] is None
        assert "sign_in_url" not in result

    def test_mints_a_sign_in_url_when_local_and_un_onboarded(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.set_bootstrap_link_provider(lambda path: f"http://x{path}?bootstrap=abc")
        result = dispatcher.status("checking")
        assert result["sign_in_url"] == "http://x/settings/connectors?bootstrap=abc"

    def test_mints_to_the_connectors_path_specifically(self):
        # issue #396 Part C: lands on the screen that actually unblocks an
        # un-onboarded install, not plain /settings (which opens on
        # General).
        calls = []
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.set_bootstrap_link_provider(lambda path: calls.append(path) or f"http://x{path}")
        dispatcher.status("checking")
        assert calls == ["/settings/connectors"]

    def test_sign_in_url_is_none_when_no_bootstrap_provider_is_wired(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        result = dispatcher.status("checking")
        assert result["sign_in_url"] is None

    def test_org_mode_never_mints_a_link_even_if_one_is_wired(self):
        dispatcher = _dispatcher({}, mode="org")
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.set_bootstrap_link_provider(lambda path: pytest.fail("must not be called"))
        result = dispatcher.status("checking")
        assert result["sign_in_url"] is None
        assert result["next_step"] == "contact_your_administrator"

    def test_caches_the_minted_link_instead_of_reminting_every_call(self, monkeypatch):
        from privacyfence.web import mcp_dispatch as mcp_dispatch_module

        clock = {"now": 1_000.0}
        monkeypatch.setattr(mcp_dispatch_module.time, "time", lambda: clock["now"])
        calls = []
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.set_bootstrap_link_provider(lambda path: calls.append(1) or f"http://x{path}?bootstrap={len(calls)}")

        first = dispatcher.status("checking")["sign_in_url"]
        clock["now"] += 60  # still well inside the cache window
        second = dispatcher.status("checking")["sign_in_url"]

        assert first == second == "http://x/settings/connectors?bootstrap=1"
        assert len(calls) == 1

    def test_remints_once_the_cache_window_elapses(self, monkeypatch):
        from privacyfence.web import mcp_dispatch as mcp_dispatch_module

        clock = {"now": 1_000.0}
        monkeypatch.setattr(mcp_dispatch_module.time, "time", lambda: clock["now"])
        calls = []
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.set_bootstrap_link_provider(lambda path: calls.append(1) or f"http://x{path}?bootstrap={len(calls)}")

        first = dispatcher.status("checking")["sign_in_url"]
        clock["now"] += McpDispatcher._STATUS_LINK_CACHE_SECONDS + 1
        second = dispatcher.status("checking")["sign_in_url"]

        assert first == "http://x/settings/connectors?bootstrap=1"
        assert second == "http://x/settings/connectors?bootstrap=2"
        assert len(calls) == 2

    def test_audits_sign_in_link_issued_only_on_a_fresh_mint(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.set_bootstrap_link_provider(lambda path: f"http://x{path}")

        dispatcher.status("first check")
        dispatcher.status("second check")  # served from cache -- not a fresh mint

        entries = self._read_entries()
        assert [e["decision"] for e in entries] == ["sign_in_link_issued", "status_checked"]
        assert entries[0]["claude_reason"] == "first check"
        assert entries[1]["claude_reason"] == "second check"

    def test_audit_summary_names_the_connectors_path(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.set_bootstrap_link_provider(lambda path: f"http://x{path}")

        dispatcher.status("checking")

        entries = self._read_entries()
        assert entries[0]["summary"] == "Issued a one-time sign-in link for /settings/connectors (via privacyfence_status)"

    def test_audits_status_checked_when_already_set_up(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail", authenticated=True)])
        dispatcher.status("just checking")
        entries = self._read_entries()
        assert entries[0]["decision"] == "status_checked"

    def test_audits_status_checked_in_org_mode(self):
        dispatcher = _dispatcher({}, mode="org")
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.status("just checking")
        entries = self._read_entries()
        assert entries[0]["decision"] == "status_checked"


# --------------------------------------------------------------------------- #
# propose_rule_change -- ported from TestProposeRuleChangeDispatch
# --------------------------------------------------------------------------- #

class TestProposeRuleChange:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        from privacyfence import auto_accept, gate
        init_audit_logger(str(tmp_path / "audit"))
        self._config_path = tmp_path / "settings.yaml"
        self._config_path.write_text("auto_accept_rules: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(self._config_path))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)

    async def test_confirmed_rule_add_is_persisted_to_disk(self):
        dispatcher = _dispatcher({})
        result = await dispatcher.propose_rule_change("s1", {
            "target": "rule", "operation": "add", "reason": "Trusting example.com.",
            "operation_key": "gmail.read_message", "rule_name": "trusted_sender_domain",
            "value": ["example.com"],
        })
        assert result["confirmed"] is True
        assert "trusted_sender_domain" in self._config_path.read_text(encoding="utf-8")

    async def test_declined_confirmation_raises(self, monkeypatch):
        from privacyfence import gate
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: False)
        dispatcher = _dispatcher({})
        with pytest.raises(RuntimeError, match="denied"):
            await dispatcher.propose_rule_change("s1", {
                "target": "rule", "operation": "add", "reason": "x",
                "operation_key": "gmail.read_message", "rule_name": "i_am_sender",
            })

    async def test_denied_immediately_when_this_sessions_unattended_flag_is_set(self, monkeypatch):
        # TST-02 regression -- see propose_rule_change's own docstring
        # comment in mcp_dispatch.py: before this fix, an unattended
        # session's own privacyfence_propose_auto_accept_rule_change call
        # never saw itself as unattended and fell through to a real
        # (never-to-be-answered) confirmation popup instead of the
        # immediate denial its tool description promises.
        called = []
        from privacyfence import gate
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: called.append(1) or True)
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "scheduled run")

        with pytest.raises(RuntimeError, match="unattended session"):
            await dispatcher.propose_rule_change("s1", {
                "target": "rule", "operation": "add", "reason": "x",
                "operation_key": "gmail.read_message", "rule_name": "i_am_sender",
            })

        assert called == []  # popup never shown

    async def test_a_different_sessions_unattended_flag_does_not_leak_into_this_one(self, monkeypatch):
        from privacyfence import gate
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description: True)
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "scheduled run")

        result = await dispatcher.propose_rule_change("s2", {
            "target": "rule", "operation": "add", "reason": "x",
            "operation_key": "gmail.read_message", "rule_name": "i_am_sender",
        })

        assert result["confirmed"] is True


# --------------------------------------------------------------------------- #
# Unattended sessions -- ported from TestUnattendedSessionDispatch
# --------------------------------------------------------------------------- #

class TestUnattendedSessions:
    def test_disabled_by_default_raises(self):
        dispatcher = _dispatcher({})
        with pytest.raises(ValueError, match="disabled"):
            dispatcher.begin_unattended_session("s1", "why")

    def test_begin_then_end_clears_the_flag(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        assert dispatcher.begin_unattended_session("s1", "why") == {"unattended": True}
        assert dispatcher.unattended_session_count() == 1
        assert dispatcher.end_unattended_session("s1") == {"unattended": False}
        assert dispatcher.unattended_session_count() == 0

    def test_end_session_clears_an_unattended_flag_like_a_dropped_connection(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "why")
        dispatcher.end_session("s1")
        assert dispatcher.unattended_session_count() == 0

    def test_end_session_on_a_session_that_was_never_unattended_is_a_no_op(self):
        dispatcher = _dispatcher({})
        dispatcher.end_session("s1")  # must not raise
        assert dispatcher.unattended_session_count() == 0

    def test_changed_listener_fires_on_begin_and_end(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        events = []
        dispatcher.set_unattended_changed_listener(lambda: events.append(1))
        dispatcher.begin_unattended_session("s1", "why")
        dispatcher.end_unattended_session("s1")
        assert len(events) == 2


# --------------------------------------------------------------------------- #
# notify_tools_changed -- issue #396 Part C. The actual notification send is
# web/routes_mcp.py's own concern (its live ServerSession registry); this
# dispatcher is just the wired seam SettingsController.refresh_connectors()
# calls into, same shape as every other set_*_provider/listener above.
# --------------------------------------------------------------------------- #

class TestToolsChangedBroadcast:
    def test_unwired_is_a_no_op(self):
        dispatcher = _dispatcher({})
        dispatcher.notify_tools_changed()  # must not raise

    def test_wired_broadcaster_is_called(self):
        dispatcher = _dispatcher({})
        calls = []
        dispatcher.set_tools_changed_broadcaster(lambda: calls.append(1))
        dispatcher.notify_tools_changed()
        dispatcher.notify_tools_changed()
        assert len(calls) == 2

    def test_unset_broadcaster_after_being_wired_goes_back_to_a_no_op(self):
        dispatcher = _dispatcher({})
        calls = []
        dispatcher.set_tools_changed_broadcaster(lambda: calls.append(1))
        dispatcher.set_tools_changed_broadcaster(None)
        dispatcher.notify_tools_changed()  # must not raise
        assert calls == []


class TestAwaitApproval:
    """privacyfence_await_approval's handler (P3, docs/https-connector-
    refactor-plan.md §5.2 point 7): long-poll the registry, status only."""

    async def test_no_registry_reports_every_id_as_unknown(self):
        dispatcher = _dispatcher({})  # registry=None, the default
        result = await dispatcher.await_approval(["a1", "a2"], timeout_seconds=1)
        assert result == {"a1": "unknown", "a2": "unknown"}

    async def test_empty_id_list_returns_immediately_with_no_status(self):
        dispatcher = _dispatcher({}, registry=PendingApprovalRegistry())
        result = await dispatcher.await_approval([], timeout_seconds=5)
        assert result == {}

    async def test_returns_as_soon_as_a_pending_approval_is_decided(self):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )

        async def _decide_soon():
            await asyncio.sleep(0.05)
            registry.finalize(approval.id, "accept")

        results = await asyncio.gather(
            dispatcher.await_approval([approval.id], timeout_seconds=10),
            _decide_soon(),
        )
        assert results[0] == {approval.id: "approved"}

    async def test_times_out_and_reports_pending_if_nobody_decides(self):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )

        result = await dispatcher.await_approval([approval.id], timeout_seconds=1)

        assert result == {approval.id: "pending"}

    async def test_timeout_is_clamped_into_a_sane_range(self):
        # Never actually waits the full (absurd) requested duration -- the
        # clamp, not the caller's number, decides how long this can block.
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        dispatcher._AWAIT_APPROVAL_MAX_TIMEOUT = 1  # keep the test itself fast
        dispatcher._AWAIT_APPROVAL_POLL_SECONDS = 0.05

        result = await asyncio.wait_for(
            dispatcher.await_approval([approval.id], timeout_seconds=999_999), timeout=2.0,
        )

        assert result == {approval.id: "pending"}

    async def test_mixed_ids_report_each_ones_own_status(self):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        approval, _ = registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
        )
        registry.finalize(approval.id, "deny")

        result = await dispatcher.await_approval([approval.id, "not-a-real-id"], timeout_seconds=1)

        assert result == {approval.id: "denied", "not-a-real-id": "unknown"}

    async def test_a_foreign_principals_approval_id_reads_as_unknown(self):
        # P9, §10.5: the same cross-principal check every other approval
        # read gets -- an id belonging to a different principal must not
        # even confirm it exists.
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        dispatcher = _dispatcher({}, registry=registry)
        with principal_scope(Principal(id="alice")):
            approval, _ = registry.register_or_coalesce(
                dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review", request_id="r1",
            )

        with principal_scope(Principal(id="bob")):
            result = await dispatcher.await_approval([approval.id], timeout_seconds=1)

        assert result == {approval.id: "unknown"}
