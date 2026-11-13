"""runtime_credentials: the delivery records, the two-phase deliver flow and its audit entries."""
from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone

import pytest

from privacyfence import agent_identity, agent_runtimes, audit_log, paths, runtime_credentials as rc
from privacyfence import scoped_token_proposals as stp
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.n8n_api import N8nApiClientError
from privacyfence.principal import Principal, principal_scope
from privacyfence.scoped_tokens import ScopedTokenError, ScopedTokenPolicy, ScopedTokenStore

ALICE = Principal(id="alice", email="alice@example.com")
ENABLED = ScopedTokenPolicy(enabled=True, max_days=365)
AGENT = agent_identity.AgentIdentity(
    id="claude-code", name="Claude Code", version="1", source=agent_identity.AgentSource.NONE,
)
CONFIG = agent_runtimes.N8nRuntimeConfig(
    enabled=True, base_url="https://n8n.example.com", project_id="proj1", mcp_url="",
)
BOUND = {
    "label": "Sales sync", "days": 30,
    "rules": [{"kind": "new", "group": "drive.folder", "value": "F1", "verbs": ["read"]}],
    "tools": ["drive_download_file"], "allow_personal_data": False, "allow_personal_data_reads": False,
}
SECRET_MARK = "pfst_"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    with principal_scope(ALICE):
        init_audit_logger(str(tmp_path / "audit"))
    rc.set_store(rc.RuntimeCredentialStore(tmp_path / "records.json"))


def _entries(tmp_path):
    path = tmp_path / "audit" / f"{current_week()}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _record(**over):
    base = dict(
        principal_id="alice", token_id="st-aaaaaaaaaaaa", proposal_id="stp-1", runtime="n8n",
        endpoint=("https", "n8n.example.com", 443), project_id="", credential_id="0123456789abcdef",
        credential_name="PrivacyFence Sales sync", workflow_id="wf1", mcp_url="https://pf/mcp",
        state=rc.CREATING, created_at=1000.0, boot_id="boot",
    )
    base.update(over)
    return rc.RuntimeCredential(**base)


def _proposal(workflow_id="wf1"):
    return stp.new_proposal(
        principal=ALICE, request=BOUND, source="bundle", agent=AGENT, reason="because",
        delivery=agent_runtimes.DeliveryTarget("n8n", workflow_id), mcp_url="https://pf/mcp",
    )


class FakeApi:
    def __init__(self, create_error=None, delete_error=None, on_create=None):
        self.create_error, self.delete_error, self.on_create = create_error, delete_error, on_create
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def create_bearer_credential(self, *, credential_id, name, token, project_id):
        self.created.append(
            {"credential_id": credential_id, "name": name, "token": token, "project_id": project_id},
        )
        if self.on_create is not None:
            self.on_create()
        if self.create_error is not None:
            raise self.create_error

    def delete_credential(self, credential_id):
        self.deleted.append(credential_id)
        if self.delete_error is not None:
            raise self.delete_error
        return True


@pytest.fixture
def tokens():
    return ScopedTokenStore(lambda: [ALICE])


def _deliver(tokens, api, proposal=None, on_abandoned=None):
    return rc.deliver(
        principal=ALICE, bound=BOUND, proposal=proposal or _proposal(), config=CONFIG, api=api,
        token_store=tokens, policy=ENABLED, on_abandoned=on_abandoned,
    )


def _remove_the_record():
    rc.get_store().remove(rc.get_store().all()[0].credential_id)


