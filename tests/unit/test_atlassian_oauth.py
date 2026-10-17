"""Tests for the shared Atlassian OAuth 2.0 (3LO) helper used by Jira and
Confluence. run_browser_oauth itself (the local-redirect/state/PKCE
mechanics) has its own dedicated real-HTTP-server tests in
test_oauth_loopback.py, so here it's monkeypatched to capture and directly
invoke the build_authorize_url/exchange closures authorize_interactive
defines -- exercising this module's own logic (request construction,
multi-site resource picking, token record assembly) without re-testing the
loopback server underneath it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from privacyfence import atlassian_oauth as atlassian_oauth_module
from privacyfence.atlassian_oauth import (
    AtlassianOAuthError,
    is_unauthorized,
    load_token_file,
    refresh,
    save_token_file,
)


def capture_run_browser_oauth(monkeypatch, exchange_response=None, loopback_error=None):
    """Monkeypatch run_browser_oauth to capture the closures authorize_interactive
    builds and, unless loopback_error is given, immediately invoke exchange()
    with a fixed code/redirect_uri/verifier -- exercising the real exchange()
    closure (which itself calls requests.post, mocked separately per test)."""
    captured: dict = {}

    def fake(build_authorize_url, exchange, port, path, **kwargs):
        captured["build_authorize_url_fn"] = build_authorize_url
        captured["exchange_fn"] = exchange
        captured["port"] = port
        captured["path"] = path
        captured["authorize_url"] = build_authorize_url("http://127.0.0.1:1234/callback", "state-abc", "challenge-xyz")
        if loopback_error is not None:
            raise loopback_error
        return exchange("test-code", "http://127.0.0.1:1234/callback", "verifier-123")

    monkeypatch.setattr(atlassian_oauth_module, "run_browser_oauth", fake)
    return captured


def fake_response(json_data=None, status_ok=True):
    resp = MagicMock()
    resp.json.return_value = json_data or {}
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.HTTPError("bad status")
    return resp


# ---------------------------------------------------------------------------- #
# is_unauthorized
# ---------------------------------------------------------------------------- #

class TestIsUnauthorized:
    def test_401_response_detected(self):
        exc = Exception("boom")
        exc.response = MagicMock(status_code=401)
        assert is_unauthorized(exc) is True

    def test_other_status_code_not_detected(self):
        exc = Exception("boom")
        exc.response = MagicMock(status_code=500)
        assert is_unauthorized(exc) is False

    def test_no_response_attribute_not_detected(self):
        assert is_unauthorized(Exception("boom")) is False


# ---------------------------------------------------------------------------- #
# load_token_file / save_token_file
# ---------------------------------------------------------------------------- #

class TestLoadSaveTokenFile:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(AtlassianOAuthError, match="No Atlassian token found"):
            load_token_file(str(tmp_path / "nope.json"))

    def test_round_trips_through_save_and_load(self, tmp_path):
        path = str(tmp_path / "sub" / "token.json")
        save_token_file(path, {"access_token": "t", "cloud_id": "c1"})
        assert load_token_file(path) == {"access_token": "t", "cloud_id": "c1"}


# ---------------------------------------------------------------------------- #
# refresh
# ---------------------------------------------------------------------------- #

class TestRefresh:
    def test_posts_refresh_grant_and_returns_json(self, monkeypatch):
        captured = {}
        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return fake_response({"access_token": "new-tok", "refresh_token": "new-rt"})
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", fake_post)

        result = refresh("client-id", "client-secret", "old-refresh-token")

        assert result == {"access_token": "new-tok", "refresh_token": "new-rt"}
        assert captured["url"] == atlassian_oauth_module.TOKEN_URL
        assert captured["json"] == {
            "grant_type": "refresh_token", "client_id": "client-id",
            "client_secret": "client-secret", "refresh_token": "old-refresh-token",
        }

    def test_http_failure_becomes_atlassian_oauth_error(self, monkeypatch):
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", lambda *a, **kw: fake_response(status_ok=False))
        with pytest.raises(AtlassianOAuthError, match="token refresh failed"):
            refresh("ci", "cs", "rt")

    def test_network_error_becomes_atlassian_oauth_error(self, monkeypatch):
        def raiser(*a, **kw):
            raise requests.ConnectionError("network down")
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", raiser)
        with pytest.raises(AtlassianOAuthError, match="token refresh failed"):
            refresh("ci", "cs", "rt")


# ---------------------------------------------------------------------------- #
# authorize_interactive: build_authorize_url closure
# ---------------------------------------------------------------------------- #

class TestAuthorizeInteractiveBuildUrl:
    def test_default_scopes_and_required_params_present(self, monkeypatch, tmp_path):
        captured = capture_run_browser_oauth(monkeypatch)
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", lambda *a, **kw: fake_response({"access_token": "t"}))
        monkeypatch.setattr(
            atlassian_oauth_module, "_fetch_accessible_resources",
            lambda token: [{"id": "cloud1", "url": "https://x.atlassian.net"}],
        )
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_email", lambda token, cloud_id: "me@x.com")
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_id", lambda token, cloud_id: "")

        atlassian_oauth_module.authorize_interactive("ci", "cs", str(tmp_path / "token.json"))

        url = captured["authorize_url"]
        assert "audience=api.atlassian.com" in url
        assert "client_id=ci" in url
        assert "response_type=code" in url
        assert "prompt=consent" in url
        assert "code_challenge=challenge-xyz" in url
        assert "code_challenge_method=S256" in url
        assert "offline_access" in url  # part of the joined default scope string

    def test_custom_scopes_override_defaults(self, monkeypatch, tmp_path):
        captured = capture_run_browser_oauth(monkeypatch)
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", lambda *a, **kw: fake_response({"access_token": "t"}))
        monkeypatch.setattr(
            atlassian_oauth_module, "_fetch_accessible_resources",
            lambda token: [{"id": "cloud1", "url": "https://x.atlassian.net"}],
        )
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_email", lambda token, cloud_id: "")
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_id", lambda token, cloud_id: "")

        atlassian_oauth_module.authorize_interactive(
            "ci", "cs", str(tmp_path / "token.json"), scopes=["read:jira-work"],
        )

        assert "read%3Ajira-work" in captured["authorize_url"] or "read:jira-work" in captured["authorize_url"]


    def test_assets_scopes_reach_the_authorize_url(self, monkeypatch, tmp_path):
        captured = capture_run_browser_oauth(monkeypatch)
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", lambda *a, **kw: fake_response({"access_token": "t"}))
        monkeypatch.setattr(
            atlassian_oauth_module, "_fetch_accessible_resources",
            lambda token: [{"id": "cloud1", "url": "https://x.atlassian.net"}],
        )
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_email", lambda token, cloud_id: "")
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_id", lambda token, cloud_id: "")

        atlassian_oauth_module.authorize_interactive(
            "ci", "cs", str(tmp_path / "token.json"),
            scopes=atlassian_oauth_module.scopes_for(assets=True),
        )

        assert "read%3Acmdb-object%3Ajira" in captured["authorize_url"]


class TestAssetsScopes:
    def test_scopes_for_without_assets_is_the_default_list(self):
        scopes = atlassian_oauth_module.scopes_for(assets=False)
        assert scopes == atlassian_oauth_module.DEFAULT_SCOPES
        assert scopes is not atlassian_oauth_module.DEFAULT_SCOPES
        assert "read:cmdb-object:jira" not in scopes

    def test_scopes_for_with_assets_appends_the_assets_scopes(self):
        scopes = atlassian_oauth_module.scopes_for(assets=True)
        assert scopes == atlassian_oauth_module.DEFAULT_SCOPES + atlassian_oauth_module.ASSETS_SCOPES
        assert "read:cmdb-object:jira" in scopes

    def test_assets_scopes_leave_out_the_jsm_scope(self):
        # The developer console does not offer every app the JSM API; asking for its scope
        # would fail the whole Atlassian sign-in (ADR 0188).
        assert "read:servicedesk-request" not in atlassian_oauth_module.ASSETS_SCOPES

    @pytest.mark.parametrize("config, expected", [
        ({}, {}),
        ({"connectors": {"assets": {"enabled": True}}}, {}),
        ({"connectors": {"assets": {"workspace_id": ""}}}, {}),
        ({"connectors": {"assets": {"workspace_id": " ws-1 "}}}, {"assets_workspace_id": "ws-1"}),
    ])
    def test_assets_workspace_override(self, config, expected):
        assert atlassian_oauth_module.assets_workspace_override(config) == expected

    def test_explicit_true_wins_over_a_bundle_without_the_flag(self):
        cfg = {"connectors": {"assets": {"enabled": True}}}
        assert atlassian_oauth_module.assets_enabled(cfg, {}) is True

    def test_explicit_false_wins_over_the_bundle_flag(self):
        cfg = {"connectors": {"assets": {"enabled": False}}}
        assert atlassian_oauth_module.assets_enabled(cfg, {"atlassian": {"assets": True}}) is False

    def test_bundle_flag_is_the_default(self):
        assert atlassian_oauth_module.assets_enabled({}, {"atlassian": {"assets": True}}) is True

    def test_off_when_neither_says_so(self):
        assert atlassian_oauth_module.assets_enabled({}, {}) is False
        assert atlassian_oauth_module.assets_enabled({"connectors": {"assets": {}}}, {"atlassian": {}}) is False


# ---------------------------------------------------------------------------- #
# authorize_interactive: exchange closure
# ---------------------------------------------------------------------------- #

class TestAuthorizeInteractiveExchange:
    def test_exchange_posts_correct_grant_and_pkce_verifier(self, monkeypatch, tmp_path):
        captured_post = {}
        def fake_post(url, **kwargs):
            captured_post["url"] = url
            captured_post["json"] = kwargs["json"]
            return fake_response({"access_token": "t", "refresh_token": "rt"})
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", fake_post)
        capture_run_browser_oauth(monkeypatch)
        monkeypatch.setattr(
            atlassian_oauth_module, "_fetch_accessible_resources",
            lambda token: [{"id": "cloud1", "url": "https://x.atlassian.net"}],
        )
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_email", lambda token, cloud_id: "")
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_id", lambda token, cloud_id: "")

        atlassian_oauth_module.authorize_interactive("ci", "cs", str(tmp_path / "token.json"))

        assert captured_post["url"] == atlassian_oauth_module.TOKEN_URL
        assert captured_post["json"] == {
            "grant_type": "authorization_code", "client_id": "ci", "client_secret": "cs",
            "code": "test-code", "redirect_uri": "http://127.0.0.1:1234/callback",
            "code_verifier": "verifier-123",
        }

    def test_exchange_http_failure_becomes_atlassian_oauth_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", lambda *a, **kw: fake_response(status_ok=False))
        capture_run_browser_oauth(monkeypatch)

        with pytest.raises(AtlassianOAuthError, match="OAuth exchange failed"):
            atlassian_oauth_module.authorize_interactive("ci", "cs", str(tmp_path / "token.json"))


# ---------------------------------------------------------------------------- #
# build_authorize_url / exchange_code / resolve_resource_and_save -- called
# directly (not through run_browser_oauth) by web/routes_connect.py's
# org-mode server-redirect flow.
# ---------------------------------------------------------------------------- #

class TestHoistedFunctions:
    def test_build_authorize_url_direct_call(self):
        url = atlassian_oauth_module.build_authorize_url(
            "ci", "https://pf.example.com/oauth/callback/jira", "state-1", "challenge-1",
        )
        assert url.startswith(f"{atlassian_oauth_module.AUTHORIZE_URL}?")
        assert "client_id=ci" in url
        assert "redirect_uri=https%3A%2F%2Fpf.example.com%2Foauth%2Fcallback%2Fjira" in url
        assert "code_challenge=challenge-1" in url
        assert "state=state-1" in url

    def test_exchange_code_returns_the_raw_token_response_only(self, monkeypatch):
        monkeypatch.setattr(
            atlassian_oauth_module.requests, "post",
            lambda *a, **kw: fake_response({"access_token": "at-1", "refresh_token": "rt-1"}),
        )
        response = atlassian_oauth_module.exchange_code("ci", "cs", "code", "https://pf.example.com/cb", "verifier")
        assert response == {"access_token": "at-1", "refresh_token": "rt-1"}

    def test_exchange_code_missing_access_token_raises(self, monkeypatch):
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", lambda *a, **kw: fake_response({}))
        with pytest.raises(AtlassianOAuthError, match="did not return an access token"):
            atlassian_oauth_module.exchange_code("ci", "cs", "code", "https://pf.example.com/cb", "verifier")

    def test_resolve_resource_and_save_single_site_needs_no_picker(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            atlassian_oauth_module, "_fetch_accessible_resources",
            lambda token: [{"id": "cloud1", "url": "https://x.atlassian.net"}],
        )
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_email", lambda token, cloud_id: "me@x.com")
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_id", lambda token, cloud_id: "")

        record = atlassian_oauth_module.resolve_resource_and_save(str(tmp_path / "token.json"), "at-1", "rt-1")

        assert record["cloud_id"] == "cloud1"
        assert record["site_url"] == "https://x.atlassian.net"
        assert load_token_file(str(tmp_path / "token.json")) == record

    def test_resolve_resource_and_save_multi_site_uses_pick_resource(self, monkeypatch, tmp_path):
        resources = [
            {"id": "cloud1", "url": "https://a.atlassian.net"},
            {"id": "cloud2", "url": "https://b.atlassian.net"},
        ]
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_accessible_resources", lambda token: resources)
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_email", lambda token, cloud_id: "")
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_id", lambda token, cloud_id: "")

        record = atlassian_oauth_module.resolve_resource_and_save(
            str(tmp_path / "token.json"), "at-1", "rt-1", pick_resource=lambda rs: rs[0],
        )

        assert record["cloud_id"] == "cloud1"  # web/routes_connect.py's own "first resource" simplification

    def test_resolve_resource_and_save_multi_site_without_picker_raises(self, monkeypatch, tmp_path):
        resources = [
            {"id": "cloud1", "url": "https://a.atlassian.net"},
            {"id": "cloud2", "url": "https://b.atlassian.net"},
        ]
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_accessible_resources", lambda token: resources)

        with pytest.raises(AtlassianOAuthError, match="Multiple Atlassian sites"):
            atlassian_oauth_module.resolve_resource_and_save(str(tmp_path / "token.json"), "at-1", "rt-1")


# ---------------------------------------------------------------------------- #
# authorize_interactive: end-to-end token record assembly
# ---------------------------------------------------------------------------- #

class TestAuthorizeInteractiveTokenAssembly:
    def _setup(self, monkeypatch, exchange_json, resources, pick_resource=None, account_email="", account_id=""):
        capture_run_browser_oauth(monkeypatch)
        monkeypatch.setattr(atlassian_oauth_module.requests, "post", lambda *a, **kw: fake_response(exchange_json))
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_accessible_resources", lambda token: resources)
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_email", lambda token, cloud_id: account_email)
        monkeypatch.setattr(atlassian_oauth_module, "_fetch_account_id", lambda token, cloud_id: account_id)

    def test_loopback_error_becomes_atlassian_oauth_error(self, monkeypatch, tmp_path):
        from privacyfence.oauth_loopback import OAuthLoopbackError
        capture_run_browser_oauth(monkeypatch, loopback_error=OAuthLoopbackError("timed out"))

        with pytest.raises(AtlassianOAuthError, match="sign-in failed"):
            atlassian_oauth_module.authorize_interactive("ci", "cs", str(tmp_path / "token.json"))

    def test_missing_access_token_raises(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, {"refresh_token": "rt"}, [])
        with pytest.raises(AtlassianOAuthError, match="did not return an access token"):
            atlassian_oauth_module.authorize_interactive("ci", "cs", str(tmp_path / "token.json"))

    def test_no_accessible_resources_raises(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, {"access_token": "t"}, [])
        with pytest.raises(AtlassianOAuthError, match="No accessible Atlassian sites"):
            atlassian_oauth_module.authorize_interactive("ci", "cs", str(tmp_path / "token.json"))

    def test_single_resource_auto_selected(self, monkeypatch, tmp_path):
        self._setup(
            monkeypatch, {"access_token": "tok", "refresh_token": "rt"},
            [{"id": "cloud1", "url": "https://x.atlassian.net"}], account_email="me@x.com", account_id="acct-1",
        )
        token_file = str(tmp_path / "token.json")

        result = atlassian_oauth_module.authorize_interactive("ci", "cs", token_file)

        assert result == {
            "access_token": "tok", "refresh_token": "rt", "cloud_id": "cloud1",
            "site_url": "https://x.atlassian.net", "account_email": "me@x.com", "account_id": "acct-1", "scopes": [],
        }
        assert load_token_file(token_file) == result

    def test_scopes_are_the_union_over_resources_with_the_chosen_id(self, monkeypatch, tmp_path):
        self._setup(
            monkeypatch, {"access_token": "t"},
            [
                {"id": "c1", "url": "https://a.atlassian.net", "scopes": ["read:jira-work", "offline_access"]},
                {"id": "c1", "url": "https://a.atlassian.net", "scopes": ["read:page:confluence", "read:jira-work"]},
                {"id": "c2", "url": "https://b.atlassian.net", "scopes": ["read:cmdb-object:jira"]},
            ],
        )

        result = atlassian_oauth_module.authorize_interactive(
            "ci", "cs", str(tmp_path / "token.json"), pick_resource=lambda rs: rs[0],
        )

        assert result["scopes"] == ["offline_access", "read:jira-work", "read:page:confluence"]

    def test_multiple_resources_without_pick_resource_raises(self, monkeypatch, tmp_path):
        self._setup(
            monkeypatch, {"access_token": "t"},
            [{"id": "c1", "url": "https://a.atlassian.net"}, {"id": "c2", "url": "https://b.atlassian.net"}],
        )
        with pytest.raises(AtlassianOAuthError, match="Multiple Atlassian sites"):
            atlassian_oauth_module.authorize_interactive("ci", "cs", str(tmp_path / "token.json"))

    def test_multiple_resources_with_pick_resource_uses_its_choice(self, monkeypatch, tmp_path):
        resources = [
            {"id": "c1", "url": "https://a.atlassian.net"},
            {"id": "c2", "url": "https://b.atlassian.net"},
        ]
        self._setup(monkeypatch, {"access_token": "t"}, resources)

        result = atlassian_oauth_module.authorize_interactive(
            "ci", "cs", str(tmp_path / "token.json"), pick_resource=lambda rs: rs[1],
        )

        assert result["cloud_id"] == "c2"
        assert result["site_url"] == "https://b.atlassian.net"


# ---------------------------------------------------------------------------- #
# _fetch_accessible_resources / _fetch_account_email
# ---------------------------------------------------------------------------- #

class TestFetchAccessibleResources:
    def test_returns_parsed_json_list(self, monkeypatch):
        monkeypatch.setattr(
            atlassian_oauth_module.requests, "get",
            lambda url, **kw: fake_response([{"id": "c1"}]),
        )
        assert atlassian_oauth_module._fetch_accessible_resources("tok") == [{"id": "c1"}]

    def test_sends_bearer_auth_header(self, monkeypatch):
        captured = {}
        def fake_get(url, **kwargs):
            captured["headers"] = kwargs["headers"]
            captured["url"] = url
            return fake_response([])
        monkeypatch.setattr(atlassian_oauth_module.requests, "get", fake_get)

        atlassian_oauth_module._fetch_accessible_resources("my-token")

        assert captured["headers"]["Authorization"] == "Bearer my-token"
        assert captured["url"] == atlassian_oauth_module.ACCESSIBLE_RESOURCES_URL

    def test_http_failure_raises_atlassian_oauth_error(self, monkeypatch):
        monkeypatch.setattr(atlassian_oauth_module.requests, "get", lambda *a, **kw: fake_response(status_ok=False))
        with pytest.raises(AtlassianOAuthError, match="Could not list accessible"):
            atlassian_oauth_module._fetch_accessible_resources("tok")


class TestFetchAccountEmail:
    def test_returns_email_on_success(self, monkeypatch):
        monkeypatch.setattr(
            atlassian_oauth_module.requests, "get",
            lambda url, **kw: fake_response({"emailAddress": "me@x.com"}),
        )
        assert atlassian_oauth_module._fetch_account_email("tok", "cloud1") == "me@x.com"

    def test_uses_cloud_id_scoped_jira_myself_url(self, monkeypatch):
        captured = {}
        def fake_get(url, **kwargs):
            captured["url"] = url
            return fake_response({"emailAddress": "me@x.com"})
        monkeypatch.setattr(atlassian_oauth_module.requests, "get", fake_get)

        atlassian_oauth_module._fetch_account_email("tok", "cloud-42")

        assert captured["url"] == "https://api.atlassian.com/ex/jira/cloud-42/rest/api/3/myself"

    def test_never_raises_on_failure_returns_empty_string(self, monkeypatch):
        def raiser(*a, **kw):
            raise requests.ConnectionError("network down")
        monkeypatch.setattr(atlassian_oauth_module.requests, "get", raiser)
        assert atlassian_oauth_module._fetch_account_email("tok", "cloud1") == ""

    def test_http_error_status_also_swallowed(self, monkeypatch):
        monkeypatch.setattr(atlassian_oauth_module.requests, "get", lambda *a, **kw: fake_response(status_ok=False))
        assert atlassian_oauth_module._fetch_account_email("tok", "cloud1") == ""


class TestFetchAccountId:
    def test_returns_the_account_id(self, monkeypatch):
        monkeypatch.setattr(
            atlassian_oauth_module.requests, "get",
            lambda url, **kw: fake_response({"accountId": "acct-1"}),
        )
        assert atlassian_oauth_module._fetch_account_id("tok", "cloud1") == "acct-1"

    def test_returns_empty_when_the_request_fails(self, monkeypatch):
        def raiser(*a, **kw):
            raise requests.ConnectionError("network down")
        monkeypatch.setattr(atlassian_oauth_module.requests, "get", raiser)
        assert atlassian_oauth_module._fetch_account_id("tok", "cloud1") == ""
