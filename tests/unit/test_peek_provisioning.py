"""Peek auto-provisioning: the per-person flow, the single worker and the sweep."""
from __future__ import annotations

import json
import logging
import threading

import pytest

from privacyfence import org_mode, paths, peek_provisioning
from privacyfence.peek_client import (
    PeekClientError,
    PeekMintedToken,
    PeekProvisionedAccount,
    load_token_file,
    save_token_file,
)
from privacyfence.peek_provisioning import (
    INACTIVE_AFTER_SECONDS,
    PeekProvisioner,
    provision_principal,
)
from privacyfence.principal import Principal

BASE = "https://peek.example.com"
NOW = 1_800_000_000


class FakeProvisioningClient:
    host = "peek.example.com"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.upsert_error: PeekClientError | None = None
        self.mint_error: PeekClientError | None = None
        self.check_error: PeekClientError | None = None
        self.disabled = False
        self.has_token = False
        self.mint_hook = None

    def check(self) -> int:
        self.calls.append(("check",))
        if self.check_error:
            raise self.check_error
        return 3

    def upsert_account(self, external_id, email, name):
        self.calls.append(("upsert", external_id, email, name))
        if self.upsert_error:
            raise self.upsert_error
        return PeekProvisionedAccount(
            external_id=external_id, account_id=1, created=True, adopted=False,
            disabled=self.disabled, has_token=self.has_token, token_expires_at=0,
        )

    def mint_token(self, external_id, expires_days=90):
        self.calls.append(("mint", external_id, expires_days))
        if self.mint_hook:
            self.mint_hook()
        if self.mint_error:
            raise self.mint_error
        return PeekMintedToken(token="secret-token-value", token_id=7, expires_at=NOW + 90 * 86400)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return FakeProvisioningClient()


def _token_path(pid="alice"):
    return paths.user_dir(Principal(id=pid)) / "credentials" / "peek_token.json"


def _state(pid="alice"):
    return json.loads((paths.user_dir(Principal(id=pid)) / peek_provisioning.STATE_FILE).read_text())


def _run(client, pid="alice", email="a@x.com", name="Alice", on_change=None, base=BASE):
    changed: list[str] = []
    status = provision_principal(
        pid, email, name, client=client, base_url=base,
        on_change=on_change or changed.append, now=NOW,
    )
    return status, changed


def _config(trust=False):
    return org_mode.PeekProvisioningConfig(enabled=True, base_url=BASE, trust_unverified_email=trust)


def _provisioner(client, trust=False, **kw):
    return PeekProvisioner(
        client=client, config=_config(trust), on_change=kw.pop("on_change", lambda _p: None),
        stop_event=kw.pop("stop_event", threading.Event()), clock=lambda: NOW, **kw,
    )