class TestStoreFile:
    def test_round_trip(self, tmp_path):
        store = rc.RuntimeCredentialStore(tmp_path / "r.json")
        assert store.all() == []
        record = _record()
        store.add(record)
        fresh = rc.RuntimeCredentialStore(tmp_path / "r.json")
        assert fresh.all() == [record]
        assert fresh.for_token("alice", "st-aaaaaaaaaaaa") == record
        assert fresh.for_proposal("alice", "stp-1") == record
        assert fresh.for_token("bob", "st-aaaaaaaaaaaa") is None
        assert fresh.for_proposal("bob", "stp-1") is None
        assert json.loads((tmp_path / "r.json").read_text())["version"] == 1

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_the_file_is_private(self, tmp_path):
        store = rc.RuntimeCredentialStore(tmp_path / "r.json")
        store.add(_record())
        assert stat.S_IMODE((tmp_path / "r.json").stat().st_mode) == 0o600

    def test_set_state_is_compare_and_set(self, tmp_path):
        store = rc.RuntimeCredentialStore(tmp_path / "r.json")
        store.add(_record())
        cid = "0123456789abcdef"
        assert store.set_state(cid, rc.DELIVERED, rc.ABANDONED) is False
        assert store.set_state(cid, rc.CREATING, rc.DELIVERED) is True
        assert store.set_state(cid, rc.CREATING, rc.ABANDONED) is False
        assert store.all()[0].state == rc.DELIVERED
        assert store.set_state("missing", rc.CREATING, rc.DELIVERED) is False

    def test_remove(self, tmp_path):
        store = rc.RuntimeCredentialStore(tmp_path / "r.json")
        store.add(_record())
        store.remove("other")
        assert len(store.all()) == 1
        store.remove("0123456789abcdef")
        assert store.all() == []

    @pytest.mark.parametrize("content", ["{not json", '{"version": 1}', '{"records": [{"principal_id": "a"}]}'])
    def test_a_malformed_file_is_never_rewritten(self, tmp_path, caplog, content):
        path = tmp_path / "r.json"
        path.write_text(content, encoding="utf-8")
        store = rc.RuntimeCredentialStore(path)
        with caplog.at_level(logging.WARNING):
            assert store.all() == []
            assert store.all() == []
        assert len([r for r in caplog.records if "cannot be read" in r.message]) == 1
        with pytest.raises(rc.RuntimeCredentialStoreError):
            store.add(_record())
        with pytest.raises(rc.RuntimeCredentialStoreError):
            store.set_state("x", rc.CREATING, rc.DELIVERED)
        with pytest.raises(rc.RuntimeCredentialStoreError):
            store.remove("x")
        assert path.read_text(encoding="utf-8") == content

    def test_an_unreadable_path_is_treated_as_unreadable(self, tmp_path):
        directory = tmp_path / "dir"
        directory.mkdir()
        store = rc.RuntimeCredentialStore(directory)
        assert store.all() == []
        with pytest.raises(rc.RuntimeCredentialStoreError):
            store.add(_record())

    def test_in_flight(self, tmp_path):
        store = rc.RuntimeCredentialStore(tmp_path / "r.json")
        assert store.is_in_flight("c") is False
        with pytest.raises(RuntimeError):
            with store.in_flight("c"):
                assert store.is_in_flight("c") is True
                raise RuntimeError("boom")
        assert store.is_in_flight("c") is False

    def test_the_default_store_lives_in_the_org_directory(self, tmp_path):
        rc.set_store(None)
        store = rc.get_store()
        assert store is rc.get_store()
        store.add(_record())
        assert (tmp_path / "org" / rc.STORE_FILE_NAME).exists()

    def test_strip_control(self):
        assert rc.strip_control("a\nb\x00c\x1b[0m d") == "abc[0m d"


class TestRecordEvent:
    def test_writes_an_entry_for_the_records_person(self, tmp_path):
        rc.record_event("runtime_credential_deleted", _record(), "Deleted it")
        (entry,) = _entries(tmp_path)
        assert entry["decision"] == "runtime_credential_deleted" and entry["summary"] == "Deleted it"
        assert entry["tool"] == "privacyfence_runtime_credential" and entry["connector"] == "policy"
        assert entry["request_id"] == "stp-1"

    def test_a_failed_write_is_swallowed(self, monkeypatch, caplog):
        def boom():
            raise OSError("no disk")
        monkeypatch.setattr(audit_log, "get_audit_logger", boom)
        with caplog.at_level(logging.WARNING):
            rc.record_event("runtime_credential_deleted", _record(), "x")
        assert "Audit log write failed" in caplog.text


