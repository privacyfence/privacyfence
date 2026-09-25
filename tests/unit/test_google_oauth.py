"""Tests for the shared Google OAuth 2.0 helper -- org mode's server-redirect
functions (web/routes_connect.py) and local mode's ``authorize_local``, which
every Google client's ``authorize_interactive`` calls.

``google_auth_oauthlib.flow.Flow``'s own network calls (``fetch_token``) are
mocked at the ``requests``/session layer it ultimately uses via
``requests_oauthlib.OAuth2Session`` -- the same "mock the transport, run the
real client logic" posture test_atlassian_oauth.py/test_slack_client.py/
test_salesforce_client.py already take for their own OAuth exchanges.
"""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import sys
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from privacyfence import google_oauth, oauth_loopback

_CLIENT_CONFIG = {
    "client_id": "cid",
    "client_secret": "csecret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["https://pf.example.com/oauth/callback/gmail"],
}

REDIRECT_URI = "https://pf.example.com/oauth/callback/gmail"


class TestWebClientConfig:
    def test_missing_client_id_or_secret_is_empty(self):
        assert google_oauth.web_client_config({}) == {}
        assert google_oauth.web_client_config({"client_id": "x"}) == {}

    def test_missing_auth_or_token_uri_is_empty(self):
        assert google_oauth.web_client_config({"client_id": "x", "client_secret": "y"}) == {}

    def test_wraps_flat_section_under_web_key(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        assert wrapped == {"web": _CLIENT_CONFIG}


class TestAuthorizeUrl:
    def test_builds_a_real_google_authorize_url_with_pkce_and_state(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        url, code_verifier = google_oauth.authorize_url(wrapped, ["scope-a"], REDIRECT_URI, "state-123")

        assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
        assert "state=state-123" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert len(code_verifier) >= 43  # RFC 7636's own minimum verifier length

    def test_does_not_ask_for_incremental_authorization(self):
        # include_granted_scopes=true makes Google issue a grant covering
        # every scope this client already holds for the user. routes_connect
        # .py wants the opposite (one grant and one token file per Google
        # connector), and asking for it broke the exchange outright: the
        # token came back with the accumulated union, and oauthlib rejected
        # it as "Scope has changed from ... to ...". Found authorizing a
        # second Google connector on a real deployment.
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        url, _ = google_oauth.authorize_url(wrapped, ["scope-a"], REDIRECT_URI, "state-123")
        assert "include_granted_scopes" not in url

    def test_each_call_gets_a_fresh_verifier(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        _, verifier1 = google_oauth.authorize_url(wrapped, ["s"], REDIRECT_URI, "state-1")
        _, verifier2 = google_oauth.authorize_url(wrapped, ["s"], REDIRECT_URI, "state-2")
        assert verifier1 != verifier2


def _fake_fetch_token(token: dict):
    """A fetch_token replacement that also does what the real
    OAuth2Session.fetch_token does as a side effect: populate
    self.oauth2session.token, which credentials_from_session (google_
    auth_oauthlib.helpers) requires to be set before Flow.credentials can
    build a real Credentials object. A bare return_value= mock skips that
    side effect entirely and credentials_from_session raises instead."""

    def fetch_token(self, **kwargs):
        self.oauth2session.token = token
        return token

    return fetch_token


class TestExchangeCode:
    def test_successful_exchange_returns_credentials_with_the_verifier_used(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        token = {
            "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
            "expires_at": 9999999999, "scope": "scope-a", "token_type": "Bearer",
        }
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", _fake_fetch_token(token)):
            creds = google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")

        assert creds.token == "at-1"
        assert creds.refresh_token == "rt-1"

    def test_a_wider_granted_scope_is_accepted(self):
        # Google legitimately returns more than was asked for -- most often
        # openid/userinfo.*, when one OAuth client serves both org mode's
        # sign-in and its connectors. oauthlib raises rather than returning
        # the token it already parsed; exchange_code recovers it.
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        token = {
            "access_token": "at-1", "refresh_token": "rt-1", "expires_at": 9999999999,
            "scope": "scope-a openid", "token_type": "Bearer",
        }
        warning = Warning('Scope has changed from "scope-a" to "scope-a openid".')
        warning.token = token
        warning.new_scope = ["scope-a", "openid"]
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", side_effect=warning):
            creds = google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")

        assert creds.token == "at-1"
        assert creds.refresh_token == "rt-1"
        # The distinction credentials_from_session keeps both fields for:
        # what was asked for, and what was actually granted.
        assert creds.scopes == ["scope-a"]
        assert creds.granted_scopes == "scope-a openid"

    def test_a_narrower_granted_scope_is_still_a_failure(self):
        # The half of oauthlib's check worth keeping: a grant missing
        # something that was requested would otherwise surface much later,
        # as an opaque permission error from the connector itself.
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        warning = Warning('Scope has changed from "scope-a scope-b" to "scope-a".')
        warning.token = {"access_token": "at-1", "expires_at": 9999999999}
        warning.new_scope = ["scope-a"]
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", side_effect=warning):
            with pytest.raises(google_oauth.GoogleOAuthError, match="scope-b"):
                google_oauth.exchange_code(
                    wrapped, ["scope-a", "scope-b"], REDIRECT_URI, "auth-code", "verifier-abc",
                )

    def test_a_warning_that_is_not_oauthlibs_scope_change_still_fails(self):
        # Bare `Warning` is an ordinary Exception subclass anything could
        # raise; only oauthlib's scope-change carries .token/.new_scope, and
        # without them there is nothing to recover.
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", side_effect=Warning("something else")):
            with pytest.raises(google_oauth.GoogleOAuthError, match="something else"):
                google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")

    def test_provider_failure_becomes_google_oauth_error(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", side_effect=RuntimeError("invalid_grant")):
            try:
                google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")
            except google_oauth.GoogleOAuthError as exc:
                assert "invalid_grant" in str(exc)
            else:
                raise AssertionError("expected GoogleOAuthError")

    def test_uses_the_persisted_code_verifier_not_a_fresh_one(self):
        # exchange_code must construct its Flow with autogenerate_code_verifier=False
        # and the caller-supplied verifier -- otherwise the PKCE check on Google's
        # own token endpoint would fail for every real exchange (the verifier used
        # at fetch_token time must match the challenge sent at authorize time).
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        fake_token = {"access_token": "at-1", "expires_at": 9999999999}
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", _fake_fetch_token(fake_token)):
            with patch.object(google_oauth.Flow, "from_client_config", wraps=google_oauth.Flow.from_client_config) as ctor:
                google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")
        _, kwargs = ctor.call_args
        assert kwargs["code_verifier"] == "verifier-abc"
        assert kwargs["autogenerate_code_verifier"] is False


_INSTALLED_CONFIG = {"installed": {k: v for k, v in _CLIENT_CONFIG.items() if k != "redirect_uris"}}


class TestAuthorizeLocal:
    """Local mode's Google sign-in, run for real through oauth_loopback's
    listener. Only the browser and the token endpoint are stand-ins."""

    @staticmethod
    def _visit(url: str, params: dict) -> None:
        # Not through HTTP(S)_PROXY: a real browser doesn't proxy loopback either.
        session = requests.Session()
        session.trust_env = False
        session.get(url.replace("://localhost:", "://127.0.0.1:"), params=params, timeout=5)

    def _run(self, monkeypatch, opener):
        exchanged = {}

        def fake_exchange(client_config, scopes, redirect_uri, code, code_verifier):
            exchanged.update(
                client_config=client_config, scopes=scopes, redirect_uri=redirect_uri,
                code=code, code_verifier=code_verifier,
            )
            return "creds"

        monkeypatch.setattr(oauth_loopback, "_default_open_browser", opener)
        monkeypatch.setattr(google_oauth, "exchange_code", fake_exchange)
        return google_oauth.authorize_local(_INSTALLED_CONFIG, ["scope-a"]), exchanged

    def test_opens_the_google_authorize_url_through_the_companion_aware_opener(self, monkeypatch):
        # The bug this guards: InstalledAppFlow.run_local_server() called
        # webbrowser.open() from the daemon, which on a separated install
        # reaches no desktop session, so Authenticate opened no tab.
        opened = []

        def opener(url: str) -> bool:
            opened.append(url)
            query = parse_qs(urlparse(url).query)
            self._visit(query["redirect_uri"][0], {"code": "auth-code", "state": query["state"][0]})
            return True

        creds, exchanged = self._run(monkeypatch, opener)

        assert creds == "creds"
        assert len(opened) == 1
        assert opened[0].startswith("https://accounts.google.com/o/oauth2/auth?")
        assert exchanged["code"] == "auth-code"
        assert exchanged["scopes"] == ["scope-a"]
        assert exchanged["client_config"] is _INSTALLED_CONFIG

    def test_redirect_uri_is_a_loopback_port_the_os_picked(self, monkeypatch):
        seen = {}

        def opener(url: str) -> bool:
            query = parse_qs(urlparse(url).query)
            seen["redirect_uri"] = query["redirect_uri"][0]
            self._visit(seen["redirect_uri"], {"code": "c", "state": query["state"][0]})
            return True

        _, exchanged = self._run(monkeypatch, opener)

        parsed = urlparse(seen["redirect_uri"])
        assert (parsed.scheme, parsed.hostname, parsed.path) == ("http", "localhost", "/")
        assert parsed.port and parsed.port != 0
        assert exchanged["redirect_uri"] == seen["redirect_uri"]

    def test_pkce_challenge_in_the_url_matches_the_verifier_exchanged(self, monkeypatch):
        seen = {}

        def opener(url: str) -> bool:
            query = parse_qs(urlparse(url).query)
            seen.update(challenge=query["code_challenge"][0], method=query["code_challenge_method"][0])
            self._visit(query["redirect_uri"][0], {"code": "c", "state": query["state"][0]})
            return True

        _, exchanged = self._run(monkeypatch, opener)

        digest = hashlib.sha256(exchanged["code_verifier"].encode("ascii")).digest()
        assert seen["method"] == "S256"
        assert seen["challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def test_a_callback_with_the_wrong_state_is_refused(self, monkeypatch):
        def opener(url: str) -> bool:
            query = parse_qs(urlparse(url).query)
            self._visit(query["redirect_uri"][0], {"code": "c", "state": "attacker-supplied"})
            return True

        with pytest.raises(oauth_loopback.OAuthLoopbackError, match="state mismatch"):
            self._run(monkeypatch, opener)


class TestSaveCredentials:
    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod/stat permission bits are a POSIX-only security model -- Windows has none to assert on (known, accepted gap)",
    )
    def test_writes_to_json_output_with_restricted_permissions(self, tmp_path):
        creds = MagicMock()
        creds.to_json.return_value = json.dumps({"token": "at-1", "refresh_token": "rt-1"})
        token_file = tmp_path / "nested" / "token.json"

        google_oauth.save_credentials(str(token_file), creds)

        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved == {"token": "at-1", "refresh_token": "rt-1"}
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_chmod_failure_is_non_fatal(self, tmp_path, monkeypatch):
        creds = MagicMock()
        creds.to_json.return_value = "{}"

        def raise_chmod(*a, **kw):
            raise OSError("no chmod here")

        monkeypatch.setattr("os.chmod", raise_chmod)
        google_oauth.save_credentials(str(tmp_path / "token.json"), creds)  # must not raise
