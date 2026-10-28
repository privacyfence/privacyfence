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
import dataclasses
import json
import time

import pytest

from privacyfence import local_files, scoped_tokens
from privacyfence.approvals import ApprovalPending, PendingApprovalRegistry
from privacyfence.audit_log import current_released_request_id, current_week, init_audit_logger, set_released_request_id
from privacyfence.connector import Connector, ToolSpec
from privacyfence.deny_feedback import DENIAL_FEEDBACK_KEY, INTENTS, DenialFeedback
from privacyfence import gate
from privacyfence.gate import GateDeniedError, is_unattended
from privacyfence.principal import Principal, principal_scope
from privacyfence.scoped_tokens import ScopedToken
from privacyfence.web import mcp_dispatch
from privacyfence.web.mcp_dispatch import McpDispatcher

from ...helpers import policy_rules


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


_SCOPE = ScopedToken(
    id="st-0123456789ab", label="sales-sync", digest="", principal_id="local", principal_email="",
    principal_display_name="", created_at=time.time(), expires_at=time.time() + 90 * 86400,
    tools=frozenset({"gmail_tool"}),
)


class ScopeAwareConnector(Connector):
    """Records the scoped token each call ran under; denies a scoped call, as the gate would
    for one none of the token's rules covers, unless ``allow_scoped``."""

    def __init__(self, name: str, *, delay: float = 0.0, allow_scoped: bool = False):
        self._name = name
        self._delay = delay
        self._allow_scoped = allow_scoped
        self.seen_scopes: list[str | None] = []

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=f"{self._name}_tool", description="t", read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        scope = scoped_tokens.current_token_scope()
        self.seen_scopes.append(scope.id if scope is not None else None)
        if self._delay:
            await asyncio.sleep(self._delay)
        if scope is not None and not self._allow_scoped:
            raise GateDeniedError("Request denied: this call uses a scoped token.")
        return "full-token result" if scope is None else "scoped result"


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

    async def test_the_connector_sees_the_whole_request_minus_reason(self):
        seen: list[str | None] = []

        class Recorder(FakeConnector):
            async def call(self, tool, args):
                seen.append(gate.current_request_args())
                return "ok"

        dispatcher = _dispatcher({"gmail": Recorder("gmail")})
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1, "reason": "r"})
        assert seen == ['{"x": 1}']
        assert gate.current_request_args() is None

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
        # McpDispatcher is one shared instance for the whole process,
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

    async def test_a_scoped_call_never_reuses_an_unscoped_result(self):
        # Within the dedupe window, a scoped call must reach the gate itself rather than be
        # handed what a full-token call with the same arguments got (ADR 0164).
        connector = ScopeAwareConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector})
        unscoped = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with scoped_tokens.token_scope(_SCOPE), pytest.raises(GateDeniedError):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert unscoped == "full-token result"
        assert connector.seen_scopes == [None, _SCOPE.id]

    async def test_an_unscoped_call_never_reuses_a_scoped_denial(self):
        # The scoped call is still in flight when the identical unscoped one arrives -- the case
        # where the dispatcher hands a caller someone else's future.
        connector = ScopeAwareConnector("gmail", delay=0.05)
        dispatcher = _dispatcher({"gmail": connector})

        async def scoped_call():
            with scoped_tokens.token_scope(_SCOPE):
                return await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})

        scoped, unscoped = await asyncio.gather(
            scoped_call(), dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}), return_exceptions=True,
        )
        assert isinstance(scoped, GateDeniedError)
        assert unscoped == "full-token result"
        assert sorted(connector.seen_scopes, key=str) == sorted([_SCOPE.id, None], key=str)

    async def test_two_identical_scoped_calls_still_dedupe(self):
        connector = ScopeAwareConnector("gmail", allow_scoped=True)
        dispatcher = _dispatcher({"gmail": connector})
        with scoped_tokens.token_scope(_SCOPE):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert connector.seen_scopes == [_SCOPE.id]

    async def test_dedupe_still_applies_within_the_same_principal(self):
        connector = FakeConnector("gmail", result="r")
        dispatcher = _dispatcher({"gmail": connector})
        with principal_scope(Principal(id="alice")):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 1

    async def test_an_identical_call_from_another_session_is_not_deduped(self):
        connector = FakeConnector("gmail", result="r", delay=0.05)
        dispatcher = _dispatcher({"gmail": connector})
        await asyncio.gather(
            dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}),
            dispatcher.call("s2", "gmail", "gmail_tool", {"x": 1}),
        )
        assert len(connector.calls) == 2

    async def test_an_unattended_session_never_gets_another_sessions_result(self):
        connector = UnattendedAwareConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector}, unattended_sessions_enabled=True)
        await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        dispatcher.begin_unattended_session("s2", "why")
        await dispatcher.call("s2", "gmail", "gmail_tool", {"x": 1})
        assert connector.observed_unattended == [False, True]

    async def test_cancelled_leader_does_not_poison_the_dedupe_key(self):
        release = asyncio.Event()

        class Waiting(FakeConnector):
            async def call(self, tool, args):
                self.calls.append((tool, args))
                await release.wait()
                return "r"

        connector = Waiting("gmail")
        dispatcher = _dispatcher({"gmail": connector})
        first = asyncio.create_task(dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert first.cancelled()
        release.set()
        assert await asyncio.wait_for(dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1}), 1) == "r"
        assert len(connector.calls) == 2
        assert dispatcher._inflight  # the completed second call is cached, the cancelled one left no stale key
        assert all(fut.done() and not fut.cancelled() for fut, _ in dispatcher._inflight.values())

    async def test_a_pending_approval_result_is_never_cached_for_reuse(self):
        # A gated call that went pending (gate.gated_call raises
        # ApprovalPending, ADR 0093) comes back as its
        # {"status": "approval_pending", ...} result and must be
        # re-runnable immediately -- Claude re-issuing the identical call
        # is exactly how it collects the real decision from gate.py's
        # ledger, and that re-issue has to actually reach the connector
        # again, not be handed the same stale pending blob back by this
        # dispatcher's own completed-result cache.
        class OnceThenRealDataConnector(FakeConnector):
            def __init__(self):
                super().__init__("gmail")

            async def call(self, tool, args):
                self.calls.append((tool, args))
                if len(self.calls) == 1:
                    raise ApprovalPending({"status": "approval_pending", "approval_id": "a1"})
                return "the real data"

        connector = OnceThenRealDataConnector()
        dispatcher = _dispatcher({"gmail": connector})

        first = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        second = await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})

        assert first == {"status": "approval_pending", "approval_id": "a1"}
        assert second == "the real data"
        assert len(connector.calls) == 2  # the re-issue actually ran the connector again

    async def test_a_released_request_does_not_outlive_its_call(self):
        # What one call's gate released (the request_id a staged download
        # is attributed to) is scoped to that call -- ADR 0093.
        seen = []

        class ReleasingConnector(FakeConnector):
            async def call(self, tool, args):
                seen.append(current_released_request_id())
                set_released_request_id("req-1")
                return "r"

        dispatcher = _dispatcher({"gmail": ReleasingConnector("gmail")})
        await dispatcher.call("s1", "gmail", "gmail_create_label", {"name": "x"})
        await dispatcher.call("s1", "gmail", "gmail_create_label", {"name": "x"})

        assert seen == ["", ""]
        assert current_released_request_id() == ""

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
        # The same staleness check above, but the write and the
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

    async def test_a_failed_call_is_not_reused_by_a_later_sequential_call(self):
        # A failed call is popped from
        # _inflight once its exception is set, so a *new*, sequential call
        # in the same dedupe window re-runs instead of replaying the same
        # failure -- unlike a concurrent retry racing the original call
        # (test_error_from_original_call_propagates_to_a_concurrent_deduped_
        # retry above), which still gets the same in-flight future and
        # therefore the same exception, since it started before the pop
        # could happen.
        connector = FakeConnector("gmail", error=RuntimeError("boom"))
        dispatcher = _dispatcher({"gmail": connector})
        with pytest.raises(RuntimeError):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with pytest.raises(RuntimeError):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 2

    async def test_a_result_with_bridge_deliveries_is_not_reused(self, monkeypatch):
        # A result that staged a file-bridge download carries a
        # single-use download_staging token -- reusing it from the dedupe
        # cache would hand a second caller a token the first claim already
        # consumed. local_files.call_context() is entered here exactly the
        # way routes_mcp.py's handle_call_tool enters it around dispatch.
        monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: True)

        class DeliveringConnector(FakeConnector):
            async def call(self, tool: str, args: dict) -> object:
                result = await super().call(tool, args)
                local_files.deliver_file(
                    "~/Downloads", "f.pdf", b"data", "application/pdf", download_mode="local",
                )
                return result

        connector = DeliveringConnector("gmail", result="ok")
        dispatcher = _dispatcher({"gmail": connector})
        with local_files.call_context(bridge_available=True, uploads={}):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with local_files.call_context(bridge_available=True, uploads={}):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        assert len(connector.calls) == 2

    async def test_a_result_with_no_deliveries_is_still_deduped_as_before(self, monkeypatch):
        # Control for the test above: entering call_context() alone must
        # not itself defeat dedupe -- only an actual staged delivery does.
        monkeypatch.setattr(local_files.privilege_separation, "is_enabled", lambda: True)
        connector = FakeConnector("gmail", result="ok")
        dispatcher = _dispatcher({"gmail": connector})
        with local_files.call_context(bridge_available=True, uploads={}):
            await dispatcher.call("s1", "gmail", "gmail_tool", {"x": 1})
        with local_files.call_context(bridge_available=True, uploads={}):
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
            "gate": "auto", "verdict": "auto_accept", "matched_rule": None, "matched_rule_id": None,
            "reason": "Unconditionally auto-accepted -- never reaches the review gate.",
            "pii_gate_may_apply": False, "pii_gate": "none", "pii_gate_detail": "", "suggested_rules": [],
        }

    @staticmethod
    def _token(*, allow_personal_data: bool) -> ScopedToken:
        return ScopedToken(
            id="st-0123456789ab", label="sync", digest="", principal_id="local", principal_email="",
            principal_display_name="", created_at=time.time(), expires_at=time.time() + 86400,
            tools=frozenset(), allow_personal_data=allow_personal_data,
        )

    def _pii(self, connector: str, tool: str, token: ScopedToken | None = None) -> dict:
        dispatcher = _dispatcher({connector: FakeConnector(connector)})
        if token is None:
            return dispatcher.check_policy(connector, tool, {})
        with scoped_tokens.token_scope(token):
            return dispatcher.check_policy(connector, tool, {})

    def test_connector_computed_slack_args_are_not_passed_to_preflight(self, monkeypatch):
        seen = {}

        def fake_preflight(operation_key, args, my_email, tool=""):
            seen["args"] = args
            return "unknown", "", "", "x"

        monkeypatch.setattr(mcp_dispatch, "preflight_auto_accept", fake_preflight)
        dispatcher = _dispatcher({"slack": FakeConnector("slack")})
        dispatcher.check_policy(
            "slack", "slack_get_thread_replies",
            {"channel_id": "D1", "is_one_to_one_dm": True, "own_user_id": "U_ME", "is_self_dm": True},
        )
        assert seen["args"] == {"channel_id": "D1"}

    def test_member_conversation_args_are_not_passed_to_preflight(self, monkeypatch):
        seen = {}

        def fake_preflight(operation_key, args, my_email, tool=""):
            seen["args"] = args
            return "unknown", "", "", "x"

        monkeypatch.setattr(mcp_dispatch, "preflight_auto_accept", fake_preflight)
        dispatcher = _dispatcher({"slack": FakeConnector("slack")})
        dispatcher.check_policy(
            "slack", "slack_search_messages",
            {"member_only": True, "is_member": True, "all_results_in_my_conversations": True},
        )
        assert seen["args"] == {"member_only": True}

    def test_matching_queries_is_not_passed_to_preflight(self, monkeypatch):
        seen = {}

        def fake_preflight(operation_key, args, my_email, tool=""):
            seen["args"] = args
            return "unknown", "", "", "x"

        monkeypatch.setattr(mcp_dispatch, "preflight_auto_accept", fake_preflight)
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        dispatcher.check_policy("gmail", "gmail_get_message", {"message_id": "m1", "matching_queries": ["in:inbox"]})
        assert seen["args"] == {"message_id": "m1"}

    def test_pii_gate_none_for_popup_tool_outside_the_upload_scan(self):
        result = self._pii("grist", "grist_update_csv")
        assert result["gate"] == "popup"
        assert (result["pii_gate"], result["pii_gate_detail"]) == ("none", "")
        assert result["pii_gate_may_apply"] is False

    def test_pii_gate_applies_unscoped(self):
        result = self._pii("salesforce", "salesforce_run_report")
        assert result["pii_gate"] == "applies"
        assert result["pii_gate_detail"] == (
            "this call is not made with a scoped token, so the personal-data scan always applies"
        )
        assert result["pii_gate_may_apply"] is True

    def test_pii_gate_applies_when_allowance_does_not_cover_the_tool(self):
        result = self._pii("salesforce", "salesforce_run_report", self._token(allow_personal_data=True))
        assert result["pii_gate"] == "applies"
        assert result["pii_gate_detail"] == (
            "the token's personal-data allowance does not cover salesforce_run_report, "
            "so the personal-data scan applies"
        )

    def test_pii_gate_allowed_by_token(self):
        result = self._pii("salesforce", "salesforce_export_report", self._token(allow_personal_data=True))
        assert (result["pii_gate"], result["pii_gate_detail"]) == ("allowed_by_token", "")

    def test_pii_gate_applies_when_token_lacks_the_flag(self):
        result = self._pii("salesforce", "salesforce_export_report", self._token(allow_personal_data=False))
        assert result["pii_gate"] == "applies"
        assert result["pii_gate_detail"] == (
            "the token does not allow personal data, so the personal-data scan applies to salesforce_export_report"
        )

    def test_pii_gate_applies_for_revoked_scope(self):
        result = self._pii("salesforce", "salesforce_export_report", scoped_tokens.REVOKED_SCOPE)
        assert result["pii_gate"] == "applies"
        assert "not made with a scoped token" in result["pii_gate_detail"]

    def test_pii_gate_allowed_unless_inline_for_drive_download(self):
        result = self._pii("drive", "drive_download_file", self._token(allow_personal_data=True))
        assert (result["pii_gate"], result["pii_gate_detail"]) == ("allowed_unless_inline", "")

    def test_pii_gate_popup_upload_tools(self):
        token = self._token(allow_personal_data=True)
        peek = self._pii("peek", "peek_upload_page", token)
        assert peek["gate"] == "popup"
        assert (peek["pii_gate"], peek["pii_gate_detail"]) == ("allowed_by_token", "")
        assert peek["pii_gate_may_apply"] is False
        upload = self._pii("drive", "drive_upload_file", token)
        assert upload["pii_gate"] == "applies"
        assert "does not cover drive_upload_file" in upload["pii_gate_detail"]

    @staticmethod
    def _reads_token(*, reads: bool, exports: bool = False) -> ScopedToken:
        return dataclasses.replace(
            TestCheckPolicy._token(allow_personal_data=exports), allow_personal_data_reads=reads,
        )

    def test_pii_gate_is_allowed_by_token_for_a_read_with_the_reads_flag(self):
        result = self._pii("gmail", "gmail_get_message", self._reads_token(reads=True))
        assert (result["pii_gate"], result["pii_gate_detail"]) == ("allowed_by_token", "")

    def test_pii_gate_applies_for_a_read_without_the_reads_flag(self):
        result = self._pii("slack", "slack_search_messages", self._reads_token(reads=False))
        assert result["pii_gate"] == "applies"
        assert result["pii_gate_detail"] == (
            "the token does not allow personal data in reads, "
            "so the personal-data scan applies to slack_search_messages"
        )

    def test_pii_gate_for_a_read_ignores_the_exports_flag(self):
        result = self._pii("gmail", "gmail_get_thread", self._reads_token(reads=False, exports=True))
        assert result["pii_gate"] == "applies"
        assert "in reads" in result["pii_gate_detail"]

    def test_pii_scan_tool_sets_stay_in_step_with_the_gate_table(self):
        from privacyfence.auto_accept import (
            PERSONAL_DATA_READ_TOOLS, PERSONAL_DATA_RULE_TOOLS, TOOL_TO_GATE, UPLOAD_PII_SCAN_TOOLS,
        )

        for tool in PERSONAL_DATA_READ_TOOLS:
            assert TOOL_TO_GATE[tool] == "review", tool
        for tool in PERSONAL_DATA_RULE_TOOLS | UPLOAD_PII_SCAN_TOOLS:
            assert TOOL_TO_GATE[tool] in ("review", "popup"), tool
        for tool in UPLOAD_PII_SCAN_TOOLS:
            assert TOOL_TO_GATE[tool] == "popup", tool

    def test_covered_call_has_no_suggested_rules(self):
        from privacyfence import auto_accept

        auto_accept.set_policy_v2_store_rules(
            policy_rules({"salesforce.run_report": [{"predicate": "approved_report_ids", "value": ["00O1"]}]})
        )
        dispatcher = _dispatcher({"salesforce": FakeConnector("salesforce")})
        result = dispatcher.check_policy("salesforce", "salesforce_export_report", {"report_id": "00O1"})
        assert result["verdict"] == "auto_accept"
        assert result["suggested_rules"] == []

    def test_uncovered_call_names_the_rule_that_would_cover_it(self):
        dispatcher = _dispatcher({"salesforce": FakeConnector("salesforce")})
        result = dispatcher.check_policy("salesforce", "salesforce_export_report", {"report_id": "00O1"})
        assert result["verdict"] == "requires_review"
        assert result["suggested_rules"]
        assert result["suggested_rules"][0]["value_known"] is True
        assert result["suggested_rules"][0]["value"] == ["00O1"]

    def test_grace_window_match_still_names_the_rule_it_would_take(self, monkeypatch):
        monkeypatch.setattr(
            mcp_dispatch, "preflight_auto_accept",
            lambda *_a, **_k: ("auto_accept", "session_temp_accept", "", "grace window"),
        )
        dispatcher = _dispatcher({"salesforce": FakeConnector("salesforce")})
        result = dispatcher.check_policy("salesforce", "salesforce_export_report", {"report_id": "00O1"})
        assert result["matched_rule"] == "session_temp_accept"
        assert result["suggested_rules"][0]["value"] == ["00O1"]

    def test_never_calls_the_connector(self):
        connector = FakeConnector("gmail")
        dispatcher = _dispatcher({"gmail": connector})
        dispatcher.check_policy("gmail", "gmail_get_message", {})
        assert connector.calls == []

    def test_slack_suggestions_never_call_the_connector(self):
        connector = FakeConnector("slack")
        dispatcher = _dispatcher({"slack": connector})
        result = dispatcher.check_policy(
            "slack", "slack_get_thread_replies", {"channel_id": "D1", "thread_ts": "1.2"},
        )
        assert connector.calls == []
        assert "slack.direct_messages_to_me" in [s["group"] for s in result["suggested_rules"]]

    def test_slack_membership_suggestion_ignores_an_agent_supplied_is_member(self):
        dispatcher = _dispatcher({"slack": FakeConnector("slack")})
        result = dispatcher.check_policy(
            "slack", "slack_get_channel_history", {"channel_id": "", "is_member": True},
        )
        assert "slack.my_conversations" not in [s["group"] for s in result["suggested_rules"]]

    def test_popup_tool_matching_args_only_rule_is_auto_accept(self):
        from privacyfence import auto_accept

        rules = policy_rules({"gmail.create_draft": [{"predicate": "to_is_myself"}]})
        auto_accept.set_policy_v2_store_rules(rules)
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail", my_email="me@example.com")})
        result = dispatcher.check_policy(
            "gmail", "gmail_create_draft", {"to": "me@example.com", "subject": "x", "body": "y"},
        )
        assert result["gate"] == "popup"
        assert result["verdict"] == "auto_accept"
        assert result["matched_rule"] == rules[0].id
        assert result["matched_rule_id"] == rules[0].id

    def test_agent_supplied_self_dm_flag_cannot_predict_an_auto_accept(self):
        from privacyfence import auto_accept

        auto_accept.set_policy_v2_store_rules(policy_rules({"slack.send_message": [{"predicate": "send_to_myself"}]}))
        dispatcher = _dispatcher({"slack": FakeConnector("slack")})
        result = dispatcher.check_policy(
            "slack", "slack_send_message", {"channel_id": "D0OTHER", "text": "hi", "is_self_dm": True},
        )
        assert result["verdict"] == "requires_review"

    def test_check_policy_under_a_token_predicts_from_the_token_rows(self):
        from privacyfence import auto_accept
        from privacyfence.policy import scope_rows

        (rule,) = policy_rules({"gmail.create_draft": [{"predicate": "to_is_myself"}]})
        auto_accept.set_policy_v2_store_rules([])
        token = dataclasses.replace(_SCOPE, rules=tuple(scope_rows.rows_for_rules([rule], rule.id)))
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail", my_email="me@example.com")})
        args = {"to": "me@example.com", "subject": "x", "body": "y"}

        with scoped_tokens.token_scope(token):
            result = dispatcher.check_policy("gmail", "gmail_create_draft", args)

        assert (result["verdict"], result["matched_rule_id"]) == ("auto_accept", rule.id)
        # Not vacuous: no rule of the person's covers the call.
        assert dispatcher.check_policy("gmail", "gmail_create_draft", args)["verdict"] == "requires_review"

    def test_matched_rule_id_is_none_when_no_rule_configured_at_all(self):
        dispatcher = _dispatcher({"gmail": FakeConnector("gmail")})
        result = dispatcher.check_policy("gmail", "gmail_get_message", {})
        assert result["verdict"] == "requires_review"
        assert result["matched_rule_id"] is None

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
# list_policy / propose_policy_change -- the sole meta-tools for
# reading/writing auto-accept policy (ADR 0004), with a "list before you
# propose" contract.
# --------------------------------------------------------------------------- #

