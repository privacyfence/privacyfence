"""Unit tests for privacyfence.plugins.approvals, against a real PendingApprovalRegistry."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.plugins.approvals import ApprovalRecord, ApprovalService, ApprovalStore
from privacyfence.plugins.manifest import Manifest
from privacyfence.plugins.protocol import RpcError

PLUGIN = "datalake"
DISPLAY = "Data Lake"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


def _manifest(*, pages: bool = True) -> Manifest:
    return Manifest(
        name=PLUGIN, display_name=DISPLAY, version="1.0.0", protocol="1.1.0", command=("run",),
        source_operations=frozenset(), max_gate_floor="review", pages=pages, service_credentials=False,
    )


def _params(**overrides) -> dict:
    params = {
        "principal": "local",
        "kind": "processor-code",
        "subject_id": "pipeline/clean.py",
        "digest": DIGEST,
        "title": "Approve the cleaner",
        "preview": [{"type": "text", "text": "Drops empty rows"}],
    }
    params.update(overrides)
    return params


def _record(approval_id: str = "a1", *, plugin: str = PLUGIN, digest: str = DIGEST,
            decided_at: str = "2026-10-08T10:00:00Z", revoked_at: str | None = None) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id, plugin=plugin, principal="local", kind="processor-code",
        subject_id="pipeline/clean.py", digest=digest, title="Approve the cleaner",
        decided_at=decided_at, revoked_at=revoked_at,
    )


def _key(digest: str = DIGEST) -> tuple[str, str, str, str, str]:
    return (PLUGIN, "local", "processor-code", "pipeline/clean.py", digest)


class Harness:
    def __init__(self, tmp_path: Path, *, pending_ttl: float = 60.0, unattended: bool = False) -> None:
        self.registry = PendingApprovalRegistry(pending_ttl=pending_ttl)
        self.store = ApprovalStore(tmp_path / "plugin-approvals.json")
        self.unattended = unattended
        self.audits: list[tuple[str, str, str]] = []
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.service = ApprovalService(
            store=self.store,
            registry_provider=lambda: self.registry,
            unattended_active=lambda: self.unattended,
            executor=self.executor,
            audit=lambda plugin, kind, status: self.audits.append((plugin, kind, status)),
        )
        self.service.poll_seconds = 0.01

    async def request(self, plugin: str = PLUGIN, *, pages: bool = True, introspecting: bool = False,
                      **overrides) -> dict:
        return await self.service.request(
            plugin, DISPLAY, _manifest(pages=pages), _params(**overrides), introspecting=introspecting,
        )

    async def check(self, plugin: str = PLUGIN, **overrides) -> dict:
        params = {k: v for k, v in _params(**overrides).items() if k in ("principal", "kind", "subject_id", "digest")}
        return await self.service.check(plugin, params)

    async def settled(self) -> None:
        """Wait until every finalizer the service started has finished."""
        for _ in range(500):
            if not self.service._finalizers:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("finalizer did not finish")

    def close(self) -> None:
        for approval in list(self.registry._pending.values()):
            approval.event.set()
        self.executor.shutdown(wait=True)


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.close()


@pytest.fixture
def expiring(tmp_path):
    h = Harness(tmp_path, pending_ttl=0.2)
    yield h
    h.close()


class TestStore:
    def test_round_trip(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        record = _record()
        store.add(record)
        assert ApprovalStore(store.path).find(*_key()) == record
        data = json.loads(store.path.read_text(encoding="utf-8"))
        assert data == {"version": 1, "approvals": [record.to_json()]}

    def test_missing_file_is_empty(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        assert store.find(*_key()) is None
        assert store.for_plugin(PLUGIN) == []

    @pytest.mark.parametrize("text", [
        "{not json",
        json.dumps({"version": 2, "approvals": []}),
        json.dumps({"version": 1, "approvals": {}}),
        json.dumps({"version": 1, "approvals": [{"approval_id": "a1"}]}),
        json.dumps({"version": 1, "approvals": [{**_record().to_json(), "revoked_at": 5}]}),
        json.dumps({"version": 1, "approvals": [{**_record().to_json(), "digest": None}]}),
    ])
    def test_corrupt_file_fails_closed(self, tmp_path, caplog, text):
        path = tmp_path / "plugin-approvals.json"
        path.write_text(text, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert ApprovalStore(path).find(*_key()) is None
        assert "corrupt" in caplog.text

    async def test_corrupt_file_checks_unknown(self, harness):
        harness.store.add(_record())
        harness.store.path.write_text("{not json", encoding="utf-8")
        assert await harness.check() == {"status": "unknown"}

    def test_find_returns_the_latest_by_decided_at(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        store.add(_record("new", decided_at="2026-10-08T11:00:00Z"))
        store.add(_record("old", decided_at="2026-10-08T09:00:00Z", revoked_at="2026-10-08T10:00:00Z"))
        assert store.find(*_key()).approval_id == "new"
        assert store.find(*_key(OTHER_DIGEST)) is None

    def test_revoke(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        store.add(_record())
        revoked = store.revoke(PLUGIN, "a1", now="2026-10-09T00:00:00Z")
        assert revoked.revoked_at == "2026-10-09T00:00:00Z"
        assert store.find(*_key()).revoked_at == "2026-10-09T00:00:00Z"
        # Revoking again keeps the first time.
        assert store.revoke(PLUGIN, "a1", now="2026-10-10T00:00:00Z").revoked_at == "2026-10-09T00:00:00Z"

    def test_revoke_unknown_or_other_plugin_is_none(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        store.add(_record())
        assert store.revoke(PLUGIN, "nope", now="2026-10-09T00:00:00Z") is None
        assert store.revoke("other", "a1", now="2026-10-09T00:00:00Z") is None
        assert store.find(*_key()).revoked_at is None

    def test_for_plugin_is_newest_first(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        store.add(_record("a", decided_at="2026-10-08T09:00:00Z"))
        store.add(_record("b", decided_at="2026-10-08T11:00:00Z"))
        store.add(_record("c", plugin="other"))
        assert [r.approval_id for r in store.for_plugin(PLUGIN)] == ["b", "a"]

    def test_forget_plugin(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        store.add(_record("a"))
        store.add(_record("b", digest=OTHER_DIGEST))
        store.add(_record("c", plugin="other"))
        assert store.forget_plugin(PLUGIN) == 2
        assert store.for_plugin(PLUGIN) == []
        assert [r.approval_id for r in store.for_plugin("other")] == ["c"]
        assert store.forget_plugin(PLUGIN) == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_file_mode_is_0600(self, tmp_path):
        store = ApprovalStore(tmp_path / "plugin-approvals.json")
        store.add(_record())
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


class TestRequest:
    async def test_card_shown(self, harness):
        result = await harness.request()
        assert result["status"] == "pending"
        assert result["expires_at"].endswith("Z")
        card = harness.registry.get(result["approval_id"])
        assert card in harness.registry.list_pending()
        assert card.kind == "confirm"
        assert card.sensitive is True
        assert "Data Lake (datalake)" in card.html
        assert "processor-code" in card.html
        assert "pipeline/clean.py" in card.html
        assert DIGEST in card.html
        assert "Drops empty rows" in card.html
        assert harness.audits == [(PLUGIN, "processor-code", "requested")]

    async def test_step_up_can_be_turned_off(self, harness):
        result = await harness.request(require_step_up=False)
        assert harness.registry.get(result["approval_id"]).sensitive is False

    async def test_already_approved_shows_no_card(self, harness):
        harness.store.add(_record())
        result = await harness.request()
        assert result == {"approval_id": "a1", "status": "approved"}
        assert harness.registry.list_pending() == []
        assert harness.audits == []

    async def test_already_approved_answers_even_while_unattended(self, harness):
        harness.store.add(_record())
        harness.unattended = True
        assert (await harness.request())["status"] == "approved"

    async def test_revoked_approval_shows_a_new_card(self, harness):
        harness.store.add(_record(revoked_at="2026-10-09T00:00:00Z"))
        result = await harness.request()
        assert result["status"] == "pending"
        assert result["approval_id"] != "a1"

    async def test_other_digest_shows_a_new_card(self, harness):
        harness.store.add(_record())
        assert (await harness.request(digest=OTHER_DIGEST))["status"] == "pending"

    async def test_pending_duplicate_returns_the_same_id(self, harness):
        first = await harness.request()
        second = await harness.request()
        assert second["approval_id"] == first["approval_id"]
        assert second["status"] == "pending"
        assert len(harness.registry.list_pending()) == 1
        third = await harness.request(digest=OTHER_DIGEST)
        assert third["approval_id"] != first["approval_id"]

    async def test_unattended_refused(self, harness):
        harness.unattended = True
        with pytest.raises(RpcError) as exc:
            await harness.request()
        assert exc.value.code == "confirmation_refused"
        assert exc.value.extra == {"reason": "unattended_session"}
        assert harness.registry.list_pending() == []

    async def test_introspection_refused(self, harness):
        with pytest.raises(RpcError) as exc:
            await harness.request(introspecting=True)
        assert exc.value.code == "introspection_only"
        assert harness.registry.list_pending() == []

    async def test_page_without_pages_refused(self, harness):
        with pytest.raises(RpcError) as exc:
            await harness.request(pages=False, page="/approval")
        assert exc.value.code == "invalid_params"
        assert exc.value.detail == "page needs pages: true in the manifest"

    @pytest.mark.parametrize("page", ["/a/../b", "/a//b", "/a%3Fpf_approval=x", "/a%23x", "/a?x=1", "/a#x", "/a%00"])
    async def test_bad_page_refused(self, harness, page):
        with pytest.raises(RpcError) as exc:
            await harness.request(page=page)
        assert exc.value.code == "invalid_params"
        assert exc.value.detail == "page is not a valid path"
        assert harness.registry.list_pending() == []

    async def test_frame_src_set_only_with_a_page(self, harness):
        with_page = await harness.request(page="/approval%20view")
        card = harness.registry.get(with_page["approval_id"])
        assert card.frame_src == f"/plugins/{PLUGIN}/approval view?pf_approval={card.id}"
        assert 'sandbox="allow-scripts"' in card.html

        without = await harness.request(digest=OTHER_DIGEST)
        card = harness.registry.get(without["approval_id"])
        assert card.frame_src == ""
        assert "<iframe" not in card.html

    async def test_unknown_principal(self, harness):
        with pytest.raises(RpcError) as exc:
            await harness.request(principal="someone")
        assert exc.value.code == "unknown_principal"

    @pytest.mark.parametrize("overrides", [
        {"kind": "Bad Kind"},
        {"digest": "sha256:ABC"},
        {"subject_id": ""},
        {"subject_id": "a‮b"},
        {"title": "‮"},
    ])
    async def test_invalid_params(self, harness, overrides):
        with pytest.raises(RpcError) as exc:
            await harness.request(**overrides)
        assert exc.value.code == "invalid_params"

    async def test_invalid_blocks(self, harness):
        with pytest.raises(RpcError) as exc:
            await harness.request(preview=[{"type": "nope"}])
        assert exc.value.code == "invalid_blocks"

    async def test_title_and_display_name_are_on_the_card_together(self, harness):
        card = harness.registry.get((await harness.request())["approval_id"])
        assert "Data Lake: Approve the cleaner" in card.html


class TestNeverAutoAccepted:
    async def test_a_rule_matching_everything_leaves_the_card_pending(self, harness):
        approval_id = (await harness.request(page="/approval"))["approval_id"]
        resolved = harness.registry.reevaluate_all(lambda operation, ctx: (True, "match_everything"))
        assert resolved == []
        assert harness.registry.await_status(approval_id) == "pending"
        card = harness.registry.get(approval_id)
        assert card.kind == "confirm"
        assert card.operation_key is None
        assert not card.event.is_set()
        assert harness.store.find(*_key()) is None


class TestFinalize:
    async def test_confirm_stores_before_the_registry_reports_approved(self, harness):
        approval_id = (await harness.request())["approval_id"]
        seen: list[tuple] = []
        real_finalize = harness.registry.finalize

        def spy(the_id, decision, rule_name=""):
            seen.append((decision, harness.store.find(*_key()), harness.registry.await_status(the_id)))
            return real_finalize(the_id, decision, rule_name)

        harness.registry.finalize = spy
        harness.registry.answer(approval_id, "confirm")
        await harness.settled()

        assert len(seen) == 1
        decision, stored, status_then = seen[0]
        assert decision == "accept"
        assert stored is not None and stored.approval_id == approval_id
        assert status_then == "pending"
        assert harness.registry.await_status(approval_id) == "approved"
        assert harness.audits[-1] == (PLUGIN, "processor-code", "approved")
        assert await harness.check() == {
            "status": "approved", "approval_id": approval_id, "decided_at": stored.decided_at,
        }
        assert stored.title == "Approve the cleaner"
        assert harness.service._pending == {}
        # A later request for the same tuple finds the stored approval and shows no card.
        assert await harness.request() == {"approval_id": approval_id, "status": "approved"}

    async def test_cancel_stores_nothing(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry.answer(approval_id, "cancel")
        await harness.settled()
        assert harness.registry.await_status(approval_id) == "denied"
        assert harness.store.find(*_key()) is None
        assert not harness.store.path.exists()
        assert harness.audits[-1] == (PLUGIN, "processor-code", "denied")
        assert harness.service._pending == {}
        # A later request opens a new card.
        again = await harness.request()
        assert again["status"] == "pending" and again["approval_id"] != approval_id

    async def test_expiry_stores_nothing(self, expiring):
        approval_id = (await expiring.request())["approval_id"]
        await expiring.settled()
        assert expiring.registry.await_status(approval_id) == "expired"
        assert expiring.store.find(*_key()) is None
        assert expiring.audits[-1] == (PLUGIN, "processor-code", "expired")
        assert expiring.service._pending == {}

    async def test_close_expires_and_removes_the_tuple(self, harness):
        approval_id = (await harness.request())["approval_id"]
        await harness.service.close()
        assert harness.registry.await_status(approval_id) == "expired"
        assert harness.store.find(*_key()) is None
        assert harness.service._pending == {}

    async def test_store_failure_denies(self, harness, monkeypatch, caplog):
        approval_id = (await harness.request())["approval_id"]

        def broken(record):
            raise OSError("disk full")

        monkeypatch.setattr(harness.store, "add", broken)
        with caplog.at_level(logging.WARNING):
            harness.registry.answer(approval_id, "confirm")
            await harness.settled()
        assert harness.registry.await_status(approval_id) == "denied"
        assert harness.audits[-1] == (PLUGIN, "processor-code", "denied")
        assert harness.service._pending == {}

    async def test_a_card_finalized_meanwhile_takes_the_record_back(self, harness):
        approval_id = (await harness.request())["approval_id"]
        real_add = harness.store.add

        def add_then_expire(record):
            real_add(record)
            harness.registry.finalize(approval_id, "expired")

        harness.store.add = add_then_expire
        harness.registry.answer(approval_id, "confirm")
        await harness.settled()
        assert harness.registry.await_status(approval_id) == "expired"
        assert harness.store.find(*_key()) is None

    async def test_scheduling_failure_denies_and_removes_the_tuple(self, harness):
        harness.executor.shutdown(wait=True)
        with pytest.raises(RuntimeError):
            await harness.request()
        assert harness.service._pending == {}
        assert harness.audits[-1] == (PLUGIN, "processor-code", "denied")


class TestAwait:
    async def test_await_approved(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry.answer(approval_id, "confirm")
        result = await harness.service.await_(PLUGIN, {"approval_id": approval_id, "timeout_ms": 5000})
        assert result["status"] == "approved"
        assert result["decided_at"].endswith("Z")

    async def test_await_denied(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.registry.answer(approval_id, "cancel")
        result = await harness.service.await_(PLUGIN, {"approval_id": approval_id, "timeout_ms": 5000})
        assert result["status"] == "denied"

    async def test_await_timeout(self, harness):
        approval_id = (await harness.request())["approval_id"]
        with pytest.raises(RpcError) as exc:
            await harness.service.await_(PLUGIN, {"approval_id": approval_id, "timeout_ms": 0})
        assert exc.value.code == "timeout"

    async def test_await_an_id_approved_without_a_card(self, harness):
        harness.store.add(_record())
        result = await harness.service.await_(PLUGIN, {"approval_id": "a1"})
        assert result == {"status": "approved", "decided_at": "2026-10-08T10:00:00Z"}

    async def test_other_plugins_id_refused(self, harness):
        approval_id = (await harness.request())["approval_id"]
        harness.store.add(_record("a1"))
        for approval in (approval_id, "a1"):
            with pytest.raises(RpcError) as exc:
                await harness.service.await_("other", {"approval_id": approval})
            assert exc.value.code == "invalid_params"

    @pytest.mark.parametrize("params", [{}, {"approval_id": ""}, {"approval_id": "x", "timeout_ms": -1}])
    async def test_bad_params(self, harness, params):
        with pytest.raises(RpcError) as exc:
            await harness.service.await_(PLUGIN, params)
        assert exc.value.code == "invalid_params"


class TestCheck:
    async def test_approved(self, harness):
        harness.store.add(_record())
        assert await harness.check() == {
            "status": "approved", "approval_id": "a1", "decided_at": "2026-10-08T10:00:00Z",
        }

    async def test_revoked(self, harness):
        harness.store.add(_record())
        harness.store.revoke(PLUGIN, "a1", now="2026-10-09T00:00:00Z")
        assert (await harness.check())["status"] == "revoked"

    async def test_unknown(self, harness):
        assert await harness.check() == {"status": "unknown"}

    async def test_a_different_digest_is_unknown(self, harness):
        harness.store.add(_record())
        assert await harness.check(digest=OTHER_DIGEST) == {"status": "unknown"}

    async def test_another_plugins_approval_is_unknown(self, harness):
        harness.store.add(_record(plugin="other"))
        assert await harness.check() == {"status": "unknown"}

    async def test_unknown_principal(self, harness):
        with pytest.raises(RpcError) as exc:
            await harness.check(principal="someone")
        assert exc.value.code == "unknown_principal"

    async def test_invalid_params(self, harness):
        with pytest.raises(RpcError) as exc:
            await harness.check(digest="md5:x")
        assert exc.value.code == "invalid_params"


class TestEmbedAllowed:
    async def test_pending_and_matching_path_only(self, harness):
        approval_id = (await harness.request(page="/approval"))["approval_id"]
        assert harness.service.embed_allowed(PLUGIN, approval_id, "/approval") is True
        assert harness.service.embed_allowed(PLUGIN, approval_id, "/other") is False
        assert harness.service.embed_allowed(PLUGIN, approval_id, "/approval/") is False
        assert harness.service.embed_allowed(PLUGIN, "nope", "/approval") is False

    async def test_finalized_is_false(self, harness):
        approval_id = (await harness.request(page="/approval"))["approval_id"]
        harness.registry.finalize(approval_id, "expired")
        assert harness.service.embed_allowed(PLUGIN, approval_id, "/approval") is False
        await harness.settled()
        assert harness.service.embed_allowed(PLUGIN, approval_id, "/approval") is False

    async def test_answered_is_false(self, harness):
        approval_id = (await harness.request(page="/approval"))["approval_id"]
        harness.registry.answer(approval_id, "confirm")
        await harness.settled()
        assert harness.service.embed_allowed(PLUGIN, approval_id, "/approval") is False

    async def test_other_plugin_is_false(self, harness):
        approval_id = (await harness.request(page="/approval"))["approval_id"]
        assert harness.service.embed_allowed("other", approval_id, "/approval") is False

    async def test_card_without_a_page_is_false(self, harness):
        approval_id = (await harness.request())["approval_id"]
        assert harness.service.embed_allowed(PLUGIN, approval_id, "") is False
        assert harness.service.embed_allowed(PLUGIN, approval_id, "/") is False


class TestCardText:
    async def test_title_line_breaks_become_spaces(self, harness):
        result = await harness.request(title="Line one\nLine two")
        html = harness.registry.get(result["approval_id"]).html
        assert "Line one Line two" in html


class TestSetupFailure:
    async def test_failed_set_html_denies_the_card_and_frees_the_slot(self, harness, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("set_html failed")

        monkeypatch.setattr(harness.registry, "set_html", boom)
        with pytest.raises(RuntimeError):
            await harness.request()
        (card,) = harness.registry._pending.values()
        assert harness.registry.await_status(card.id) == "denied"
        assert harness.service._active_total == 0
        assert not any(status == "requested" for _, _, status in harness.audits)
