"""What the plugin framework refuses, each against a real plugin process (ADR 0120-0126).

One parametrized test; every case sets up its own daemon pieces, provokes one refusal and checks
both the refusal and that nothing it would have unlocked happened. ``echo`` is the plugin under
test, or one of the ``echo-variants`` cases when the plugin itself has to be wrong in a way the
SDK would not let a plugin author write.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from privacyfence import privilege_separation
from privacyfence.plugins import trust
from privacyfence.plugins.host import CHANGED_SINCE_REVIEW
from privacyfence.plugins.state import HASH_DRIFT_REASON
from tests.fixtures.plugins.echo.harness import Stack, install_echo, install_variant, web_session

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

CaseFn = Callable[[Stack, pytest.MonkeyPatch], Awaitable[None]]


@dataclass(frozen=True)
class Case:
    run: CaseFn
    serve: bool = False
    cards: bool = False


CASES: dict[str, Case] = {}


def case(name: str, *, serve: bool = False, cards: bool = False) -> Callable[[CaseFn], CaseFn]:
    def register(fn: CaseFn) -> CaseFn:
        CASES[name] = Case(fn, serve=serve, cards=cards)
        return fn

    return register


async def echo_running(stack: Stack, **manifest) -> None:
    install_echo(stack.plugins, **manifest)
    await stack.start()
    await stack.enable()


async def source_page(stack: Stack, operation: str, params: dict | None = None, extra: dict | None = None) -> dict:
    page = await stack.page(
        "/source", operation=operation, params=json.dumps(params or {}), extra=json.dumps(extra or {}),
    )
    return json.loads(page["body"])


def source_audit(stack: Stack) -> list[str]:
    return [e["summary"] for e in stack.audit() if e["decision"] == "plugin_source"]


# --------------------------------------------------------------------------------------- source API


@case("operation outside the allowlist")
async def _(stack, monkeypatch):
    await echo_running(stack)

    answer = await source_page(stack, "gmail.send", {"to": "someone@example.com"})

    assert answer["error"] == "operation_not_allowed"
    stack.calendar.list_events.assert_not_called()
    [summary] = source_audit(stack)
    assert summary.endswith("error=operation_not_allowed")


@case("operation outside the manifest")
async def _(stack, monkeypatch):
    await echo_running(stack)

    answer = await source_page(stack, "jira.search", {"jql": "project = X"})

    assert answer["error"] == "operation_not_allowed"
    [summary] = source_audit(stack)
    assert summary.endswith("error=operation_not_allowed")


@case("org-only field in local mode")
async def _(stack, monkeypatch):
    await echo_running(stack)
    params = {"time_min": "2026-10-07T00:00:00Z", "time_max": "2026-10-08T00:00:00Z"}

    answer = await source_page(stack, "calendar.list_events", params, extra={"credential": {"token": "x"}})

    assert answer["error"] == "org_only_field"
    stack.calendar.list_events.assert_not_called()


@case("operation on a connector that is not connected")
async def _(stack, monkeypatch):
    await echo_running(stack)
    stack.calendar_state = (False, None)
    params = {"time_min": "2026-10-07T00:00:00Z", "time_max": "2026-10-08T00:00:00Z"}

    answer = await source_page(stack, "calendar.list_events", params)

    assert answer == {"error": "connector_unavailable", "reason": "disabled"}
    stack.calendar.list_events.assert_not_called()


# --------------------------------------------------------------------------------------- trust


@case("user-writable executable")
async def _(stack, monkeypatch):
    monkeypatch.setattr(
        privilege_separation, "admin_only_write_problem", lambda path: f"{path} can be rewritten by any user",
    )
    install_echo(stack.plugins)
    await stack.start(trust_check=trust.admin_only_problem)

    row = stack.row()
    assert row["state"] == "rejected"
    assert row["reason"] == "executable is writable by non-administrators"
    with pytest.raises(ValueError, match="writable by non-administrators"):
        await stack.inspect()
    assert stack.host.connectors() == {}


@case("changed hash")
async def _(stack, monkeypatch):
    target = install_echo(stack.plugins)
    await stack.start()
    summary = await stack.enable()
    (target / "echo_plugin.py").write_text(
        (target / "echo_plugin.py").read_text(encoding="utf-8") + "\n# edited after review\n", encoding="utf-8",
    )

    await stack.restart()

    row = stack.row()
    assert row["state"] == "disabled" and row["reason"] == HASH_DRIFT_REASON
    assert stack.host.connectors() == {}
    with pytest.raises(ValueError, match=CHANGED_SINCE_REVIEW):
        await stack.run(stack.host.enable(
            "echo", executable_sha256=summary["executable_sha256"], manifest_sha256=summary["manifest_sha256"],
        ))
    assert f"disabled: {HASH_DRIFT_REASON}" in [
        e["summary"] for e in stack.audit() if e["decision"] == "plugin_lifecycle"
    ]


@case("major mismatch")
async def _(stack, monkeypatch):
    install_variant(stack.plugins, "major-2")
    await stack.start()

    assert stack.row("variant")["state"] == "rejected"
    assert stack.row("variant")["reason"] == "protocol major mismatch"
    with pytest.raises(ValueError, match="protocol major mismatch"):
        await stack.inspect("variant")


# --------------------------------------------------------------------------------------- manifests


async def variant_refused(stack: Stack, name: str, case_dir: str, *, rejected_at_scan: bool, reason: str) -> None:
    install_variant(stack.plugins, case_dir)
    await stack.start()

    if rejected_at_scan:
        assert stack.row(name)["state"] == "rejected"
        assert stack.row(name)["reason"] == f"manifest invalid: {reason}"
    with pytest.raises(ValueError, match=f"manifest invalid: {reason}"):
        await stack.enable(name)
    assert stack.host.connectors() == {}
    assert name not in stack.host._store.load()


@case("manifest with an unknown key")
async def _(stack, monkeypatch):
    await variant_refused(stack, "variant", "bad-manifest-key", rejected_at_scan=True, reason="unknown key 'colour'")


@case("reserved plugin name")
async def _(stack, monkeypatch):
    await variant_refused(stack, "gmail", "reserved-name", rejected_at_scan=True, reason="name 'gmail' is reserved")


@case("org-only service credentials in local mode")
async def _(stack, monkeypatch):
    await variant_refused(
        stack, "variant", "org-only-credentials", rejected_at_scan=True,
        reason="service_credentials is not allowed in local mode",
    )


# --------------------------------------------------------------------------------------- tool definitions


@case("non-read-only tool on auto without the floor")
async def _(stack, monkeypatch):
    await variant_refused(
        stack, "variant", "write-auto-no-floor", rejected_at_scan=False,
        reason="tool ping needs max_gate_floor: auto to use the auto gate",
    )


@case("read tool on auto without the floor")
async def _(stack, monkeypatch):
    await variant_refused(
        stack, "variant", "read-auto-no-floor", rejected_at_scan=False,
        reason="tool ping needs max_gate_floor: auto to use the auto gate",
    )


@case("echo itself with the review floor")
async def _(stack, monkeypatch):
    install_echo(stack.plugins, max_gate_floor="review")
    await stack.start()

    with pytest.raises(ValueError, match="needs max_gate_floor: auto to use the auto gate"):
        await stack.enable()
    assert stack.host.connectors() == {}


@case("destructive tool not on popup")
async def _(stack, monkeypatch):
    await variant_refused(
        stack, "variant", "destructive-not-popup", rejected_at_scan=False,
        reason="destructive tool wipe must use the popup gate",
    )


@case("array parameter")
async def _(stack, monkeypatch):
    await variant_refused(
        stack, "variant", "array-param", rejected_at_scan=False,
        reason="parameter ids of ping: only string, integer, number and boolean are supported",
    )


@case("tool name colliding with a built-in tool")
async def _(stack, monkeypatch):
    await variant_refused(
        stack, "apps", "builtin-collision", rejected_at_scan=False,
        reason="tool apps_script_get_content collides with a built-in tool",
    )


@case("tools.changed adding an unreviewed tool")
async def _(stack, monkeypatch):
    await echo_running(stack)
    connector = stack.host.connectors()["echo"]
    before = [spec.name for spec in connector.tool_specs()]

    await stack.page("/widen")

    assert [spec.name for spec in connector.tool_specs()] == before
    assert "was not in the list reviewed at enable" in connector.last_tools_rejection
    with pytest.raises(ValueError, match="Unknown tool"):
        await stack.run(connector.call("echo_extra", {}))


# --------------------------------------------------------------------------------------- web


@case("block HTML rendered escaped", cards=True)
async def _(stack, monkeypatch):
    await echo_running(stack)
    connector = stack.host.connectors()["echo"]

    result = await stack.run(connector.call("echo_review_read", {"dataset": "alpha", "text": "<script>alert(1)</script>"}))

    # The AI gets the text as data; the card the human saw has it escaped.
    assert result["blocks"][-1]["text"] == "<script>alert(1)</script>"
    [card] = stack.cards.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in card
    assert "<script>alert(1)</script>" not in card


@case("POST to a plugin page", serve=True)
async def _(stack, monkeypatch):
    await echo_running(stack)
    client = await web_session(stack.server)
    try:
        for method in ("POST", "PUT", "DELETE"):
            response = await client.request(method, "/plugins/echo/")
            assert response.status_code == 405, method
            assert response.headers["allow"] == "GET, HEAD"
            assert "Echo" not in response.text
    finally:
        await client.aclose()


class TestRefusals:
    @pytest.mark.parametrize("name", list(CASES))
    async def test_refused(self, name, tmp_path, monkeypatch):
        spec = CASES[name]
        stack = Stack(tmp_path, monkeypatch, serve=spec.serve, capture_cards=spec.cards)
        try:
            await spec.run(stack, monkeypatch)
        finally:
            await stack.stop()
