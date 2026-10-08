"""Plugin approvals end to end: a real ``echo`` plugin process asks, a human answers on the
registry, and the answer is stored until it is revoked.

The daemon's side runs in this process; the plugin's page and recorder are read back through the
page route and the plugin's own event log.
"""
from __future__ import annotations

import json

import pytest

from privacyfence.plugins import storage
from tests.fixtures.plugins.echo.harness import Stack, install_echo, mcp_session, until, web_session

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

TEMPLATE = "Hello {name}"
CHANGED = "Goodbye {name}"


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch, serve=True)
    install_echo(stack.plugins)
    await stack.start()
    await stack.enable()
    (storage.install_dir("echo") / "events.jsonl").unlink(missing_ok=True)
    try:
        yield stack
    finally:
        await stack.stop()


async def request(mcp, text: str = TEMPLATE, note: str = "") -> dict:
    """``note`` is ignored by the plugin; it keeps a repeated call from being replayed as a duplicate."""
    result = await mcp.call("echo_approve", text=text, note=note)
    assert result.is_error is False, result
    return result.structured_content


async def check(stack, text: str = TEMPLATE) -> str:
    page = await stack.page("/approval-check", text=text)
    return json.loads(page["body"])["status"]


def approval_audit(stack) -> list[str]:
    return [e["summary"] for e in stack.audit() if e["decision"] == "plugin_approval"]


def recorded(stack, event: str) -> list[dict]:
    path = storage.install_dir("echo") / "events.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [line["params"] for line in lines if line["event"] == event]


class TestRequestAndApprove:
    async def test_a_request_opens_a_card_that_frames_the_plugins_page(self, stack):
        async with mcp_session(stack.server) as mcp:
            ticket = await request(mcp)

        assert ticket["status"] == "pending"
        card = stack.registry.get(ticket["approval_id"])
        assert card.frame_src == f"/plugins/echo/approval?pf_approval={ticket['approval_id']}"
        assert "Approve template" in card.html and "echo-template" in card.html
        assert await check(stack) == "unknown"

    async def test_the_cards_frame_loads_while_waiting_and_is_a_normal_page_once_answered(self, stack):
        async with mcp_session(stack.server) as mcp:
            ticket = await request(mcp)
            card = stack.registry.get(ticket["approval_id"])
            client = await web_session(stack.server)
            try:
                waiting = await client.get(card.frame_src)
                assert waiting.status_code == 200
                assert "frame-ancestors 'self'" in waiting.headers["content-security-policy"]
                assert waiting.headers["x-frame-options"] == "SAMEORIGIN"

                assert stack.registry.answer(ticket["approval_id"], "confirm")
                await until(lambda: len(approval_audit(stack)) == 2)
                answered = await client.get(card.frame_src)
            finally:
                await client.aclose()

        assert answered.status_code == 200
        assert "frame-ancestors 'none'" in answered.headers["content-security-policy"]
        assert "frame-ancestors 'self'" not in answered.headers["content-security-policy"]
        assert answered.headers["x-frame-options"] != "SAMEORIGIN"

    async def test_approving_it_makes_the_check_say_approved(self, stack):
        async with mcp_session(stack.server) as mcp:
            ticket = await request(mcp)
            assert stack.registry.answer(ticket["approval_id"], "confirm")
            await until(lambda: approval_audit(stack) == ["echo-template; requested", "echo-template; approved"])

        assert await check(stack) == "approved"
        assert await check(stack, CHANGED) == "unknown"

    async def test_the_same_content_again_opens_no_card(self, stack):
        async with mcp_session(stack.server) as mcp:
            first = await request(mcp)
            stack.registry.answer(first["approval_id"], "confirm")
            await until(lambda: len(approval_audit(stack)) == 2)
            known = {c.id for c in stack.registry.list_pending()}

            again = await request(mcp, note="again")

        assert again == {"approval_id": first["approval_id"], "status": "approved"}
        assert {c.id for c in stack.registry.list_pending()} == known
        assert approval_audit(stack) == ["echo-template; requested", "echo-template; approved"]

    async def test_changed_content_opens_a_new_card(self, stack):
        async with mcp_session(stack.server) as mcp:
            first = await request(mcp)
            stack.registry.answer(first["approval_id"], "confirm")
            await until(lambda: len(approval_audit(stack)) == 2)

            second = await request(mcp, CHANGED)

        assert second["status"] == "pending" and second["approval_id"] != first["approval_id"]
        assert stack.registry.get(second["approval_id"]) is not None
        assert await check(stack, CHANGED) == "unknown"

    async def test_a_denied_card_stores_nothing(self, stack):
        async with mcp_session(stack.server) as mcp:
            ticket = await request(mcp)
            stack.registry.answer(ticket["approval_id"], "cancel")
            await until(lambda: len(approval_audit(stack)) == 2)

        assert approval_audit(stack) == ["echo-template; requested", "echo-template; denied"]
        assert await check(stack) == "unknown"