class TestDeliverHappyPath:
    def test_delivers_and_records(self, tmp_path, tokens, caplog):
        api = FakeApi()
        with caplog.at_level(logging.DEBUG):
            outcome = _deliver(tokens, api)
        assert outcome.status == 200 and outcome.error == ""
        token, record = outcome.token, outcome.record
        assert record.state == rc.DELIVERED and record.boot_id == rc.BOOT_ID
        assert record.token_id == token.id and record.proposal_id
        assert record.endpoint == ("https", "n8n.example.com", 443) and record.project_id == "proj1"
        assert record.credential_name == "PrivacyFence Sales sync" and record.mcp_url == "https://pf/mcp"
        assert [t.id for t in tokens.tokens_for(ALICE)] == [token.id]
        assert rc.get_store().all() == [record]
        (call,) = api.created
        assert call["credential_id"] == record.credential_id and call["project_id"] == "proj1"
        assert call["token"].startswith(SECRET_MARK)

        (entry,) = _entries(tmp_path)
        assert entry["decision"] == "runtime_credential_delivered"
        assert entry["summary"] == (
            f"Delivered scoped token 'Sales sync' ({token.id}) to n8n at n8n.example.com as credential "
            f"'PrivacyFence Sales sync' ({record.credential_id}), for workflow wf1 as reported by the AI client"
        )
        secret = call["token"]
        assert secret not in (tmp_path / "records.json").read_text()
        assert secret not in json.dumps(_entries(tmp_path))
        assert secret not in caplog.text

    def test_no_workflow_leaves_the_clause_out(self, tmp_path, tokens):
        outcome = _deliver(tokens, FakeApi(), _proposal(workflow_id=""))
        (entry,) = _entries(tmp_path)
        assert entry["summary"].endswith(f"({outcome.record.credential_id})")

    def test_the_record_is_in_flight_while_the_runtime_is_called(self, tokens):
        seen = []
        api = FakeApi(on_create=lambda: seen.append(
            [rc.get_store().is_in_flight(r.credential_id) for r in rc.get_store().all()]))
        outcome = _deliver(tokens, api)
        assert seen == [[True]]
        assert rc.get_store().is_in_flight(outcome.record.credential_id) is False

    def test_a_proposal_without_delivery_is_refused(self, tokens):
        plain = stp.new_proposal(principal=ALICE, request=BOUND, source="bundle", agent=AGENT, reason="r")
        with pytest.raises(ValueError):
            _deliver(tokens, FakeApi(), plain)

    def test_control_characters_are_stripped_from_summaries(self, tmp_path, tokens):
        _deliver(tokens, FakeApi(), _proposal(workflow_id="wf\n1\x00"))
        (entry,) = _entries(tmp_path)
        assert "\n" not in entry["summary"] and "\x00" not in entry["summary"]
        assert "workflow wf1 as" in entry["summary"]


class TestDeliverFailures:
    def test_an_n8n_error_revokes_the_token_and_abandons_the_record(self, tmp_path, tokens, caplog):
        error = N8nApiClientError("n8n at n8n.example.com answered HTTP 500.", status=500)
        wakes = []
        with caplog.at_level(logging.DEBUG):
            outcome = _deliver(tokens, FakeApi(create_error=error), on_abandoned=lambda: wakes.append(1))
        assert (outcome.status, outcome.error, outcome.token, outcome.record) == (502, rc.DELIVERY_FAILED, None, None)
        assert tokens.tokens_for(ALICE) == []
        (record,) = rc.get_store().all()
        assert record.state == rc.ABANDONED
        assert wakes == [1]
        (entry,) = _entries(tmp_path)
        assert entry["decision"] == "runtime_credential_delivery_failed"
        assert entry["summary"].endswith("to n8n at n8n.example.com: n8n at n8n.example.com answered HTTP 500.")
        assert SECRET_MARK not in caplog.text + json.dumps(_entries(tmp_path))

    def test_an_unexpected_error_never_reaches_the_audit_text(self, tmp_path, tokens):
        outcome = _deliver(tokens, FakeApi(create_error=RuntimeError("secret detail")))
        assert outcome.status == 502
        (entry,) = _entries(tmp_path)
        assert "secret detail" not in entry["summary"] and "an unexpected error occurred" in entry["summary"]

    def test_without_a_wake_callback(self, tokens):
        assert _deliver(tokens, FakeApi(create_error=RuntimeError("x"))).status == 502

    def test_a_failed_revoke_keeps_the_record_creating(self, tmp_path, tokens, monkeypatch, caplog):
        def boom(*args):
            raise OSError("disk full")
        monkeypatch.setattr(tokens, "revoke", boom)
        wakes = []
        with caplog.at_level(logging.ERROR):
            outcome = _deliver(
                tokens, FakeApi(create_error=RuntimeError("x")), on_abandoned=lambda: wakes.append(1),
            )
        assert (outcome.status, outcome.error) == (500, rc.DELIVERY_FAILED_NOT_REVOKED)
        assert len(tokens.tokens_for(ALICE)) == 1
        assert [r.state for r in rc.get_store().all()] == [rc.CREATING]
        assert wakes == []
        assert [e["decision"] for e in _entries(tmp_path)] == ["runtime_credential_delivery_failed"]

    def test_a_token_error_removes_the_record(self, tokens, monkeypatch):
        def refuse(*args, **kwargs):
            raise ScopedTokenError("A token with that name already exists.")
        monkeypatch.setattr(tokens, "create", refuse)
        api = FakeApi()
        outcome = _deliver(tokens, api)
        assert (outcome.status, outcome.error) == (400, "A token with that name already exists.")
        assert rc.get_store().all() == [] and api.created == []

    def test_a_token_error_with_an_unreadable_records_file_still_answers_400(self, tmp_path, tokens, monkeypatch):
        def refuse(*args, **kwargs):
            (tmp_path / "records.json").write_text("{bad", encoding="utf-8")
            raise ScopedTokenError("nope")
        monkeypatch.setattr(tokens, "create", refuse)
        assert _deliver(tokens, FakeApi()).status == 400

    def test_a_record_that_cannot_be_written_creates_nothing(self, tmp_path, tokens):
        (tmp_path / "records.json").write_text("{bad", encoding="utf-8")
        api = FakeApi()
        outcome = _deliver(tokens, api)
        assert (outcome.status, outcome.error) == (500, rc.RECORD_FAILED)
        assert tokens.tokens_for(ALICE) == [] and api.created == []

    def test_a_vanished_record_revokes_the_token_and_deletes_the_credential(self, tokens):
        api = FakeApi(on_create=_remove_the_record)
        wakes = []
        outcome = _deliver(tokens, api, on_abandoned=lambda: wakes.append(1))
        assert (outcome.status, outcome.error) == (502, rc.DELIVERY_FAILED)
        assert tokens.tokens_for(ALICE) == []
        assert api.deleted == [api.created[0]["credential_id"]]
        assert wakes == [1]

    def test_a_failed_credential_delete_is_logged_not_raised(self, tokens, caplog):
        api = FakeApi(
            on_create=_remove_the_record,
            delete_error=N8nApiClientError("n8n at n8n.example.com could not be reached."),
        )
        with caplog.at_level(logging.WARNING):
            outcome = _deliver(tokens, api)
        assert outcome.status == 502 and "Could not delete n8n credential" in caplog.text

    def test_an_unreadable_records_file_after_the_runtime_call_revokes_the_token(self, tmp_path, tokens):
        api = FakeApi(on_create=lambda: (tmp_path / "records.json").write_text("{bad", encoding="utf-8"))
        outcome = _deliver(tokens, api)
        assert outcome.status == 502 and tokens.tokens_for(ALICE) == []
        assert api.deleted == [api.created[0]["credential_id"]]


