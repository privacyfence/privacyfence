"""Tests for web/routes_downloads.py: the GET /downloads/{token} claim
route."""
from __future__ import annotations

import base64
import json
import urllib.parse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence import paths
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.download_staging import DownloadStagingStore
from privacyfence.principal import Principal
from privacyfence.web import org_session, routes_downloads
from privacyfence.web import routes_org_identity as roi

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com")
BOB = Principal(id="bob", email="bob@example.com")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _app(*, sessions=None, store=None):
    sessions = sessions or org_session.OrgSessionStore()
    store = store if store is not None else DownloadStagingStore()
    routes = routes_downloads.build_routes(sessions=sessions, store=store)
    return Starlette(routes=routes), sessions, store


def _client(app) -> TestClient:
    return TestClient(app, base_url=ISSUER, follow_redirects=False)


def _signed_in(client: TestClient, sessions: org_session.OrgSessionStore, principal: Principal) -> None:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)


def _url_token(token: bytes) -> str:
    return base64.urlsafe_b64encode(token).decode("ascii")


class TestAuthRequired:
    def test_no_cookie_redirects_to_login_and_back_to_the_link(self):
        app, _sessions, store = _app()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        r = _client(app).get(f"/downloads/{_url_token(token)}")
        assert r.status_code == 302
        assert r.headers["cache-control"] == "no-store"
        location = urllib.parse.urlsplit(r.headers["location"])
        assert location.path == "/login"
        assert urllib.parse.parse_qs(location.query) == {"next": [f"/downloads/{_url_token(token)}"]}


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _sign_in_through_the_idp(client: TestClient, monkeypatch, login_location: str, principal: Principal):
    """Follow a /login redirect through a faked IdP to the callback's own redirect."""
    monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
    monkeypatch.setattr(
        roi.org_identity, "verify_id_token",
        lambda idp, token, *, nonce: {"sub": principal.id, "email": principal.email, "nonce": nonce},
    )
    to_idp = client.get(login_location)
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(to_idp.headers["location"]).query))["state"]
    return client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")


class TestSignInReturnsToTheLink:
    """/downloads/{token} -> /login?next=... -> IdP -> callback -> /downloads/{token}, with the
    real /login route in front of the claim."""

    def _app(self):
        sessions = org_session.OrgSessionStore()
        store = DownloadStagingStore()
        routes = roi.build_routes(idp=_idp(), sessions=sessions, base_url=ISSUER)
        routes += routes_downloads.build_routes(sessions=sessions, store=store)
        return Starlette(routes=routes), store

    def test_the_staged_principal_gets_the_file_after_signing_in(self, monkeypatch):
        app, store = self._app()
        token = store.stage(ALICE, b"file bytes", "report.pdf", "application/pdf")
        client = _client(app)
        bounce = client.get(f"/downloads/{_url_token(token)}")
        back = _sign_in_through_the_idp(client, monkeypatch, bounce.headers["location"], ALICE)
        assert back.headers["location"] == f"/downloads/{_url_token(token)}"
        r = client.get(back.headers["location"])
        assert r.status_code == 200
        assert r.content == b"file bytes"

    def test_signing_in_as_someone_else_still_cannot_claim_it(self, monkeypatch):
        # The redirect carries the link through sign-in; it does not carry the right to use it.
        app, store = self._app()
        token = store.stage(ALICE, b"file bytes", "report.pdf", "application/pdf")
        client = _client(app)
        bounce = client.get(f"/downloads/{_url_token(token)}")
        back = _sign_in_through_the_idp(client, monkeypatch, bounce.headers["location"], BOB)
        assert back.headers["location"] == f"/downloads/{_url_token(token)}"
        r = client.get(back.headers["location"])
        assert r.status_code == 404
        # ...and the file is still there for the principal it was staged for.
        assert store.claim(token, ALICE.id) is not None

    @pytest.mark.parametrize(
        "token",
        ["%5C%5Cevil.example.com", "%5Cevil.example.com", "https:%2F%2Fevil.example.com",
         "%2F%2Fevil.example.com", "x%3Fnext%3D%2F%2Fevil.example.com", "%09%2F%2Fevil.example.com",
         "..%5C..%5Cevil.example.com"],
    )
    def test_next_cannot_be_turned_into_an_open_redirect(self, monkeypatch, token):
        """Whatever the path segment holds, the sign-in lands on a same-origin /downloads/ path."""
        app, _store = self._app()
        client = _client(app)
        bounce = client.get(f"/downloads/{token}")
        if bounce.status_code == 404:
            return  # never matched this route at all, so nothing was redirected
        assert bounce.status_code == 302
        back = _sign_in_through_the_idp(client, monkeypatch, bounce.headers["location"], ALICE)
        target = back.headers["location"]
        parts = urllib.parse.urlsplit(target)
        assert (parts.scheme, parts.netloc) == ("", "")
        assert target.startswith("/downloads/")
        assert "/" not in target.removeprefix("/downloads/")
        assert "\\" not in target and "\t" not in target


