"""Tests for the retrying Google transport (ADR 0105)."""

from __future__ import annotations

import http.client
import logging
import re
import socket
import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch

import googleapiclient.discovery
import googleapiclient.http
import httplib2
import pytest

import privacyfence
from privacyfence.google_http import (
    RETRYABLE_ERRORS,
    RetryOnceAuthorizedHttp,
    authorized_http,
)

URI = "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=secret-query-xyz"
LOGGER = "privacyfence.google_http"


def _eof() -> ssl.SSLEOFError:
    return ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2427)")


def _ok(body: bytes = b"{}") -> tuple[httplib2.Response, bytes]:
    return httplib2.Response({"status": "200"}), body


def _creds() -> MagicMock:
    return MagicMock(universe_domain="googleapis.com")


def _transport(side_effect) -> tuple[RetryOnceAuthorizedHttp, MagicMock]:
    inner = MagicMock(spec=httplib2.Http)
    inner.request.side_effect = side_effect
    return RetryOnceAuthorizedHttp(_creds(), http=inner), inner


def _infos(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


@pytest.mark.parametrize(
    "error",
    [
        _eof(),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        ConnectionResetError(),
        ConnectionAbortedError(),
        BrokenPipeError(),
    ],
    ids=lambda e: type(e).__name__,
)
def test_get_retries_once_after_error(error, caplog):
    assert isinstance(error, RETRYABLE_ERRORS)
    caplog.set_level(logging.INFO, logger=LOGGER)
    ok = _ok()
    transport, inner = _transport([error, ok])

    assert transport.request(URI, "GET") == ok

    assert inner.request.call_count == 2
    assert inner.close.call_count == 1
    records = _infos(caplog)
    assert len(records) == 1
    assert type(error).__name__ in records[0].getMessage()
    assert "gmail.googleapis.com" in records[0].getMessage()
    assert "secret-query-xyz" not in caplog.text


def test_two_drops_in_a_row_raise_the_second(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    first, second = _eof(), _eof()
    transport, inner = _transport([first, second])

    with pytest.raises(ssl.SSLEOFError) as raised:
        transport.request(URI, "GET")

    assert raised.value is second
    assert inner.request.call_count == 2
    assert "secret-query-xyz" not in caplog.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_are_never_retried(method, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    transport, inner = _transport([_eof(), _ok()])

    with pytest.raises(ssl.SSLEOFError):
        transport.request(URI, method, body=b"{}")

    assert inner.request.call_count == 1
    inner.close.assert_not_called()
    assert _infos(caplog) == []
    assert "secret-query-xyz" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ssl.SSLCertVerificationError(),
        ConnectionRefusedError(),
        httplib2.ServerNotFoundError("x"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_non_transport_errors_are_not_retried(error, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    transport, inner = _transport([error, _ok()])

    with pytest.raises(type(error)):
        transport.request(URI, "GET")

    assert inner.request.call_count == 1
    assert "secret-query-xyz" not in caplog.text


@pytest.mark.parametrize("status", [429, 500, 503])
def test_http_error_responses_are_not_retried(status):
    response = (httplib2.Response({"status": str(status)}), b"{}")
    transport, inner = _transport([response, _ok()])

    assert transport.request(URI, "GET") == response
    assert inner.request.call_count == 1


def test_refresh_reentry_does_not_retry_again(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    unauthorized = (httplib2.Response({"status": "401"}), b"")
    transport, inner = _transport([unauthorized, _eof(), _eof()])
    transport.credentials.refresh = MagicMock()

    with pytest.raises(ssl.SSLEOFError):
        transport.request(URI, "GET")

    assert inner.request.call_count == 3
    assert len(_infos(caplog)) == 1
    assert "secret-query-xyz" not in caplog.text


def test_refresh_reentry_passes_through(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    transport, inner = _transport([_eof(), _ok()])

    with pytest.raises(ssl.SSLEOFError):
        transport.request(URI, "GET", _credential_refresh_attempt=1)

    assert inner.request.call_count == 1
    assert _infos(caplog) == []


def test_authorized_http_defaults_to_build_http():
    creds = _creds()
    with patch.object(socket, "getdefaulttimeout", return_value=None):
        transport = authorized_http(creds)

    assert isinstance(transport, RetryOnceAuthorizedHttp)
    assert transport.credentials is creds
    assert 308 not in transport.http.redirect_codes
    assert transport.http.timeout == googleapiclient.http.DEFAULT_HTTP_TIMEOUT_SEC


def test_authorized_http_uses_the_given_transport():
    inner = MagicMock(spec=httplib2.Http)
    assert authorized_http(_creds(), http=inner).http is inner


def test_execute_through_googleapiclient():
    inner = MagicMock(spec=httplib2.Http)
    inner.request.side_effect = [_eof(), _ok(b'{"messages": []}')]
    service = googleapiclient.discovery.build(
        "gmail", "v1", http=RetryOnceAuthorizedHttp(_creds(), http=inner), cache_discovery=False
    )
    assert service.users().messages().list(userId="me").execute() == {"messages": []}
    assert inner.request.call_count == 2

    write_inner = MagicMock(spec=httplib2.Http)
    write_inner.request.side_effect = [_eof(), _ok()]
    write_service = googleapiclient.discovery.build(
        "gmail", "v1", http=RetryOnceAuthorizedHttp(_creds(), http=write_inner), cache_discovery=False
    )
    with pytest.raises(ssl.SSLEOFError):
        write_service.users().drafts().create(userId="me", body={}).execute()
    assert write_inner.request.call_count == 1


def test_every_google_client_builds_with_the_retrying_transport():
    matched = []
    for path in sorted(Path(privacyfence.__file__).parent.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from googleapiclient.discovery import build" not in text:
            continue
        matched.append(path.name)
        assert not re.search(r"build\([^)]*credentials=", text, re.S), (
            f"{path.name} builds a Google service with credentials=; use google_http.authorized_http"
        )
        assert "authorized_http(" in text, f"{path.name} does not use google_http.authorized_http"
    assert len(matched) >= 6
