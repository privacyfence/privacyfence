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
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from privacyfence import auto_accept
from tests.fixtures.plugins.echo.harness import (  # noqa: I001  (puts the SDK sources on sys.path)
    Stack,
    install_echo,
    load_echo_plugin,
    mcp_session,
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
