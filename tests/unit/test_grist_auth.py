"""Tests for the Grist auth module.

The invariant: a Grist credential is only ever used with the server it was
entered or issued for. The tests fake ``requests`` at the module boundary (no
network), and check that no token, key or client secret reaches a raised
message or a repr.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from privacyfence import grist_auth
from privacyfence.grist_auth import (
    GRIST_SCOPES,
    GristApiKey,
    GristBundle,
    GristClientError,
    GristOAuthConfig,
    GristOAuthEndpoints,
    GristTokenProvider,
    bundle_settings,
    discover,
    normalize_server_url,
)
from privacyfence.oauth_loopback import OAuthLoopbackError

pytestmark = pytest.mark.unit

SECRET = "s3cr3t-client-secret"
ACCESS = "grist_at_ACCESSVALUE"
REFRESH = "grist_rt_REFRESHVALUE"
API_KEY = "APIKEYVALUE123"
CODE = "AUTHCODEVALUE"
SECRETS = (SECRET, ACCESS, REFRESH, API_KEY, CODE)

SERVER = "https://grist.example.com"
CONFIG = GristOAuthConfig(SERVER, "client-id", SECRET, SERVER)
BASIC_ENDPOINTS = GristOAuthEndpoints(f"{SERVER}/oidc/auth", f"{SERVER}/oidc/token", True)
POST_ENDPOINTS = GristOAuthEndpoints(f"{SERVER}/oidc/auth", f"{SERVER}/oidc/token", False)


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, *, raw_json: bool = True) -> None:
        self.status_code = status
        self._body = body
        self._raw_json = raw_json

    def json(self) -> Any:
        if not self._raw_json:
            raise ValueError("not json")
        return self._body


def document(issuer: str = SERVER, **extra: Any) -> dict[str, Any]:
    base = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oidc/auth",
        "token_endpoint": f"{issuer}/oidc/token",
    }
    return {**base, **extra}


class Net:
    """Records requests and answers from a per-URL table (or a callable)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answers: dict[str, Any] = {}
        monkeypatch.setattr(requests, "get", lambda url, **kw: self._answer("GET", url, kw))
        monkeypatch.setattr(requests, "post", lambda url, **kw: self._answer("POST", url, kw))

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    return Net(monkeypatch)


def assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


def oauth_record(**overrides: Any) -> dict[str, Any]:
    return {
        "auth": "oauth", "server_url": SERVER, "access_token": ACCESS,
        "refresh_token": REFRESH, "expires_at": 4_000_000_000.0, **overrides,
    }


def key_record(server: str = SERVER) -> dict[str, Any]:
    return {"auth": "api_key", "server_url": server, "api_key": API_KEY}


class TestNormalizeServerUrl:
    def test_lowercases_and_strips(self):
        assert normalize_server_url("  HTTPS://Docs.GetGrist.com/  ") == "https://docs.getgrist.com"

    def test_keeps_a_path_prefix_and_port(self):
        assert normalize_server_url("https://host.example:8484/grist/") == "https://host.example:8484/grist"

    @pytest.mark.parametrize("url", ["http://localhost:8484", "http://127.0.0.1:8484", "http://[::1]:8484"])
    def test_http_is_allowed_for_loopback(self, url):
        assert normalize_server_url(url) == url

    @pytest.mark.parametrize("url", ["", "   ", "/", "https://"])
    def test_empty_or_hostless(self, url):
        with pytest.raises(GristClientError) as err:
            normalize_server_url(url)
        assert str(err.value) == "Enter the Grist server address, such as https://docs.getgrist.com."

    @pytest.mark.parametrize("url", ["http://grist.example.com", "ftp://grist.example.com", "grist.example.com", "https://[x"])
    def test_scheme_rule(self, url):
        with pytest.raises(GristClientError) as err:
            normalize_server_url(url)
        assert str(err.value) == (
            "The Grist server address must start with https:// (http:// is allowed only for localhost)."
        )

    @pytest.mark.parametrize("url", [
        "https://user:pw@grist.example.com", "https://grist.example.com?x=1", "https://grist.example.com/#frag",
    ])
    def test_userinfo_query_fragment(self, url):
        with pytest.raises(GristClientError) as err:
            normalize_server_url(url)
        assert str(err.value) == (
            "The Grist server address must not contain a user name, password, query or fragment."
        )

    @pytest.mark.parametrize("url", ["https://grist.example.com/api", "https://grist.example.com/x/API/y"])
    def test_api_segment(self, url):
        with pytest.raises(GristClientError) as err:
            normalize_server_url(url)
        assert str(err.value) == "Enter the server address without /api."

    def test_a_segment_merely_containing_api_is_fine(self):
        assert normalize_server_url("https://grist.example.com/apiary") == "https://grist.example.com/apiary"


