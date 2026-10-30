"""grist_provisioning: the per-person flow, the single worker and the sweep. No network."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from privacyfence import grist_provisioning as gp
from privacyfence import paths
from privacyfence.grist_auth import key_id, save_api_key
from privacyfence.grist_provisioner_api import (
    GristIssuedKey,
    GristProvisionedAccount,
    GristProvisionerClientError,
)
from privacyfence.org_mode import GristProvisioningConfig
from privacyfence.principal import Principal

SERVER = "https://grist.example.com"
PROVISIONER = "https://prov.example.com"
NOW = 1_800_000_000.0
EMAIL = "alice@example.com"
KEY = "k" * 40


def make_config(**overrides: Any) -> GristProvisioningConfig:
    values: dict[str, Any] = {"enabled": True, "provisioner_url": PROVISIONER, "server_url": SERVER}
    values.update(overrides)
    return GristProvisioningConfig(**values)


def account(**overrides: Any) -> GristProvisionedAccount:
    values: dict[str, Any] = {
        "external_id": "alice", "user_id": 7, "disabled": False, "has_key": False,
        "managed": False, "key_id": "", "expires_at": 0,
    }
    values.update(overrides)
    return GristProvisionedAccount(**values)


def issued(**overrides: Any) -> GristIssuedKey:
    values: dict[str, Any] = {"key": KEY, "managed": True, "key_id": key_id(KEY), "expires_at": int(NOW) + 90 * 86400}
    values.update(overrides)
    return GristIssuedKey(**values)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.account = account()
        self.key = issued()
        self.upsert_error: GristProvisionerClientError | None = None
        self.issue_error: GristProvisionerClientError | None = None
        self.on_issue: Any = None
        self.check_result: int | GristProvisionerClientError = 3
        self.release_error: GristProvisionerClientError | None = None

    def upsert_account(self, external_id: str, email: str) -> GristProvisionedAccount:
        self.calls.append(("upsert", external_id, email))
        if self.upsert_error:
            raise self.upsert_error
        return self.account

    def issue_key(self, external_id: str) -> GristIssuedKey:
        self.calls.append(("issue", external_id))
        if self.on_issue:
            self.on_issue()
        if self.issue_error:
            raise self.issue_error
        return self.key

    def release_account(self, external_id: str) -> Any:
        self.calls.append(("release", external_id))
        if self.release_error:
            raise self.release_error

    def check(self) -> int:
        self.calls.append(("check",))
        if isinstance(self.check_result, GristProvisionerClientError):
            raise self.check_result
        return self.check_result


@pytest.fixture
def data(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def changes() -> list[str]:
    return []


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def user_path(name: str = "alice") -> Path:
    return paths.user_dir(Principal(id=name))


def token_path(name: str = "alice") -> Path:
    return user_path(name) / gp.TOKEN_FILE


def state_of(name: str = "alice") -> dict[str, Any]:
    return json.loads((user_path(name) / gp.STATE_FILE).read_text())


def write_state(name: str = "alice", **fields: Any) -> None:
    state = {"email": EMAIL, "status": "pending", "detail": "", "last_sign_in": int(NOW), "updated_at": int(NOW)}
    state.update(fields)
    path = user_path(name) / gp.STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def write_token(kind: str, name: str = "alice", *, server: str = SERVER, key: str = KEY, **kw: Any) -> None:
    path = token_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "manual":
        save_api_key(str(path), server, key)
    else:
        save_api_key(str(path), server, key, source="provisioned", **kw)


def run(client: FakeClient, changes: list[str], *, config: GristProvisioningConfig | None = None,
        now: float = NOW, name: str = "alice") -> str:
    return gp.provision_principal(
        name, client=client, config=config or make_config(), on_change=changes.append, now=now,  # type: ignore[arg-type]
    )


def make_provisioner(client: FakeClient, changes: list[str], *, stop: threading.Event | None = None,
                     config: GristProvisioningConfig | None = None, **kw: Any) -> gp.GristProvisioner:
    return gp.GristProvisioner(
        client=client, config=config or make_config(), on_change=changes.append,  # type: ignore[arg-type]
        stop_event=stop or threading.Event(), clock=lambda: NOW, **kw,
    )


class TestProvisionPrincipal:
    def test_no_email_writes_no_email(self, data, client, changes):
        write_state(email="")
        assert run(client, changes) == "no_email"
        assert state_of()["status"] == "no_email"
        assert client.calls == []

    def test_released_status_skips_provisioning(self, data, client, changes):
        write_state(status="released", email="")
        assert run(client, changes) == "released"
        assert client.calls == []
        assert state_of()["status"] == "released"

    def test_update_state_keeps_other_fields_and_sets_updated_at(self, data):
        write_state(last_sign_in=5)
        state = gp._update_state("alice", now=1234.9, status="adopted", detail="x")
        assert state["email"] == EMAIL and state["last_sign_in"] == 5
        assert state["status"] == "adopted" and state["updated_at"] == 1234
        assert state_of() == state

    def test_unreadable_or_non_object_state_counts_as_empty(self, data):
        assert gp._read_state("alice") == {}
        (user_path() / gp.STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        (user_path() / gp.STATE_FILE).write_text("[1]")
        assert gp._read_state("alice") == {}
        (user_path() / gp.STATE_FILE).write_text("{nope")
        assert gp._update_state("alice", now=NOW, status="x")["status"] == "x"

    def test_manual_file_for_same_server_is_kept_and_provisioner_not_called(self, data, client, changes):
        write_state()
        write_token("manual", key="mine")
        assert run(client, changes) == "user_connected"
        assert client.calls == []
        assert json.loads(token_path().read_text())["api_key"] == "mine"
        assert state_of()["status"] == "user_connected"

    def test_manual_file_for_other_server_is_replaced(self, data, client, changes):
        write_state()
        write_token("manual", server="https://other.example.com", key="mine")
        assert run(client, changes) == "provisioned"
        assert json.loads(token_path().read_text())["api_key"] == KEY

    def test_new_key_is_issued_saved_managed_and_evicted(self, data, client, changes):
        write_state()
        assert run(client, changes) == "provisioned"
        saved = json.loads(token_path().read_text())
        assert saved == {
            "auth": "api_key", "server_url": SERVER, "api_key": KEY, "source": "provisioned",
            "managed": True, "expires_at": int(NOW) + 90 * 86400,
        }
        assert changes == ["alice"]
        assert client.calls == [("upsert", "alice", EMAIL), ("issue", "alice")]
        state = state_of()
        assert state["status"] == "provisioned" and state["email"] == EMAIL and state["last_sign_in"] == int(NOW)

    def test_existing_key_is_adopted(self, data, client, changes):
        write_state()
        client.key = issued(managed=False, expires_at=0)
        assert run(client, changes) == "adopted"
        assert json.loads(token_path().read_text())["managed"] is False
        assert state_of()["status"] == "adopted"

    def test_fresh_managed_key_is_not_reissued(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=True, expires_at=int(NOW) + 31 * 86400)
        client.account = account(has_key=True, managed=True, key_id=key_id(KEY))
        assert run(client, changes) == "provisioned"
        assert ("issue", "alice") not in client.calls
        assert changes == []

    def test_release_before_final_status_is_not_overwritten(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=True, expires_at=int(NOW) + 31 * 86400)
        client.account = account(has_key=True, managed=True, key_id=key_id(KEY))
        original = client.upsert_account

        def upsert(external_id: str, email: str) -> GristProvisionedAccount:
            result = original(external_id, email)
            write_state(status="released", email="")
            return result

        client.upsert_account = upsert  # type: ignore[method-assign]
        assert run(client, changes) == "released"
        assert state_of()["status"] == "released"

    def test_managed_key_within_30_days_is_reissued(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=True, expires_at=int(NOW) + 29 * 86400)
        client.account = account(has_key=True, managed=True, key_id=key_id(KEY))
        assert run(client, changes) == "provisioned"
        assert ("issue", "alice") in client.calls
        assert json.loads(token_path().read_text())["expires_at"] == int(NOW) + 90 * 86400
        assert changes == ["alice"]

    def test_adopted_key_is_not_reissued_while_key_id_matches(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=False, expires_at=0)
        client.account = account(has_key=True, key_id=key_id(KEY))
        assert run(client, changes) == "adopted"
        assert ("issue", "alice") not in client.calls

    def test_changed_key_id_is_reissued(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=False, expires_at=0)
        client.account = account(has_key=True, key_id="0" * 12)
        client.key = issued(key="n" * 40, managed=False, expires_at=0)
        assert run(client, changes) == "adopted"
        assert json.loads(token_path().read_text())["api_key"] == "n" * 40
        assert changes == ["alice"]

    def test_has_key_false_is_reissued(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=True, expires_at=int(NOW) + 80 * 86400)
        client.account = account(has_key=False)
        run(client, changes)
        assert ("issue", "alice") in client.calls

    def test_disabled_account_removes_provisioned_file(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=True, expires_at=int(NOW) + 80 * 86400)
        client.account = account(disabled=True)
        assert run(client, changes) == "disabled"
        assert not token_path().exists()
        assert changes == ["alice"]
        assert ("issue", "alice") not in client.calls

    def test_disabled_account_without_provisioned_file_changes_nothing(self, data, client, changes):
        write_state()
        client.account = account(disabled=True)
        assert run(client, changes) == "disabled"
        assert changes == []

    def test_disabled_account_keeps_a_file_that_is_not_provisioned(self, data, client, changes):
        write_state()
        write_token("manual", server="https://other.example.com")
        client.account = account(disabled=True)
        assert run(client, changes) == "disabled"
        assert token_path().exists() and changes == []

    def test_disabled_403_on_issue_removes_provisioned_file(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=True, expires_at=int(NOW) + 1)
        client.account = account(has_key=True, key_id=key_id(KEY))
        client.issue_error = GristProvisionerClientError("x", status=403, detail="account disabled")
        assert run(client, changes) == "disabled"
        assert not token_path().exists()
        assert changes == ["alice"]

    def test_admin_409_writes_admin_and_removes_provisioned_file(self, data, client, changes):
        write_state()
        write_token("provisioned", managed=False, expires_at=0)
        client.upsert_error = GristProvisionerClientError(
            "x", status=409, detail="account is a Grist admin; connect it by hand"
        )
        assert run(client, changes) == "admin"
        assert not token_path().exists()
        assert changes == ["alice"]
        assert state_of()["status"] == "admin"

    def test_no_account_404_writes_no_account(self, data, client, changes):
        write_state()
        client.upsert_error = GristProvisionerClientError("x", status=404, detail="no Grist account for this email")
        assert run(client, changes) == "no_account"
        assert state_of()["status"] == "no_account"

    @pytest.mark.parametrize(
        ("status", "detail"),
        [(409, "account is linked to another external id or email"), (403, "other"), (404, "not found"), (500, "")],
    )
    def test_other_error_writes_error_with_message(self, data, client, changes, status, detail):
        write_state()
        client.upsert_error = GristProvisionerClientError("It broke.", status=status, detail=detail)
        assert run(client, changes) == "error"
        state = state_of()
        assert state["status"] == "error" and state["detail"] == "It broke."

    def test_manual_save_during_issue_wins(self, data, client, changes):
        write_state()
        client.on_issue = lambda: write_token("manual", key="mine")
        assert run(client, changes) == "user_connected"
        assert json.loads(token_path().read_text())["api_key"] == "mine"
        assert changes == []
        assert state_of()["status"] == "user_connected"

    def test_release_during_upsert_stops_before_issuing(self, data, client, changes):
        write_state()
        original = client.upsert_account

        def upsert(external_id: str, email: str) -> GristProvisionedAccount:
            result = original(external_id, email)
            write_state(status="released", email="")
            return result

        client.upsert_account = upsert  # type: ignore[method-assign]
        assert run(client, changes) == "released"
        assert not any(call[0] == "issue" for call in client.calls)
        assert not token_path().exists() and changes == []
        assert state_of()["status"] == "released"

    def test_release_during_issue_skips_save_and_keeps_released(self, data, client, changes):
        write_state()
        client.on_issue = lambda: write_state(status="released", email="")
        assert run(client, changes) == "released"
        assert not token_path().exists() and changes == []
        assert state_of()["status"] == "released"
        assert ("release", "alice") in client.calls

    def test_a_key_issued_during_a_release_is_undone_or_reported(self, data, client, changes, caplog):
        write_state()
        client.on_issue = lambda: write_state(status="released", email="")
        client.release_error = GristProvisionerClientError("Broken.", status=500)
        assert run(client, changes) == "released"
        assert "run --grist-release again" in caplog.text
        assert KEY not in caplog.text and EMAIL not in caplog.text

    def test_unreadable_or_foreign_token_files_are_not_usable(self, data, client, changes):
        write_state()
        token_path().parent.mkdir(parents=True, exist_ok=True)
        token_path().write_text(json.dumps({"auth": "oauth", "server_url": SERVER, "access_token": "a",
                                            "refresh_token": "r", "expires_at": 1}))
        assert run(client, changes) == "provisioned"
        token_path().write_text("{bad")
        gp._drop_provisioned("alice", changes.append)
        assert token_path().exists()

    def test_log_lines_carry_no_email_or_key(self, data, client, changes, caplog):
        caplog.set_level(logging.DEBUG)
        write_state()
        run(client, changes)
        client.upsert_error = GristProvisionerClientError("Broken.", status=500)
        run(client, changes)
        text = caplog.text
        assert "Grist provisioning for alice: provisioned" in text
        assert "Grist provisioning for alice failed: Broken." in text
        assert EMAIL not in text and KEY not in text


class TestRequest:
    def test_request_unverified_email_is_not_queued(self, data, client, changes):
        p = make_provisioner(client, changes)
        p.request(Principal(id="alice", email=EMAIL), email_verified=False)
        assert state_of()["status"] == "unverified_email"
        assert p.process_next(0) is False

    def test_request_unverified_email_is_queued_when_trusted(self, data, client, changes):
        p = make_provisioner(client, changes, config=make_config(trust_unverified_email=True))
        p.request(Principal(id="alice", email=EMAIL), email_verified=False)
        assert state_of()["status"] == "pending"
        assert p.process_next(0) is True

    def test_request_no_email_is_not_queued(self, data, client, changes):
        p = make_provisioner(client, changes)
        p.request(Principal(id="alice", email=""), email_verified=True)
        assert state_of()["status"] == "no_email"
        assert p.process_next(0) is False

    def test_request_records_last_sign_in_and_sets_pending(self, data, client, changes):
        write_state(status="error", detail="old", last_sign_in=1)
        p = make_provisioner(client, changes)
        p.request(Principal(id="alice", email="new@example.com"), email_verified=True)
        state = state_of()
        assert state["status"] == "pending" and state["detail"] == ""
        assert state["email"] == "new@example.com" and state["last_sign_in"] == int(NOW)

    def test_sign_in_after_release_sets_pending_and_provisions(self, data, client, changes):
        write_state(status="released", email="")
        p = make_provisioner(client, changes, stop=_instant_stop())
        p.request(Principal(id="alice", email=EMAIL), email_verified=True)
        assert state_of()["status"] == "pending"
        assert p.process_next(0) is True
        assert state_of()["status"] == "provisioned"

    def test_request_is_deduplicated_while_queued(self, data, client, changes):
        p = make_provisioner(client, changes, stop=_instant_stop())
        for _ in range(3):
            p.request(Principal(id="alice", email=EMAIL), email_verified=True)
        assert p.process_next(0) is True
        assert p.process_next(0) is False
        assert client.calls.count(("upsert", "alice", EMAIL)) == 1

    def test_full_queue_drops_with_warning(self, data, client, changes, monkeypatch, caplog):
        monkeypatch.setattr(gp, "QUEUE_MAX", 1)
        p = make_provisioner(client, changes)
        p.request(Principal(id="alice", email=EMAIL), email_verified=True)
        with caplog.at_level(logging.WARNING):
            p.request(Principal(id="bob", email="bob@example.com"), email_verified=True)
        assert "Grist provisioning queue is full; bob waits for the next sweep or sign-in" in caplog.text
        assert p._queued == {"alice"}
        assert state_of("bob")["status"] == "pending"


def _instant_stop() -> threading.Event:
    stop = threading.Event()
    stop.set()  # wait(PACE_SECONDS) returns at once
    return stop


class TestWorker:
    def test_process_next_returns_false_on_empty_queue(self, data, client, changes):
        assert make_provisioner(client, changes).process_next(0) is False

    def test_process_next_paces_with_stop_event_wait(self, data, client, changes):
        waits: list[float] = []

        class Spy(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                waits.append(timeout or 0)
                return True

        write_state()
        p = make_provisioner(client, changes, stop=Spy())
        p._enqueue("alice")
        assert p.process_next(0) is True
        assert waits == [gp.PACE_SECONDS]

    def test_process_next_logs_unexpected_errors(self, data, client, changes, monkeypatch, caplog):
        def boom(*a: Any, **k: Any) -> str:
            raise RuntimeError("kaput")

        monkeypatch.setattr(gp, "provision_principal", boom)
        p = make_provisioner(client, changes)
        p._enqueue("alice")
        with caplog.at_level(logging.WARNING):
            assert p.process_next(0) is True
        assert "Grist provisioning worker error: kaput" in caplog.text
        assert p._queued == set()

    def test_work_loop_runs_until_stop(self, data, client, changes):
        stop = threading.Event()
        p = make_provisioner(client, changes, stop=stop)
        seen: list[float] = []

        def fake_next(timeout: float) -> bool:
            seen.append(timeout)
            stop.set()
            return False

        p.process_next = fake_next  # type: ignore[method-assign]
        p._work_loop()
        assert seen == [1.0]


class TestSweep:
    def seed(self, name: str, **fields: Any) -> None:
        write_state(name, **fields)

    def test_sweep_enqueues_recent_provisioned_adopted_error_and_no_account_only(self, data, client, changes):
        for status in ("pending", "provisioned", "adopted", "error", "no_account"):
            self.seed(f"in-{status}", status=status)
        for status in ("admin", "disabled", "user_connected", "no_email", "unverified_email", "released"):
            self.seed(f"out-{status}", status=status)
        self.seed("out-old", status="provisioned", last_sign_in=int(NOW) - 90 * 86400 - 1)
        self.seed("in-edge", status="provisioned", last_sign_in=int(NOW) - 90 * 86400)
        self.seed("out-bad-time", status="provisioned", last_sign_in="yesterday")
        p = make_provisioner(client, changes)
        assert p.sweep_once() == 6
        assert p._queued == {f"in-{s}" for s in ("pending", "provisioned", "adopted", "error", "no_account")} | {"in-edge"}
        assert client.calls == []

    def test_sweep_skips_non_directories_unsafe_names_and_stateless_users(self, data, client, changes):
        p = make_provisioner(client, changes)
        assert p.sweep_once() == 0  # no users/ yet
        users = data / "users"
        users.mkdir()
        (users / "afile").write_text("x")
        (users / "bad name").mkdir()
        (users / "nostate").mkdir()
        self.seed("ok")
        assert p.sweep_once() == 1
        assert p.sweep_once() == 0  # already queued

    def test_sweep_loop_checks_then_sweeps_until_stop(self, data, client, changes, caplog):
        class TwoPasses(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                return len(calls) >= 2

        stop = TwoPasses()
        check = FakeClient()
        p = make_provisioner(client, changes, stop=stop, check_client=check)  # type: ignore[arg-type]
        calls: list[int] = []

        def fake_sweep() -> int:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk")
            return 0

        p.sweep_once = fake_sweep  # type: ignore[method-assign]
        with caplog.at_level(logging.INFO):
            p._sweep_loop()
        assert calls == [1, 1]
        assert "Grist provisioning ready at prov.example.com (3 accounts managed)" in caplog.text
        assert "Grist provisioning sweep failed (will retry): disk" in caplog.text
        assert check.calls == [("check",)]

    def test_sweep_loop_logs_failed_check(self, data, client, changes, caplog):
        stop = _instant_stop()
        client.check_result = GristProvisionerClientError("Nope.", status=401)
        p = make_provisioner(client, changes, stop=stop)
        p.sweep_once = lambda: 0  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING):
            p._sweep_loop()
        assert "Grist provisioning check failed: Nope." in caplog.text

    def test_start_runs_two_daemon_threads_that_exit_on_stop(self, data, client, changes):
        stop = _instant_stop()
        p = make_provisioner(client, changes, stop=stop)
        p.start()
        threads = [t for t in threading.enumerate() if t.name.startswith("grist-provisioning")]
        assert {t.name for t in threads} <= {"grist-provisioning", "grist-provisioning-sweep"}
        for t in threads:
            t.join(2)
            assert not t.is_alive()


class TestModuleFunctions:
    def org_config(self, **auto: Any) -> dict[str, Any]:
        section = {"enabled": True, "provisioner_url": PROVISIONER, **auto}
        return {"mode": "org", "grist": {"server_url": SERVER, "auto_provision": section}}

    def test_start_returns_none_when_disabled(self, data, changes):
        assert gp.start({"mode": "org"}, on_change=changes.append, stop_event=threading.Event()) is None
        assert gp._INSTANCE is None

    def test_start_returns_none_and_warns_without_key(self, data, changes, monkeypatch, caplog):
        monkeypatch.delenv("PRIVACYFENCE_GRIST_PROVISIONING_KEY", raising=False)
        with caplog.at_level(logging.WARNING):
            assert gp.start(self.org_config(), on_change=changes.append, stop_event=threading.Event()) is None
        assert (
            "Grist auto-provisioning is on, but PRIVACYFENCE_GRIST_PROVISIONING_KEY is not set; "
            "people connect Grist by hand on /connect."
        ) in caplog.text
        assert gp._INSTANCE is None

    def test_start_reads_the_key_from_a_file(self, data, changes, monkeypatch, tmp_path):
        key_file = tmp_path / "grist_key"
        key_file.write_text("gristprov_file\n")
        monkeypatch.delenv("PRIVACYFENCE_GRIST_PROVISIONING_KEY", raising=False)
        monkeypatch.setenv("PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE", str(key_file))
        provisioner = gp.start(self.org_config(), on_change=changes.append, stop_event=_instant_stop())
        assert provisioner is not None
        assert provisioner._client._headers["Authorization"] == "Bearer gristprov_file"
        for t in threading.enumerate():
            if t.name.startswith("grist-provisioning"):
                t.join(2)

    def test_start_refuses_a_key_file_that_cannot_be_read(self, data, changes, monkeypatch, tmp_path):
        monkeypatch.delenv("PRIVACYFENCE_GRIST_PROVISIONING_KEY", raising=False)
        monkeypatch.setenv("PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE", str(tmp_path / "absent"))
        with pytest.raises(gp.org_mode.ConfigurationError, match="PRIVACYFENCE_GRIST_PROVISIONING_KEY_FILE"):
            gp.start(self.org_config(), on_change=changes.append, stop_event=threading.Event())

    def test_start_builds_and_starts_the_provisioner(self, data, changes, monkeypatch):
        monkeypatch.setenv("MY_KEY", " gristprov_abc ")
        stop = _instant_stop()
        provisioner = gp.start(self.org_config(key_env="MY_KEY"), on_change=changes.append, stop_event=stop)
        assert provisioner is gp._INSTANCE and provisioner is not None
        assert provisioner._client is not provisioner._check_client
        for t in threading.enumerate():
            if t.name.startswith("grist-provisioning"):
                t.join(2)

    def test_start_logs_ready_and_check_failed(self, data, client, changes, caplog):
        for result, text in (
            (4, "Grist provisioning ready at prov.example.com (4 accounts managed)"),
            (GristProvisionerClientError("Nope.", status=401), "Grist provisioning check failed: Nope."),
        ):
            caplog.clear()
            client.check_result = result
            p = make_provisioner(client, changes, stop=_instant_stop())
            p.sweep_once = lambda: 0  # type: ignore[method-assign]
            with caplog.at_level(logging.INFO):
                p._sweep_loop()
            assert text in caplog.text

    def test_notify_sign_in_is_noop_without_instance(self, data):
        gp.notify_sign_in(Principal(id="alice", email=EMAIL), email_verified=True)
        assert not (data / "users").exists()

    def test_notify_sign_in_queues_through_the_instance(self, data, client, changes):
        gp._INSTANCE = make_provisioner(client, changes)
        gp.notify_sign_in(Principal(id="alice", email=EMAIL), email_verified=True)
        assert state_of()["status"] == "pending"

    def test_notify_sign_in_never_raises(self, data, caplog):
        class Broken:
            def request(self, principal: Principal, *, email_verified: bool) -> None:
                raise OSError("disk full")

        gp._INSTANCE = Broken()  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING):
            gp.notify_sign_in(Principal(id="alice", email=EMAIL), email_verified=True)
        assert "Grist provisioning could not take the sign-in of alice: disk full" in caplog.text

    @pytest.mark.parametrize(
        ("claims", "expected"),
        [({"email_verified": True}, True), ({"email_verified": "true"}, True), ({"email_verified": False}, False),
         ({"email_verified": "false"}, False), ({}, False), ({"email_verified": 1}, False)],
    )
    def test_email_verified_claim(self, claims, expected):
        assert gp.email_verified_claim(claims) is expected

    def test_release_removes_provisioned_file_keeps_manual_file_and_clears_email(self, data):
        write_state(status="provisioned")
        write_token("provisioned", managed=True, expires_at=1)
        gp.release("alice")
        assert not token_path().exists()
        state = state_of()
        assert state["status"] == "released" and state["email"] == "" and state["detail"] == ""

        write_token("manual", key="mine")
        gp.release("alice")
        assert json.loads(token_path().read_text())["api_key"] == "mine"

    def test_release_without_token_file_only_marks_released(self, data):
        gp.release("alice")
        assert state_of()["status"] == "released"

    def test_principal_lock_is_one_lock_per_id(self):
        assert gp.principal_lock("a") is gp.principal_lock("a")
        assert gp.principal_lock("a") is not gp.principal_lock("b")