class TestClaimSuccess:
    def test_valid_cookie_and_token_returns_200_with_headers_and_deletes_the_file(self):
        app, sessions, store = _app()
        token = store.stage(ALICE, b"file bytes", "report.pdf", "application/pdf")
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.get(f"/downloads/{_url_token(token)}")
        assert r.status_code == 200
        assert r.content == b"file bytes"
        assert r.headers["content-type"] == "application/pdf"
        assert 'filename="report.pdf"' in r.headers["content-disposition"]
        assert r.headers["cache-control"] == "no-store"

        # Single-use: a second claim of the same token 404s.
        r2 = client.get(f"/downloads/{_url_token(token)}")
        assert r2.status_code == 404

    def test_audit_log_failure_never_blocks_the_download(self, monkeypatch):
        """Wrapped in try/except, same posture as gate.py's own _audit --
        a broken audit logger must not turn a successful claim into a 500."""
        def raise_error():
            raise RuntimeError("disk full")

        monkeypatch.setattr(routes_downloads, "get_audit_logger", raise_error)
        app, sessions, store = _app()
        token = store.stage(ALICE, b"file bytes", "report.pdf", "application/pdf")
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        r = client.get(f"/downloads/{_url_token(token)}")
        assert r.status_code == 200
        assert r.content == b"file bytes"

    def test_claim_writes_an_audit_entry_with_no_token_in_it(self, tmp_path):
        """The moment a staged download is actually served must
        leave a trail -- and that trail must never carry the token
        itself."""
        init_audit_logger(str(tmp_path / "audit"))
        app, sessions, store = _app()
        token = store.stage(ALICE, b"file bytes", "report.pdf", "application/pdf")
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        client.get(f"/downloads/{_url_token(token)}")

        week_file = tmp_path / "audit" / f"{current_week()}.jsonl"
        entries = [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 1
        assert entries[0]["decision"] == "staged_download_served"
        assert entries[0]["sender"] == "alice"
        assert "report.pdf" in entries[0]["summary"]
        assert _url_token(token) not in json.dumps(entries[0])


class TestClaimFailures:
    def test_someone_elses_token_is_404(self):
        app, sessions, store = _app()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        client = _client(app)
        _signed_in(client, sessions, BOB)
        r = client.get(f"/downloads/{_url_token(token)}")
        assert r.status_code == 404

    def test_unknown_token_is_404(self):
        app, sessions, _store = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        forged = _url_token(b"\x00" * 32)
        r = client.get(f"/downloads/{forged}")
        assert r.status_code == 404

    def test_malformed_token_is_404_not_500(self):
        app, sessions, _store = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/downloads/not-valid-base64!!!")
        assert r.status_code == 404

    def test_undecodable_token_is_404_not_500(self):
        # A base64url payload short enough that padding can't fix it up --
        # exercises the decode-raised-an-exception branch specifically,
        # distinct from "decoded fine but to the wrong length" above.
        app, sessions, _store = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/downloads/a")
        assert r.status_code == 404

    def test_wrong_length_token_is_404_not_500(self):
        app, sessions, _store = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/downloads/{_url_token(b'short')}")
        assert r.status_code == 404

    def test_mismatched_origin_is_rejected(self):
        app, sessions, store = _app()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/downloads/{_url_token(token)}", headers={"Origin": "https://evil.example.com"})
        assert r.status_code == 403

    def test_404_and_403_responses_are_no_store(self):
        # A per-token path is low caching risk either way, but the error
        # branches still send Cache-Control: no-store, like the success one.
        app, sessions, store = _app()
        token = store.stage(ALICE, b"data", "f.txt", "text/plain")
        client = _client(app)
        _signed_in(client, sessions, ALICE)

        unknown = client.get(f"/downloads/{_url_token(bytes(32))}")
        assert unknown.status_code == 404
        assert unknown.headers["cache-control"] == "no-store"

        forbidden = client.get(f"/downloads/{_url_token(token)}", headers={"Origin": "https://evil.example.com"})
        assert forbidden.status_code == 403
        assert forbidden.headers["cache-control"] == "no-store"