class TestBundleSettings:
    def test_empty_section_in_local_mode(self):
        assert bundle_settings({}, org_mode=False) == GristBundle("", None)

    def test_server_only(self):
        assert bundle_settings({"server_url": "https://g.example.com/"}, org_mode=True) == GristBundle(
            "https://g.example.com", None,
        )

    def test_client_pair_defaults_server_and_auth_server(self):
        bundle = bundle_settings({"client_id": "id", "client_secret": "sec"}, org_mode=False)
        assert bundle.server_url == "https://docs.getgrist.com"
        assert bundle.oauth == GristOAuthConfig(
            "https://docs.getgrist.com", "id", "sec", "https://docs.getgrist.com",
        )

    def test_auth_server_url_defaults_to_the_server(self):
        bundle = bundle_settings(
            {"server_url": "https://g.example.com", "client_id": "id", "client_secret": "sec"}, org_mode=True,
        )
        assert bundle.oauth.auth_server_url == "https://g.example.com"

    def test_explicit_auth_server_url(self):
        bundle = bundle_settings({
            "server_url": "https://g.example.com", "client_id": "id", "client_secret": "sec",
            "auth_server_url": "https://login.example.com/",
        }, org_mode=False)
        assert bundle.oauth.auth_server_url == "https://login.example.com"

    @pytest.mark.parametrize("section", [{"client_id": "id"}, {"client_secret": "sec"}])
    def test_half_a_pair_is_incomplete(self, section):
        with pytest.raises(GristClientError) as err:
            bundle_settings(section, org_mode=False)
        assert str(err.value) == "Grist organization config is incomplete: client_id and client_secret go together."

    def test_org_mode_needs_a_server(self):
        with pytest.raises(GristClientError) as err:
            bundle_settings({}, org_mode=True)
        assert str(err.value) == "Grist organization config not installed"

    def test_a_bad_url_raises_grist_client_error(self):
        with pytest.raises(GristClientError):
            bundle_settings({"server_url": "http://g.example.com"}, org_mode=False)

    def test_the_secret_is_not_in_the_repr(self):
        bundle = bundle_settings({"client_id": "id", "client_secret": SECRET}, org_mode=False)
        assert SECRET not in repr(bundle)


