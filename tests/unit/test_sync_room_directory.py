"""Tests for scripts/sync_room_directory.py.

This script deliberately doesn't import the ``privacyfence`` package (see
its own module docstring -- it's meant to be runnable standalone, without
a PrivacyFence install) and instead carries its own copy of what used to be
src/privacyfence/room_directory_client.py (retired once this script became
its only caller) plus small standalone copies of CalendarRoom's field shape
and secure_files.atomic_write_text. These tests exist to lock in that the
Admin SDK call, its 403 handling, the room-field mapping, and the OAuth
token lifecycle all still work now that they live here instead.

Imported by file path (importlib) rather than as a package, since scripts/
isn't part of the installed ``privacyfence`` distribution -- same pattern as
test_build_org_bundle.py.
"""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync_room_directory.py"
_spec = importlib.util.spec_from_file_location("sync_room_directory", _SCRIPT_PATH)
sync_room_directory = importlib.util.module_from_spec(_spec)
# Register in sys.modules *before* exec_module: sync_room_directory.py's
# @dataclass-decorated _Room needs to resolve its own module by name (for
# ClassVar/InitVar detection) while it's still executing, same as any
# module loaded this way that uses dataclasses.
sys.modules[_spec.name] = sync_room_directory
_spec.loader.exec_module(sync_room_directory)


def make_client(service: MagicMock) -> "sync_room_directory.RoomDirectoryClient":
    client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file="/tmp/unused-room-token.json")
    client._local.service = service
    return client


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


class TestScopes:
    def test_scope_is_admin_directory_readonly_only(self):
        assert sync_room_directory.SCOPES == [
            "https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly"
        ]


class TestAuthorizeInteractive:
    def test_missing_client_config_raises(self, tmp_path):
        client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file=str(tmp_path / "token.json"))
        with pytest.raises(sync_room_directory.RoomDirectoryClientError, match="No admin client config given"):
            client.authorize_interactive()

    def test_runs_local_server_flow_and_persists_returned_credentials(self, tmp_path, monkeypatch):
        token_file = tmp_path / "nested" / "token.json"
        client = sync_room_directory.RoomDirectoryClient(
            client_config={"installed": {"client_id": "cid"}}, token_file=str(token_file)
        )

        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'
        fake_flow = MagicMock()
        fake_flow.run_local_server.return_value = fake_creds
        mock_from_client_config = MagicMock(return_value=fake_flow)
        monkeypatch.setattr(
            "sync_room_directory.InstalledAppFlow.from_client_config", mock_from_client_config
        )

        client.authorize_interactive()

        mock_from_client_config.assert_called_once_with(
            {"installed": {"client_id": "cid"}}, sync_room_directory.SCOPES
        )
        fake_flow.run_local_server.assert_called_once_with(port=0)
        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'


