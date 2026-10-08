"""The SDK's ``PluginTestHost`` and the real daemon must agree (ADR 0126).

Plugin authors test against ``PluginTestHost`` and ship against PrivacyFence, so a scenario that
passes on one must pass on the other. Each test runs one scenario twice, once on the SDK test host
with the echo plugin imported in-process and once on the real daemon (host, gate, MCP and web
server) with the same plugin as a child process, and compares the outcomes field by field.

Known differences, which these tests deliberately do not compare (and do not hide):

- The daemon's ``web.request`` timeout is 10 s; the test host's is 30 s.
- A 405 body reads "Method Not Allowed" on the daemon and "Method not allowed." in the SDK.
- Daemon responses also carry ``Permissions-Policy`` and ``Cross-Origin-Opener-Policy``.
- A call accepted by an "Always allow" rule reports ``approval.via == "rule"`` on the test host and
  ``"card"`` on the daemon, which does not tell the two apart in protocol 1.
- Revoking an approval adds an ``echo-template; revoked`` row to the audit log on the daemon and none
  on the test host, so the approval audit comparisons leave that row out.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from privacyfence import auto_accept
from privacyfence.plugins import storage
from tests.fixtures.plugins.echo.harness import (  # noqa: I001  (puts the SDK sources on sys.path)
    Stack,
    install_echo,
    load_echo_plugin,
    calendar_event,
    mcp_session,
    until,
    web_session,
)
from privacyfence_plugin_sdk.responses import ToolDefinitionError  # noqa: E402
from privacyfence_plugin_sdk.testing import PluginTestHost, samples  # noqa: E402
from tests.helpers import policy_rules

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

SANDBOX_HEADERS = (
    "content-security-policy", "x-content-type-options", "referrer-policy", "cache-control", "x-frame-options",
)
# The daemon names a denied card "rejected"; the test host says "denied".
DECISION_NAMES = {"rejected": "denied"}


@dataclass
class Outcome:
    """What a scenario observed, in terms both sides can state."""

    card_shown: bool = False
    released: Any = None
    errored: bool = False
    scopes: dict | None = None
    decisions: list[str] = field(default_factory=list)


def assert_same(sdk: Any, daemon: Any) -> None:
    if isinstance(sdk, Outcome):
        for name in vars(sdk):
            assert getattr(sdk, name) == getattr(daemon, name), (
                f"{name}: test host {getattr(sdk, name)!r} != daemon {getattr(daemon, name)!r}"
            )
    else:
        assert sdk == daemon


class SdkSide:
    """The echo plugin on the SDK test host."""

    def __init__(self, host: PluginTestHost) -> None:
        self.host = host

    async def call(self, tool: str, args: dict | None = None, decide: str = "approve") -> Outcome:
        outcome = await self.host.call_tool(tool, args or {}, decide=decide)
        return Outcome(
            card_shown=outcome.card_shown,
            released=outcome.released,
            errored=outcome.error is not None,
            scopes=outcome.card.scopes if outcome.card_shown else None,
            decisions=[a["decision"] for a in outcome.audit if a["connector"] == "echo"],
        )

    def allow_scope(self, scope: str, values: list[str]) -> None:
        self.host.rules.allow_scope(scope, values)

    async def page(self, method: str, path: str) -> dict:
        response = await self.host.request(method, path)
        return {"status": response.status, "headers": response.headers, "body": response.body}

    async def download(self, data: bytes, revision: str) -> tuple[dict, int]:
        self.host.source.load(samples.drive_download(data, revision=revision, file_id="FILE-1"))
        response = await self.host.get("/download?file_id=FILE-1")
        return json.loads(response.text), len(self.host.source.calls)


    async def paging(self, first: list[dict], second: list[dict]) -> tuple[dict, int]:
        self.host.source.when("calendar.list_events", **CALENDAR_WINDOW).returns_pages([first, second])
        response = await self.host.get("/pages?op=calendar.list_events")
        return json.loads(response.text), len(self.host.source.calls)

    async def request_approval(self, text: str, note: str = "") -> dict:
        outcome = await self.host.call_tool("approve", {"text": text, "note": note}, decide="approve")
        assert outcome.error is None, outcome.error
        return outcome.released

    async def decide_approval(self, approval_id: str, decision: str) -> None:
        await self.host.decide_approval(approval_id, decision)

    async def revoke_approval(self, approval_id: str) -> None:
        await self.host.revoke_approval(approval_id)

    async def check_approval(self, text: str) -> str:
        return json.loads((await self.host.get("/approval-check", query={"text": text})).text)["status"]

    async def revoked_events(self) -> list[dict]:
        return recorded(json.loads((await self.host.get("/events")).text), "approval.revoked")

    def approval_audit(self) -> list[str]:
        return [
            e["summary"] for e in self.host.audit
            if e["decision"] == "plugin_approval" and not e["summary"].endswith("; revoked")
        ]


class DaemonSide:
    """The echo plugin as a child process of the real daemon."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack

    async def call(self, tool: str, args: dict | None = None, decide: str = "approve") -> Outcome:
        stack = self.stack
        stack.popups.decision = "accept" if decide == "approve" else "deny"
        shown_before = len(stack.popups.read) + len(stack.popups.write)
        mark = len(stack.audit())
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call(f"echo_{tool}", **(args or {}))
        cards = [*stack.popups.read, *stack.popups.write]
        shown = len(cards) > shown_before
        # The gate's popups receive the call's raw data as JSON; its scopes are the card's scopes.
        scopes = json.loads(cards[-1][0][2])["scopes"] if shown else None
        return Outcome(
            card_shown=shown,
            released=None if result.is_error else result.structured_content,
            errored=bool(result.is_error),
            scopes=scopes,
            decisions=[
                DECISION_NAMES.get(e["decision"], e["decision"])
                for e in stack.audit()[mark:] if e["connector"] == "echo"
            ],
        )

    def allow_scope(self, scope: str, values: list[str]) -> None:
        auto_accept.add_policy_v2_rules(policy_rules({
            "plugin.echo.review_read": [{"predicate": f"plugin:echo:{scope}", "value": v} for v in values],
        }))

    async def page(self, method: str, path: str) -> dict:
        client = await web_session(self.stack.server)
        try:
            response = await client.request(method, f"/plugins/echo{path}")
        finally:
            await client.aclose()
        return {"status": response.status_code, "headers": dict(response.headers), "body": response.content}

    async def download(self, data: bytes, revision: str) -> tuple[dict, int]:
        self.stack.drive.data = data
        self.stack.drive.revision = revision
        before = len([e for e in self.stack.audit() if e["decision"] == "plugin_source"])
        page = await self.stack.page("/download", file_id="FILE-1")
        calls = len([e for e in self.stack.audit() if e["decision"] == "plugin_source"]) - before
        return json.loads(page["body"]), calls


    async def paging(self, first: list[dict], second: list[dict]) -> tuple[dict, int]:
        pages = {None: (first, "page-2"), "page-2": (second, None)}
        self.stack.calendar.list_events.side_effect = (
            lambda calendar_id, page_size, time_min, time_max, token=None: (
                [calendar_event(e["title"], id=e["id"]) for e in pages[token][0]], pages[token][1]
            )
        )
        before = len([e for e in self.stack.audit() if e["decision"] == "plugin_source"])
        page = await self.stack.page("/pages", op="calendar.list_events")
        calls = len([e for e in self.stack.audit() if e["decision"] == "plugin_source"]) - before
        return json.loads(page["body"]), calls

    async def request_approval(self, text: str, note: str = "") -> dict:
        self.stack.popups.decision = "accept"
        async with mcp_session(self.stack.server) as mcp:
            result = await mcp.call("echo_approve", text=text, note=note)
        assert result.is_error is False, result
        return result.structured_content

    async def decide_approval(self, approval_id: str, decision: str) -> None:
        mark = len(self._approval_audit())
        assert self.stack.registry.answer(approval_id, DECIDE_ANSWER[decision])
        await until(lambda: len(self._approval_audit()) > mark)

    async def revoke_approval(self, approval_id: str) -> None:
        await self.stack.run(self.stack.host.revoke_approval("echo", approval_id))

    async def check_approval(self, text: str) -> str:
        return json.loads((await self.stack.page("/approval-check", text=text))["body"])["status"]

    async def revoked_events(self) -> list[dict]:
        await until(lambda: self._events() and any(e["event"] == "approval.revoked" for e in self._events()))
        return recorded(self._events(), "approval.revoked")

    def _events(self) -> list[dict]:
        path = storage.install_dir("echo") / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _approval_audit(self) -> list[str]:
        return [e["summary"] for e in self.stack.audit() if e["decision"] == "plugin_approval"]

    def approval_audit(self) -> list[str]:
        return [summary for summary in self._approval_audit() if not summary.endswith("; revoked")]


