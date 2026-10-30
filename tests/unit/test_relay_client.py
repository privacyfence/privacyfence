"""relay_client: the host rules, the well-known token, registration and the check loop."""
from __future__ import annotations

import json
import logging
import stat
import threading
import time
from typing import Any

import pytest
import requests

from privacyfence import oauth_apps, paths, relay_client

ISSUER = "https://pf.acme.example.com"
SLACK_RELAYED = {"slack": {"client_id": "x", "relay": True}}
HOUR = 3600.0


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


class _Response:
    def __init__(self, status_code: int = 200, body: Any = None, *, bad_json: bool = False) -> None:
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class _Posts(list):
    reply: dict[str, Any]


@pytest.fixture
def posts(monkeypatch):
    calls = _Posts()
    reply: dict[str, Any] = {"value": _Response()}
    calls.reply = reply

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        value = reply["value"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(relay_client.requests, "post", fake_post)
    return calls


class TestToken:
    def test_created_once_with_43_characters_and_mode_0600(self, tmp_path):
        token = relay_client.well_known_token()
        assert len(token) == 43
        assert relay_client.well_known_token() == token
        assert stat.S_IMODE((tmp_path / "org" / "relay_token.txt").stat().st_mode) == 0o600

    @pytest.mark.parametrize("content", ["short", "!" * 43, ""])
    def test_regenerated_when_corrupt(self, tmp_path, content):
        path = tmp_path / "org" / "relay_token.txt"
        path.parent.mkdir(parents=True)
        path.write_text(content)
        token = relay_client.well_known_token()
        assert token != content and len(token) == 43
        assert path.read_text() == token

    def test_replacement_after_a_recorded_success_logs_an_error_once_without_the_token(self, tmp_path, caplog):
        relay_client._persist_last_ok(1.0)
        with caplog.at_level(logging.ERROR):
            first = relay_client.well_known_token()
            (tmp_path / "org" / "relay_token.txt").write_text("bad")
            second = relay_client.well_known_token()
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "relay_token.txt" in errors[0].getMessage()
        assert first not in caplog.text and second not in caplog.text

    def test_first_creation_logs_no_error(self, caplog):
        with caplog.at_level(logging.ERROR):
            relay_client.well_known_token()
        assert caplog.records == []


class TestHostRules:
    @pytest.mark.parametrize("host", [
        "pf.acme.example.com", "pf.acme.example.com:8443", "a.io", "x-y.example.org:65535", "a" * 63 + ".com",
    ])
    def test_valid_hosts(self, host):
        assert relay_client.valid_relay_host(host)

    @pytest.mark.parametrize("host", [
        "localhost", "127.1", "2130706433", "0x7f000001", "127.0.0.1", "a..b", "-x.com", "x-.com",
        "x.com:0", "x.com:99999", "x.com:08080", "x.com:", "foo.localhost.", "example.com.", "PF.example.com",
        "a" * 64 + ".com", "x." + "a" * 64, "x.c", "x.1com", "u@x.com", ("a." * 130) + "com", "",
    ])
    def test_invalid_hosts(self, host):
        assert not relay_client.valid_relay_host(host)

    @pytest.mark.parametrize("host", ["pf.acme.example.com", "pf.acme.example.com:8443", "localhost.evil.com"])
    def test_public_hosts(self, host):
        assert relay_client.public_host(host)

    @pytest.mark.parametrize("host", [
        "pf.localhost", "a.b.localhost:8443", "x.local", "x.internal", "x.home.arpa", "x.lan", "localhost", "a..b",
    ])
    def test_non_public_hosts(self, host):
        assert not relay_client.public_host(host)

    @pytest.mark.parametrize("host", ["localhost", "localhost:8765", "127.0.0.1", "127.0.0.1:65535"])
    def test_loopback_hosts(self, host):
        assert relay_client.loopback_host(host)

    @pytest.mark.parametrize("host", [
        "localhost:0", "localhost:65536", "LOCALHOST", "localhost.evil.com", "localhost:80@evil.com",
        "127.0.0.1.nip.io", "[::1]", "localhost:", "",
    ])
    def test_not_loopback_hosts(self, host):
        assert not relay_client.loopback_host(host)

    def test_issuer_host_is_the_lowercased_netloc(self):
        assert relay_client.issuer_host("https://PF.Acme.example.com:8443/") == "pf.acme.example.com:8443"


class TestRegister:
    @pytest.mark.parametrize("issuer", [
        "http://pf.acme.example.com", "https://pf.acme.example.com/app", "https://pf.acme.example.com?x=1",
        "https://pf.acme.example.com/#f", "ftp://pf.acme.example.com", "pf.acme.example.com",
    ])
    def test_refuses_an_issuer_the_relay_cannot_serve(self, issuer, posts):
        assert relay_client.register(issuer) == (False, "the relay needs an https issuer_url with no path")
        assert posts == []

    @pytest.mark.parametrize("issuer", [
        "https://pf.acme.local", "https://10.0.0.1", "https://intranet", "https://pf.acme.internal:8443",
        "http://localhost:8765", "http://127.0.0.1:8765", "https://localhost",
    ])
    def test_refuses_a_non_public_host_without_calling_the_relay(self, issuer, posts):
        assert relay_client.register(issuer) == (False, "host_not_public")
        assert posts == []

    def test_posts_exactly_the_specified_request(self, posts):
        assert relay_client.register("https://PF.acme.example.com:8443/") == (True, "")
        assert posts == [{
            "url": "https://oauth.privacyfence.eu/register",
            "json": {"host": "pf.acme.example.com:8443", "token": relay_client.well_known_token()},
            "timeout": 10,
            "allow_redirects": False,
        }]

    @pytest.mark.parametrize("response,expected", [
        (_Response(403, {"error": "proof_failed"}), "proof_failed"),
        (_Response(429, {"error": "too_soon"}), "too_soon"),
        (_Response(429, {"error": "too_many_new_hosts"}), "too_many_new_hosts"),
        (_Response(500), "HTTP 500"),
        (_Response(403, {"error": "something_new"}), "HTTP 403"),
        (_Response(502, ["x"]), "HTTP 502"),
        (_Response(502, bad_json=True), "HTTP 502"),
        (_Response(400, {"error": 7}), "HTTP 400"),
    ])
    def test_failures_are_reported_as_a_short_detail(self, posts, response, expected):
        posts.reply["value"] = response
        assert relay_client.register(ISSUER) == (False, expected)

    def test_a_network_error_is_reported_without_its_text(self, posts):
        posts.reply["value"] = requests.ConnectionError("secret detail")
        assert relay_client.register(ISSUER) == (False, "could not reach oauth.privacyfence.eu")


def _set_state(**fields: Any) -> None:
    relay_client.status()  # loads the persisted value first, as a real caller would
    for key, value in fields.items():
        setattr(relay_client._state, key, value)


class TestStatus:
    def test_starts_empty(self):
        assert relay_client.status() == {
            "last_ok_at": None, "last_error": "", "needed": False, "signin_domains_verified": [],
        }

    def test_a_recorded_success_survives_a_restart(self, posts):
        relay_client._tick(ISSUER, SLACK_RELAYED, 1000.0)
        relay_client.reset_for_tests()
        assert relay_client.status()["last_ok_at"] == 1000.0

    @pytest.mark.parametrize("content", ["not json", json.dumps({"last_ok_at": "x"}), json.dumps({"last_ok_at": True}), "[]"])
    def test_an_unreadable_status_file_means_no_success(self, tmp_path, content):
        (tmp_path / "org").mkdir()
        (tmp_path / "org" / "relay_status.json").write_text(content)
        assert relay_client.status()["last_ok_at"] is None

    def test_an_unwritable_status_file_is_a_warning_not_a_failure(self, monkeypatch, caplog):
        def boom(path, data, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(relay_client, "atomic_write_json", boom)
        with caplog.at_level(logging.WARNING):
            relay_client._persist_last_ok(5.0)
        assert "Could not save the relay registration status" in caplog.text


class TestTick:
    def test_not_needed_makes_no_call(self, posts):
        assert relay_client._tick(ISSUER, {"slack": {"client_id": "x"}, "mode": "org"}, 100.0) == HOUR
        assert posts == []
        assert relay_client.status()["needed"] is False

    def test_a_loopback_issuer_needs_no_registration(self, posts):
        assert relay_client._tick("http://localhost:8765", SLACK_RELAYED, 100.0) == HOUR
        assert posts == []
        assert relay_client.status()["needed"] is False

    def test_registered_two_hours_ago_makes_no_call(self, posts):
        _set_state(last_ok_at=100.0)
        assert relay_client._tick(ISSUER, SLACK_RELAYED, 100.0 + 2 * HOUR) == HOUR
        assert posts == []
        assert relay_client.status()["needed"] is True

    def test_registered_25_hours_ago_calls(self, posts):
        _set_state(last_ok_at=100.0)
        assert relay_client._tick(ISSUER, SLACK_RELAYED, 100.0 + 25 * HOUR) == HOUR
        assert len(posts) == 1
        assert relay_client.status()["last_ok_at"] == 100.0 + 25 * HOUR

    def test_never_registered_calls_with_the_need_already_published(self, posts, monkeypatch):
        seen = []
        original = relay_client._register

        def register_spy(issuer_url, signin_domains):
            seen.append(relay_client.status()["needed"])  # the well-known route is open while the relay fetches it
            return original(issuer_url, signin_domains)

        monkeypatch.setattr(relay_client, "_register", register_spy)
        assert relay_client._tick(ISSUER, SLACK_RELAYED, 100.0) == HOUR
        assert seen == [True]

    def test_an_error_after_a_recent_success_still_retries(self, posts):
        _set_state(last_ok_at=100.0, last_error="too_soon")
        relay_client._tick(ISSUER, SLACK_RELAYED, 200.0)
        assert len(posts) == 1

    def test_backoff_doubles_and_is_capped_and_a_success_clears_it(self, posts, caplog):
        posts.reply["value"] = _Response(500)
        waits = [relay_client._tick(ISSUER, SLACK_RELAYED, 100.0) for _ in range(8)]
        assert waits == [60.0, 120.0, 240.0, 480.0, 960.0, 1920.0, 3600.0, 3600.0]
        assert relay_client.status()["last_error"] == "HTTP 500"
        posts.reply["value"] = _Response()
        with caplog.at_level(logging.INFO):
            assert relay_client._tick(ISSUER, SLACK_RELAYED, 200.0) == HOUR
        assert relay_client.status() == {
            "last_ok_at": 200.0, "last_error": "", "needed": True, "signin_domains_verified": [],
        }
        posts.reply["value"] = _Response(500)
        assert relay_client._tick(ISSUER, SLACK_RELAYED, 200.0 + 25 * HOUR) == 60.0

    @pytest.mark.parametrize("code", ["too_soon", "too_many_new_hosts"])
    def test_a_busy_relay_does_not_close_sign_in(self, posts, code):
        _set_state(last_ok_at=100.0)
        posts.reply["value"] = _Response(429, {"error": code})
        assert relay_client._tick(ISSUER, SLACK_RELAYED, 100.0 + 25 * HOUR) == 60.0
        status = relay_client.status()
        assert status["last_ok_at"] == 100.0
        assert status["last_error"] == code

    def test_the_token_never_reaches_the_log(self, posts, caplog):
        with caplog.at_level(logging.DEBUG):
            relay_client._tick(ISSUER, SLACK_RELAYED, 100.0)
            posts.reply["value"] = _Response(500)
            relay_client._tick(ISSUER, SLACK_RELAYED, 100.0 + 25 * HOUR)
        assert relay_client.well_known_token() not in caplog.text

    def test_sees_a_section_added_later_to_the_shared_config(self, posts):
        org_config: dict[str, Any] = {}
        assert relay_client._tick(ISSUER, org_config, 1.0) == HOUR
        oauth_apps.refresh_in_place(org_config, dict(SLACK_RELAYED))
        relay_client._tick(ISSUER, org_config, 2.0)
        assert len(posts) == 1


class TestLoop:
    def test_request_check_wakes_a_waiting_loop_and_stop_ends_it(self, monkeypatch):
        calls: list[float] = []
        second = threading.Event()

        def fake_tick(issuer_url, org_config, now):
            calls.append(now)
            if len(calls) >= 2:
                second.set()
            return 3600.0

        monkeypatch.setattr(relay_client, "_tick", fake_tick)
        stop = threading.Event()
        thread = relay_client.start_registration_loop(ISSUER, {}, stop)
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.01)
        assert len(calls) == 1
        relay_client.request_check()
        assert second.wait(5)
        stop.set()
        relay_client.request_check()
        thread.join(5)
        assert not thread.is_alive()
        assert len(calls) == 2

    def test_a_failing_pass_does_not_kill_the_loop(self, monkeypatch, caplog):
        calls: list[int] = []
        retried = threading.Event()

        def fake_tick(issuer_url, org_config, now):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            retried.set()
            return 3600.0

        monkeypatch.setattr(relay_client, "_tick", fake_tick)
        monkeypatch.setattr(relay_client, "_FIRST_BACKOFF_SECONDS", 0.01)
        stop = threading.Event()
        with caplog.at_level(logging.ERROR):
            thread = relay_client.start_registration_loop(ISSUER, {}, stop)
            assert retried.wait(5)
        stop.set()
        relay_client.request_check()
        thread.join(5)
        assert "Relay registration check failed" in caplog.text


def _builtin(admins: list[str], allowed: list[str] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"idp": {
        "issuer": "https://accounts.google.com", "client_id": "builtin", "builtin": True, "relay": True,
        "admin_emails": admins,
    }}
    if allowed is not None:
        config["authz"] = {"allowed_domains": allowed}
    return config


BUILTIN = _builtin(["Alice@Acme.com", "bob@acme.com"], ["acme.org", "ACME.com"])


def _verified_reply(*domains: str) -> _Response:
    return _Response(200, {"host": "pf.acme.example.com", "signin_domains_verified": list(domains),
                           "signin_domains_missing": []})


def _txt_value() -> str:
    import hashlib

    return "pf-relay=" + hashlib.sha256(relay_client.well_known_token().encode()).hexdigest()


class TestSigninDomains:
    def test_the_admins_domains_and_the_allowed_domains_lowercased_and_deduplicated(self):
        assert relay_client.signin_domains(BUILTIN) == ["acme.com", "acme.org"]

    def test_none_without_the_builtin_sign_in(self):
        assert relay_client.signin_domains({}) is None
        own = {"idp": {"issuer": "https://accounts.google.com", "client_id": "own", "admin_emails": ["a@acme.com"]}}
        assert relay_client.signin_domains(own) is None

    def test_names_the_relay_would_refuse_are_never_sent(self, caplog):
        config = _builtin(["a@gmail.com", "b@googlemail.com", "c@acme.com", "d@x.local", "e@ac\u212ame.com"])
        with caplog.at_level(logging.WARNING):
            assert relay_client.signin_domains(config) == ["acme.com"]
            relay_client.signin_domains(config)
        warnings = [r for r in caplog.records if "nobody from" in r.getMessage()]
        assert len(warnings) == 1 and "gmail.com" in warnings[0].getMessage()

    def test_a_broken_idp_setting_proves_no_domain(self, monkeypatch):
        monkeypatch.setenv("PRIVACYFENCE_IDP", "something-else")
        assert relay_client.signin_domains({}) is None

    def test_malformed_admin_addresses_are_skipped(self):
        config = _builtin(["a@acme.com", "no-at-sign", "@acme.net", "b@", 7])
        assert relay_client.signin_domains(config) == ["acme.com"]

    def test_at_most_twenty(self):
        config = _builtin(["a@acme.com"], [f"d{i:02}.example.com" for i in range(25)])
        domains = relay_client.signin_domains(config)
        assert domains is not None and len(domains) == relay_client.MAX_SIGNIN_DOMAINS == 20


class TestSigninRegistration:
    def test_register_sends_the_signin_domains(self, posts):
        relay_client.register(ISSUER, ["acme.com", "acme.org"])
        assert posts[0]["json"] == {
            "host": "pf.acme.example.com", "token": relay_client.well_known_token(),
            "signin_domains": ["acme.com", "acme.org"],
        }

    def test_tick_registers_at_once_and_persists_the_verified_set(self, posts, tmp_path):
        posts.reply["value"] = _verified_reply("acme.com", "evil.com")
        assert relay_client._tick(ISSUER, BUILTIN, 100.0) == HOUR
        assert posts[0]["json"]["signin_domains"] == ["acme.com", "acme.org"]
        assert relay_client.status()["needed"] is True
        # Only names the server sent can come back as verified.
        assert relay_client.status()["signin_domains_verified"] == ["acme.com"]
        saved = json.loads((tmp_path / "org" / "relay_status.json").read_text())
        assert saved == {"last_ok_at": 100.0, "signin_domains_verified": ["acme.com"]}
        relay_client.reset_for_tests()
        assert relay_client.verified_signin_domains() == frozenset({"acme.com"})

    def test_needed_from_the_first_start_without_any_relayed_service(self, posts):
        relay_client._tick(ISSUER, BUILTIN, 100.0)
        assert relay_client.status()["needed"] is True and len(posts) == 1

    def test_a_missing_domain_is_checked_again_at_the_next_pass(self, posts):
        posts.reply["value"] = _verified_reply("acme.com")
        relay_client._tick(ISSUER, BUILTIN, 100.0)
        posts.reply["value"] = _verified_reply("acme.com", "acme.org")
        relay_client._tick(ISSUER, BUILTIN, 100.0 + HOUR)
        assert len(posts) == 2
        assert relay_client.status()["signin_domains_verified"] == ["acme.com", "acme.org"]
        relay_client._tick(ISSUER, BUILTIN, 100.0 + 2 * HOUR)
        assert len(posts) == 2  # everything proven and fresh: no call

    def test_a_failed_registration_keeps_the_verified_set(self, posts):
        posts.reply["value"] = _verified_reply("acme.com")
        relay_client._tick(ISSUER, BUILTIN, 100.0)
        posts.reply["value"] = _Response(500)
        relay_client._tick(ISSUER, BUILTIN, 100.0 + HOUR)
        assert relay_client.status()["signin_domains_verified"] == ["acme.com"]

    def test_a_connector_only_server_sends_no_signin_domains(self, posts):
        relay_client._tick(ISSUER, SLACK_RELAYED, 100.0)
        assert "signin_domains" not in posts[0]["json"]

    @pytest.mark.parametrize("body", [None, {"signin_domains_verified": "acme.com"}, {"signin_domains_verified": [3]}])
    def test_an_odd_answer_verifies_nothing(self, posts, body):
        posts.reply["value"] = _Response(200, body)
        relay_client._tick(ISSUER, BUILTIN, 100.0)
        assert relay_client.status()["signin_domains_verified"] == []

    @pytest.mark.parametrize("content", [
        json.dumps({"last_ok_at": 1.0, "signin_domains_verified": "acme.com"}),
        json.dumps({"last_ok_at": 1.0, "signin_domains_verified": [1, "gmail.com", "-x.com"]}),
    ])
    def test_an_odd_status_file_verifies_nothing(self, tmp_path, content):
        (tmp_path / "org").mkdir()
        (tmp_path / "org" / "relay_status.json").write_text(content)
        assert relay_client.verified_signin_domains() == frozenset()


class TestDnsRecordLines:
    def test_one_line_per_missing_domain_with_the_hash_of_the_token(self, posts, caplog):
        posts.reply["value"] = _verified_reply("acme.com")
        with caplog.at_level(logging.WARNING):
            relay_client._tick(ISSUER, BUILTIN, 100.0)
        value = _txt_value()
        expected = (
            f"Built-in Google sign-in: add the DNS TXT record _privacyfence-relay.acme.org with the value "
            f"{value}. Until then nobody from acme.org can sign in."
        )
        assert relay_client.missing_dns_records(BUILTIN) == [expected]
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [expected]
        assert relay_client.well_known_token() not in caplog.text

    def test_every_domain_is_missing_before_any_registration(self):
        lines = relay_client.missing_dns_records(BUILTIN)
        assert [line.split()[8] for line in lines] == [
            "_privacyfence-relay.acme.com", "_privacyfence-relay.acme.org",
        ]

    def test_no_lines_without_the_builtin_sign_in_or_once_everything_is_proven(self, posts):
        assert relay_client.missing_dns_records(SLACK_RELAYED) == []
        posts.reply["value"] = _verified_reply("acme.com", "acme.org")
        relay_client._tick(ISSUER, BUILTIN, 100.0)
        assert relay_client.missing_dns_records(BUILTIN) == []

    def test_a_replaced_token_gives_the_new_values_and_forgets_the_old_proofs(self, posts, tmp_path, caplog):
        posts.reply["value"] = _verified_reply("acme.com", "acme.org")
        relay_client._tick(ISSUER, BUILTIN, 100.0)
        old_value = _txt_value()
        (tmp_path / "org" / "relay_token.txt").unlink()
        relay_client.reset_for_tests()  # a restart
        with caplog.at_level(logging.WARNING):
            relay_client.log_missing_dns_records(BUILTIN)
        new_value = _txt_value()
        assert new_value != old_value
        messages = [r.getMessage() for r in caplog.records]
        assert messages[0].startswith("org/relay_token.txt was missing or unreadable and has been replaced.")
        assert len(messages) == 3
        assert all(new_value in line and old_value not in line for line in messages[1:])
        assert relay_client.verified_signin_domains() == frozenset()
        assert relay_client.status()["last_ok_at"] == 100.0
        assert relay_client.well_known_token() not in caplog.text
        # The next pass registers again at once rather than waiting out the day.
        relay_client._tick(ISSUER, BUILTIN, 200.0)
        assert len(posts) == 2