class _Stop(Exception):
    pass


def _raise_stop(*args, **kwargs):
    raise _Stop


class TestCrashPoints:
    """A process that dies mid-delivery leaves no live token without a record."""

    @staticmethod
    def _assert_no_unrecorded_live_token(tokens):
        recorded = {r.token_id for r in rc.get_store().all()}
        assert {t.id for t in tokens.tokens_for(ALICE)} <= recorded

    def test_stop_while_writing_the_record(self, tokens, monkeypatch):
        monkeypatch.setattr(rc.RuntimeCredentialStore, "add", _raise_stop)
        with pytest.raises(_Stop):
            _deliver(tokens, FakeApi())
        assert tokens.tokens_for(ALICE) == []
        self._assert_no_unrecorded_live_token(tokens)

    def test_stop_while_creating_the_token(self, tokens, monkeypatch):
        monkeypatch.setattr(tokens, "create", _raise_stop)
        with pytest.raises(_Stop):
            _deliver(tokens, FakeApi())
        (record,) = rc.get_store().all()
        assert record.state == rc.CREATING
        assert rc.get_store().is_in_flight(record.credential_id) is False
        self._assert_no_unrecorded_live_token(tokens)

    def test_stop_while_calling_the_runtime(self, tokens):
        api = FakeApi(create_error=_Stop())
        outcome = _deliver(tokens, api)
        assert outcome.status == 502
        self._assert_no_unrecorded_live_token(tokens)

    def test_stop_while_marking_the_record_delivered(self, tokens, monkeypatch):
        monkeypatch.setattr(rc.RuntimeCredentialStore, "set_state", _raise_stop)
        with pytest.raises(_Stop):
            _deliver(tokens, FakeApi())
        assert [r.state for r in rc.get_store().all()] == [rc.CREATING]
        self._assert_no_unrecorded_live_token(tokens)