CALENDAR_WINDOW = {"time_min": "2026-10-07T00:00:00Z", "time_max": "2026-10-08T00:00:00Z"}
DECIDE_ANSWER = {"approve": "confirm", "deny": "cancel"}


def recorded(events: list[dict], name: str) -> list[dict]:
    """The comparable part of each ``name`` event an echo plugin logged."""
    return [
        {key: e["params"][key] for key in ("kind", "subject_id", "digest")} for e in events if e["event"] == name
    ]


@pytest.fixture
async def sdk():
    async with PluginTestHost(load_echo_plugin().plugin, max_gate_floor="auto") as host:
        yield SdkSide(host)


@pytest.fixture
async def daemon(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch, serve=True)
    install_echo(stack.plugins, source_operations=["calendar.list_events", "drive.download"])
    await stack.start()
    await stack.enable()
    try:
        yield DaemonSide(stack)
    finally:
        await stack.stop()


async def both(sdk: SdkSide, daemon: DaemonSide, scenario) -> tuple[Any, Any]:
    return await scenario(sdk), await scenario(daemon)


class TestSameOutcomes:
    async def test_auto_read(self, sdk, daemon):
        async def scenario(side):
            return await side.call("auto_read", {"text": "hello"})

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk.card_shown is False and on_sdk.decisions == ["auto_accepted"]
        assert on_sdk.released == {"blocks": [{"type": "text", "text": "hello"}]}

    async def test_review_read(self, sdk, daemon):
        async def scenario(side):
            return await side.call("review_read", {"dataset": "alpha", "text": "approved"})

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk.card_shown is True and on_sdk.scopes == {"dataset": ["alpha"]}
        assert on_sdk.decisions == ["approved"]

    async def test_review_read_denied(self, sdk, daemon):
        async def scenario(side):
            return await side.call("review_read", {"dataset": "alpha"}, decide="deny")

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk.errored and on_sdk.released is None and on_sdk.decisions == ["denied"]

    async def test_popup_write(self, sdk, daemon):
        async def scenario(side):
            return await side.call("popup_write", {"text": "note"})

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk.card_shown is True and on_sdk.released == {"stored": 1, "via": "card"}

    async def test_scope_rule_match(self, sdk, daemon):
        async def scenario(side):
            side.allow_scope("dataset", ["alpha"])
            return await side.call("review_read", {"dataset": "alpha", "text": "by rule"})

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk.card_shown is False and on_sdk.decisions == ["auto_accepted"]

    @pytest.mark.parametrize("dataset", ["beta", "alpha,beta"])
    async def test_scope_rule_mismatch(self, sdk, daemon, dataset):
        async def scenario(side):
            side.allow_scope("dataset", ["alpha"])
            return await side.call("review_read", {"dataset": dataset})

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk.card_shown is True and on_sdk.decisions == ["approved"]

    async def test_block_validation(self, sdk, daemon):
        async def scenario(side):
            # The preview echoes the dataset; 70 000 characters is past the 64 KiB preview cap.
            return await side.call("review_read", {"dataset": "x" * 70_000})

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk.errored and on_sdk.card_shown is False and on_sdk.released is None


