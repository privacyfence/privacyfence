"""runtime_credential_sweeper: the rules for what the cleanup deletes and when, and its thread."""
from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from privacyfence import agent_runtimes, paths, runtime_credential_sweeper as sw, runtime_credentials as rc
from privacyfence import scoped_tokens
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.n8n_api import N8nApiClientError
from privacyfence.principal import Principal, principal_scope
from privacyfence.scoped_tokens import ScopedTokenPolicy, ScopedTokenStore

ALICE = Principal(id="alice", email="alice@example.com")
ENABLED = ScopedTokenPolicy(enabled=True, max_days=365)
CONFIG = agent_runtimes.N8nRuntimeConfig(
    enabled=True, base_url="https://n8n.example.com", project_id="", mcp_url="",
)
NOW = 5000.0
LATER = time.time() + 40 * 86400  # past a 30-day token's expiry


class FakeApi:
    def __init__(self, delete_error=None, check_error=None):
        self.delete_error, self.check_error = delete_error, check_error
        self.deleted: list[str] = []
        self.checks = 0
        self.checked_types: list[str] = []
        self.deleted_event = threading.Event()

    def check_type(self, credential_type):
        self.checks += 1
        self.checked_types.append(credential_type)
        if self.check_error is not None:
            raise self.check_error

    def delete_credential(self, credential_id):
        self.deleted.append(credential_id)
        self.deleted_event.set()
        if self.delete_error is not None:
            raise self.delete_error
        return True


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    with principal_scope(ALICE):
        init_audit_logger(str(tmp_path / "audit"))
    rc.set_store(rc.RuntimeCredentialStore(tmp_path / "records.json"))
    scoped_tokens.set_store(None)


@pytest.fixture
def tokens():
    return ScopedTokenStore(lambda: [ALICE])


@pytest.fixture
def records():
    return rc.get_store()


@pytest.fixture
def api():
    return FakeApi()


def _entries(tmp_path):
    path = tmp_path / "audit" / f"{current_week()}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _decisions(tmp_path):
    return [e["decision"] for e in _entries(tmp_path)]


def _token(tokens, token_id="st-aaaaaaaaaaaa"):
    tokens.create(
        ALICE, label="Sales sync", days=30, rules=[{"kind": "new", "group": "drive.folder", "value": "F1", "verbs": ["read"]}],
        tools=["drive_download_file"], policy=ENABLED, allow_personal_data=False,
        allow_personal_data_reads=False, token_id=token_id,
    )
    return token_id


def _record(records, **over):
    base = dict(
        principal_id="alice", token_id="st-aaaaaaaaaaaa", proposal_id="stp-1", runtime="n8n",
        endpoint=("https", "n8n.example.com", 443), project_id="", credential_id="0123456789abcdef",
        credential_name="PrivacyFence Sales sync", workflow_id="", mcp_url="https://pf/mcp",
        state=rc.DELIVERED, created_at=1000.0, boot_id="boot",
    )
    base.update(over)
    record = rc.RuntimeCredential(**base)
    records.add(record)
    return record


def _sweep(records, tokens, api, now=NOW):
    sw.sweep_once(records=records, token_store=tokens, config=CONFIG, api=api, now=now)