class TestLoadCredentials:
    def test_missing_token_file_raises(self, tmp_path):
        client = sync_room_directory.RoomDirectoryClient(
            client_config={}, token_file=str(tmp_path / "does-not-exist.json")
        )
        with pytest.raises(sync_room_directory.RoomDirectoryClientError, match="No OAuth token found"):
            client._load_credentials()

    def test_valid_token_is_returned_without_refresh_or_network(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = True
        monkeypatch.setattr(
            "sync_room_directory.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file=str(token_file))

        result = client._load_credentials()

        assert result is fake_creds
        fake_creds.refresh.assert_not_called()

    def test_expired_token_with_refresh_token_is_refreshed_and_saved_back(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh-me"
        fake_creds.to_json.return_value = '{"token": "refreshed"}'
        monkeypatch.setattr(
            "sync_room_directory.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file=str(token_file))

        result = client._load_credentials()

        assert result is fake_creds
        fake_creds.refresh.assert_called_once()
        assert token_file.read_text(encoding="utf-8") == '{"token": "refreshed"}'

    def test_expired_token_refresh_failure_raises_clear_error(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh-me"
        fake_creds.refresh.side_effect = Exception("token has been revoked")
        monkeypatch.setattr(
            "sync_room_directory.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file=str(token_file))

        with pytest.raises(
            sync_room_directory.RoomDirectoryClientError,
            match="Failed to refresh Room Directory OAuth token.*revoked",
        ):
            client._load_credentials()

    def test_expired_token_without_refresh_token_raises_invalid_cached_token(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = ""
        monkeypatch.setattr(
            "sync_room_directory.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file=str(token_file))

        with pytest.raises(sync_room_directory.RoomDirectoryClientError, match="Cached Room Directory OAuth token is invalid"):
            client._load_credentials()


class TestSaveToken:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_writes_credentials_json_with_owner_only_permissions(self, tmp_path):
        token_file = tmp_path / "nested" / "token.json"
        client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'

        client._save_token(fake_creds)

        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


class TestGetService:
    def test_same_thread_reuses_cached_service(self, monkeypatch):
        client = sync_room_directory.RoomDirectoryClient(client_config={}, token_file="/tmp/unused-room-token.json")
        mock_build = MagicMock(side_effect=lambda *a, **k: MagicMock())
        monkeypatch.setattr("sync_room_directory.build", mock_build)
        monkeypatch.setattr(client, "_load_credentials", MagicMock(side_effect=MagicMock))

        assert client._get_service() is client._get_service()
        assert mock_build.call_count == 1


class TestListRooms:
    def test_maps_response(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.return_value = {
            "items": [{
                "resourceId": "r1", "resourceName": "Room A", "resourceEmail": "room-a@x.com",
                "buildingId": "b1", "floorName": "3", "capacity": "10",
                "generatedResourceName": "Room A (3rd floor)",
            }]
        }
        client = make_client(service)

        rooms = client.list_rooms()

        assert [asdict(r) for r in rooms] == [asdict(sync_room_directory._Room(
            resource_id="r1", resource_name="Room A", resource_email="room-a@x.com",
            building_id="b1", floor_name="3", capacity=10, description="Room A (3rd floor)",
        ))]

    def test_403_gives_actionable_admin_access_message(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.side_effect = (
            http_error(403)
        )
        client = make_client(service)

        with pytest.raises(sync_room_directory.RoomDirectoryClientError, match="Workspace admin access"):
            client.list_rooms()

    def test_other_http_error_gives_generic_message(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.side_effect = (
            http_error(500)
        )
        client = make_client(service)

        with pytest.raises(sync_room_directory.RoomDirectoryClientError, match="list_rooms failed"):
            client.list_rooms()

    def test_query_param_included_only_when_given(self):
        service = MagicMock()
        service.resources.return_value.calendars.return_value.list.return_value.execute.return_value = {
            "items": []
        }
        client = make_client(service)

        client.list_rooms()
        assert "query" not in service.resources.return_value.calendars.return_value.list.call_args.kwargs

        client.list_rooms(query="floor 3")
        assert service.resources.return_value.calendars.return_value.list.call_args.kwargs["query"] == "floor 3"


class TestLoadAdminClientSecret:
    def test_extracts_installed_block(self, tmp_path):
        secret_path = tmp_path / "client_secret.json"
        secret_path.write_text('{"installed": {"client_id": "cid"}}', encoding="utf-8")

        assert sync_room_directory._load_admin_client_secret(str(secret_path)) == {"client_id": "cid"}

    def test_rejects_file_without_installed_or_web_block(self, tmp_path):
        secret_path = tmp_path / "client_secret.json"
        secret_path.write_text('{"nope": {}}', encoding="utf-8")

        with pytest.raises(SystemExit, match="doesn't look like a Google OAuth client_secret.json"):
            sync_room_directory._load_admin_client_secret(str(secret_path))


def _stub_directory(monkeypatch, items):
    fake_creds = MagicMock()
    fake_creds.valid = True
    monkeypatch.setattr(
        "sync_room_directory.Credentials.from_authorized_user_file",
        MagicMock(return_value=fake_creds),
    )
    service = MagicMock()
    list_call = service.resources.return_value.calendars.return_value.list.return_value.execute
    if isinstance(items, Exception):
        list_call.side_effect = items
    else:
        list_call.return_value = {"items": items}
    monkeypatch.setattr("sync_room_directory.build", MagicMock(return_value=service))


def _write_secrets(tmp_path):
    secret_path = tmp_path / "client_secret.json"
    secret_path.write_text('{"installed": {"client_id": "cid"}}', encoding="utf-8")
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    return secret_path, token_path


class TestMain:
    def test_writes_the_rooms_file(self, tmp_path, monkeypatch):
        secret_path, token_path = _write_secrets(tmp_path)
        output = tmp_path / "rooms.json"
        _stub_directory(monkeypatch, [{
            "resourceId": "r1", "resourceName": "Room A", "resourceEmail": "room-a@x.com",
            "buildingId": "b1", "floorName": "3", "capacity": "10",
        }])

        rc = sync_room_directory.main([
            "--admin-client-secret", str(secret_path),
            "--output", str(output),
            "--token-file", str(token_path),
        ])

        assert rc == 0
        written = json.loads(output.read_text(encoding="utf-8"))
        assert set(written) == {"rooms", "rooms_synced_at"}
        assert written["rooms"] == [{
            "resource_name": "Room A", "resource_email": "room-a@x.com",
            "building_id": "b1", "floor_name": "3", "capacity": 10, "description": "",
        }]
        assert written["rooms_synced_at"].endswith("Z")

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX permission bits have no Windows equivalent here (known, accepted gap)",
    )
    def test_the_rooms_file_is_owner_only(self, tmp_path, monkeypatch):
        secret_path, token_path = _write_secrets(tmp_path)
        output = tmp_path / "rooms.json"
        _stub_directory(monkeypatch, [])

        assert sync_room_directory.main([
            "--admin-client-secret", str(secret_path), "--output", str(output), "--token-file", str(token_path),
        ]) == 0

        assert stat.S_IMODE(output.stat().st_mode) == 0o600

    def test_output_defaults_to_rooms_json_in_the_working_directory(self, tmp_path, monkeypatch):
        secret_path, token_path = _write_secrets(tmp_path)
        _stub_directory(monkeypatch, [])
        monkeypatch.chdir(tmp_path)

        assert sync_room_directory.main([
            "--admin-client-secret", str(secret_path), "--token-file", str(token_path),
        ]) == 0

        assert json.loads((tmp_path / "rooms.json").read_text(encoding="utf-8"))["rooms"] == []

    def test_an_existing_output_is_replaced_not_merged(self, tmp_path, monkeypatch):
        secret_path, token_path = _write_secrets(tmp_path)
        output = tmp_path / "rooms.json"
        output.write_text(json.dumps({"google": {"client_id": "old"}, "rooms": [{"resource_name": "Old"}]}))
        _stub_directory(monkeypatch, [])

        assert sync_room_directory.main([
            "--admin-client-secret", str(secret_path), "--output", str(output), "--token-file", str(token_path),
        ]) == 0

        assert set(json.loads(output.read_text(encoding="utf-8"))) == {"rooms", "rooms_synced_at"}

    def test_org_config_is_refused_before_any_sign_in(self, tmp_path, monkeypatch, capsys):
        secret_path = tmp_path / "client_secret.json"
        secret_path.write_text('{"installed": {"client_id": "cid"}}', encoding="utf-8")
        flow = MagicMock()
        monkeypatch.setattr("sync_room_directory.InstalledAppFlow.from_client_config", flow)
        org_config = tmp_path / "org_config.json"
        org_config.write_text('{"version": 1}', encoding="utf-8")

        rc = sync_room_directory.main([
            "--admin-client-secret", str(secret_path), "--org-config", str(org_config),
            "--token-file", str(tmp_path / "token.json"),
        ])

        assert rc == 2
        assert capsys.readouterr().err == (
            "error: --org-config has been replaced by --output; upload the file on the Admin page (ADR 0257).\n"
        )
        flow.assert_not_called()
        assert org_config.read_text(encoding="utf-8") == '{"version": 1}'
        assert not (tmp_path / "rooms.json").exists()

    def test_org_config_is_hidden_from_help_and_sign_key_is_gone(self, capsys):
        with pytest.raises(SystemExit):
            sync_room_directory.main(["--help"])
        out = capsys.readouterr().out
        usage = out.split("options:")[0]
        assert "--output" in usage
        assert "--org-config" not in usage
        assert "--sign-key" not in out
        with pytest.raises(SystemExit) as excinfo:
            sync_room_directory.main(["--admin-client-secret", "x", "--sign-key", "k"])
        assert excinfo.value.code == 2

    def test_sync_failure_returns_nonzero_and_writes_nothing(self, tmp_path, monkeypatch):
        secret_path, token_path = _write_secrets(tmp_path)
        output = tmp_path / "rooms.json"
        _stub_directory(monkeypatch, http_error(403))

        rc = sync_room_directory.main([
            "--admin-client-secret", str(secret_path), "--output", str(output), "--token-file", str(token_path),
        ])

        assert rc == 1
        assert not output.exists()