class TestSamePages:
    @pytest.mark.parametrize("method,path", [("GET", "/"), ("HEAD", "/"), ("POST", "/"), ("DELETE", "/events")])
    async def test_status_allow_and_sandbox_headers(self, sdk, daemon, method, path):
        async def scenario(side):
            response = await side.page(method, path)
            headers = {k.lower(): v for k, v in response["headers"].items()}
            return {
                "status": response["status"],
                "allow": headers.get("allow"),
                **{name: headers.get(name) for name in SANDBOX_HEADERS},
            }

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        if method in ("POST", "DELETE"):
            assert on_sdk["status"] == 405 and on_sdk["allow"] == "GET, HEAD"
        else:
            assert on_sdk["status"] == 200

    async def test_the_page_body(self, sdk, daemon):
        async def scenario(side):
            return (await side.page("GET", "/"))["body"]

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert b"document.cookie" in on_sdk

    async def test_a_path_that_climbs_out_never_reaches_the_plugin(self, sdk, daemon):
        async def scenario(side):
            response = await side.page("GET", "/%2e%2e/secret")
            return response["status"]

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        assert on_sdk == 400


class TestSameDownload:
    async def test_chunked_download_matches_the_daemons_spool(self, sdk, daemon):
        data = os.urandom(9 * 1024 * 1024)  # more than one 8 MiB chunk

        async def scenario(side):
            return await side.download(data, "r1")

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        result, calls = on_sdk
        assert result["size"] == len(data) and result["sha256"] == hashlib.sha256(data).hexdigest()
        assert result["revision"] == "r1" and calls == 2


