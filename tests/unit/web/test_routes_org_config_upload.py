"""The helpers ``web/routes_settings.py`` shares with the Admin routes' multipart uploads: the bounded
form read, the step-up check bound to a file's hash, the form-field assertion parse, the argument
coercion and the token fingerprint bodies."""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from privacyfence import paths
from privacyfence import webauthn_stepup as wa
from privacyfence.principal import Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import routes_settings as rs
from privacyfence.webauthn_stepup import StepUpChallengeStore

ORIGIN = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
LIMIT = 100
VERIFIED = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _refuse(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _form_client(**kwargs) -> TestClient:
    async def read(request):
        form = await rs.read_bounded_form(request, max_bytes=LIMIT, slack=1024, refuse=_refuse, **kwargs)
        if isinstance(form, JSONResponse):
            return form
        return JSONResponse({"fields": sorted(form.keys())})

    return TestClient(Starlette(routes=[Route("/up", read, methods=["POST"])]))


class TestReadBoundedForm:
    def test_a_file_within_the_limit_is_read(self):
        r = _form_client().post("/up", data={"csrf": "x"}, files={"file": ("a.json", b"{}", "application/json")})
        assert (r.status_code, r.json()) == (200, {"fields": ["csrf", "file"]})

    def test_a_body_that_does_not_declare_its_size_is_refused_with_411(self):
        r = _form_client().post("/up", content=iter([b"--x--"]), headers={"Content-Type": "multipart/form-data; boundary=x"})
        assert (r.status_code, r.json()) == (411, {"error": "The upload must declare its size."})

    def test_a_declared_size_past_the_limit_is_refused_with_413_and_the_callers_text(self):
        r = _form_client(too_large="Too big.").post(
            "/up", data={"csrf": "x"}, files={"file": ("a.json", b" " * (LIMIT + 2048), "application/json")},
        )
        assert (r.status_code, r.json()) == (413, {"error": "Too big."})

    def test_the_default_refusal_text_for_an_oversized_upload(self):
        r = _form_client().post(
            "/up", data={"csrf": "x"}, files={"file": ("a.json", b" " * (LIMIT + 2048), "application/json")},
        )
        assert r.json() == {"error": "The file is too large."}

    def test_a_second_file_is_refused_with_400(self):
        r = _form_client().post("/up", files=[
            ("file", ("a.json", b"{}", "application/json")), ("file", ("b.json", b"{}", "application/json")),
        ])
        assert (r.status_code, r.json()) == (400, {"error": "Upload exactly one file."})


def _gate_client(step_up: StepUpConfig | None, *, always: bool) -> TestClient:
    challenges = StepUpChallengeStore()

    async def upload(request):
        form = await request.form()
        raw = await form["file"].read()
        refusal = rs._gate_upload(
            "org_config_upload", {"sha256": hashlib.sha256(raw).hexdigest()},
            rs._parse_form_assertion(form.get("webauthn_assertion")),
            principal=ALICE, step_up=step_up, step_up_origin=ORIGIN, challenges=challenges, always=always,
        )
        return refusal if refusal is not None else JSONResponse({"ok": True})

    return TestClient(Starlette(routes=[Route("/up", upload, methods=["POST"])]), base_url=ORIGIN)


def _enroll():
    wa.add_credential(ALICE, wa.WebAuthnCredential(
        credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
    ))


def _upload(client, raw=b"{}", **fields):
    return client.post("/up", data=fields, files={"file": ("a.json", raw, "application/json")})


class TestGateUpload:
    def test_nothing_is_asked_when_step_up_is_not_in_force(self):
        assert _upload(_gate_client(StepUpConfig(enabled=True, rp_id="pf.example.com"), always=False)).json() == {"ok": True}
        assert _upload(_gate_client(None, always=False)).json() == {"ok": True}

    def test_always_asks_even_when_step_up_is_off_and_hard_fails_without_a_passkey(self):
        client = _gate_client(StepUpConfig(enabled=False, rp_id="pf.example.com"), always=True)
        r = _upload(client)
        assert (r.status_code, r.json()["error"]) == (403, "Add a passkey on /security first.")

    def test_a_required_passkey_challenges_then_accepts_an_assertion_for_the_same_file(self):
        _enroll()
        client = _gate_client(StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True), always=False)
        assert _upload(client).status_code == 428
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=VERIFIED):
            r = _upload(client, webauthn_assertion=json.dumps({"id": "Y3JlZC0x"}))
        assert (r.status_code, r.json()) == (200, {"ok": True})

    def test_an_assertion_made_for_a_different_file_is_not_accepted(self):
        _enroll()
        client = _gate_client(StepUpConfig(enabled=True, rp_id="pf.example.com", require_passkey=True), always=False)
        assert _upload(client, b"{}").status_code == 428
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=VERIFIED):
            r = _upload(client, b'{"other": 1}', webauthn_assertion=json.dumps({"id": "Y3JlZC0x"}))
        assert r.status_code == 400


@pytest.mark.parametrize(("value", "expected"), [
    (None, None), ("", None), ("not json", None), ("[1]", None), ('{"id": "x"}', {"id": "x"}),
])
def test_a_form_assertion_parses_to_a_dict_or_nothing(value, expected):
    assert rs._parse_form_assertion(value) == expected


class TestCoerce:
    def test_an_integer_accepts_digits_in_a_string_and_refuses_the_rest(self):
        assert rs._coerce("7", int) == 7
        for bad in (True, 1.5, None, "seven"):
            with pytest.raises(rs._BadAction, match="expected an integer"):
                rs._coerce(bad, int)

    def test_a_string_treats_none_as_empty_and_refuses_other_types(self):
        assert rs._coerce(None, str) == ""
        assert rs._coerce("x", str) == "x"
        with pytest.raises(rs._BadAction, match="expected a string"):
            rs._coerce(3, str)

    def test_a_bool_and_an_unannotated_value_pass_through(self):
        assert rs._coerce(1, bool) is True
        assert rs._coerce({"a": 1}, None) == {"a": 1}


class TestTokenFingerprintBodies:
    def test_revoke_needs_a_string_id(self):
        assert rs._scoped_revoke_fingerprint_body({"token_id": "t"}) == {"token_id": "t"}
        assert rs._scoped_revoke_fingerprint_body({"token_id": 1}) is None

    def test_rename_needs_a_string_id_and_label(self):
        assert rs._scoped_rename_fingerprint_body({"token_id": "t", "label": "l"}) == {"token_id": "t", "label": "l"}
        assert rs._scoped_rename_fingerprint_body({"token_id": "t"}) is None

    def test_extend_needs_a_string_id_and_an_integer_day_count(self):
        assert rs._scoped_extend_fingerprint_body({"token_id": "t", "days": 5}) == {"token_id": "t", "days": 5}
        assert rs._scoped_extend_fingerprint_body({"token_id": "t", "days": True}) is None
        assert rs._scoped_extend_fingerprint_body({"token_id": "t", "days": "5"}) is None
