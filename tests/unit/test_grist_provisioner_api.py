"""Tests for the Grist provisioner client.

Requests are faked at the ``requests.Session.request`` boundary; nothing touches
the network. The provisioning key and an issued Grist key never appear in a
repr, an error message or a log line.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest
import requests

from privacyfence.grist_provisioner_api import (
    GristIssuedKey,
    GristProvisionedAccount,
    GristProvisionerClient,
    GristProvisionerClientError,
    GristRelease,
)

pytestmark = pytest.mark.unit

BASE = "https://prov.example.com"
HOST = "prov.example.com"
PROV_KEY = "gristprov_SECRETKEY"
ISSUED_KEY = "ISSUEDGRISTKEY123"
EMAIL = "alice@example.com"

ACCOUNT_BODY = {
    "external_id": "alice",
    "user_id": 7,
    "disabled": False,
    "has_key": True,
    "managed": True,
    "key_id": "0123456789ab",
    "expires_at": 1800000000,
}
KEY_BODY = {"key": ISSUED_KEY, "managed": True, "key_id": "0123456789ab", "expires_at": 1800000000}


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, raw: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self) -> Any:
        if self._raw or self._body is None:
            raise ValueError("not json")
        return self._body


class Recorder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(requests.Session, "request", self._request)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def client() -> GristProvisionerClient:
    return GristProvisionerClient(BASE, PROV_KEY)


def error_of(call: Any, *args: Any) -> GristProvisionerClientError:
    with pytest.raises(GristProvisionerClientError) as err:
        call(*args)
    return err.value


UNEXPECTED = "The Grist provisioner answered with something unexpected."


class TestCalls:
    def test_check_returns_accounts(self, monkeypatch, client):
        rec = Recorder(monkeypatch, FakeResponse(200, {"name": "p", "accounts": 3}))
        assert client.check() == 3
        assert rec.calls[0]["method"] == "GET"
        assert rec.calls[0]["url"] == f"{BASE}/api/provisioning/self"

    def test_check_without_accounts_is_unexpected(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(200, {"name": "p"}))
        assert str(error_of(client.check)) == UNEXPECTED

    def test_non_json_and_non_object_are_unexpected(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(200, raw=True))
        assert str(error_of(client.check)) == UNEXPECTED
        Recorder(monkeypatch, FakeResponse(200, [1]))
        assert str(error_of(client.check)) == UNEXPECTED

    def test_upsert_sends_put_with_email_and_bearer(self, monkeypatch, client):
        rec = Recorder(monkeypatch, FakeResponse(200, ACCOUNT_BODY))
        client.upsert_account("alice", EMAIL)
        call = rec.calls[0]
        assert call["method"] == "PUT"
        assert call["url"] == f"{BASE}/api/provisioning/accounts/alice"
        assert call["json"] == {"email": EMAIL}
        assert call["headers"] == {"Authorization": f"Bearer {PROV_KEY}", "Accept": "application/json"}

    def test_upsert_parses_account(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(200, ACCOUNT_BODY))
        assert client.upsert_account("alice", EMAIL) == GristProvisionedAccount(
            "alice", 7, False, True, True, "0123456789ab", 1800000000
        )

    @pytest.mark.parametrize("missing", list(ACCOUNT_BODY))
    def test_upsert_missing_field_is_unexpected(self, monkeypatch, client, missing):
        Recorder(monkeypatch, FakeResponse(200, {k: v for k, v in ACCOUNT_BODY.items() if k != missing}))
        assert str(error_of(client.upsert_account, "alice", EMAIL)) == UNEXPECTED

    def test_upsert_bool_is_true_only_for_json_true(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(200, {**ACCOUNT_BODY, "disabled": "true", "user_id": True}))
        assert str(error_of(client.upsert_account, "alice", EMAIL)) == UNEXPECTED
        Recorder(monkeypatch, FakeResponse(200, {**ACCOUNT_BODY, "disabled": "true"}))
        assert client.upsert_account("alice", EMAIL).disabled is False

    def test_issue_key_parses_and_hides_key_in_repr(self, monkeypatch, client):
        rec = Recorder(monkeypatch, FakeResponse(201, KEY_BODY))
        issued = client.issue_key("alice")
        assert issued == GristIssuedKey(ISSUED_KEY, True, "0123456789ab", 1800000000)
        assert ISSUED_KEY not in repr(issued)
        assert rec.calls[0]["method"] == "POST"
        assert rec.calls[0]["url"] == f"{BASE}/api/provisioning/accounts/alice/key"
        assert rec.calls[0]["json"] == {}

    @pytest.mark.parametrize("key", ["", None])
    def test_issue_key_without_key_is_unexpected(self, monkeypatch, client, key):
        Recorder(monkeypatch, FakeResponse(201, {**KEY_BODY, "key": key}))
        assert str(error_of(client.issue_key, "alice")) == UNEXPECTED

    def test_release_returns_flags(self, monkeypatch, client):
        rec = Recorder(monkeypatch, FakeResponse(200, {"released": True, "revoked": False}))
        assert client.release_account("alice") == GristRelease(True, False)
        assert rec.calls[0]["method"] == "DELETE"
        assert rec.calls[0]["url"] == f"{BASE}/api/provisioning/accounts/alice"

    def test_release_404_not_found_is_false_false(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "not found"}))
        assert client.release_account("alice") == GristRelease(False, False)


class TestErrors:
    CALLS = [
        ("check", ()),
        ("upsert_account", ("alice", EMAIL)),
        ("issue_key", ("alice",)),
        ("release_account", ("alice",)),
    ]

    @pytest.mark.parametrize("name,args", CALLS)
    def test_plain_404_is_not_a_provisioner(self, monkeypatch, client, name, args):
        Recorder(monkeypatch, FakeResponse(404, raw=True))
        err = error_of(getattr(client, name), *args)
        assert str(err) == "This server is not a Grist provisioner (HTTP 404). Check --grist-provisioner-url."
        assert (err.status, err.detail) == (404, "no detail")

    def test_json_404_on_upsert_is_no_account(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "no Grist account for this email"}))
        err = error_of(client.upsert_account, "alice", EMAIL)
        assert str(err) == "Grist has no account for this person yet (HTTP 404)."
        assert err.status == 404
        assert err.detail == "no Grist account for this email"

    def test_json_404_on_issue_key_is_no_link(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "not found"}))
        err = error_of(client.issue_key, "alice")
        assert str(err) == "The Grist provisioner has no link for this person (HTTP 404)."
        assert err.status == 404

    def test_json_404_on_check_is_a_plain_refusal(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(404, {"error": "gone"}))
        assert str(error_of(client.check)) == "The Grist provisioner refused the request (HTTP 404): gone"

    def test_401_message(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(401, {"error": "bad key"}))
        err = error_of(client.check)
        assert str(err) == (
            "The Grist provisioner refused the provisioning key (HTTP 401). "
            "Check the key in the daemon's environment."
        )
        assert (err.status, err.detail) == (401, "bad key")

    @pytest.mark.parametrize("status", [403, 409])
    def test_403_and_409_carry_detail(self, monkeypatch, client, status):
        Recorder(monkeypatch, FakeResponse(status, {"error": "account is a Grist admin; connect it by hand"}))
        err = error_of(client.upsert_account, "alice", EMAIL)
        assert str(err) == (
            f"The Grist provisioner refused this account (HTTP {status}): "
            "account is a Grist admin; connect it by hand"
        )
        assert (err.status, err.detail) == (status, "account is a Grist admin; connect it by hand")

    def test_5xx_message(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(503, {"error": "down"}))
        err = error_of(client.check)
        assert str(err) == "The Grist provisioner answered with an error (HTTP 503)."
        assert (err.status, err.detail) == (503, "down")

    def test_other_4xx_message(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(400, {"error": "x" * 300}))
        err = error_of(client.upsert_account, "alice", EMAIL)
        assert err.detail == "x" * 200
        assert str(err) == f"The Grist provisioner refused the request (HTTP 400): {'x' * 200}"

    def test_error_without_text_has_no_detail(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(400, {"error": 5}))
        assert error_of(client.check).detail == "no detail"
        Recorder(monkeypatch, FakeResponse(400, ["x"]))
        assert error_of(client.check).detail == "no detail"

    def test_redirect_is_refused(self, monkeypatch, client):
        Recorder(monkeypatch, FakeResponse(302, raw=True))
        err = error_of(client.check)
        assert str(err) == (
            "The Grist provisioner answered with a redirect (HTTP 302). Check --grist-provisioner-url."
        )
        assert err.status == 302

    def test_transport_error_message(self, monkeypatch, client):
        Recorder(monkeypatch, requests.ConnectionError("secret detail"))
        err = error_of(client.check)
        assert str(err) == f"Could not reach the Grist provisioner at {HOST}: ConnectionError"
        assert err.status is None

    @pytest.mark.parametrize("bad", ["", "a/b", "a b", "x" * 201, "é"])
    @pytest.mark.parametrize("name", ["upsert_account", "issue_key", "release_account"])
    def test_invalid_external_id_is_refused_before_any_request(self, monkeypatch, client, name, bad):
        rec = Recorder(monkeypatch, FakeResponse(200, {}))
        args = (bad, EMAIL) if name == "upsert_account" else (bad,)
        assert str(error_of(getattr(client, name), *args)) == f"Not a Grist provisioning id: {bad!r}"
        assert rec.calls == []


class TestPlumbing:
    def test_repr_has_host_not_key(self, client):
        assert repr(client) == f"GristProvisionerClient({HOST})"
        assert PROV_KEY not in repr(client)

    def test_allow_redirects_false_and_timeout(self, monkeypatch, client):
        rec = Recorder(monkeypatch, FakeResponse(200, {"accounts": 0}))
        client.check()
        assert rec.calls[0]["allow_redirects"] is False
        assert rec.calls[0]["timeout"] == 30

    def test_log_lines_carry_no_email_or_key(self, monkeypatch, client, caplog):
        Recorder(monkeypatch, FakeResponse(200, ACCOUNT_BODY))
        with caplog.at_level(logging.DEBUG, logger="privacyfence.grist_provisioner_api"):
            client.upsert_account("alice", EMAIL)
            Recorder(monkeypatch, FakeResponse(201, KEY_BODY))
            client.issue_key("alice")
        assert caplog.records
        text = caplog.text
        assert "Grist provisioning at prov.example.com: upsert_account alice" in text
        for secret in (EMAIL, PROV_KEY, ISSUED_KEY):
            assert secret not in text
