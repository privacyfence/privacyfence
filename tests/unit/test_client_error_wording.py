"""Every Google API client's credential errors must point at a way to
authorize that actually works for the person reading them.

The per-connector OAuth command-line flags only run as the service account
on a packaged install (and a generic ``--oauth-setup`` never existed at all),
so an error telling an end user to "re-run with --something-oauth" is a dead
end. The working route is the Connect page (``/connect``); these tests pin both
the absence of any CLI flag and the presence of that route across every
token-failure path.
"""

from __future__ import annotations

import importlib
import inspect
import re
from unittest.mock import MagicMock

import pytest

_CLI_FLAG = re.compile(r"--[a-z][a-z-]*")
_OAUTH_FLAG_IN_SOURCE = re.compile(r"--[a-z-]*oauth[a-z-]*")

# (module, client class, error class, the service name its wording uses)
_CLIENTS = [
    ("gmail_client", "GmailClient", "GmailClientError", "Gmail"),
    ("drive_client", "DriveClient", "DriveClientError", "Google Drive"),
    ("calendar_client", "CalendarClient", "CalendarClientError", "Google Calendar"),
    ("contacts_client", "ContactsClient", "ContactsClientError", "Google Contacts"),
    ("tasks_client", "TasksClient", "TasksClientError", "Google Tasks"),
    ("apps_script_client", "AppsScriptClient", "AppsScriptClientError", "Apps Script"),
]


def _fake_creds(*, refresh_token: str, refresh_error: Exception | None) -> MagicMock:
    creds = MagicMock()
    creds.valid = False
    creds.expired = True
    creds.refresh_token = refresh_token
    if refresh_error is not None:
        creds.refresh.side_effect = refresh_error
    return creds


def _error_for(module_name, client_name, error_name, path, tmp_path, monkeypatch) -> str:
    module = importlib.import_module(f"privacyfence.{module_name}")
    client_cls = getattr(module, client_name)
    error_cls = getattr(module, error_name)
    token_file = tmp_path / "token.json"
    if path != "missing":
        token_file.write_text("{}", encoding="utf-8")
        creds = _fake_creds(
            refresh_token="" if path == "unrefreshable" else "refresh-me",
            refresh_error=Exception("token revoked") if path == "refresh_failed" else None,
        )
        monkeypatch.setattr(
            f"privacyfence.{module_name}.Credentials.from_authorized_user_file",
            MagicMock(return_value=creds),
        )
    client = client_cls(client_config={}, token_file=str(token_file))
    with pytest.raises(error_cls) as excinfo:
        client._load_credentials()
    return str(excinfo.value)


@pytest.mark.parametrize("path", ["missing", "refresh_failed", "unrefreshable"])
@pytest.mark.parametrize(("module_name", "client_name", "error_name", "service"), _CLIENTS)
def test_credential_error_names_the_connect_page_not_a_cli_flag(
    module_name, client_name, error_name, service, path, tmp_path, monkeypatch
):
    message = _error_for(module_name, client_name, error_name, path, tmp_path, monkeypatch)

    assert not _CLI_FLAG.search(message), message
    assert f"Connect {service} on the Connect page (/connect)" in message


@pytest.mark.parametrize(("module_name", "client_name", "error_name", "service"), _CLIENTS)
def test_client_module_never_mentions_an_oauth_cli_flag(module_name, client_name, error_name, service):
    source = inspect.getsource(importlib.import_module(f"privacyfence.{module_name}"))

    assert not _OAUTH_FLAG_IN_SOURCE.search(source)
