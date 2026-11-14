"""Tests for the n8n public API client."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import requests

from privacyfence.n8n_api import N8nApi, N8nApiClientError

BASE = "https://n8n.example.test"
HOST = "n8n.example.test"
KEY = "n8n-api-key-SECRET-1111"
TOKEN = "pf-token-SECRET-2222"
CRED_ID = "0123456789abcdef"
OTHER_ID = "ZZ9988"
OK_BODY = {"id": CRED_ID, "name": "PrivacyFence x", "type": "httpBearerAuth"}


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, raw: bytes | None = None) -> None:
        self.status_code = status
        self.closed = False
        if raw is not None:
            self._raw = raw
        elif body is None:
            self._raw = b""
        else:
            import json

            self._raw = json.dumps(body).encode()

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int = 1) -> Any:
        for i in range(0, len(self._raw), chunk_size):
            yield self._raw[i : i + chunk_size]


class Recorder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(requests.Session, "request", self._request)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def api() -> N8nApi:
    return N8nApi(BASE + "/", KEY)


def create(api: N8nApi, project_id: str = "") -> None:
    api.create_bearer_credential(credential_id=CRED_ID, name="PrivacyFence x", token=TOKEN, project_id=project_id)


def error_of(call: Any, *args: Any, **kwargs: Any) -> N8nApiClientError:
    with pytest.raises(N8nApiClientError) as err:
        call(*args, **kwargs)
    return err.value


class TestCreate:
    @pytest.mark.parametrize("status", [200, 201])
    def test_success_sends_body_and_headers(self, monkeypatch, api, status):
        rec = Recorder(monkeypatch, FakeResponse(status, OK_BODY))
        create(api)
        call = rec.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == BASE + "/api/v1/credentials"
        assert call["json"] == {
            "id": CRED_ID,
            "name": "PrivacyFence x",
            "type": "httpBearerAuth",
            "data": {"token": TOKEN},
        }
        assert call["headers"]["X-N8N-API-KEY"] == KEY
        assert call["headers"]["Idempotency-Key"] == CRED_ID
        assert call["headers"]["Accept"] == "application/json"
        assert call["timeout"] == 30
        assert call["allow_redirects"] is False

    def test_project_id_only_when_set(self, monkeypatch, api):
        rec = Recorder(monkeypatch, FakeResponse(200, OK_BODY))
        create(api, "proj1")
        create(api, "")
        assert rec.calls[0]["json"]["projectId"] == "proj1"
        assert "projectId" not in rec.calls[1]["json"]

    def test_wrong_id_deletes_the_returned_id_then_raises(self, monkeypatch, api):
        rec = Recorder(monkeypatch, FakeResponse(200, {**OK_BODY, "id": OTHER_ID}), FakeResponse(200, {}))
        err = error_of(create, api)
        assert str(err) == f"n8n at {HOST} did not create the credential as asked."
        assert rec.calls[1]["method"] == "DELETE"
        assert rec.calls[1]["url"] == f"{BASE}/api/v1/credentials/{OTHER_ID}"

    def test_wrong_id_cleanup_errors_are_swallowed(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(200, {**OK_BODY, "id": OTHER_ID}), FakeResponse(500))
        assert "did not create" in str(error_of(create, api))

    def test_wrong_id_that_is_not_an_id_is_not_deleted(self, monkeypatch, api):
        rec = Recorder(monkeypatch, FakeResponse(200, {**OK_BODY, "id": "../x"}))
        error_of(create, api)
        assert len(rec.calls) == 1

    def test_wrong_type_raises(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(200, {**OK_BODY, "type": "other"}), FakeResponse(200, {}))
        assert "did not create" in str(error_of(create, api))

    def test_unreadable_answer_raises(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(200, raw=b"<html>"))
        assert "did not create" in str(error_of(create, api))

    def test_non_object_answer_raises(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(200, ["x"]))
        assert "did not create" in str(error_of(create, api))

    def test_204_is_not_a_creation(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(204))
        assert error_of(create, api).status == 204


class TestDelete:
    @pytest.mark.parametrize("status", [200, 204])
    def test_deleted(self, monkeypatch, api, status):
        rec = Recorder(monkeypatch, FakeResponse(status, {}))
        assert api.delete_credential(CRED_ID) is True
        assert rec.calls[0]["method"] == "DELETE"
        assert rec.calls[0]["allow_redirects"] is False

    def test_404_is_already_gone(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(404))
        assert api.delete_credential(CRED_ID) is False

    def test_500_raises(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(500))
        err = error_of(api.delete_credential, CRED_ID)
        assert err.status == 500

    @pytest.mark.parametrize("bad", ["", "ABCDEF0123456789", "0123456789abcde", "../etc/passwd", CRED_ID + "0"])
    def test_non_hex_id_raises_before_any_request(self, monkeypatch, api, bad):
        rec = Recorder(monkeypatch, FakeResponse(200, {}))
        with pytest.raises(ValueError):
            api.delete_credential(bad)
        assert rec.calls == []


class TestCheck:
    def test_gets_the_schema(self, monkeypatch, api):
        rec = Recorder(monkeypatch, FakeResponse(200, {"type": "object"}))
        api.check()
        assert rec.calls[0]["method"] == "GET"
        assert rec.calls[0]["url"] == BASE + "/api/v1/credentials/schema/httpBearerAuth"

    def test_401_raises(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(401))
        assert str(error_of(api.check)) == f"n8n at {HOST} answered HTTP 401."


class TestErrors:
    @pytest.mark.parametrize("response", [FakeResponse(503, {"message": "boom"}), FakeResponse(302)])
    def test_a_refused_response_is_closed(self, monkeypatch, api, response):
        Recorder(monkeypatch, response)
        error_of(create, api)
        assert response.closed

    def test_a_successful_response_is_closed(self, monkeypatch, api):
        response = FakeResponse(200, OK_BODY)
        Recorder(monkeypatch, response)
        create(api)
        assert response.closed

    def test_status(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(503, {"message": "boom"}))
        err = error_of(create, api)
        assert str(err) == f"n8n at {HOST} answered HTTP 503."
        assert err.status == 503

    def test_unreachable(self, monkeypatch, api):
        Recorder(monkeypatch, requests.ConnectionError("secret detail"))
        err = error_of(create, api)
        assert str(err) == f"n8n at {HOST} could not be reached."
        assert err.status is None

    def test_redirect_raises_and_is_never_followed(self, monkeypatch, api):
        rec = Recorder(monkeypatch, FakeResponse(302))
        err = error_of(create, api)
        assert str(err) == (
            f"n8n at {HOST} redirected the request; set PRIVACYFENCE_N8N_URL to the address it redirects to."
        )
        assert len(rec.calls) == 1
        assert rec.calls[0]["allow_redirects"] is False

    def test_oversize_body(self, monkeypatch, api):
        Recorder(monkeypatch, FakeResponse(200, raw=b"x" * 1_000_001))
        assert str(error_of(create, api)) == f"n8n at {HOST} did not create the credential as asked."


class TestNoSecretLeaks:
    @pytest.mark.parametrize(
        "response",
        [
            FakeResponse(500, {"message": f"{KEY} {TOKEN}"}),
            FakeResponse(302),
            FakeResponse(200, raw=f"{KEY}{TOKEN}".encode()),
            FakeResponse(200, {**OK_BODY, "id": OTHER_ID, "echo": TOKEN}),
            requests.ConnectionError(f"{KEY} {TOKEN}"),
        ],
    )
    def test_nothing_contains_the_key_or_the_token(self, monkeypatch, caplog, api, response):
        Recorder(monkeypatch, response, response)
        with caplog.at_level(logging.DEBUG):
            err = error_of(create, api)
            deleted = None
            if not isinstance(response, FakeResponse) or response.status_code != 200:
                deleted = error_of(api.delete_credential, CRED_ID)
        for secret in (KEY, TOKEN):
            assert secret not in str(err)
            assert secret not in repr(err)
            assert secret not in caplog.text
            assert err.__cause__ is None
            assert err.__context__ is None
            if deleted is not None:
                assert secret not in str(deleted)
                assert deleted.__context__ is None

    def test_repr_shows_the_host_only(self, api):
        assert repr(api) == f"N8nApi({HOST})"
        assert KEY not in repr(api)