class TestRevoke:
    async def approved(self, stack, mcp) -> str:
        ticket = await request(mcp)
        stack.registry.answer(ticket["approval_id"], "confirm")
        await until(lambda: len(approval_audit(stack)) == 2)
        return ticket["approval_id"]

    async def test_revoking_tells_the_plugin_and_the_check_says_revoked(self, stack):
        async with mcp_session(stack.server) as mcp:
            approval_id = await self.approved(stack, mcp)

        await stack.run(stack.host.revoke_approval("echo", approval_id))
        await until(lambda: recorded(stack, "approval.revoked"))

        [event] = recorded(stack, "approval.revoked")
        assert event["approval_id"] == approval_id
        assert event["kind"] == "echo-template" and event["subject_id"] == "templates/a"
        assert event["digest"].startswith("sha256:")
        assert await check(stack) == "revoked"
        assert approval_audit(stack)[-1] == "echo-template; revoked"
        [row] = stack.row()["approvals"]
        assert row["approval_id"] == approval_id and row["revoked_at"] is not None

    async def test_a_request_after_a_revoke_opens_a_new_card(self, stack):
        async with mcp_session(stack.server) as mcp:
            approval_id = await self.approved(stack, mcp)
            await stack.run(stack.host.revoke_approval("echo", approval_id))

            again = await request(mcp, note="again")

        assert again["status"] == "pending" and again["approval_id"] != approval_id

    async def test_an_unknown_approval_is_refused(self, stack):
        with pytest.raises(ValueError, match="No such approval"):
            await stack.run(stack.host.revoke_approval("echo", "approval-nope"))


class TestStoredAcrossRestarts:
    async def restart_plugin(self, stack) -> None:
        await stack.run(stack.host.disable("echo"))
        await stack.enable()

    async def test_an_approval_survives_a_plugin_restart(self, stack):
        async with mcp_session(stack.server) as mcp:
            ticket = await request(mcp)
            stack.registry.answer(ticket["approval_id"], "confirm")
            await until(lambda: len(approval_audit(stack)) == 2)

        await self.restart_plugin(stack)

        assert await check(stack) == "approved"

    async def test_a_revoked_approval_stays_revoked_after_a_plugin_restart(self, stack):
        async with mcp_session(stack.server) as mcp:
            ticket = await request(mcp)
            stack.registry.answer(ticket["approval_id"], "confirm")
            await until(lambda: len(approval_audit(stack)) == 2)
        await stack.run(stack.host.revoke_approval("echo", ticket["approval_id"]))

        await self.restart_plugin(stack)

        assert await check(stack) == "revoked"


class TestNeverAutomatic:
    async def test_refused_while_a_session_is_unattended(self, stack):
        stack.unattended = True
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_approve", text=TEMPLATE)

        assert result.is_error is True
        assert approval_audit(stack) == ["echo-template; refused"]
        assert stack.registry.list_pending() == []
        assert await check(stack) == "unknown"

    async def test_a_rule_matching_everything_leaves_the_card_pending(self, stack):
        async with mcp_session(stack.server) as mcp:
            ticket = await request(mcp)
            resolved = stack.registry.reevaluate_all(lambda operation, ctx: (True, "match_everything"))

            assert ticket["approval_id"] not in resolved
            assert stack.registry.await_status(ticket["approval_id"]) == "pending"

        assert await check(stack) == "unknown"
        assert approval_audit(stack) == ["echo-template; requested"]
