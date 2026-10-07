"""Unit tests for privacyfence.plugins.confirm, against a real PendingApprovalRegistry."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.plugins.confirm import ConfirmationService
from privacyfence.plugins.constants import CONFIRM_AWAIT_MAX_MS
from privacyfence.plugins.protocol import RpcError

PLUGIN = "today"
DISPLAY = "Today"


def _params(**overrides) -> dict:
    params = {
        "principal": "local",
        "kind": "publish_note",
        "title": "Publish the note",
        "preview": [{"type": "text", "text": "The note body"}],
    }
    params.update(overrides)
    return params


class Harness:
    def __init__(self, *, pending_ttl: float = 60.0, unattended: bool = False) -> None:
        self.registry = PendingApprovalRegistry(pending_ttl=pending_ttl)
        self.unattended = unattended
        self.audits: list[tuple[str, str, str]] = []
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.service = ConfirmationService(
            registry_provider=lambda: self.registry,
            unattended_active=lambda: self.unattended,
            executor=self.executor,
            audit=lambda plugin, kind, status: self.audits.append((plugin, kind, status)),
        )
        self.service.poll_seconds = 0.01

    async def request(self, plugin: str = PLUGIN, **overrides) -> dict:
        return await self.service.request(plugin, DISPLAY, _params(**overrides), introspecting=False)

    async def await_(self, approval_id: str, timeout_ms: int = 5000, plugin: str = PLUGIN) -> dict:
        return await self.service.await_(plugin, {"approval_id": approval_id, "timeout_ms": timeout_ms})

    async def settled(self) -> None:
        """Wait until every finalizer the service started has finished."""
        for _ in range(500):
            if not self.service._finalizers:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("finalizer did not finish")


@pytest.fixture
def harness():
    h = Harness()
    yield h
    for approval in list(h.registry._pending.values()):
        approval.event.set()
    h.executor.shutdown(wait=True)


def _expiring_harness() -> Harness:
    return Harness(pending_ttl=0.2)


class TestConfirm:
    async def test_never_auto_accepted(self, harness):
        result = await harness.request()
        approval_id = result["approval_id"]

        # A rule that would match anything, for every operation.
        resolved = harness.registry.reevaluate_all(lambda operation, ctx: (True, "match_everything"))

        assert resolved == []
        assert harness.registry.await_status(approval_id) == "pending"
        card = harness.registry.get(approval_id)
        assert card.kind == "confirm"
        assert card.operation_key is None
        assert not card.event.is_set()

    async def test_finalized_after_answer(self, harness):
        confirmed = (await harness.request())["approval_id"]
        cancelled = (await harness.request())["approval_id"]
        harness.registry.answer(confirmed, "confirm")
        harness.registry.answer(cancelled, "cancel")
        await harness.settled()
        assert harness.registry.await_status(confirmed) == "approved"
        assert harness.registry.await_status(cancelled) == "denied"

        expiring = _expiring_harness()
        try:
            expired = (await expiring.request())["approval_id"]
            await expiring.settled()
            assert expiring.registry.await_status(expired) == "expired"
        finally:
            expiring.executor.shutdown(wait=True)

    async def test_audits_requested_and_outcome(self, harness):
        approval_id = (await harness.request())["approval_id"]
        assert harness.audits == [(PLUGIN, "publish_note", "requested")]
        harness.registry.answer(approval_id, "confirm")
        await harness.settled()
        assert harness.audits == [
            (PLUGIN, "publish_note", "requested"), (PLUGIN, "publish_note", "approved"),
        ]

    async def test_audit_failure_never_fails_the_request(self, harness, caplog):
        def broken(*_):
            raise OSError("disk full")

        harness.service._audit_fn = broken
        with caplog.at_level(logging.WARNING):
            result = await harness.request()
        assert harness.registry.await_status(result["approval_id"]) == "pending"
        assert "OSError" in caplog.text
        assert "disk full" not in caplog.text

    async def test_notifies_human(self, harness):
        seen = []
        harness.registry.add_created_listener(seen.append)
        approval_id = (await harness.request())["approval_id"]
        assert [a.id for a in seen] == [approval_id]

    async def test_card_html_shows_display_name_and_blocks(self, harness):
        approval_id = (await harness.request(
            title="Publish‮ the note",
            preview=[{"type": "code", "text": "<script>x</script>"}],
        ))["approval_id"]
        html = harness.registry.get(approval_id).html
        assert "Today: Publish the note" in html
        assert "‮" not in html
        assert "&lt;script&gt;x&lt;/script&gt;" in html
        assert 'data-pf-action="cancel">Deny<' in html
        assert 'data-pf-action="confirm">Approve<' in html

    async def test_returns_expiry_as_rfc3339_utc(self, harness):
        result = await harness.request()
        assert result["expires_at"].endswith("Z")
        assert "T" in result["expires_at"]

    async def test_refused_when_unattended(self, harness):
        harness.unattended = True
        with pytest.raises(RpcError) as info:
            await harness.request()
        assert info.value.code == "confirmation_refused"
        assert info.value.to_error()["data"]["reason"] == "unattended_session"
        assert harness.registry.list_pending() == []
        assert harness.audits == []

    async def test_step_up_marks_sensitive(self, harness):
        default = (await harness.request())["approval_id"]
        explicit = (await harness.request(require_step_up=True))["approval_id"]
        off = (await harness.request(require_step_up=False))["approval_id"]
        assert harness.registry.get(default).sensitive is True
        assert harness.registry.get(explicit).sensitive is True
        assert harness.registry.get(off).sensitive is False

    async def test_await_approved(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry.answer(approval_id, "confirm")
        result = await harness.await_(approval_id)
        assert result["status"] == "approved"
        assert result["decided_at"].endswith("Z")

    async def test_await_denied(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry.answer(approval_id, "cancel")
        assert (await harness.await_(approval_id))["status"] == "denied"

    async def test_await_expired(self):
        h = _expiring_harness()
        h.service.poll_seconds = 0.01
        try:
            approval_id = (await h.request())["approval_id"]
            result = await h.await_(approval_id)
            assert result["status"] == "expired"
            assert "decided_at" in result
        finally:
            h.executor.shutdown(wait=True)

    async def test_await_unknown_reads_as_expired(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry._pending.pop(approval_id).event.set()
        assert await harness.await_(approval_id) == {"status": "expired"}

    async def test_await_timeout(self, harness):
        approval_id = (await harness.request())["approval_id"]
        with pytest.raises(RpcError) as info:
            await harness.await_(approval_id, timeout_ms=50)
        assert info.value.code == "timeout"
        assert harness.registry.await_status(approval_id) == "pending"

    async def test_other_plugins_id_refused(self, harness):
        approval_id = (await harness.request(plugin="today"))["approval_id"]
        harness.registry.answer(approval_id, "confirm")
        with pytest.raises(RpcError) as info:
            await harness.await_(approval_id, plugin="other")
        assert info.value.code == "invalid_params"
        with pytest.raises(RpcError) as info:
            await harness.await_("0" * 32)
        assert info.value.code == "invalid_params"

    async def test_await_approval_meta_tool_sees_status(self, harness):
        approval_id = (await harness.request())["approval_id"]
        assert harness.registry.await_status(approval_id) == "pending"
        harness.registry.answer(approval_id, "confirm")
        await harness.settled()
        assert harness.registry.await_status(approval_id) == "approved"

    async def test_registry_expiry_sweep_keeps_its_own_decision(self, harness):
        approval_id = (await harness.request())["approval_id"]
        card = harness.registry.get(approval_id)
        card.expires_at = 0.0
        assert harness.registry.pop_expired_events() == [card]
        await harness.settled()
        assert harness.registry.await_status(approval_id) == "expired"
        assert harness.audits[-1] == (PLUGIN, "publish_note", "expired")

    async def test_refused_while_introspecting(self, harness):
        with pytest.raises(RpcError) as info:
            await harness.service.request(PLUGIN, DISPLAY, _params(), introspecting=True)
        assert info.value.code == "introspection_only"
        assert harness.registry.list_pending() == []

    async def test_unknown_principal(self, harness):
        with pytest.raises(RpcError) as info:
            await harness.request(principal="alice")
        assert info.value.code == "unknown_principal"

    @pytest.mark.parametrize("kind", ["Publish", "1x", "has space", "a" * 32, "dash-ed"])
    async def test_bad_kind(self, harness, kind):
        with pytest.raises(RpcError) as info:
            await harness.request(kind=kind)
        assert info.value.code == "invalid_params"

    async def test_invalid_blocks(self, harness):
        with pytest.raises(RpcError) as info:
            await harness.request(preview=[{"type": "image", "src": "x"}])
        assert info.value.code == "invalid_blocks"
        assert harness.registry.list_pending() == []

    async def test_invalid_params(self, harness):
        with pytest.raises(RpcError) as info:
            await harness.service.request(PLUGIN, DISPLAY, {"principal": "local"}, introspecting=False)
        assert info.value.code == "invalid_params"

    @pytest.mark.parametrize("params", [
        "x",
        {},
        {"approval_id": ""},
        {"approval_id": 5},
        {"approval_id": "a", "timeout_ms": "5"},
        {"approval_id": "a", "timeout_ms": True},
        {"approval_id": "a", "timeout_ms": -1},
        {"approval_id": "a", "timeout_ms": CONFIRM_AWAIT_MAX_MS + 1},
    ])
    async def test_await_bad_params(self, harness, params):
        with pytest.raises(RpcError) as info:
            await harness.service.await_(PLUGIN, params)
        assert info.value.code == "invalid_params"

    async def test_await_default_timeout_is_the_maximum(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry.answer(approval_id, "confirm")
        result = await harness.service.await_(PLUGIN, {"approval_id": approval_id})
        assert result["status"] == "approved"

    async def test_finalizer_failure_is_logged(self, harness, caplog):
        def boom(_approval_id):
            raise RuntimeError("finalizer broke")

        harness.service._finalize_when_answered = boom
        with caplog.at_level(logging.WARNING):
            await harness.request()
            await harness.settled()
        assert "finalizer failed" in caplog.text

    async def test_finalizer_for_a_dropped_card_does_nothing(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry._pending.pop(approval_id).event.set()
        await harness.settled()
        harness.service._finalize_when_answered(approval_id)
        assert harness.audits[0] == (PLUGIN, "publish_note", "requested")