class TestListPolicy:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from privacyfence import auto_accept
        init_audit_logger(str(tmp_path / "audit"))
        self._audit_dir = tmp_path / "audit"
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(config_path))

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def test_empty_store_lists_no_rules_but_a_real_scope_catalogue(self):
        result = _dispatcher({}).list_policy()
        assert result["rules"] == []
        assert any(group["id"] == "drive.folder" for group in result["scope_groups"])

    async def test_a_v2_rule_is_listed_with_its_id_sentence_and_covered_tools(self, monkeypatch):
        from privacyfence import gate
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read"],
        )

        result = _dispatcher({}).list_policy()
        assert len(result["rules"]) == 1
        row = result["rules"][0]
        assert row["id"]
        assert "folder1" in row["sentence"]
        assert "drive_get_file_content" in row["covered_tools"]

    async def test_a_listed_row_carries_the_scope_row_keys(self, monkeypatch):
        from privacyfence import gate
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)
        await gate.propose_policy_change(
            operation="add", reason="x", group="drive.folder", value=["folder1"], verbs=["read", "download"],
        )

        (row,) = _dispatcher({}).list_policy()["rules"]
        assert set(row) == {
            "id", "sentence", "connector", "scope", "scope_type", "value", "operations", "verbs",
            "conditions", "covered_tools",
        }
        assert row["scope"] == "drive.folder"
        assert row["scope_type"] == "drive.folder"
        assert [v["verb"] for v in row["verbs"]] == ["read", "download"]
        assert row["conditions"] == []

    def test_records_a_policy_listed_audit_entry(self):
        _dispatcher({}).list_policy("checking before a scheduled run")
        entries = self._read_entries()
        assert entries[0]["decision"] == "policy_listed"
        assert entries[0]["claude_reason"] == "checking before a scheduled run"


