"""google_errors: which Google answers count as "not found / not shared", and
that the message the agent sees is entirely ours (ADR 0106)."""

from __future__ import annotations

import json
import types

import pytest
from googleapiclient.errors import HttpError

from privacyfence.drive_client import DriveClientError
from privacyfence.google_errors import (
    _MESSAGES,
    NO_PERMISSION,
    NOT_FOUND,
    GoogleResourceUnavailableError,
    classify_http_error,
    unavailable_error,
)
from privacyfence.safe_errors import public_message


def http_error(status, body):
    resp = types.SimpleNamespace(status=status, reason="error")
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HttpError(resp, content)


def v1(status, reason, message="m"):
    return {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}


def v4(status, message, code_status="PERMISSION_DENIED"):
    return {"error": {"code": status, "message": message, "status": code_status}}


def wrapped(status, body):
    err = DriveClientError("x failed: boom")
    err.__cause__ = http_error(status, body)
    return err


def test_404_v1_body_is_not_found():
    assert classify_http_error(wrapped(404, v1(404, "notFound"))) == NOT_FOUND


def test_404_with_non_json_body_is_not_found():
    assert classify_http_error(wrapped(404, b"Not Found")) == NOT_FOUND


def test_410_deleted_is_not_found():
    assert classify_http_error(wrapped(410, v1(410, "deleted"))) == NOT_FOUND


def test_410_without_reason_is_unclassified():
    assert classify_http_error(wrapped(410, {"error": {"code": 410}})) is None


def test_403_insufficient_file_permissions_is_no_permission():
    assert classify_http_error(wrapped(403, v1(403, "insufficientFilePermissions"))) == NO_PERMISSION


def test_403_required_access_level_is_no_permission():
    assert classify_http_error(wrapped(403, v1(403, "requiredAccessLevel"))) == NO_PERMISSION


def test_403_v4_caller_does_not_have_permission():
    body = v4(403, "The caller does not have permission")
    assert classify_http_error(wrapped(403, body)) == NO_PERMISSION


def test_403_rate_limit_is_unclassified():
    assert classify_http_error(wrapped(403, v1(403, "rateLimitExceeded"))) is None


def test_403_missing_scope_is_unclassified():
    assert classify_http_error(wrapped(403, v1(403, "insufficientPermissions"))) is None


def test_403_v4_insufficient_scopes_is_unclassified():
    body = v4(403, "Request had insufficient authentication scopes.")
    assert classify_http_error(wrapped(403, body)) is None


@pytest.mark.parametrize("status", [400, 401, 500])
def test_other_statuses_are_unclassified(status):
    assert classify_http_error(wrapped(status, v1(status, "notFound"))) is None


def test_client_error_without_cause_is_unclassified():
    assert classify_http_error(DriveClientError("x failed")) is None


def test_non_http_cause_is_unclassified():
    err = DriveClientError("x failed")
    err.__cause__ = OSError("net down")
    assert classify_http_error(err) is None


def test_cause_cycle_terminates():
    a, b = DriveClientError("a"), DriveClientError("b")
    a.__cause__, b.__cause__ = b, a
    assert classify_http_error(a) is None


def test_two_levels_of_wrapping_are_followed():
    inner = wrapped(404, v1(404, "notFound"))
    outer = RuntimeError("outer")
    outer.__cause__ = inner
    assert classify_http_error(outer) == NOT_FOUND


def test_unavailable_error_message_is_the_exact_template():
    err = unavailable_error("drive", wrapped(404, v1(404, "notFound")), "alice@example.com")
    assert isinstance(err, GoogleResourceUnavailableError)
    assert str(err) == (
        "Google Drive says this file does not exist or is not shared with the connected "
        "Google account (alice@example.com). Check the link or file ID, or share the file "
        "with that account."
    )


def test_unavailable_error_is_none_when_unclassified():
    assert unavailable_error("drive", wrapped(500, v1(500, "backendError")), "a@b.com") is None


@pytest.mark.parametrize(
    "account", ["", "unknown", "contacts-api (found 3 contact(s))", "tasks-api (found 1 task list(s))"]
)
def test_account_that_is_not_an_address_is_left_out(account):
    err = unavailable_error("drive", wrapped(404, v1(404, "notFound")), account)
    assert "the connected Google account." in str(err)
    assert "(" not in str(err)


_SERVICE_NAMES = {
    "drive": "Google Drive",
    "gmail": "Gmail",
    "calendar": "Google Calendar",
    "contacts": "Google Contacts",
    "tasks": "Google Tasks",
    "apps_script": "Google Apps Script",
}


@pytest.mark.parametrize("connector", sorted(_MESSAGES))
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, v1(404, "notFound")),
        (403, v1(403, "insufficientFilePermissions")),
    ],
    ids=["not_found", "no_permission"],
)
def test_every_connector_has_both_messages(connector, status, body):
    err = unavailable_error(connector, wrapped(status, body), "alice@example.com")
    assert str(err).startswith(f"{_SERVICE_NAMES[connector]} says")
    assert "(alice@example.com)" in str(err)
    assert "{who}" not in str(err)
    assert public_message(err) == str(err)


def test_google_body_text_never_reaches_the_message():
    body = {"error": {"code": 404, "message": "secret-marker-123", "errors": [{"reason": "notFound"}]}}
    err = unavailable_error("drive", wrapped(404, body), "alice@example.com")
    assert "secret-marker-123" not in str(err)