class TestProvisionPrincipal:
    def test_no_email_writes_no_email(self, client):
        status, _ = _run(client, email="")
        assert status == "no_email"
        assert _state()["status"] == "no_email"
        assert client.calls == []

    def test_device_file_for_same_server_is_kept_and_peek_not_called(self, client):
        save_token_file(str(_token_path()), BASE, "mine")
        status, changed = _run(client)
        assert status == "user_connected"
        assert client.calls == []
        assert changed == []
        assert load_token_file(str(_token_path()))["token"] == "mine"

    def test_device_file_for_other_server_is_replaced(self, client):
        save_token_file(str(_token_path()), "https://other.example.com", "old")
        status, changed = _run(client)
        assert status == "provisioned"
        record = load_token_file(str(_token_path()))
        assert (record["token"], record["source"]) == ("secret-token-value", "provisioned")
        assert changed == ["alice"]

    def test_new_person_is_upserted_minted_saved_and_evicted(self, client):
        status, changed = _run(client)
        assert status == "provisioned"
        assert client.names() == ["upsert", "mint"]
        assert client.calls[1] == ("mint", "alice", 90)
        record = load_token_file(str(_token_path()))
        assert record["source"] == "provisioned"
        assert record["expires_at"] == str(NOW + 90 * 86400)
        assert changed == ["alice"]
        state = _state()
        assert (state["email"], state["display_name"], state["status"]) == ("a@x.com", "Alice", "provisioned")

    @pytest.mark.parametrize("display", ["a@x.com", "alice"])
    def test_name_is_empty_when_display_name_is_email_or_id(self, client, display):
        _run(client, name=display)
        assert client.calls[0] == ("upsert", "alice", "a@x.com", "")

    def test_name_is_cut_to_100_characters(self, client):
        _run(client, name="n" * 150)
        assert client.calls[0][3] == "n" * 100

    def _provisioned_file(self, expires_at):
        save_token_file(str(_token_path()), BASE, "tok", source="provisioned", expires_at=expires_at)

    def test_fresh_provisioned_token_is_not_reminted(self, client):
        self._provisioned_file(NOW + 60 * 86400)
        client.has_token = True
        status, changed = _run(client)
        assert status == "provisioned"
        assert client.names() == ["upsert"]
        assert changed == []

    def test_token_within_30_days_is_reminted(self, client):
        self._provisioned_file(NOW + 10 * 86400)
        client.has_token = True
        _run(client)
        assert client.names() == ["upsert", "mint"]

    def test_has_token_false_is_reminted(self, client):
        self._provisioned_file(NOW + 60 * 86400)
        _run(client)
        assert client.names() == ["upsert", "mint"]

    def test_disabled_account_is_not_minted(self, client):
        client.disabled = True
        status, _ = _run(client)
        assert status == "disabled"
        assert client.names() == ["upsert"]

    def test_admin_409_writes_admin_and_removes_provisioned_file(self, client):
        save_token_file(str(_token_path()), BASE, "tok", source="provisioned", expires_at=NOW)
        client.upsert_error = PeekClientError("x", status=409, detail="account is a Peek admin; connect it by hand")
        status, changed = _run(client)
        assert status == "admin"
        assert not _token_path().exists()
        assert changed == ["alice"]

    def test_admin_409_without_provisioned_file_does_not_evict(self, client):
        client.upsert_error = PeekClientError("x", status=409, detail="account is a Peek admin; connect it by hand")
        status, changed = _run(client)
        assert status == "admin"
        assert changed == []

    def test_admin_403_on_promoted_account_writes_admin(self, client):
        save_token_file(str(_token_path()), BASE, "tok", source="provisioned", expires_at=NOW)
        client.mint_error = PeekClientError("x", status=403, detail="account is a Peek admin; connect it by hand")
        assert _run(client)[0] == "admin"
        assert not _token_path().exists()

    def test_provisioned_file_vanishing_before_delete_is_tolerated(self, client, monkeypatch):
        save_token_file(str(_token_path()), BASE, "tok", source="provisioned", expires_at=NOW)
        client.upsert_error = PeekClientError("x", status=409, detail="account is a Peek admin; x")
        real = peek_provisioning._token_record

        calls = []

        def record_then_remove(pid):
            record = real(pid)
            calls.append(pid)
            if len(calls) == 2:  # the read inside the delete: the file goes before the unlink
                _token_path(pid).unlink(missing_ok=True)
            return record

        monkeypatch.setattr(peek_provisioning, "_token_record", record_then_remove)
        status, changed = _run(client)
        assert status == "admin"
        assert changed == []

    def test_other_error_writes_error_with_message(self, client):
        client.upsert_error = PeekClientError("Peek is down", status=500, detail="boom")
        status, _ = _run(client)
        assert status == "error"
        assert _state()["detail"] == "Peek is down"

    def test_non_admin_409_is_an_error(self, client):
        client.upsert_error = PeekClientError("m", status=409, detail="account is linked to another external id")
        assert _run(client)[0] == "error"

    def test_device_sign_in_during_mint_wins(self, client):
        client.mint_hook = lambda: save_token_file(str(_token_path()), BASE, "mine")
        status, changed = _run(client)
        assert status == "user_connected"
        assert load_token_file(str(_token_path()))["token"] == "mine"
        assert changed == []

    def test_log_lines_carry_no_email_or_token(self, client, caplog):
        caplog.set_level(logging.DEBUG)
        _run(client, email="private@x.com", name="Private Name")
        client.upsert_error = PeekClientError("Peek is down", status=500)
        _run(client, email="private@x.com", name="Private Name")
        text = caplog.text
        assert "private@x.com" not in text and "Private Name" not in text and "secret-token-value" not in text
        assert "Peek provisioning for alice: provisioned" in text
        assert "Peek provisioning for alice failed: Peek is down" in text