class TestDiscover:
    def test_parses_the_endpoints(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, document())
        assert discover(SERVER, SERVER) == BASIC_ENDPOINTS
        method, _, kwargs = net.calls[0]
        assert method == "GET" and kwargs["timeout"] == 30 and kwargs["allow_redirects"] is False

    @pytest.mark.parametrize("methods, expected", [
        (None, True),
        (["client_secret_basic", "client_secret_post"], True),
        (["client_secret_post"], False),
        ("client_secret_basic", False),
    ])
    def test_client_secret_basic(self, net, methods, expected):
        extra = {} if methods is None else {"token_endpoint_auth_methods_supported": methods}
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, document(**extra))
        assert discover(SERVER, SERVER).client_secret_basic is expected

    def test_cached_per_url(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, document())
        first = discover(SERVER, SERVER)
        assert discover(SERVER, SERVER) is first
        assert len(net.calls) == 1

    def test_http_endpoint_on_a_public_host_is_rejected(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(
            200, document(token_endpoint="http://grist.example.com/oidc/token"),
        )
        with pytest.raises(GristClientError, match="sign-in settings at grist.example.com are not usable"):
            discover(SERVER, SERVER)

    def test_http_endpoint_on_loopback_is_allowed(self, net):
        local = "http://localhost:8484"
        net.answers[f"{local}/.well-known/oauth-authorization-server"] = FakeResponse(200, document(local))
        assert discover(local, local).token_endpoint == f"{local}/oidc/token"

    @pytest.mark.parametrize("status", [301, 302, 308])
    def test_a_redirect_is_rejected(self, net, status):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(status, None)
        with pytest.raises(GristClientError) as err:
            discover(SERVER, SERVER)
        assert str(err.value) == (
            f"Grist's sign-in server answered with a redirect (HTTP {status}). "
            "Check the sign-in server in the organization config."
        )

    def test_a_server_error(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(503, None)
        with pytest.raises(GristClientError) as err:
            discover(SERVER, SERVER)
        assert str(err.value) == "Grist's sign-in server answered with an error (HTTP 503)."

    def test_a_non_json_answer(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, raw_json=False)
        with pytest.raises(GristClientError) as err:
            discover(SERVER, SERVER)
        assert str(err.value) == "Grist's sign-in server answered with something other than JSON (HTTP 200)."

    def test_a_json_array_is_not_an_object(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, [1])
        with pytest.raises(GristClientError, match="something other than JSON"):
            discover(SERVER, SERVER)

    def test_a_transport_failure_names_the_host_and_class_only(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = requests.ConnectTimeout("secret detail")
        with pytest.raises(GristClientError) as err:
            discover(SERVER, SERVER)
        assert str(err.value) == "Could not reach Grist's sign-in server at grist.example.com: ConnectTimeout"

    def test_a_missing_document_is_unusable(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(404, {"error": "x"})
        with pytest.raises(GristClientError, match="are not usable"):
            discover(SERVER, SERVER)

    @pytest.mark.parametrize("bad", [
        document(issuer=None),
        document(issuer=7),
        document(issuer="http://grist.example.com"),
        {"issuer": SERVER, "token_endpoint": f"{SERVER}/t"},
        {"issuer": SERVER, "authorization_endpoint": f"{SERVER}/a"},
        document(token_endpoint=5),
    ])
    def test_unusable_documents(self, net, bad):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, bad)
        with pytest.raises(GristClientError, match="are not usable"):
            discover(SERVER, SERVER)

    def test_getgrist_shape_follows_the_issuer_once(self, net):
        auth = "https://docs.getgrist.com"
        issuer = "https://login.getgrist.com"
        net.answers[f"{auth}/.well-known/oauth-authorization-server"] = FakeResponse(200, {
            "issuer": "https://login.getgrist.com/",
            "authorization_endpoint": "https://login.getgrist.com/oidc/auth",
            "token_endpoint": "https://login.getgrist.com/oidc/token",
        })
        net.answers[f"{issuer}/.well-known/oauth-authorization-server"] = FakeResponse(200, {
            "issuer": "https://login.getgrist.com/",
            "authorization_endpoint": "https://login.getgrist.com/oidc/auth",
            "token_endpoint": "https://login.getgrist.com/oidc/token",
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
        })
        endpoints = discover(auth, auth)
        assert endpoints == GristOAuthEndpoints(
            "https://login.getgrist.com/oidc/auth", "https://login.getgrist.com/oidc/token", True,
        )
        assert [url for _, url, _ in net.calls] == [
            f"{auth}/.well-known/oauth-authorization-server",
            f"{issuer}/.well-known/oauth-authorization-server",
        ]

    def test_an_issuer_whose_own_document_names_another_issuer(self, net):
        auth = "https://docs.getgrist.com"
        issuer = "https://login.getgrist.com"
        net.answers[f"{auth}/.well-known/oauth-authorization-server"] = FakeResponse(200, document(issuer))
        net.answers[f"{issuer}/.well-known/oauth-authorization-server"] = FakeResponse(
            200, document("https://evil.example.com"),
        )
        with pytest.raises(GristClientError, match="are not usable"):
            discover(auth, auth)
        assert len(net.calls) == 2

    def test_an_endpoint_on_another_host_is_rejected(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(
            200, document(token_endpoint="https://evil.example.com/oidc/token"),
        )
        with pytest.raises(GristClientError, match="are not usable"):
            discover(SERVER, SERVER)

    def test_an_endpoint_with_userinfo_is_rejected(self, net):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(
            200, document(token_endpoint="https://grist.example.com@evil.example.com/oidc/token"),
        )
        with pytest.raises(GristClientError, match="are not usable"):
            discover(SERVER, SERVER)


class TestBuildAuthorizeUrl:
    def test_every_parameter(self):
        url = grist_auth.build_authorize_url(BASIC_ENDPOINTS, "client-id", "http://localhost:53685/callback", "st", "ch")
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{SERVER}/oidc/auth"
        assert {k: v[0] for k, v in parse_qs(parts.query).items()} == {
            "response_type": "code",
            "client_id": "client-id",
            "redirect_uri": "http://localhost:53685/callback",
            "state": "st",
            "scope": "doc:read doc:write doc.schema:write offline_access",
            "prompt": "consent",
            "code_challenge": "ch",
            "code_challenge_method": "S256",
        }

    def test_scope_constant(self):
        assert GRIST_SCOPES == "doc:read doc:write doc.schema:write offline_access"


def token_answer(**overrides: Any) -> FakeResponse:
    body = {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600, **overrides}
    return FakeResponse(200, {k: v for k, v in body.items() if v is not None})


class TestExchangeCode:
    def exchange(self, net, endpoints, answer):
        net.answers[endpoints.token_endpoint] = answer
        return grist_auth.exchange_code(CONFIG, endpoints, CODE, "http://localhost:53685/callback", "verifier")

    def test_basic_auth_header_and_record(self, net, monkeypatch):
        monkeypatch.setattr(grist_auth.time, "time", lambda: 1000.0)
        record = self.exchange(net, BASIC_ENDPOINTS, token_answer())
        assert record == {
            "auth": "oauth", "server_url": SERVER, "access_token": ACCESS,
            "refresh_token": REFRESH, "expires_at": 4600.0,
        }
        _, _, kwargs = net.calls[0]
        assert kwargs["data"] == {
            "grant_type": "authorization_code", "code": CODE,
            "redirect_uri": "http://localhost:53685/callback", "code_verifier": "verifier",
        }
        # "client-id:s3cr3t-client-secret", base64
        assert kwargs["headers"]["Authorization"] == "Basic Y2xpZW50LWlkOnMzY3IzdC1jbGllbnQtc2VjcmV0"
        assert kwargs["timeout"] == 30 and kwargs["allow_redirects"] is False

    def test_credentials_are_percent_encoded_before_basic(self, net):
        config = GristOAuthConfig(SERVER, "a b", "c:d", SERVER)
        net.answers[BASIC_ENDPOINTS.token_endpoint] = token_answer()
        grist_auth.exchange_code(config, BASIC_ENDPOINTS, CODE, "r", "v")
        # "a%20b:c%3Ad"
        assert net.calls[0][2]["headers"]["Authorization"] == "Basic YSUyMGI6YyUzQWQ="

    def test_form_secret_when_basic_is_not_offered(self, net):
        self.exchange(net, POST_ENDPOINTS, token_answer())
        _, _, kwargs = net.calls[0]
        assert kwargs["data"]["client_id"] == "client-id" and kwargs["data"]["client_secret"] == SECRET
        assert "Authorization" not in kwargs["headers"]

    def test_default_lifetime_when_expires_in_is_missing(self, net, monkeypatch):
        monkeypatch.setattr(grist_auth.time, "time", lambda: 10.0)
        assert self.exchange(net, BASIC_ENDPOINTS, token_answer(expires_in=None))["expires_at"] == 3610.0

    def test_default_lifetime_when_expires_in_is_not_a_number(self, net, monkeypatch):
        monkeypatch.setattr(grist_auth.time, "time", lambda: 10.0)
        assert self.exchange(net, BASIC_ENDPOINTS, token_answer(expires_in=True))["expires_at"] == 3610.0

    def test_no_refresh_token(self, net):
        with pytest.raises(GristClientError) as err:
            self.exchange(net, BASIC_ENDPOINTS, token_answer(refresh_token=None))
        assert str(err.value) == (
            "Grist did not return a refresh token. Check that the PrivacyFence app in Grist allows offline_access."
        )

    def test_no_access_token(self, net):
        with pytest.raises(GristClientError, match="did not return an access token"):
            self.exchange(net, BASIC_ENDPOINTS, token_answer(access_token=None))

    def test_400_with_error_and_description(self, net):
        answer = FakeResponse(400, {"error": "invalid_client", "error_description": "bad client"})
        with pytest.raises(GristClientError) as err:
            self.exchange(net, BASIC_ENDPOINTS, answer)
        assert str(err.value) == "Grist sign-in failed: invalid_client: bad client"
        assert_no_secret(str(err.value))

    def test_error_without_description_and_long_values_are_cut(self, net):
        with pytest.raises(GristClientError) as err:
            self.exchange(net, BASIC_ENDPOINTS, FakeResponse(400, {"error": "e" * 500}))
        assert str(err.value) == "Grist sign-in failed: " + "e" * 200

    def test_4xx_without_json(self, net):
        with pytest.raises(GristClientError) as err:
            self.exchange(net, BASIC_ENDPOINTS, FakeResponse(403, raw_json=False))
        assert str(err.value) == "Grist sign-in failed: HTTP 403"

    def test_redirect_and_server_error(self, net):
        with pytest.raises(GristClientError, match="redirect"):
            self.exchange(net, BASIC_ENDPOINTS, FakeResponse(302))
        with pytest.raises(GristClientError, match="error \\(HTTP 500\\)"):
            self.exchange(net, BASIC_ENDPOINTS, FakeResponse(500))


class TestRefresh:
    def refresh(self, net, answer, endpoints=BASIC_ENDPOINTS):
        net.answers[endpoints.token_endpoint] = answer
        return grist_auth.refresh(CONFIG, endpoints, oauth_record(expires_at=1.0))

    def test_a_new_refresh_token_replaces_the_old(self, net, monkeypatch):
        monkeypatch.setattr(grist_auth.time, "time", lambda: 100.0)
        record = self.refresh(net, token_answer(access_token="grist_at_NEW", refresh_token="grist_rt_NEW"))
        assert record == oauth_record(access_token="grist_at_NEW", refresh_token="grist_rt_NEW", expires_at=3700.0)
        assert net.calls[0][2]["data"] == {"grant_type": "refresh_token", "refresh_token": REFRESH}

    def test_an_absent_refresh_token_keeps_the_old(self, net):
        record = self.refresh(net, token_answer(access_token="grist_at_NEW", refresh_token=None))
        assert record["refresh_token"] == REFRESH and record["access_token"] == "grist_at_NEW"

    def test_form_secret_when_basic_is_not_offered(self, net):
        self.refresh(net, token_answer(), POST_ENDPOINTS)
        assert net.calls[0][2]["data"]["client_secret"] == SECRET

    @pytest.mark.parametrize("answer", [
        FakeResponse(400, {"error": "invalid_grant", "error_description": "gone"}),
        FakeResponse(400, raw_json=False),
        FakeResponse(401, raw_json=False),
    ])
    def test_expired_or_revoked(self, net, answer):
        with pytest.raises(GristClientError) as err:
            self.refresh(net, answer)
        assert str(err.value) == (
            "Your Grist sign-in has expired or was revoked. Use Authenticate… in PrivacyFence Settings to sign in again."
        )

    def test_other_4xx_with_a_body(self, net):
        with pytest.raises(GristClientError) as err:
            self.refresh(net, FakeResponse(400, {"error": "invalid_client", "error_description": "no"}))
        assert str(err.value) == "Grist sign-in refresh failed: invalid_client: no"

    def test_other_4xx_without_a_body(self, net):
        with pytest.raises(GristClientError) as err:
            self.refresh(net, FakeResponse(429, raw_json=False))
        assert str(err.value) == "Grist sign-in refresh failed: HTTP 429"

    def test_no_access_token(self, net):
        with pytest.raises(GristClientError, match="did not return an access token"):
            self.refresh(net, token_answer(access_token=None))


class TestTokenFile:
    def test_oauth_round_trip(self, tmp_path):
        path = str(tmp_path / "grist_token.json")
        grist_auth.save_token_file(path, oauth_record())
        assert grist_auth.load_token_file(path) == oauth_record()

    def test_api_key_round_trip_and_shape(self, tmp_path):
        path = str(tmp_path / "grist_token.json")
        grist_auth.save_api_key(path, "https://Grist.Example.com/", API_KEY)
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh) == {"auth": "api_key", "server_url": SERVER, "api_key": API_KEY}
        assert grist_auth.load_token_file(path) == key_record()

    def test_save_api_key_refuses_a_bad_server(self, tmp_path):
        with pytest.raises(GristClientError):
            grist_auth.save_api_key(str(tmp_path / "t.json"), "http://grist.example.com", API_KEY)
        assert not (tmp_path / "t.json").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_mode_is_0600(self, tmp_path):
        path = tmp_path / "grist_token.json"
        grist_auth.save_api_key(str(path), SERVER, API_KEY)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_load_normalizes_the_server(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(key_record("https://Grist.example.com/")), encoding="utf-8")
        assert grist_auth.load_token_file(str(path))["server_url"] == SERVER

    def test_load_refuses_plain_http_on_a_public_host(self, tmp_path):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(key_record("http://grist.example.com")), encoding="utf-8")
        with pytest.raises(GristClientError, match="must start with https://"):
            grist_auth.load_token_file(str(path))

    NOT_AUTH = "Grist is not authenticated. Use Authenticate… in PrivacyFence Settings."
    UNREADABLE = (
        "Grist's saved sign-in could not be read. Use Authenticate… in PrivacyFence Settings to connect again."
    )

    def test_missing_file(self, tmp_path):
        with pytest.raises(GristClientError) as err:
            grist_auth.load_token_file(str(tmp_path / "none.json"))
        assert str(err.value) == self.NOT_AUTH

    @pytest.mark.parametrize("record", [
        {"server_url": SERVER, "api_key": API_KEY},
        {"auth": "basic", "server_url": SERVER, "api_key": API_KEY},
        {"auth": ["oauth"], "server_url": SERVER},
        {"auth": "api_key", "server_url": SERVER},
        {"auth": "api_key", "api_key": API_KEY},
        {"auth": "api_key", "server_url": SERVER, "api_key": ""},
        {"auth": "oauth", "server_url": SERVER, "refresh_token": REFRESH, "expires_at": 1.0},
        {"auth": "oauth", "server_url": SERVER, "access_token": ACCESS, "expires_at": 1.0},
        {"auth": "oauth", "server_url": SERVER, "access_token": ACCESS, "refresh_token": REFRESH},
        {"auth": "oauth", "server_url": SERVER, "access_token": ACCESS, "refresh_token": REFRESH, "expires_at": True},
    ])
    def test_unknown_auth_or_missing_key(self, tmp_path, record):
        path = tmp_path / "t.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(GristClientError) as err:
            grist_auth.load_token_file(str(path))
        assert str(err.value) == self.NOT_AUTH

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"', ""])
    def test_invalid_json_and_non_objects(self, tmp_path, content):
        path = tmp_path / "t.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(GristClientError) as err:
            grist_auth.load_token_file(str(path))
        assert str(err.value) == self.UNREADABLE

    def test_an_unreadable_file(self, tmp_path):
        with pytest.raises(GristClientError) as err:
            grist_auth.load_token_file(str(tmp_path))  # a directory exists but cannot be read as a file
        assert str(err.value) == self.UNREADABLE


class TestResolveCredential:
    OAUTH_BUNDLE = GristBundle(SERVER, CONFIG)
    PLAIN_BUNDLE = GristBundle("", None)
    PINNED_BUNDLE = GristBundle(SERVER, None)
    DIFFERENT = (
        "Grist was connected to a different server than your organization uses. "
        "Use Authenticate… in PrivacyFence Settings to connect again."
    )

    def test_oauth_bundle_and_oauth_record(self):
        server, credential = grist_auth.resolve_credential(self.OAUTH_BUNDLE, oauth_record(), "tok.json")
        assert server == SERVER
        assert isinstance(credential, GristTokenProvider) and credential.can_refresh is True

    def test_oauth_bundle_and_api_key_record(self):
        with pytest.raises(GristClientError) as err:
            grist_auth.resolve_credential(self.OAUTH_BUNDLE, key_record(), "tok.json")
        assert str(err.value) == (
            "Your organization connects to Grist with OAuth. Use Authenticate… in PrivacyFence Settings."
        )
        assert_no_secret(str(err.value))

    def test_no_oauth_and_api_key_record_uses_the_records_server(self):
        server, credential = grist_auth.resolve_credential(self.PLAIN_BUNDLE, key_record("https://other.example.com"), "t")
        assert server == "https://other.example.com"
        assert isinstance(credential, GristApiKey) and credential.access_token() == API_KEY

    def test_no_oauth_and_oauth_record(self):
        with pytest.raises(GristClientError) as err:
            grist_auth.resolve_credential(self.PLAIN_BUNDLE, oauth_record(), "t")
        assert str(err.value) == "Grist is not authenticated. Use Authenticate… in PrivacyFence Settings."

    def test_pinned_server_matching(self):
        server, _ = grist_auth.resolve_credential(self.PINNED_BUNDLE, key_record(), "t")
        assert server == SERVER

    def test_pinned_server_different_for_an_api_key(self):
        with pytest.raises(GristClientError) as err:
            grist_auth.resolve_credential(self.PINNED_BUNDLE, key_record("https://other.example.com"), "t")
        assert str(err.value) == self.DIFFERENT
        assert_no_secret(str(err.value))

    def test_oauth_server_different_for_an_oauth_record(self):
        with pytest.raises(GristClientError) as err:
            grist_auth.resolve_credential(self.OAUTH_BUNDLE, oauth_record(server_url="https://other.example.com"), "t")
        assert str(err.value) == self.DIFFERENT
        assert_no_secret(str(err.value))


class TestGristApiKey:
    def test_returns_the_key(self):
        key = GristApiKey(API_KEY)
        assert key.access_token() == API_KEY
        assert key.access_token(force_refresh=True) == API_KEY

    def test_cannot_refresh(self):
        assert GristApiKey(API_KEY).can_refresh is False

    def test_repr_hides_the_key(self):
        assert_no_secret(repr(GristApiKey(API_KEY)))


class TestGristTokenProvider:
    @pytest.fixture
    def refreshed(self, monkeypatch):
        calls: list[dict[str, Any]] = []

        def fake_refresh(config, endpoints, record):
            calls.append(record)
            return {**record, "access_token": "grist_at_NEW", "expires_at": 9_000_000_000.0}

        monkeypatch.setattr(grist_auth, "refresh", fake_refresh)
        monkeypatch.setattr(grist_auth, "discover", lambda auth, server: BASIC_ENDPOINTS)
        return calls

    def test_no_refresh_while_valid_and_the_file_is_never_read(self, tmp_path, refreshed, monkeypatch):
        monkeypatch.setattr(grist_auth, "load_token_file", lambda path: pytest.fail("read the file"))
        provider = GristTokenProvider(CONFIG, str(tmp_path / "t.json"), oauth_record())
        assert provider.access_token() == ACCESS
        assert refreshed == []

    def test_refreshes_within_a_minute_of_expiry_and_saves(self, tmp_path, refreshed, monkeypatch):
        monkeypatch.setattr(grist_auth.time, "time", lambda: 1000.0)
        path = str(tmp_path / "t.json")
        provider = GristTokenProvider(CONFIG, path, oauth_record(expires_at=1059.0))
        assert provider.access_token() == "grist_at_NEW"
        assert len(refreshed) == 1
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["access_token"] == "grist_at_NEW"
        assert provider.access_token() == "grist_at_NEW"
        assert len(refreshed) == 1

    def test_does_not_refresh_just_outside_the_margin(self, tmp_path, refreshed, monkeypatch):
        monkeypatch.setattr(grist_auth.time, "time", lambda: 1000.0)
        provider = GristTokenProvider(CONFIG, str(tmp_path / "t.json"), oauth_record(expires_at=1061.0))
        assert provider.access_token() == ACCESS
        assert refreshed == []

    def test_force_refresh(self, tmp_path, refreshed):
        provider = GristTokenProvider(CONFIG, str(tmp_path / "t.json"), oauth_record())
        assert provider.access_token(force_refresh=True) == "grist_at_NEW"
        assert len(refreshed) == 1

    def test_a_failed_refresh_does_not_save(self, tmp_path, monkeypatch):
        def failing(config, endpoints, record):
            raise GristClientError("expired")

        monkeypatch.setattr(grist_auth, "refresh", failing)
        monkeypatch.setattr(grist_auth, "discover", lambda auth, server: BASIC_ENDPOINTS)
        path = tmp_path / "t.json"
        provider = GristTokenProvider(CONFIG, str(path), oauth_record())
        with pytest.raises(GristClientError):
            provider.access_token(force_refresh=True)
        assert not path.exists()

    def test_repr_hides_the_token(self):
        provider = GristTokenProvider(CONFIG, "t.json", oauth_record())
        assert_no_secret(repr(provider))
        assert_no_secret(repr(CONFIG))


class TestAuthorizeInteractive:
    def test_runs_the_browser_flow_and_saves(self, tmp_path, net, monkeypatch):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, document())
        net.answers[BASIC_ENDPOINTS.token_endpoint] = token_answer()
        seen: dict[str, Any] = {}

        def fake_run(build, exchange, **kwargs):
            seen.update(kwargs)
            seen["url"] = build("http://localhost:53685/callback", "st", "ch")
            return exchange(CODE, "http://localhost:53685/callback", "verifier")

        monkeypatch.setattr(grist_auth.oauth_loopback, "run_browser_oauth", fake_run)
        path = str(tmp_path / "t.json")
        record = grist_auth.authorize_interactive(CONFIG, path)
        assert seen["port"] == 53685 and seen["path"] == "/callback" and seen["redirect_host"] == "localhost"
        assert seen["url"].startswith(f"{SERVER}/oidc/auth?")
        assert record["auth"] == "oauth" and record["server_url"] == SERVER
        assert grist_auth.load_token_file(path) == record

    def test_loopback_error(self, tmp_path, net, monkeypatch):
        net.answers[f"{SERVER}/.well-known/oauth-authorization-server"] = FakeResponse(200, document())

        def fake_run(build, exchange, **kwargs):
            raise OAuthLoopbackError("timed out")

        monkeypatch.setattr(grist_auth.oauth_loopback, "run_browser_oauth", fake_run)
        with pytest.raises(GristClientError) as err:
            grist_auth.authorize_interactive(CONFIG, str(tmp_path / "t.json"))
        assert str(err.value) == "Grist sign-in failed: timed out"
        assert not (tmp_path / "t.json").exists()