class TestSamePaging:
    async def test_two_provider_pages_give_the_same_items_and_calls(self, sdk, daemon):
        first = [{"id": f"a{n}", "title": f"A{n}"} for n in range(3)]
        second = [{"id": f"b{n}", "title": f"B{n}"} for n in range(2)]

        async def scenario(side):
            return await side.paging(first, second)

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        result, calls = on_sdk
        assert result == {"pages": 2, "items": 5, "ids": ["a0", "a1", "a2", "b0", "b1"]}
        assert calls == 2


class TestSameApprovals:
    async def test_request_decide_and_check_statuses_match(self, sdk, daemon):
        async def scenario(side):
            seen = [await side.check_approval("v1")]
            ticket = await side.request_approval("v1")
            seen += [ticket["status"], await side.check_approval("v1")]
            await side.decide_approval(ticket["approval_id"], "approve")
            seen += [await side.check_approval("v1")]
            again = await side.request_approval("v1", note="again")
            seen += [again["status"], again["approval_id"] == ticket["approval_id"]]
            changed = await side.request_approval("v2")
            seen += [changed["status"], await side.check_approval("v2")]
            await side.decide_approval(changed["approval_id"], "deny")
            seen += [await side.check_approval("v2")]
            return seen, side.approval_audit()

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        statuses, audit = on_sdk
        assert statuses == ["unknown", "pending", "unknown", "approved", "approved", True, "pending", "unknown", "unknown"]
        assert audit == [
            "echo-template; requested", "echo-template; approved",
            "echo-template; requested", "echo-template; denied",
        ]

    async def test_revoking_matches(self, sdk, daemon):
        async def scenario(side):
            ticket = await side.request_approval("v1")
            await side.decide_approval(ticket["approval_id"], "approve")
            await side.revoke_approval(ticket["approval_id"])
            renewed = await side.request_approval("v1", note="after revoke")
            return (
                await side.check_approval("v1"), renewed["status"], renewed["approval_id"] != ticket["approval_id"],
                await side.revoked_events(), side.approval_audit(),
            )

        on_sdk, on_daemon = await both(sdk, daemon, scenario)

        assert_same(on_sdk, on_daemon)
        check, status, is_new, events, audit = on_sdk
        assert (check, status, is_new) == ("revoked", "pending", True)
        assert [e["subject_id"] for e in events] == ["templates/a"]
        assert audit == [
            "echo-template; requested", "echo-template; approved", "echo-template; requested",
        ]


class TestSameFloor:
    async def test_an_auto_tool_needs_the_floor(self, tmp_path, monkeypatch):
        with pytest.raises(ToolDefinitionError) as sdk_error:
            async with PluginTestHost(load_echo_plugin().plugin):
                pass
        stack = Stack(tmp_path, monkeypatch)
        install_echo(stack.plugins, max_gate_floor="review")
        try:
            await stack.start()
            with pytest.raises(ValueError) as daemon_error:
                await stack.enable()
        finally:
            await stack.stop()

        on_sdk = str(sdk_error.value).partition(";")[0]
        on_daemon = str(daemon_error.value).removeprefix("manifest invalid: ")
        assert on_sdk == on_daemon == "tool auto_read needs max_gate_floor: auto to use the auto gate"