class TestProposePolicyChangeDispatch:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        from privacyfence import auto_accept, gate
        init_audit_logger(str(tmp_path / "audit"))
        self._config_path = tmp_path / "settings.yaml"
        self._config_path.write_text("auto_accept: {}\n", encoding="utf-8")
        auto_accept.init_config_path(str(self._config_path))
        monkeypatch.setattr(gate, "show_rule_confirmation_popup", lambda description, *, sensitive=False: True)

    async def test_confirmed_add_is_persisted_to_disk(self):
        dispatcher = _dispatcher({})
        result = await dispatcher.propose_policy_change("s1", {
            "operation": "add", "reason": "Trusting the sandbox folder.",
            "group": "drive.folder", "value": ["folder1"], "verbs": ["read"],
        })
        assert result["confirmed"] is True
        assert "scope: drive.folder" in self._config_path.read_text(encoding="utf-8")

    async def test_denied_immediately_when_this_sessions_unattended_flag_is_set(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "scheduled run")

        with pytest.raises(RuntimeError, match="unattended session"):
            await dispatcher.propose_policy_change("s1", {
                "operation": "add", "reason": "x", "group": "drive.folder",
                "value": ["folder1"], "verbs": ["read"],
            })


# --------------------------------------------------------------------------- #
# status -- privacyfence_status's handler. No meta-tool mints a sign-in
# link (ADR 0013), so there is none to test beside it.
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
        assert result["next_step"] == "open_privacyfence_companion"

    def test_setup_complete_when_at_least_one_connector_is_authenticated(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [
            self._row("gmail", authenticated=True), self._row("slack", blocked_by="not_authenticated"),
        ])
        result = dispatcher.status("checking")
        assert result["setup_complete"] is True
        assert result["next_step"] is None
        assert "sign_in_url" not in result

    def test_never_hands_a_sign_in_url_to_the_caller(self):
        """This tool must not mint a live sign-in credential, since it is
        called because a model decided to check rather than because a human
        asked -- and nothing on this server mints one (ADR 0013), so the
        field stays null and the message names the companion."""
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        result = dispatcher.status("checking")
        assert result["sign_in_url"] is None
        assert result["next_step"] == "open_privacyfence_companion"
        assert "companion" in result["message"]
        assert "no link for you to hand them" in result["message"]

    def test_org_mode_points_at_an_administrator_instead(self):
        dispatcher = _dispatcher({}, mode="org")
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        result = dispatcher.status("checking")
        assert result["sign_in_url"] is None
        assert result["next_step"] == "contact_your_administrator"

    def test_audits_status_checked_when_un_onboarded(self):
        dispatcher = _dispatcher({})
        dispatcher.set_connectors_state_provider(lambda: [self._row("gmail")])
        dispatcher.status("checking")
        entries = self._read_entries()
        assert [e["decision"] for e in entries] == ["status_checked"]
        assert entries[0]["claude_reason"] == "checking"

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
# notify_tools_changed -- the actual notification send is
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
    """privacyfence_await_approval's handler: long-poll the registry, status only."""

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
        # The same cross-principal check every other approval
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