class TestRequest:
    def test_request_unverified_email_is_not_queued(self, client):
        p = _provisioner(client)
        p.request(Principal(id="alice", email="a@x.com"), email_verified=False)
        assert _state()["status"] == "unverified_email"
        assert p.process_next(0) is False

    def test_request_unverified_email_is_queued_when_trusted(self, client):
        p = _provisioner(client, trust=True)
        p.request(Principal(id="alice", email="a@x.com"), email_verified=False)
        assert p.process_next(0) is True
        assert _state()["status"] == "provisioned"

    def test_request_without_email_is_recorded_not_queued(self, client):
        p = _provisioner(client)
        p.request(Principal(id="alice"), email_verified=True)
        assert _state()["status"] == "no_email"
        assert p.process_next(0) is False

    def test_request_records_last_sign_in(self, client):
        p = _provisioner(client)
        p.request(Principal(id="alice", email="a@x.com", display_name="Alice"), email_verified=True)
        state = _state()
        assert (state["last_sign_in"], state["email"], state["display_name"]) == (NOW, "a@x.com", "Alice")

    def test_request_is_deduplicated_while_queued(self, client):
        p = _provisioner(client)
        who = Principal(id="alice", email="a@x.com")
        p.request(who, email_verified=True)
        p.request(who, email_verified=True)
        assert p.process_next(0) is True
        assert p.process_next(0) is False
        assert client.names().count("upsert") == 1

    def test_full_queue_drops_with_warning(self, client, monkeypatch, caplog):
        monkeypatch.setattr(peek_provisioning, "QUEUE_MAX", 1)
        p = _provisioner(client)
        p.request(Principal(id="alice", email="a@x.com"), email_verified=True)
        p.request(Principal(id="bob", email="b@x.com"), email_verified=True)
        assert "Peek provisioning queue is full; bob waits for the next sweep or sign-in" in caplog.text
        assert p.process_next(0) is True
        assert p.process_next(0) is False


class TestProcessNext:
    def test_process_next_returns_false_on_empty_queue(self, client):
        assert _provisioner(client).process_next(0) is False

    def test_process_next_paces_with_stop_event_wait(self, client):
        waits: list[float] = []
        stop = threading.Event()
        stop.wait = waits.append  # type: ignore[method-assign]
        p = _provisioner(client, stop_event=stop)
        p.request(Principal(id="alice", email="a@x.com"), email_verified=True)
        assert p.process_next(0) is True
        assert waits == [peek_provisioning.PACE_SECONDS]

    def test_process_next_logs_unexpected_errors(self, client, caplog):
        p = _provisioner(client, on_change=lambda _p: (_ for _ in ()).throw(RuntimeError("evict broke")))
        p.request(Principal(id="alice", email="a@x.com"), email_verified=True)
        # provision_principal swallows only PeekClientError, so a failing on_change reaches the worker guard
        assert p.process_next(0) is True
        assert "Peek provisioning worker error: evict broke" in caplog.text


def _write_state(tmp_path, pid, status, last_sign_in):
    d = tmp_path / "users" / pid / "credentials"
    d.mkdir(parents=True)
    (d / "peek_provisioning.json").write_text(json.dumps({
        "email": f"{pid}@x.com", "display_name": "", "status": status, "detail": "",
        "last_sign_in": last_sign_in, "updated_at": 0,
    }))


