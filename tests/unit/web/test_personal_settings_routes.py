"""Tests for web/personal_settings_routes.py on an organization server: the export download, the
preview and the step-up-gated, merge-only import. The factory takes hooks for the mode-specific
parts, so the hook-only branches are exercised here with fakes too."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import yaml
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from privacyfence import paths, personal_settings as ps, step_up_config
from privacyfence import webauthn_stepup as wa
from privacyfence.policy import scope_rows
from privacyfence.policy.scope_rows import ScopeRow
from privacyfence.principal import Principal, principal_scope
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session
from privacyfence.web import personal_settings_routes as psr
from privacyfence.web import routes_settings as rs

BASE_URL = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
NOW = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
FILE = ("privacyfence-personal-settings.json", None, "application/json")
STEP_UP_ON = StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True)


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


@pytest.fixture
def audits(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(rs, "_record_settings_audit", lambda principal, summary: lines.append(summary))
    return lines


def _settings_path(tmp_path):
    return tmp_path / "users" / ALICE.id / "authority" / "config" / "settings.yaml"


def _seed(tmp_path, rows=()):
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {"auto_accept": scope_rows.rows_to_config(rows)} if rows else {}
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _stored_rows(tmp_path) -> list[ScopeRow]:
    """The rows on disk, read without going through a request."""
    path = _settings_path(tmp_path)
    return scope_rows.rows_from_config(yaml.safe_load(path.read_text())) if path.exists() else []


@pytest.fixture
def home(tmp_path):
    """The signed-in principal's settings directory, empty."""
    _seed(tmp_path)
    return tmp_path


def _client(tmp_path, *, signed_in=True, step_up: StepUpConfig | None = None):
    sessions = org_session.OrgSessionStore()
    routes = rs.build_org_routes(
        sessions=sessions, install_wide_settings={}, step_up=step_up or StepUpConfig(),
        step_up_origin=BASE_URL,
    )
    client = TestClient(Starlette(routes=routes), base_url=BASE_URL)
    csrf = "x"
    if signed_in:
        csrf = sessions.create(ALICE)
        client.cookies.set(org_session.SESSION_COOKIE, csrf)
    return client, csrf


def _row(scope: str, value: list[str], verbs: tuple | None = None) -> ScopeRow:
    wanted = tuple(verbs) if verbs is not None else scope_rows.scope_verbs(scope)
    return ScopeRow(
        id=scope_rows.row_id_for(scope, value), scope=scope, value=value,
        verbs=frozenset(wanted), operations=scope_rows.implied_operations(scope, wanted),
    )


def _file_bytes(rows, step_up_scope=None) -> bytes:
    return ps.export_bytes(ps.build_export(rows, step_up_scope, mode="org", version="6.3.0", now=NOW))


def _post(client, path, raw, csrf, **fields):
    data = {"csrf": csrf, **fields}
    return client.post(path, data=data, files={"file": FILE[:1] + (raw, FILE[2])})


def _import(client, raw, csrf, **fields):
    fields.setdefault("sha256", hashlib.sha256(raw).hexdigest())
    fields.setdefault("confirmed", "true")
    return _post(client, "/api/settings/personal/import", raw, csrf, **fields)


def _enroll():
    wa.add_credential(ALICE, wa.WebAuthnCredential(
        credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
    ))


FAKE_VERIFIED = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()


KEEP_VERBS = scope_rows.scope_verbs("drive.folder")[:1]