class TestRules:
    def test_endpoint_moved_revokes_audits_and_never_deletes(self, tmp_path, records, tokens, api):
        _token(tokens)
        _record(records, endpoint=("https", "old.example.com", 443))
        _sweep(records, tokens, api)
        assert tokens.token_state(ALICE, "st-aaaaaaaaaaaa") == "absent"
        assert records.all() == []
        assert api.deleted == []
        entries = _entries(tmp_path)
        assert [e["decision"] for e in entries] == ["runtime_credential_orphaned"]
        assert "old.example.com" in entries[0]["summary"]

    def test_endpoint_moved_with_an_absent_token_still_audits(self, tmp_path, records, tokens, api):
        _record(records, endpoint=("https", "old.example.com", 443))
        _sweep(records, tokens, None)
        assert records.all() == []
        assert _decisions(tmp_path) == ["runtime_credential_orphaned"]

    def test_endpoint_moved_with_an_unreadable_token_file_waits(self, records, tokens, api, monkeypatch):
        _record(records, endpoint=("https", "old.example.com", 443))
        monkeypatch.setattr(tokens, "token_state", lambda *a, **k: "unknown")
        _sweep(records, tokens, api)
        assert len(records.all()) == 1

    def test_endpoint_moved_with_a_revoke_that_raises_keeps_the_record(self, tmp_path, records, tokens, api, monkeypatch):
        _token(tokens)
        _record(records, endpoint=("https", "old.example.com", 443))
        monkeypatch.setattr(tokens, "revoke", lambda *a: (_ for _ in ()).throw(OSError("disk")))
        _sweep(records, tokens, api)
        assert len(records.all()) == 1
        assert _decisions(tmp_path) == []

    def test_no_api_removes_nothing_and_warns_once(self, records, tokens, caplog):
        _record(records, state=rc.ABANDONED)
        _record(records, credential_id="fedcba9876543210", proposal_id="stp-2", token_id="st-bbbbbbbbbbbb")
        with caplog.at_level(logging.WARNING):
            _sweep(records, tokens, None)
        assert len(records.all()) == 2
        assert sum("API key" in m for m in caplog.messages) == 1

    def test_delivered_and_live_is_kept(self, tmp_path, records, tokens, api):
        _token(tokens)
        _record(records)
        _sweep(records, tokens, api)
        assert len(records.all()) == 1
        assert api.deleted == []
        assert _decisions(tmp_path) == []

    def test_delivered_and_absent_is_deleted_as_revoked(self, tmp_path, records, tokens, api):
        _record(records)
        _sweep(records, tokens, api)
        assert records.all() == []
        assert api.deleted == ["0123456789abcdef"]
        entries = _entries(tmp_path)
        assert entries[0]["decision"] == "runtime_credential_deleted"
        assert entries[0]["summary"].endswith("token revoked")

    def test_delivered_and_expired_is_deleted_as_expired(self, tmp_path, records, tokens, api):
        _token(tokens)
        _record(records)
        _sweep(records, tokens, api, now=LATER)
        assert records.all() == []
        assert _entries(tmp_path)[0]["summary"].endswith("token expired")

    def test_delivered_with_an_unknown_token_state_is_skipped(self, records, tokens, api, monkeypatch):
        _record(records)
        monkeypatch.setattr(tokens, "token_state", lambda *a, **k: "unknown")
        _sweep(records, tokens, api)
        assert len(records.all()) == 1
        assert api.deleted == []

    def test_creating_in_flight_is_skipped(self, records, tokens, api):
        _record(records, state=rc.CREATING, boot_id=rc.BOOT_ID)
        with records.in_flight("0123456789abcdef"):
            _sweep(records, tokens, api)
        assert records.all()[0].state == rc.CREATING
        assert api.deleted == []

    def test_creating_from_an_earlier_boot_is_revoked_and_deleted(self, tmp_path, records, tokens, api):
        _token(tokens)
        _record(records, state=rc.CREATING)
        _sweep(records, tokens, api)
        assert tokens.token_state(ALICE, "st-aaaaaaaaaaaa") == "absent"
        assert records.all() == []
        assert api.deleted == ["0123456789abcdef"]
        assert _entries(tmp_path)[0]["summary"].endswith("token not delivered")

    def test_a_record_that_turns_delivered_meanwhile_is_left_alone(self, records, tokens, api, monkeypatch):
        _token(tokens)
        _record(records, state=rc.CREATING)
        real = records.is_in_flight

        def finish_first(credential_id):
            records.set_state(credential_id, rc.CREATING, rc.DELIVERED)
            return real(credential_id)

        monkeypatch.setattr(records, "is_in_flight", finish_first)
        _sweep(records, tokens, api)
        assert records.all()[0].state == rc.DELIVERED
        assert tokens.token_state(ALICE, "st-aaaaaaaaaaaa") == "live"
        assert api.deleted == []

    def test_abandoned_with_a_revoke_that_raises_is_kept(self, records, tokens, api, monkeypatch):
        _token(tokens)
        _record(records, state=rc.ABANDONED)
        monkeypatch.setattr(tokens, "revoke", lambda *a: (_ for _ in ()).throw(OSError("disk")))
        _sweep(records, tokens, api)
        assert len(records.all()) == 1
        assert api.deleted == []

    def test_abandoned_with_an_unknown_token_state_is_kept(self, records, tokens, api, monkeypatch):
        _record(records, state=rc.ABANDONED)
        monkeypatch.setattr(tokens, "token_state", lambda *a, **k: "unknown")
        _sweep(records, tokens, api)
        assert len(records.all()) == 1

    def test_an_unreadable_records_file_is_skipped_per_record(self, records, tokens, api, monkeypatch, caplog):
        _record(records, state=rc.ABANDONED)
        monkeypatch.setattr(
            records, "remove", lambda *a: (_ for _ in ()).throw(rc.RuntimeCredentialStoreError("cannot read")),
        )
        with caplog.at_level(logging.WARNING):
            _sweep(records, tokens, api)
        assert any("skipped" in m for m in caplog.messages)


class TestOneBadRecord:
    def test_a_malformed_credential_id_does_not_stop_the_other_records(self, records, tokens, api, caplog):
        real_delete = api.delete_credential

        def delete_like_the_client(credential_id):
            if credential_id != "fedcba9876543210":
                raise ValueError("Not a PrivacyFence credential id.")
            return real_delete(credential_id)

        api.delete_credential = delete_like_the_client
        _record(records, credential_id="not a credential id", proposal_id="stp-1")
        _record(records, credential_id="fedcba9876543210", proposal_id="stp-2", token_id="st-bbbbbbbbbbbb")
        with caplog.at_level(logging.WARNING):
            _sweep(records, tokens, api)
        assert api.deleted == ["fedcba9876543210"]
        assert [r.credential_id for r in records.all()] == ["not a credential id"]
        assert any("skipped a record: ValueError" in m for m in caplog.messages)
        assert "not a credential id" not in caplog.text


