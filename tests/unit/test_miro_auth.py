"""Tests for the Miro auth module.

The invariant: a Miro token is only kept when it expires and carries
boards:read and boards:write. The tests fake ``requests.post`` at the module
boundary (no network), and check that no token, code or client secret reaches a
raised message or a repr.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from privacyfence import miro_auth
from privacyfence.miro_auth import (
    NOT_AUTHENTICATED,
    MiroClientError,
    MiroOAuthConfig,
    MiroTokenProvider,
    bundle_settings,
)
from privacyfence.oauth_loopback import OAuthLoopbackError

pytestmark = pytest.mark.unit

SECRET = "s3cr3t-client-secret"
ACCESS = "miro_at_ACCESSVALUE"
REFRESH = "miro_rt_REFRESHVALUE"
CODE = "AUTHCODEVALUE"
SECRETS = (SECRET, ACCESS, REFRESH, CODE)
TEAM = "3458764549483493025"
CONFIG = MiroOAuthConfig("client-id", SECRET)
REDIRECT = "http://localhost:53686/callback"
UNREADABLE = (
    "Miro's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect again."
)
EXPIRED = (
    "Your Miro sign-in has expired or was revoked. Use Authenticate… in PrivacyFence Settings to sign in again."
)


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, raw_json: bool = True) -> None:
        self.status_code = status
        self._body = body
        self._raw_json = raw_json

    def json(self) -> Any:
        if not self._raw_json:
            raise ValueError("not json")
        return self._body


class FakePost:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_post(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> FakePost:
    post = FakePost(*responses)
    monkeypatch.setattr(miro_auth.requests, "post", post)
    return post


def token_answer(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "expires_in": 3599,
        "token_type": "bearer",
        "scope": "boards:read boards:write",
        "team_id": TEAM,
        "user_id": "3074457350000000001",
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


def record(**overrides: Any) -> dict[str, Any]:
    base = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_at": time.time() + 3600,
        "team_id": TEAM,
        "user_id": "",
        "scope": "boards:read boards:write",
    }
    base.update(overrides)
    return base


def assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


class TestBundleSettings:
    @pytest.mark.parametrize("section", [{}, {"client_id": "", "client_secret": ""}, {"client_id": "  "}])
    def test_empty_section_is_not_installed(self, section):
        with pytest.raises(MiroClientError, match="^Miro organization config not installed$"):
            bundle_settings(section)

    @pytest.mark.parametrize("section", [{"client_id": "cid"}, {"client_secret": "sec"}])
    def test_one_key_is_incomplete(self, section):
        with pytest.raises(MiroClientError) as info:
            bundle_settings(section)
        assert str(info.value) == (
            "Miro organization config is incomplete: client_id and client_secret go together."
        )

    def test_both_keys_give_the_config(self):
        config = bundle_settings({"client_id": " cid ", "client_secret": " sec "})
        assert config == MiroOAuthConfig("cid", "sec")

    def test_repr_hides_the_secret(self):
        assert SECRET not in repr(MiroOAuthConfig("cid", SECRET))
        assert "cid" in repr(MiroOAuthConfig("cid", SECRET))


class TestTokenFile:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "miro_token.json")
        saved = record(user_id="3074457350000000001")
        miro_auth.save_token_file(path, saved)
        assert miro_auth.load_token_file(path) == saved

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_file_mode_is_owner_only(self, tmp_path):
        path = tmp_path / "miro_token.json"
        miro_auth.save_token_file(str(path), record())
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_missing_file(self, tmp_path):
        with pytest.raises(MiroClientError) as info:
            miro_auth.load_token_file(str(tmp_path / "absent.json"))
        assert str(info.value) == NOT_AUTHENTICATED

    @pytest.mark.parametrize("key", ["access_token", "refresh_token", "team_id", "expires_at"])
    def test_each_missing_required_key(self, tmp_path, key):
        data = record()
        del data[key]
        path = tmp_path / "t.json"
        path.write_text(json.dumps(data))
        with pytest.raises(MiroClientError) as info:
            miro_auth.load_token_file(str(path))
        assert str(info.value) == NOT_AUTHENTICATED

    @pytest.mark.parametrize("key", ["access_token", "refresh_token", "team_id"])
    @pytest.mark.parametrize("value", ["", 5, None])
    def test_each_empty_or_wrong_typed_required_key(self, tmp_path, key, value):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(record(**{key: value})))
        with pytest.raises(MiroClientError) as info:
            miro_auth.load_token_file(str(path))
        assert str(info.value) == NOT_AUTHENTICATED

    @pytest.mark.parametrize("value", ["soon", None, True, [1]])
    def test_non_number_expires_at(self, tmp_path, value):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(record(expires_at=value)))
        with pytest.raises(MiroClientError) as info:
            miro_auth.load_token_file(str(path))
        assert str(info.value) == NOT_AUTHENTICATED

    def test_expires_at_zero_is_accepted(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(record(expires_at=0)))
        assert miro_auth.load_token_file(str(path))["expires_at"] == 0

    @pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"text"', "null"])
    def test_invalid_json_and_non_object(self, tmp_path, text):
        path = tmp_path / "t.json"
        path.write_text(text)
        with pytest.raises(MiroClientError) as info:
            miro_auth.load_token_file(str(path))
        assert str(info.value) == UNREADABLE

    def test_unreadable_file(self, tmp_path):
        with pytest.raises(MiroClientError) as info:
            miro_auth.load_token_file(str(tmp_path))  # a directory exists but cannot be read as a file
        assert str(info.value) == UNREADABLE

    def test_optional_keys_default_to_empty(self, tmp_path):
        path = tmp_path / "t.json"
        data = record()
        del data["user_id"], data["scope"]
        path.write_text(json.dumps(data))
        loaded = miro_auth.load_token_file(str(path))
        assert loaded["user_id"] == "" and loaded["scope"] == ""


class TestBuildAuthorizeUrl:
    def test_exactly_the_four_parameters(self):
        url = miro_auth.build_authorize_url("cid", REDIRECT, "st4te")
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == miro_auth.AUTHORIZE_URL
        assert parse_qs(parts.query) == {
            "response_type": ["code"],
            "client_id": ["cid"],
            "redirect_uri": [REDIRECT],
            "state": ["st4te"],
        }

    def test_no_scope_and_no_pkce(self):
        query = urlsplit(miro_auth.build_authorize_url("cid", REDIRECT, "s")).query
        assert "scope" not in query and "code_challenge" not in query


class TestTokenRequest:
    def test_credentials_go_in_the_form_body_only(self, monkeypatch):
        post = fake_post(monkeypatch, FakeResponse(200, {"a": 1}))
        form = {"grant_type": "refresh_token", "client_secret": SECRET}
        assert miro_auth._token_request(form) == (200, {"a": 1})
        call = post.calls[0]
        assert call["url"] == miro_auth.TOKEN_URL and SECRET not in call["url"]
        assert call["data"] == form
        assert "params" not in call
        assert call["allow_redirects"] is False
        assert call["timeout"] == 30

    def test_4xx_is_returned_for_the_caller(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(403, {"message": "no"}))
        assert miro_auth._token_request({}) == (403, {"message": "no"})

    def test_network_error(self, monkeypatch):
        fake_post(monkeypatch, requests.ConnectionError(f"boom {SECRET}"))
        with pytest.raises(MiroClientError) as info:
            miro_auth._token_request({})
        assert str(info.value) == "Could not reach Miro's sign-in server: ConnectionError"
        assert_no_secret(str(info.value))

    def test_redirect(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(302, {}))
        with pytest.raises(MiroClientError) as info:
            miro_auth._token_request({})
        assert str(info.value) == "Miro's sign-in server answered with a redirect (HTTP 302)."

    def test_server_error(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(500, {}))
        with pytest.raises(MiroClientError) as info:
            miro_auth._token_request({})
        assert str(info.value) == "Miro's sign-in server answered with an error (HTTP 500)."

    @pytest.mark.parametrize("response", [
        FakeResponse(200, raw_json=False),
        FakeResponse(400, raw_json=False),
        FakeResponse(200, [1, 2]),
    ])
    def test_not_a_json_object(self, monkeypatch, response):
        fake_post(monkeypatch, response)
        with pytest.raises(MiroClientError) as info:
            miro_auth._token_request({})
        assert str(info.value) == (
            f"Miro's sign-in server answered with something other than JSON (HTTP {response.status_code})."
        )

    @pytest.mark.parametrize("body, expected", [
        ({"message": "m", "error_description": "d", "error": "e"}, "m"),
        ({"error_description": "d", "error": "e"}, "d"),
        ({"error": "e"}, "e"),
        ({"message": ""}, "no detail"),
        ({"message": 5}, "no detail"),
        ({"message": "x" * 300}, "x" * 200),
    ])
    def test_message(self, body, expected):
        assert miro_auth._message(body) == expected


class TestExchangeCode:
    def test_record_shape_and_request(self, monkeypatch):
        post = fake_post(monkeypatch, FakeResponse(200, token_answer()))
        before = time.time()
        result = miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        after = time.time()
        assert post.calls[0]["data"] == {
            "grant_type": "authorization_code",
            "client_id": "client-id",
            "client_secret": SECRET,
            "code": CODE,
            "redirect_uri": REDIRECT,
        }
        expires_at = result.pop("expires_at")
        assert before + 3599 <= expires_at <= after + 3599
        assert result == {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "team_id": TEAM,
            "user_id": "3074457350000000001",
            "scope": "boards:read boards:write",
        }

    def test_numeric_ids_become_strings(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(200, token_answer(team_id=345, user_id=7)))
        result = miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        assert result["team_id"] == "345" and result["user_id"] == "7"

    def test_4xx_with_a_message(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(400, {"message": "Invalid code"}))
        with pytest.raises(MiroClientError) as info:
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        assert str(info.value) == "Miro sign-in failed: Invalid code"
        assert_no_secret(str(info.value))

    def test_4xx_without_detail(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(401, {}))
        with pytest.raises(MiroClientError, match="^Miro sign-in failed: no detail$"):
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)

    @pytest.mark.parametrize("key", ["access_token", "team_id"])
    @pytest.mark.parametrize("value", [None, "", True])
    def test_missing_key_is_incomplete(self, monkeypatch, key, value):
        answer = token_answer()
        answer.pop(key)
        if value is not None:
            answer[key] = value
        fake_post(monkeypatch, FakeResponse(200, answer))
        with pytest.raises(MiroClientError) as info:
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        assert str(info.value) == f"Miro returned an incomplete sign-in answer (no {key})."

    @pytest.mark.parametrize("value", ["soon", 0, -5, True, float("inf")])
    def test_bad_expires_in_is_incomplete(self, monkeypatch, value):
        fake_post(monkeypatch, FakeResponse(200, token_answer(expires_in=value)))
        with pytest.raises(MiroClientError) as info:
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        assert str(info.value) == "Miro returned an incomplete sign-in answer (no expires_in)."

    @pytest.mark.parametrize("missing", ["refresh_token", "expires_in", "both"])
    def test_non_expiring_token_is_refused(self, monkeypatch, missing):
        answer = token_answer()
        for key in ("refresh_token", "expires_in"):
            if missing in (key, "both"):
                del answer[key]
        fake_post(monkeypatch, FakeResponse(200, answer))
        with pytest.raises(MiroClientError) as info:
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        assert str(info.value) == (
            "Miro returned a token that never expires. Create the PrivacyFence app in Miro with "
            "\"Expire user authorization token\" turned on; see the Miro setup guide."
        )
        assert_no_secret(str(info.value))

    def test_empty_refresh_token_counts_as_missing(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(200, token_answer(refresh_token="")))
        with pytest.raises(MiroClientError, match="never expires"):
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)

    def test_missing_write_scope(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(200, token_answer(scope="boards:read")))
        with pytest.raises(MiroClientError) as info:
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        assert str(info.value) == (
            "The Miro app is missing the permission(s) boards:write. Turn on boards:read and "
            "boards:write in the app's settings in Miro, then sign in again."
        )

    def test_both_scopes_missing(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(200, token_answer(scope="identity:read")))
        with pytest.raises(MiroClientError, match=r"permission\(s\) boards:read, boards:write\."):
            miro_auth.exchange_code(CONFIG, CODE, REDIRECT)

    def test_extra_scopes_are_accepted(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(200, token_answer(scope="boards:write identity:read boards:read")))
        assert miro_auth.exchange_code(CONFIG, CODE, REDIRECT)["scope"] == (
            "boards:write identity:read boards:read"
        )

    @pytest.mark.parametrize("scope", [None, "", "   ", 5])
    def test_no_usable_scope_key_is_accepted(self, monkeypatch, scope):
        fake_post(monkeypatch, FakeResponse(200, token_answer(scope=scope)))
        result = miro_auth.exchange_code(CONFIG, CODE, REDIRECT)
        assert result["scope"] == ("   " if scope == "   " else "")

    def test_missing_user_id_is_empty(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(200, token_answer(user_id=None)))
        assert miro_auth.exchange_code(CONFIG, CODE, REDIRECT)["user_id"] == ""


class TestRefresh:
    def test_new_refresh_token_replaces_the_old(self, monkeypatch):
        post = fake_post(monkeypatch, FakeResponse(200, {
            "access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3599,
        }))
        old = record()
        result = miro_auth.refresh(CONFIG, old)
        assert post.calls[0]["data"] == {
            "grant_type": "refresh_token",
            "client_id": "client-id",
            "client_secret": SECRET,
            "refresh_token": "rt-1",
        }
        assert result["access_token"] == "at-2" and result["refresh_token"] == "rt-2"
        assert result["team_id"] == TEAM and result["scope"] == old["scope"]
        assert result["expires_at"] > time.time() + 3000
        assert old["access_token"] == "at-1"  # the input is not changed

    @pytest.mark.parametrize("extra", [{}, {"refresh_token": ""}, {"refresh_token": 5}])
    def test_absent_refresh_token_keeps_the_old(self, monkeypatch, extra):
        fake_post(monkeypatch, FakeResponse(200, {"access_token": "at-2", "expires_in": 60, **extra}))
        assert miro_auth.refresh(CONFIG, record())["refresh_token"] == "rt-1"

    def test_no_access_token_is_incomplete(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(200, {"expires_in": 60}))
        with pytest.raises(MiroClientError) as info:
            miro_auth.refresh(CONFIG, record())
        assert str(info.value) == "Miro returned an incomplete sign-in answer (no access_token)."

    @pytest.mark.parametrize("value", [None, "soon", 0])
    def test_bad_expires_in_is_incomplete(self, monkeypatch, value):
        answer = {"access_token": "at-2", "expires_in": value}
        fake_post(monkeypatch, FakeResponse(200, answer))
        with pytest.raises(MiroClientError) as info:
            miro_auth.refresh(CONFIG, record())
        assert str(info.value) == "Miro returned an incomplete sign-in answer (no expires_in)."

    @pytest.mark.parametrize("status", [400, 401])
    def test_expired_or_revoked(self, monkeypatch, status):
        fake_post(monkeypatch, FakeResponse(status, {"message": "invalid_grant"}))
        with pytest.raises(MiroClientError) as info:
            miro_auth.refresh(CONFIG, record())
        assert str(info.value) == EXPIRED

    def test_other_4xx_with_json(self, monkeypatch):
        fake_post(monkeypatch, FakeResponse(403, {"message": "Forbidden app"}))
        with pytest.raises(MiroClientError) as info:
            miro_auth.refresh(CONFIG, record())
        assert str(info.value) == "Miro sign-in refresh failed: Forbidden app"


class TestMiroTokenProvider:
    @staticmethod
    def provider(tmp_path, rec: dict[str, Any]) -> tuple[MiroTokenProvider, str]:
        path = str(tmp_path / "miro_token.json")
        miro_auth.save_token_file(path, rec)
        return MiroTokenProvider(CONFIG, path, rec), path

    @staticmethod
    def refreshed(access: str = "at-2", refresh: str = "rt-2") -> FakeResponse:
        return FakeResponse(200, {"access_token": access, "refresh_token": refresh, "expires_in": 3600})

    def test_team_id(self, tmp_path):
        provider, _ = self.provider(tmp_path, record())
        assert provider.team_id == TEAM

    def test_valid_token_needs_no_refresh_and_no_file_read(self, monkeypatch, tmp_path):
        provider, path = self.provider(tmp_path, record())
        post = fake_post(monkeypatch)
        os.remove(path)  # a file read would find nothing; none must happen
        monkeypatch.setattr(miro_auth, "load_token_file", lambda _p: pytest.fail("file read"))
        assert provider.access_token() == "at-1"
        assert post.calls == []

    def test_refreshes_60_seconds_before_expiry(self, monkeypatch, tmp_path):
        provider, _ = self.provider(tmp_path, record(expires_at=time.time() + 30))
        post = fake_post(monkeypatch, self.refreshed())
        assert provider.access_token() == "at-2"
        assert len(post.calls) == 1

    def test_just_outside_the_margin_does_not_refresh(self, monkeypatch, tmp_path):
        provider, _ = self.provider(tmp_path, record(expires_at=time.time() + 120))
        post = fake_post(monkeypatch)
        assert provider.access_token() == "at-1"
        assert post.calls == []

    def test_refreshes_at_expires_at_zero(self, monkeypatch, tmp_path):
        provider, _ = self.provider(tmp_path, record(expires_at=0))
        fake_post(monkeypatch, self.refreshed())
        assert provider.access_token() == "at-2"

    def test_force_refresh(self, monkeypatch, tmp_path):
        provider, _ = self.provider(tmp_path, record())
        post = fake_post(monkeypatch, self.refreshed())
        assert provider.access_token(force_refresh=True) == "at-2"
        assert len(post.calls) == 1
        assert provider.access_token() == "at-2"  # the new token is valid, no second refresh

    def test_refreshed_record_is_saved(self, monkeypatch, tmp_path):
        provider, path = self.provider(tmp_path, record(expires_at=0))
        fake_post(monkeypatch, self.refreshed("at-9", "rt-9"))
        provider.access_token()
        saved = miro_auth.load_token_file(path)
        assert saved["access_token"] == "at-9" and saved["refresh_token"] == "rt-9"
        assert saved["team_id"] == TEAM

    def test_a_valid_newer_record_on_disk_is_adopted_without_http(self, monkeypatch, tmp_path):
        provider, path = self.provider(tmp_path, record(expires_at=0))
        miro_auth.save_token_file(path, record(access_token="at-disk", refresh_token="rt-disk"))
        post = fake_post(monkeypatch)
        assert provider.access_token() == "at-disk"
        assert post.calls == []
        assert provider.access_token() == "at-disk"

    def test_force_refresh_uses_the_newer_record_on_disk(self, monkeypatch, tmp_path):
        provider, path = self.provider(tmp_path, record())
        miro_auth.save_token_file(path, record(access_token="at-disk", refresh_token="rt-disk"))
        post = fake_post(monkeypatch, self.refreshed())
        assert provider.access_token(force_refresh=True) == "at-2"
        assert post.calls[0]["data"]["refresh_token"] == "rt-disk"

    def test_an_expired_newer_record_on_disk_supplies_the_refresh_token(self, monkeypatch, tmp_path):
        provider, path = self.provider(tmp_path, record(expires_at=0))
        miro_auth.save_token_file(path, record(access_token="at-disk", refresh_token="rt-disk", expires_at=0))
        post = fake_post(monkeypatch, self.refreshed())
        assert provider.access_token() == "at-2"
        assert post.calls[0]["data"]["refresh_token"] == "rt-disk"

    def test_the_same_refresh_token_on_disk_is_refreshed(self, monkeypatch, tmp_path):
        provider, _ = self.provider(tmp_path, record(expires_at=0))
        post = fake_post(monkeypatch, self.refreshed())
        assert provider.access_token() == "at-2"
        assert post.calls[0]["data"]["refresh_token"] == "rt-1"

    @pytest.mark.parametrize("content", [None, "{broken", "[]"])
    def test_an_unreadable_file_is_ignored(self, monkeypatch, tmp_path, content):
        provider, path = self.provider(tmp_path, record(expires_at=0))
        if content is None:
            os.remove(path)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        post = fake_post(monkeypatch, self.refreshed())
        assert provider.access_token() == "at-2"
        assert post.calls[0]["data"]["refresh_token"] == "rt-1"

    def test_a_failed_refresh_raises_and_saves_nothing(self, monkeypatch, tmp_path):
        provider, path = self.provider(tmp_path, record(expires_at=0))
        fake_post(monkeypatch, FakeResponse(400, {"message": "gone"}))
        with pytest.raises(MiroClientError) as info:
            provider.access_token()
        assert str(info.value) == EXPIRED
        assert miro_auth.load_token_file(path)["access_token"] == "at-1"

    def test_repr_hides_every_token(self, tmp_path):
        provider, _ = self.provider(tmp_path, record(access_token=ACCESS, refresh_token=REFRESH))
        assert_no_secret(repr(provider))
        assert_no_secret(str(provider))


class TestAuthorizeInteractive:
    @staticmethod
    def capture(monkeypatch: pytest.MonkeyPatch, result: Any) -> dict[str, Any]:
        seen: dict[str, Any] = {}

        def fake_run(build, exchange, **kwargs):
            seen.update(build=build, exchange=exchange, kwargs=kwargs)
            if isinstance(result, Exception):
                raise result
            return exchange("the-code", REDIRECT, "verifier")

        monkeypatch.setattr(miro_auth.oauth_loopback, "run_browser_oauth", fake_run)
        return seen

    def test_loopback_arguments_and_saved_record(self, monkeypatch, tmp_path):
        seen = self.capture(monkeypatch, None)
        post = fake_post(monkeypatch, FakeResponse(200, token_answer()))
        path = str(tmp_path / "miro_token.json")
        result = miro_auth.authorize_interactive(CONFIG, path)
        assert seen["kwargs"] == {"port": 53686, "path": "/callback", "redirect_host": "localhost"}
        assert post.calls[0]["data"]["code"] == "the-code"
        assert post.calls[0]["data"]["redirect_uri"] == REDIRECT
        assert miro_auth.load_token_file(path) == {**result, "user_id": result["user_id"]}
        assert result["access_token"] == ACCESS

    def test_build_callback_ignores_the_challenge(self, monkeypatch, tmp_path):
        seen = self.capture(monkeypatch, None)
        fake_post(monkeypatch, FakeResponse(200, token_answer()))
        miro_auth.authorize_interactive(CONFIG, str(tmp_path / "t.json"))
        url = seen["build"](REDIRECT, "st4te", "ignored-challenge")
        assert url == miro_auth.build_authorize_url("client-id", REDIRECT, "st4te")
        assert "ignored-challenge" not in url

    def test_loopback_failure_is_mapped(self, monkeypatch, tmp_path):
        self.capture(monkeypatch, OAuthLoopbackError("timed out"))
        path = tmp_path / "t.json"
        with pytest.raises(MiroClientError) as info:
            miro_auth.authorize_interactive(CONFIG, str(path))
        assert str(info.value) == "Miro sign-in failed: timed out"
        assert not path.exists()

    def test_a_refused_token_saves_nothing_and_leaks_nothing(self, monkeypatch, tmp_path):
        self.capture(monkeypatch, None)
        fake_post(monkeypatch, FakeResponse(200, token_answer(scope="boards:read")))
        path = tmp_path / "t.json"
        with pytest.raises(MiroClientError) as info:
            miro_auth.authorize_interactive(CONFIG, str(path))
        assert not path.exists()
        assert_no_secret(str(info.value))
        assert "the-code" not in str(info.value)