class TestExport:
    def test_is_a_download_with_the_exact_headers_and_a_parseable_body(self, home, audits):
        _seed(home, [_row("drive.folder", ["FOLDER1"], verbs=KEEP_VERBS)])
        client, _ = _client(home)
        r = client.get("/api/settings/personal/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["content-disposition"] == 'attachment; filename="privacyfence-personal-settings.json"'
        assert r.headers["cache-control"] == "no-store"
        document = ps.parse_document(r.content)
        assert [row.scope for row in document.rows] == ["drive.folder"]
        assert json.loads(r.content)["source"]["mode"] == "org"
        assert audits == [f"Exported personal settings (1 rule(s)) (principal={ALICE.id})"]

    def test_carries_the_personal_step_up_scope(self, home):
        with principal_scope(ALICE):
            step_up_config.set_personal_scope(ALICE, "writes_and_reads", floor="writes")
        client, _ = _client(home, step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes"))
        assert json.loads(client.get("/api/settings/personal/export").content)["step_up"] == {
            "scope": "writes_and_reads",
        }

    def test_requires_a_session(self, home):
        client, _ = _client(home, signed_in=False)
        assert client.get("/api/settings/personal/export").status_code == 401


class TestPreview:
    def test_a_good_file_returns_the_preview_and_writes_nothing(self, home):
        _seed(home, [_row("drive.folder", ["KEEP"], verbs=KEEP_VERBS)])
        before = _stored_rows(home)
        client, csrf = _client(home)
        raw = _file_bytes([
            _row("drive.folder", ["KEEP"], verbs=scope_rows.scope_verbs("drive.folder")[:1]),
            _row("drive.folder", ["NEW"]),
        ])
        r = _post(client, "/api/settings/personal/preview", raw, csrf)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {
            "sha256", "source_mode", "exported_at", "counts", "dropped_rows", "items", "step_up", "warnings",
        }
        assert body["source_mode"] == "org"
        assert body["counts"]["add"] == 1
        assert _stored_rows(home) == before

    @pytest.mark.parametrize(("raw", "message"), [
        (b"not json", "This is not a PrivacyFence settings export."),
        (b'{"format": "x"}', "This is not a PrivacyFence settings export."),
        (json.dumps({"format": ps.FORMAT, "format_version": 2}).encode(),
         "This file was made by a newer PrivacyFence. Update this install and try again."),
        (json.dumps({"format": ps.FORMAT, "format_version": 1}).encode(), "This settings export has no rules section."),
    ])
    def test_a_bad_file_is_a_400_with_the_exact_message(self, home, raw, message):
        client, csrf = _client(home)
        r = _post(client, "/api/settings/personal/preview", raw, csrf)
        assert r.status_code == 400
        assert r.json() == {"error": message}

    def test_missing_file_is_a_400(self, home):
        client, csrf = _client(home)
        r = client.post("/api/settings/personal/preview", data={"csrf": csrf})
        assert r.status_code == 400
        assert r.json() == {"error": "missing file"}

    def test_requires_a_session_a_csrf_and_the_right_origin(self, home):
        raw = _file_bytes([])
        anonymous, _ = _client(home, signed_in=False)
        assert _post(anonymous, "/api/settings/personal/preview", raw, "x").status_code == 401
        client, csrf = _client(home)
        assert _post(client, "/api/settings/personal/preview", raw, "wrong").status_code == 401
        r = client.post(
            "/api/settings/personal/preview", data={"csrf": csrf}, headers={"Origin": "http://evil.example"},
            files={"file": ("f.json", raw, "application/json")},
        )
        assert r.status_code == 403

    def test_a_non_string_csrf_field_is_refused(self, home):
        client, _ = _client(home)
        r = client.post(
            "/api/settings/personal/preview", files={"file": ("f.json", _file_bytes([]), "application/json")},
        )
        assert r.status_code == 401

    def test_oversized_content_length_is_a_413_before_the_form_is_read(self, home):
        client, csrf = _client(home)
        # Starlette cannot declare more than it sends, so call the route function's check through a
        # real oversized multipart envelope: a file past the envelope slack is refused on Content-Length.
        r = _post(client, "/api/settings/personal/preview", b" " * (ps.MAX_IMPORT_BYTES + 65_537), csrf)
        assert r.status_code == 413

    def test_oversized_file_is_a_413(self, home):
        client, csrf = _client(home)
        r = _post(client, "/api/settings/personal/preview", b" " * (ps.MAX_IMPORT_BYTES + 1), csrf)
        assert r.status_code == 413
        assert r.json() == {"error": "This file is too large to be a PrivacyFence settings export."}


class TestImport:
    def test_merges_and_keeps_an_unrelated_rule(self, home, audits):
        _seed(home, [_row("drive.folder", ["KEEP"], verbs=KEEP_VERBS)])
        client, csrf = _client(home)
        raw = _file_bytes([_row("drive.folder", ["NEW"]), _row("drive.folder", ["KEEP"])])
        r = _import(client, raw, csrf)
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == 1 and body["step_up_applied"] is False
        assert {tuple(row.value or ()) for row in _stored_rows(home)} == {("KEEP",), ("NEW",)}
        assert audits and audits[-1].startswith("Imported personal settings: 1 added")

    def test_a_duplicate_is_present_and_applying_twice_is_a_no_op(self, home, audits):
        client, csrf = _client(home)
        raw = _file_bytes([_row("drive.folder", ["NEW"]), _row("drive.folder", ["NEW"])])
        first = _import(client, raw, csrf).json()
        assert (first["added"], first["present"]) == (1, 1)
        rows_after_first = _stored_rows(home)
        second = _import(client, raw, csrf).json()
        assert (second["added"], second["present"]) == (0, 2)
        assert _stored_rows(home) == rows_after_first

    def test_the_audit_line_has_counts_and_no_rule_value(self, home, audits):
        client, csrf = _client(home)
        _import(client, _file_bytes([_row("drive.folder", ["SECRET_FOLDER_ID"])]), csrf)
        line = audits[-1]
        assert line == (
            "Imported personal settings: 1 added, 0 extended, 0 already present, 0 skipped "
            f"(principal={ALICE.id})"
        )
        assert "SECRET_FOLDER_ID" not in line and "drive" not in line

    def test_a_stale_preview_is_replanned_against_the_current_rows(self, home):
        client, csrf = _client(home)
        raw = _file_bytes([_row("drive.folder", ["NEW"])])
        preview = _post(client, "/api/settings/personal/preview", raw, csrf).json()
        assert preview["counts"]["add"] == 1
        _seed(home, [_row("drive.folder", ["NEW"])])
        result = _import(client, raw, csrf, sha256=preview["sha256"]).json()
        assert (result["added"], result["present"]) == (0, 1)

    def test_applies_the_step_up_scope_when_step_up_is_on(self, home):
        client, csrf = _client(home, step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        result = _import(client, _file_bytes([], step_up_scope="writes_and_reads"), csrf).json()
        assert result["step_up_applied"] is True
        with principal_scope(ALICE):
            assert step_up_config.personal_scope(ALICE) == "writes_and_reads"

    def test_gates_in_order(self, home):
        raw = _file_bytes([_row("drive.folder", ["NEW"])])
        anonymous, _ = _client(home, signed_in=False)
        assert _import(anonymous, raw, "x").status_code == 401
        client, csrf = _client(home)
        assert _import(client, raw, "wrong").status_code == 401
        r = client.post(
            "/api/settings/personal/import", data={"csrf": csrf, "confirmed": "true"},
            headers={"Origin": "http://evil.example"}, files={"file": ("f.json", raw, "application/json")},
        )
        assert r.status_code == 403
        r = _import(client, raw, csrf, confirmed="false")
        assert (r.status_code, r.json()) == (400, {"error": "confirmation required"})
        r = _import(client, raw, csrf, sha256="0" * 64)
        assert (r.status_code, r.json()) == (400, {"error": "file_changed"})
        assert _import(client, b"junk", csrf).status_code == 400
        assert _import(client, b" " * (ps.MAX_IMPORT_BYTES + 1), csrf).status_code == 413
        r = client.post("/api/settings/personal/import", data={"csrf": csrf, "confirmed": "true"})
        assert r.json() == {"error": "missing file"}
        assert _stored_rows(home) == []

    def test_an_oversized_envelope_is_a_413_before_the_form_is_read(self, home):
        client, csrf = _client(home)
        r = _post(client, "/api/settings/personal/import", b" " * (ps.MAX_IMPORT_BYTES + 65_537), csrf)
        assert r.status_code == 413

    def test_a_non_string_csrf_is_refused(self, home):
        client, _ = _client(home)
        r = client.post(
            "/api/settings/personal/import", files={"file": ("f.json", _file_bytes([]), "application/json")},
        )
        assert r.status_code == 401


class TestImportStepUp:
    def _setup(self, home):
        _enroll()
        return _client(home, step_up=STEP_UP_ON)

    def test_without_an_assertion_the_import_is_challenged_and_writes_nothing(self, home):
        client, csrf = self._setup(home)
        r = _import(client, _file_bytes([_row("drive.folder", ["NEW"])]), csrf)
        assert r.status_code == 428
        assert "webauthn_options" in r.json()
        assert _stored_rows(home) == []

    def test_no_enrolled_passkey_hard_fails(self, home):
        client, csrf = _client(home, step_up=STEP_UP_ON)
        r = _import(client, _file_bytes([_row("drive.folder", ["NEW"])]), csrf)
        assert (r.status_code, r.json()["error"]) == (403, "passkey_enrollment_required")

    def test_a_valid_assertion_applies(self, home):
        client, csrf = self._setup(home)
        raw = _file_bytes([_row("drive.folder", ["NEW"])])
        assert _import(client, raw, csrf).status_code == 428
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=FAKE_VERIFIED):
            r = _import(client, raw, csrf, webauthn_assertion=json.dumps({"id": "Y3JlZC0x"}))
        assert r.status_code == 200
        assert r.json()["added"] == 1
        assert len(_stored_rows(home)) == 1

    def test_an_assertion_made_for_a_different_file_is_rejected(self, home):
        client, csrf = self._setup(home)
        assert _import(client, _file_bytes([_row("drive.folder", ["ONE"])]), csrf).status_code == 428
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=FAKE_VERIFIED):
            r = _import(
                client, _file_bytes([_row("drive.folder", ["TWO"])]), csrf,
                webauthn_assertion=json.dumps({"id": "Y3JlZC0x"}),
            )
        assert r.status_code == 400
        assert _stored_rows(home) == []

    @pytest.mark.parametrize("assertion", ["not json", "[1]", ""])
    def test_a_malformed_assertion_reads_as_none(self, home, assertion):
        client, csrf = self._setup(home)
        r = _import(client, _file_bytes([_row("drive.folder", ["NEW"])]), csrf, webauthn_assertion=assertion)
        assert r.status_code == 428


class TestScopeError:
    """The org hooks turn an error from open_scope into a response; the factory honours both outcomes."""

    def _hooks(self, **overrides):
        from contextlib import contextmanager

        @contextmanager
        def open_scope(principal):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        base = dict(
            authenticate=lambda request: ALICE,
            check_mutation=lambda request, csrf: None,
            open_scope=open_scope,
            scope_error=lambda principal, exc: JSONResponse({"error": "handled"}, status_code=500),
            read_rows=lambda: [], merge_rules=lambda rules: False,
            step_up_state=lambda: (False, None), step_up_gate=lambda principal, body, assertion: None,
            audit=lambda principal, summary: None, mode="org",
        )
        base.update(overrides)
        return psr.PersonalSettingsHooks(**base)

    def _client(self, hooks):
        from starlette.applications import Starlette
        return TestClient(Starlette(routes=psr.build_personal_routes(hooks)), raise_server_exceptions=True)

    def test_a_handled_error_becomes_the_response_on_all_three_routes(self):
        client = self._client(self._hooks())
        raw = _file_bytes([])
        assert client.get("/api/settings/personal/export").json() == {"error": "handled"}
        files = {"file": ("f.json", raw, "application/json")}
        assert client.post("/api/settings/personal/preview", data={"csrf": "x"}, files=files).status_code == 500
        import hashlib
        r = client.post(
            "/api/settings/personal/import",
            data={"csrf": "x", "confirmed": "true", "sha256": hashlib.sha256(raw).hexdigest()}, files=files,
        )
        assert r.json() == {"error": "handled"}

    def test_an_unhandled_error_is_re_raised(self):
        client = self._client(self._hooks(scope_error=lambda principal, exc: None))
        with pytest.raises(RuntimeError, match="boom"):
            client.get("/api/settings/personal/export")

    def test_a_refusal_from_check_mutation_wins(self):
        refusal = JSONResponse({"error": "no"}, status_code=403)
        client = self._client(self._hooks(check_mutation=lambda request, csrf: refusal))
        files = {"file": ("f.json", _file_bytes([]), "application/json")}
        assert client.post("/api/settings/personal/preview", data={"csrf": "x"}, files=files).status_code == 403
        assert client.post("/api/settings/personal/import", data={"csrf": "x"}, files=files).status_code == 403

    def test_a_bad_content_length_header_is_treated_as_unset(self):
        client = self._client(self._hooks(open_scope=lambda principal: __import__("contextlib").nullcontext()))
        files = {"file": ("f.json", _file_bytes([]), "application/json")}
        r = client.post(
            "/api/settings/personal/preview", data={"csrf": "x"}, files=files, headers={"Content-Length": "abc"},
        )
        assert r.status_code == 200