class TestAwaitApprovalDenialFeedback:
    """ADR 0083: every approval_id value stays a bare status string; a denied
    id with feedback also appears under the one reserved denial_feedback key."""

    FB = DenialFeedback("rewrite", "Make it shorter.")

    def _setup(self):
        registry = PendingApprovalRegistry(hold_window=5.0, pending_ttl=5.0, ledger_ttl=5.0)
        return registry, _dispatcher({}, registry=registry)

    def _card(self, registry, key):
        approval, _ = registry.register_or_coalesce(
            dedupe_key=key, connector="gmail", tool="gmail_create_draft", gate_kind="popup", request_id=key,
        )
        return approval

    def _deny(self, registry, approval, feedback):
        registry.answer(approval.id, "deny", feedback=feedback)
        registry.finalize(approval.id, "deny")

    async def test_denied_with_feedback_adds_the_reserved_key(self):
        registry, dispatcher = self._setup()
        denied = self._card(registry, "k1")
        approved = self._card(registry, "k2")
        self._deny(registry, denied, self.FB)
        registry.finalize(approved.id, "accept")

        result = await dispatcher.await_approval([denied.id, approved.id], timeout_seconds=1)

        assert result == {
            denied.id: "denied",
            approved.id: "approved",
            "denial_feedback": {
                denied.id: {
                    "intent": "rewrite", "note": "Make it shorter.",
                    "guidance": "The user wants the content changed. Revise it, then ask again.",
                },
            },
        }
        json.dumps(result)  # plain JSON all the way down

    async def test_plain_deny_has_no_reserved_key(self):
        registry, dispatcher = self._setup()
        denied = self._card(registry, "k1")
        self._deny(registry, denied, DenialFeedback())
        result = await dispatcher.await_approval([denied.id], timeout_seconds=1)
        assert result == {denied.id: "denied"}

    async def test_only_denied_ids_with_feedback_are_listed(self):
        registry, dispatcher = self._setup()
        noted, plain, pending = (self._card(registry, k) for k in ("k1", "k2", "k3"))
        self._deny(registry, noted, DenialFeedback(intent="stop"))
        self._deny(registry, plain, DenialFeedback())

        result = await dispatcher.await_approval([noted.id, plain.id, pending.id], timeout_seconds=1)

        assert result["denial_feedback"] == {
            noted.id: {"intent": "stop", "note": None, "guidance": INTENTS["stop"][1]},
        }
        assert result[pending.id] == "pending"
        assert all(isinstance(v, str) for k, v in result.items() if k != "denial_feedback")

    async def test_foreign_principal_sees_neither_status_nor_feedback(self):
        registry, dispatcher = self._setup()
        with principal_scope(Principal(id="alice")):
            denied = self._card(registry, "k1")
        self._deny(registry, denied, self.FB)

        with principal_scope(Principal(id="bob")):
            result = await dispatcher.await_approval([denied.id], timeout_seconds=1)

        assert result == {denied.id: "unknown"}

    async def test_owner_sees_their_own_feedback_in_org_mode(self):
        registry, dispatcher = self._setup()
        with principal_scope(Principal(id="alice")):
            denied = self._card(registry, "k1")
        self._deny(registry, denied, self.FB)

        with principal_scope(Principal(id="alice")):
            result = await dispatcher.await_approval([denied.id], timeout_seconds=1)

        assert result["denial_feedback"][denied.id]["note"] == "Make it shorter."

    async def test_approval_ids_are_uuid4_hex_and_cannot_be_the_reserved_key(self):
        import uuid

        registry, _ = self._setup()
        approval = self._card(registry, "k1")
        assert uuid.UUID(hex=approval.id).version == 4
        assert uuid.UUID(hex=approval.id).hex == approval.id
        assert approval.id != DENIAL_FEEDBACK_KEY
        assert len(DENIAL_FEEDBACK_KEY) != 32  # a uuid4().hex is always 32 hex characters

    def test_tool_description_mentions_denial_feedback(self):
        from privacyfence.web.mcp_tools import AWAIT_APPROVAL_TOOL

        description = AWAIT_APPROVAL_TOOL.description
        assert (
            "'denied' (a human said no -- re-issuing will not change that; don't retry, ask the user "
            "how to proceed unless denial_feedback says otherwise)"
        ) in description
        assert "denial_feedback holds the user's instruction for a denial: follow it." in description