class TestSweep:
    def test_sweep_enqueues_recent_provisioned_and_error_only(self, client, data_dir):
        recent = NOW - 86400
        for pid, status in [
            ("p-ok", "provisioned"), ("p-err", "error"), ("p-admin", "admin"), ("p-dis", "disabled"),
            ("p-user", "user_connected"), ("p-noem", "no_email"), ("p-unv", "unverified_email"),
            ("p-rel", "released"),
        ]:
            _write_state(data_dir, pid, status, recent)
        _write_state(data_dir, "p-old", "provisioned", NOW - INACTIVE_AFTER_SECONDS - 1)
        (data_dir / "users" / "bad id").mkdir()
        (data_dir / "users" / "stray-file").write_text("x")
        p = _provisioner(client)
        assert p.sweep_once() == 2
        assert p.sweep_once() == 0
        seen = []
        while p.process_next(0):
            seen.append(client.calls[-1][1] if client.calls else None)
        assert sorted({c[1] for c in client.calls if c[0] == "upsert"}) == ["p-err", "p-ok"]

    def test_sweep_without_users_dir_enqueues_nothing(self, client):
        assert _provisioner(client).sweep_once() == 0

    def test_sweep_skips_a_state_without_numeric_last_sign_in(self, client, data_dir):
        _write_state(data_dir, "p-ok", "provisioned", "never")
        assert _provisioner(client).sweep_once() == 0