class TestDeleteFailures:
    def test_record_kept_and_audited_once_within_a_day(self, tmp_path, records, tokens):
        failing = FakeApi(delete_error=N8nApiClientError("n8n said no"))
        _record(records)
        _sweep(records, tokens, failing, now=NOW)
        _sweep(records, tokens, failing, now=NOW + 3600)
        assert len(records.all()) == 1
        assert failing.deleted == ["0123456789abcdef"] * 2
        entries = _entries(tmp_path)
        assert [e["decision"] for e in entries] == ["runtime_credential_delete_failed"]
        assert "n8n said no" in entries[0]["summary"]

    def test_audited_again_after_a_day(self, tmp_path, records, tokens):
        failing = FakeApi(delete_error=N8nApiClientError("n8n said no"))
        _record(records)
        _sweep(records, tokens, failing, now=NOW)
        _sweep(records, tokens, failing, now=NOW + sw.DELETE_FAILURE_AUDIT_SECONDS)
        assert _decisions(tmp_path) == ["runtime_credential_delete_failed"] * 2


def _wait(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def thread_env(tmp_path, tokens, api, monkeypatch):
    scoped_tokens.set_store(tokens)
    monkeypatch.setattr(agent_runtimes, "api_for", lambda config: api)
    monkeypatch.setattr(sw, "_POLL_SECONDS", 0.02)
    stop_event = threading.Event()
    yield stop_event
    stop_event.set()
    sw.stop()


class TestThread:
    def test_wake_without_a_sweeper_is_a_no_op(self):
        sw.wake()

    def test_no_api_starts_nothing(self, monkeypatch):
        monkeypatch.setattr(agent_runtimes, "api_for", lambda config: None)
        sw.start(config=CONFIG, stop_event=threading.Event())
        assert sw._INSTANCE is None

    def test_first_sweep_runs_and_wake_runs_another(self, records, api, thread_env):
        _record(records)
        sw.start(config=CONFIG, stop_event=thread_env)
        assert _wait(api.deleted_event.is_set)
        assert api.checks == 1
        api.deleted_event.clear()
        _record(records, credential_id="fedcba9876543210", proposal_id="stp-2", token_id="st-bbbbbbbbbbbb")
        sw.wake()
        assert _wait(api.deleted_event.is_set)
        assert api.deleted == ["0123456789abcdef", "fedcba9876543210"]

    def test_the_start_up_check_uses_the_configured_type(self, records, api, thread_env):
        config = agent_runtimes.N8nRuntimeConfig(
            enabled=True, base_url="https://n8n.example.com", project_id="", mcp_url="https://pf/mcp",
            credential_type="privacyFenceApi",
        )
        _record(records)
        sw.start(config=config, stop_event=thread_env)
        assert _wait(api.deleted_event.is_set)
        assert api.checked_types == ["privacyFenceApi"]

    def test_the_default_start_up_check_is_the_bearer_type(self, records, api, thread_env):
        _record(records)
        sw.start(config=CONFIG, stop_event=thread_env)
        assert _wait(api.deleted_event.is_set)
        assert api.checked_types == ["httpBearerAuth"]

    def test_a_failed_check_warns_and_the_thread_goes_on(self, records, tokens, thread_env, monkeypatch, caplog):
        failing = FakeApi(check_error=N8nApiClientError("down"))
        monkeypatch.setattr(agent_runtimes, "api_for", lambda config: failing)
        _record(records)
        with caplog.at_level(logging.WARNING):
            sw.start(config=CONFIG, stop_event=thread_env)
            assert _wait(failing.deleted_event.is_set)
        assert any("check failed" in m for m in caplog.messages)

    def test_a_sweep_that_raises_does_not_end_the_thread(self, records, api, thread_env, monkeypatch):
        calls = []

        def boom(**kwargs):
            calls.append(1)
            raise RuntimeError("bug")

        monkeypatch.setattr(sw, "sweep_once", boom)
        sw.start(config=CONFIG, stop_event=thread_env)
        assert _wait(lambda: calls)
        sw.wake()
        assert _wait(lambda: len(calls) >= 2)

    def test_the_stop_event_ends_the_thread(self, thread_env):
        sw.start(config=CONFIG, stop_event=thread_env)
        thread = sw._INSTANCE.thread
        thread_env.set()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_stop_ends_the_thread_and_start_replaces_a_running_one(self, thread_env):
        sw.start(config=CONFIG, stop_event=thread_env)
        first = sw._INSTANCE.thread
        sw.start(config=CONFIG, stop_event=thread_env)
        assert not first.is_alive()
        second = sw._INSTANCE.thread
        sw.stop()
        assert not second.is_alive()
        assert sw._INSTANCE is None

    def test_stop_from_the_sweeper_thread_does_not_join_itself(self):
        sweeper = sw._Sweeper(threading.current_thread())
        sw._INSTANCE = sweeper
        sw.stop()
        assert sweeper.stopped.is_set()