# --------------------------------------------------------------------------- #
# privacyfence_propose_scoped_token + the session ledger (ADR 0251)
# --------------------------------------------------------------------------- #

_LOCAL_POLICY = scoped_tokens.policy_from_org_config({}, org_mode=False)
_RULE = {"kind": "new", "group": "gmail.label", "value": "Receipts", "verbs": ["update"]}
_SUGGESTION = {
    "group": "gmail.label", "label": "Gmail -- label", "value": ["Receipts"], "value_known": True,
    "verb": "update", "operation": "gmail.update_label",
}


class ReleasingConnector(FakeConnector):
    """Behaves like a connector whose gate approved the call: records a released request id."""

    async def call(self, tool: str, args: dict) -> object:
        set_released_request_id("req-1")
        return await super().call(tool, args)


class TestProposeScopedToken:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        from privacyfence import auto_accept, paths
        from privacyfence.policy import suggest

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(auto_accept, "get_policy_rows", lambda: [])
        monkeypatch.setattr(mcp_dispatch, "get_policy_rows", lambda: [])
        monkeypatch.setattr(suggest, "suggested_rules", lambda tool, args, my_email: [dict(_SUGGESTION)])
        init_audit_logger(str(tmp_path / "audit"))
        self._audit_dir = tmp_path / "audit"
        self.store = scoped_tokens.ScopedTokenStore(lambda: [Principal(id="local")])
        scoped_tokens.set_store(self.store)

    def _entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    def _dispatcher(self, connector=None, policy=_LOCAL_POLICY, **kwargs):
        return _dispatcher(
            {"gmail": connector or FakeConnector("gmail")}, scoped_token_policy=policy, **kwargs,
        )

    @staticmethod
    def _params(**overrides):
        params = {"reason": "nightly job", "label": "nightly", "rules": [dict(_RULE)], "tools": ["gmail_get_message"]}
        params.update(overrides)
        return params

    async def _propose(self, dispatcher, params=None, session="s1"):
        return await dispatcher.propose_scoped_token(session, params or self._params(), "http://127.0.0.1:9/")

    @staticmethod
    def _pending():
        from privacyfence import scoped_token_proposals

        return scoped_token_proposals.get_registry().pending_for("local")

    async def test_bundle_proposal_returns_a_review_url_and_no_secret(self):
        result = await self._propose(self._dispatcher())

        assert result["review_url"] == f"http://127.0.0.1:9/settings/scoped_tokens/proposals/{result['proposal_id']}"
        assert result["label"] == "nightly" and result["days"] == scoped_tokens.DEFAULT_DAYS
        assert result["tools"] == ["gmail_get_message"]
        assert len(result["rules"]) == 1 and result["uncovered_tools"] == []
        assert "pfst_" not in json.dumps(result)
        assert self.store.tokens_for(Principal(id="local")) == []

    async def test_proposal_is_registered_and_audited(self):
        result = await self._propose(self._dispatcher())

        [proposal] = self._pending()
        assert proposal.id == result["proposal_id"] and proposal.source == "bundle"
        [entry] = [e for e in self._entries() if e["decision"] == "scoped_token_proposed"]
        assert entry["tool"] == "privacyfence_propose_scoped_token"
        assert entry["claude_reason"] == "nightly job"
        assert entry["request_id"] == proposal.id

    async def test_missing_tools_or_rules_default_to_empty(self):
        result = await self._propose(self._dispatcher(), {"reason": "r", "label": "tools-only", "tools": ["gmail_get_message"]})
        assert result["rules"] == []
        result = await self._propose(self._dispatcher(), {"reason": "r", "label": "rules-only", "rules": [dict(_RULE)]})
        assert result["tools"] == []

    async def test_refused_when_proposals_disabled(self):
        for policy in (None, scoped_tokens.ScopedTokenPolicy(True, 90, False), scoped_tokens.ScopedTokenPolicy(False, 90, True)):
            dispatcher = self._dispatcher(policy=policy)
            assert dispatcher.scoped_token_proposals_enabled() is False
            with pytest.raises(ValueError, match="Token proposals are not enabled"):
                await self._propose(dispatcher)
        assert self._pending() == []

    async def test_refused_for_a_local_non_owner(self):
        with principal_scope(Principal(id="alice")), pytest.raises(ValueError, match="this computer's owner"):
            await self._propose(self._dispatcher())

    async def test_refused_in_an_unattended_session_and_audited(self):
        dispatcher = self._dispatcher(unattended_sessions_enabled=True)
        dispatcher.begin_unattended_session("s1", "scheduled")

        with pytest.raises(GateDeniedError, match="unattended session"):
            await self._propose(dispatcher)

        assert self._pending() == []
        [entry] = [e for e in self._entries() if e["decision"] == "denied_unattended"]
        assert entry["tool"] == "privacyfence_propose_scoped_token"
        assert entry["claude_reason"] == "nightly job"

    async def test_refused_under_a_scoped_token(self):
        with scoped_tokens.token_scope(_SCOPE), pytest.raises(GateDeniedError, match="cannot propose tokens"):
            await self._propose(self._dispatcher())
        assert self._pending() == []

    async def test_duplicate_rule_kind_is_refused(self):
        rules = [{"kind": "duplicate", "token_id": "st-1", "rule_id": "r-1"}]
        with pytest.raises(ValueError, match="A proposed rule is"):
            await self._propose(self._dispatcher(), self._params(rules=rules))
        assert self._pending() == []

    async def test_from_session_and_rules_together_are_refused(self):
        with pytest.raises(ValueError, match="either from_session or rules and tools"):
            await self._propose(self._dispatcher(), self._params(from_session=True))

    async def test_days_are_capped_at_the_policy_max(self):
        dispatcher = self._dispatcher(policy=scoped_tokens.ScopedTokenPolicy(True, 30, True))
        result = await self._propose(dispatcher, self._params(days=365))
        assert result["days"] == 30

    async def test_invalid_request_errors_before_registering(self):
        with pytest.raises(scoped_tokens.ScopedTokenError):
            await self._propose(self._dispatcher(), self._params(label="not a valid label!!"))
        assert self._pending() == []

    async def test_reproposing_an_existing_label_is_not_refused(self):
        self.store.create(
            Principal(id="local"), label="nightly", days=30, rules=[dict(_RULE)], tools=["gmail_get_message"],
            policy=_LOCAL_POLICY,
        )
        result = await self._propose(self._dispatcher())
        assert result["label"] == "nightly"


    def _ledger_dispatcher(self):
        class Pending(FakeConnector):
            async def call(self, tool, args):
                raise ApprovalPending({"status": "approval_pending", "approval_id": "a1"})

        return _dispatcher(
            {
                "ok": ReleasingConnector("ok", result="data"),
                "denied": FakeConnector("denied", error=GateDeniedError("Request denied")),
                "pending": Pending("pending"),
                "auto": FakeConnector("auto", result="listed"),
            },
            scoped_token_policy=_LOCAL_POLICY,
        )

    async def test_from_session_builds_from_approved_calls_only(self):
        dispatcher = self._ledger_dispatcher()
        await dispatcher.call("s1", "ok", "gmail_get_message", {"message_id": "m1"})
        with pytest.raises(GateDeniedError):
            await dispatcher.call("s1", "denied", "drive_read_file", {"file_id": "f1"})
        await dispatcher.call("s1", "pending", "calendar_get_event", {"event_id": "e1"})

        result = await self._propose(dispatcher, {"reason": "r", "label": "from-calls", "from_session": True})

        assert result["tools"] == ["gmail_get_message"]
        assert len(result["rules"]) == 1
        [proposal] = self._pending()
        assert proposal.source == "session"
        assert [r["group"] for r in proposal.request["rules"]] == ["gmail.label"]

    async def test_from_session_includes_auto_tools(self):
        dispatcher = self._ledger_dispatcher()
        await dispatcher.call("s1", "auto", "gmail_list_messages", {"query": "x"})

        result = await self._propose(dispatcher, {"reason": "r", "label": "listing", "from_session": True})

        assert result["tools"] == ["gmail_list_messages"] and result["rules"] == []

    async def test_from_session_with_no_calls_is_refused(self):
        with pytest.raises(ValueError, match="no approved calls"):
            await self._propose(self._ledger_dispatcher(), {"reason": "r", "label": "empty", "from_session": True})

    async def test_ledger_drops_computed_args(self):
        dispatcher = self._ledger_dispatcher()
        await dispatcher.call("s1", "ok", "gmail_get_message", {"message_id": "m1", "is_self_dm": True})

        assert dispatcher._session_calls["s1"] == [("gmail_get_message", {"message_id": "m1"}, "")]

    async def test_session_ledger_is_dropped_on_end_session(self):
        dispatcher = self._ledger_dispatcher()
        await dispatcher.call("s1", "ok", "gmail_get_message", {"message_id": "m1"})
        dispatcher.end_session("s1")

        assert "s1" not in dispatcher._session_calls

    async def test_session_ledger_keeps_the_newest_200(self):
        dispatcher = self._ledger_dispatcher()
        for i in range(205):
            await dispatcher.call("s1", "ok", "gmail_get_message", {"message_id": f"m{i}"})

        calls = dispatcher._session_calls["s1"]
        assert len(calls) == 200
        assert calls[0][1] == {"message_id": "m5"} and calls[-1][1] == {"message_id": "m204"}

    async def test_scoped_token_calls_are_not_recorded(self):
        dispatcher = self._ledger_dispatcher()
        with scoped_tokens.token_scope(_SCOPE):
            await dispatcher.call("s1", "ok", "gmail_get_message", {"message_id": "m1"})
            await dispatcher.call("s1", "auto", "gmail_list_messages", {"query": "x"})

        assert dispatcher._session_calls == {}