class TestStart:
    def test_start_returns_none_when_disabled(self):
        assert peek_provisioning.start({}, on_change=lambda _p: None, stop_event=threading.Event()) is None
        assert peek_provisioning._INSTANCE is None

    def _org(self):
        return {"mode": "org", "peek": {"base_url": BASE, "auto_provision": {"enabled": True}}}

    def test_start_returns_none_and_warns_without_key(self, monkeypatch, caplog):
        monkeypatch.delenv("PRIVACYFENCE_PEEK_PROVISIONING_KEY", raising=False)
        assert peek_provisioning.start(self._org(), on_change=lambda _p: None, stop_event=threading.Event()) is None
        assert (
            "Peek auto-provisioning is on, but PRIVACYFENCE_PEEK_PROVISIONING_KEY is not set; "
            "people connect Peek by hand on /connect."
        ) in caplog.text

    def test_start_runs_threads_and_sets_instance(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_PEEK_PROVISIONING_KEY", " peekprov_x ")
        stop = threading.Event()
        stop.set()
        threads: list[threading.Thread] = []
        real_start = threading.Thread.start
        monkeypatch.setattr(
            threading.Thread, "start", lambda self: (threads.append(self), real_start(self))[1]
        )
        monkeypatch.setattr("privacyfence.peek_provisioning.PeekProvisioningClient.check", lambda self: 0)
        provisioner = peek_provisioning.start(self._org(), on_change=lambda _p: None, stop_event=stop)
        assert provisioner is peek_provisioning._INSTANCE
        for t in threads:
            t.join(2)
        assert {t.name for t in threads} == {"peek-provisioning", "peek-provisioning-sweep"}
        assert all(t.daemon and not t.is_alive() for t in threads)

    def test_start_logs_ready_and_check_failed(self, client, caplog):
        caplog.set_level(logging.INFO)
        stop = threading.Event()
        stop.set()
        p = _provisioner(client, stop_event=stop)
        p._sweep_loop()
        assert "Peek provisioning ready at peek.example.com (3 accounts managed)" in caplog.text
        client.check_error = PeekClientError("Peek refused the provisioning key (HTTP 401).", status=401)
        p._sweep_loop()
        assert "Peek provisioning check failed: Peek refused the provisioning key (HTTP 401)." in caplog.text

    def test_sweep_loop_survives_a_failing_sweep(self, client, monkeypatch, caplog):
        stop = threading.Event()
        stop.set()
        p = _provisioner(client, stop_event=stop)
        monkeypatch.setattr(p, "sweep_once", lambda: (_ for _ in ()).throw(RuntimeError("disk")))
        p._sweep_loop()
        assert "Peek provisioning sweep failed (will retry): disk" in caplog.text

    def test_sweep_loop_repeats_until_stopped(self, client):
        stop = threading.Event()
        answers = iter([False, True])
        stop.wait = lambda timeout: next(answers)  # type: ignore[method-assign]
        p = _provisioner(client, stop_event=stop)
        sweeps = []
        p.sweep_once = lambda: sweeps.append(1) or 0  # type: ignore[method-assign]
        p._sweep_loop()
        assert len(sweeps) == 2

    def test_worker_waits_for_the_check_then_processes_until_stopped(self, client):
        stop = threading.Event()
        p = _provisioner(client, stop_event=stop)
        p.request(Principal(id="alice", email="a@x.com"), email_verified=True)
        done = threading.Event()
        original = p.process_next

        def process_and_stop(timeout):
            result = original(timeout)
            done.set()
            stop.set()
            return result

        p.process_next = process_and_stop  # type: ignore[method-assign]
        thread = threading.Thread(target=p._work_loop, daemon=True)
        thread.start()
        thread.join(0.1)
        assert thread.is_alive() is True or done.is_set() is False  # blocked on the check
        assert client.calls == []
        p._checked.set()
        thread.join(3)
        assert done.is_set() and not thread.is_alive()

    def test_worker_loop_exits_when_stopped_before_the_check(self, client):
        stop = threading.Event()
        p = _provisioner(client, stop_event=stop)
        thread = threading.Thread(target=p._work_loop, daemon=True)
        thread.start()
        thread.join(0.05)
        stop.set()
        thread.join(3)
        assert not thread.is_alive()


class TestModuleFunctions:
    def test_notify_sign_in_is_noop_without_instance(self):
        peek_provisioning.notify_sign_in(Principal(id="alice", email="a@x.com"), email_verified=True)

    def test_notify_sign_in_calls_the_instance(self, client):
        p = _provisioner(client)
        peek_provisioning._INSTANCE = p
        peek_provisioning.notify_sign_in(Principal(id="alice", email="a@x.com"), email_verified=True)
        assert p.process_next(0) is True

    def test_notify_sign_in_never_raises(self, caplog):
        class Broken:
            def request(self, *a, **k):
                raise RuntimeError("nope")

        peek_provisioning._INSTANCE = Broken()  # type: ignore[assignment]
        peek_provisioning.notify_sign_in(Principal(id="alice", email="a@x.com"), email_verified=True)
        assert "Peek provisioning could not take the sign-in of alice: nope" in caplog.text

    @pytest.mark.parametrize(
        "claims,expected",
        [
            ({"email_verified": True}, True),
            ({"email_verified": "true"}, True),
            ({"email_verified": False}, False),
            ({"email_verified": "false"}, False),
            ({}, False),
            ({"email_verified": 1}, False),
        ],
    )
    def test_email_verified_claim(self, claims, expected):
        assert peek_provisioning.email_verified_claim(claims) is expected

    def test_release_removes_provisioned_file_keeps_device_file_and_clears_email(self, client):
        _run(client)
        assert _token_path().exists()
        peek_provisioning.release("alice")
        assert not _token_path().exists()
        state = _state()
        assert (state["status"], state["email"]) == ("released", "")
        save_token_file(str(_token_path()), BASE, "mine")
        peek_provisioning.release("alice")
        assert load_token_file(str(_token_path()))["token"] == "mine"

    def test_principal_lock_is_one_lock_per_id(self):
        assert peek_provisioning.principal_lock("a") is peek_provisioning.principal_lock("a")
        assert peek_provisioning.principal_lock("a") is not peek_provisioning.principal_lock("b")

    def test_unreadable_state_file_reads_as_empty(self, data_dir):
        d = data_dir / "users" / "alice" / "credentials"
        d.mkdir(parents=True)
        (d / "peek_provisioning.json").write_text("[1]")
        assert peek_provisioning._read_state("alice") == {}
        (d / "peek_provisioning.json").write_text("{")
        assert peek_provisioning._read_state("alice") == {}