class TestStatusFor:
    NOT_DELIVERED = {"state": "not_delivered"}

    @staticmethod
    def _status(tokens, proposal_id, *, registry=None, principal_id="alice", now=None):
        return rc.status_for(
            principal_id, proposal_id, token_store=tokens, registry=registry or stp.ProposalRegistry(), now=now,
        )

    def test_a_delivered_record_with_a_live_token_gives_the_credential(self, tokens):
        proposal = _proposal()
        outcome = _deliver(tokens, FakeApi(), proposal)

        answer = self._status(tokens, proposal.id)

        assert answer == {
            "state": "delivered", "runtime": "n8n", "credential_id": outcome.record.credential_id,
            "credential_name": "PrivacyFence Sales sync", "credential_type": "httpBearerAuth",
            "node_authentication": "bearerAuth", "mcp_url": "https://pf/mcp",
            "token_expires_at": datetime.fromtimestamp(outcome.token.expires_at, timezone.utc).isoformat(),
        }
        assert outcome.token.id not in json.dumps(answer)

    def test_a_creating_record_is_pending(self, tokens):
        rc.get_store().add(_record(proposal_id="stp-1"))
        assert self._status(tokens, "stp-1") == {"state": "pending"}

    def test_a_proposal_still_in_the_registry_is_pending_with_its_expiry(self, tokens):
        registry = stp.ProposalRegistry()
        proposal = _proposal()
        registry.add(proposal)

        answer = self._status(tokens, proposal.id, registry=registry)

        assert answer == {
            "state": "pending", "expires_at": datetime.fromtimestamp(proposal.expires_at, timezone.utc).isoformat(),
        }

    def test_a_creating_record_whose_proposal_is_in_the_registry_carries_the_expiry(self, tokens):
        registry = stp.ProposalRegistry()
        proposal = _proposal()
        registry.add(proposal)
        rc.get_store().add(_record(proposal_id=proposal.id))

        answer = self._status(tokens, proposal.id, registry=registry)

        assert answer["state"] == "pending" and "expires_at" in answer

    def test_another_persons_record_and_proposal_are_not_delivered(self, tokens):
        proposal = _proposal()
        outcome = _deliver(tokens, FakeApi(), proposal)
        registry = stp.ProposalRegistry()
        registry.add(proposal)
        assert outcome.status == 200

        assert self._status(tokens, proposal.id, registry=registry, principal_id="bob") == self.NOT_DELIVERED

    def test_an_abandoned_record_is_not_delivered(self, tokens):
        rc.get_store().add(_record(proposal_id="stp-1", state=rc.ABANDONED))
        assert self._status(tokens, "stp-1") == self.NOT_DELIVERED

    def test_an_abandoned_record_with_the_proposal_restored_is_pending(self, tokens):
        registry = stp.ProposalRegistry()
        proposal = _proposal()
        registry.add(proposal)
        rc.get_store().add(_record(proposal_id=proposal.id, state=rc.ABANDONED))

        answer = self._status(tokens, proposal.id, registry=registry)

        assert answer["state"] == "pending" and "expires_at" in answer

    def test_a_retry_after_an_abandoned_record_answers_from_the_newest_live_record(self, tokens):
        proposal = _proposal()
        rc.get_store().add(_record(proposal_id=proposal.id, credential_id="aaaaaaaaaaaaaaaa", state=rc.ABANDONED))
        outcome = _deliver(tokens, FakeApi(), proposal)

        answer = self._status(tokens, proposal.id)

        assert answer["state"] == "delivered" and answer["credential_id"] == outcome.record.credential_id
        assert outcome.token.id not in json.dumps(answer)

    def test_an_abandoned_record_after_a_creating_one_does_not_hide_it(self, tokens):
        rc.get_store().add(_record(credential_id="aaaaaaaaaaaaaaaa"))
        rc.get_store().add(_record(credential_id="bbbbbbbbbbbbbbbb", state=rc.ABANDONED))
        assert self._status(tokens, "stp-1") == {"state": "pending"}

    def test_an_expired_token_is_not_delivered(self, tokens):
        proposal = _proposal()
        outcome = _deliver(tokens, FakeApi(), proposal)
        assert self._status(tokens, proposal.id, now=outcome.token.expires_at + 1) == self.NOT_DELIVERED

    def test_a_revoked_token_is_not_delivered(self, tokens):
        proposal = _proposal()
        outcome = _deliver(tokens, FakeApi(), proposal)
        tokens.revoke(ALICE, outcome.token.id)
        assert self._status(tokens, proposal.id) == self.NOT_DELIVERED

    def test_an_unknown_id_is_not_delivered(self, tokens):
        assert self._status(tokens, "stp-nothing") == self.NOT_DELIVERED

    def test_a_token_that_vanishes_between_the_two_reads_is_not_delivered(self):
        rc.get_store().add(_record(proposal_id="stp-1", state=rc.DELIVERED))

        class Racing:
            def token_state(self, principal, token_id, now=None):
                return "live"

            def tokens_for(self, principal):
                return []

        assert self._status(Racing(), "stp-1") == self.NOT_DELIVERED
