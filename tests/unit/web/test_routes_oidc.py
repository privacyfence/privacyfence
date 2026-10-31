"""Tests for the OIDC provider's routes: discovery, key set, authorize, token and userinfo."""
from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import parse_qs, quote, urlsplit

import jwt
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import oidc_provider
from privacyfence.principal import Principal
from privacyfence.web import routes_oidc
from privacyfence.web.org_session import SESSION_COOKIE, OrgSessionStore


def _client(tmp_path, issuer):
    key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
    provider = oidc_provider.OidcProvider(issuer=issuer, clients={}, key=key)
    routes = routes_oidc.build_routes(provider, sessions=OrgSessionStore())
    return TestClient(Starlette(routes=routes)), key


def test_discovery_document_exactly(tmp_path):
    client, _ = _client(tmp_path, "https://pf.example.com" + "/oidc")
    response = client.get("/oidc/.well-known/openid-configuration")
    assert response.status_code == 200
    iss = "https://pf.example.com/oidc"
    assert response.json() == {
        "issuer": iss, "authorization_endpoint": f"{iss}/authorize", "token_endpoint": f"{iss}/token",
        "userinfo_endpoint": f"{iss}/userinfo", "jwks_uri": f"{iss}/jwks",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": ["sub", "email", "email_verified", "name"],
    }


def test_issuer_built_from_a_url_with_a_trailing_slash(tmp_path):
    issuer = "https://pf.example.com/".rstrip("/") + "/oidc"
    client, _ = _client(tmp_path, issuer)
    document = client.get("/oidc/.well-known/openid-configuration").json()
    assert document["issuer"] == "https://pf.example.com/oidc"
    assert document["jwks_uri"] == "https://pf.example.com/oidc/jwks"


def test_a_token_signed_with_the_key_verifies_against_the_jwks(tmp_path):
    client, key = _client(tmp_path, "https://pf.example.com/oidc")
    document = client.get("/oidc/jwks").json()
    token = jwt.encode(
        {"iss": "https://pf.example.com/oidc", "sub": "u1", "aud": "peek"},
        key.private_key, algorithm="RS256", headers={"kid": key.kid},
    )
    kid = jwt.get_unverified_header(token)["kid"]
    (jwk,) = [k for k in document["keys"] if k["kid"] == kid]
    public = jwt.PyJWK(jwk).key
    claims = jwt.decode(token, public, algorithms=["RS256"], audience="peek", issuer="https://pf.example.com/oidc")
    assert claims["sub"] == "u1"


def test_only_get_is_allowed(tmp_path):
    client, _ = _client(tmp_path, "https://pf.example.com/oidc")
    assert client.post("/oidc/jwks").status_code == 405


# ----- #
# ----- Authorization-code flow
# ----- #

ISS = "https://pf.example.com/oidc"
PEEK_SECRET = "p" * 40
GRIST_SECRET = "g" * 40
PEEK_CB = "https://peek.example.com/cb"
GRIST_CB = "https://grist.example.com/cb?tenant=a"
VERIFIER = "v" * 50
ANN = Principal(id="u-ann", email="ann@example.com", display_name="Ann Example")


def _challenge(verifier: str = VERIFIER) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class _Env:
    def __init__(self, tmp_path, *, email_verified=True, principal=ANN, signed_in=True):
        self.clock = _Clock()
        self.key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
        clients = {
            "peek": oidc_provider.OidcClient("peek", (PEEK_CB,), PEEK_SECRET),
            "grist": oidc_provider.OidcClient("grist", (GRIST_CB,), GRIST_SECRET),
        }
        self.provider = oidc_provider.OidcProvider(issuer=ISS, clients=clients, key=self.key, clock=self.clock)
        self.sessions = OrgSessionStore()
        app = Starlette(routes=routes_oidc.build_routes(self.provider, sessions=self.sessions))
        self.http = TestClient(app, base_url="https://pf.example.com", follow_redirects=False)
        if signed_in:
            self.http.cookies.set(SESSION_COOKIE, self.sessions.create(principal, email_verified=email_verified))

    def authorize(self, **overrides):
        params = {
            "client_id": "peek", "redirect_uri": PEEK_CB, "response_type": "code", "scope": "openid email profile",
            "state": "st-1", "nonce": "n-1", "code_challenge": _challenge(), "code_challenge_method": "S256",
        }
        params.update(overrides)
        return self.http.get("/oidc/authorize", params={k: v for k, v in params.items() if v is not None})

    def code(self, **overrides) -> str:
        response = self.authorize(**overrides)
        assert response.status_code == 302, response.text
        return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]

    def token(self, code, *, client_id="peek", secret=PEEK_SECRET, redirect_uri=PEEK_CB, verifier=VERIFIER,
              basic=True, extra=None):
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if verifier is not None:
            data["code_verifier"] = verifier
        headers = {}
        if basic:
            headers["Authorization"] = _basic(client_id, secret)
        else:
            data.update({"client_id": client_id, "client_secret": secret})
        data.update(extra or {})
        return self.http.post("/oidc/token", data=data, headers=headers)


def _basic(client_id: str, secret: str) -> str:
    raw = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _query(response) -> dict[str, list[str]]:
    return parse_qs(urlsplit(response.headers["location"]).query)


def _verify_id_token(env: _Env, id_token: str, audience: str = "peek") -> dict:
    document = env.http.get("/oidc/jwks").json()
    kid = jwt.get_unverified_header(id_token)["kid"]
    (jwk,) = [k for k in document["keys"] if k["kid"] == kid]
    return jwt.decode(
        id_token, jwt.PyJWK(jwk).key, algorithms=["RS256"], audience=audience, issuer=ISS,
        options={"verify_exp": False, "verify_iat": False},
    )


def test_authorize_unknown_client_400(tmp_path):
    response = _Env(tmp_path).authorize(client_id="nobody")
    assert response.status_code == 400
    assert response.text == "Unknown client."
    assert "location" not in response.headers


def test_authorize_unregistered_redirect_400(tmp_path):
    env = _Env(tmp_path)
    for uri in ("https://evil.example.com/cb", PEEK_CB + "/", GRIST_CB, None):
        response = env.authorize(redirect_uri=uri)
        assert response.status_code == 400
        assert response.text == "This redirect address is not registered for this client."
        assert "location" not in response.headers


def test_authorize_without_session_redirects_to_login_keeping_query(tmp_path):
    env = _Env(tmp_path, signed_in=False)
    response = env.authorize()
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/login?next=")
    next_path = parse_qs(urlsplit(location).query)["next"][0]
    assert next_path.startswith("/oidc/authorize?")
    assert parse_qs(urlsplit(next_path).query)["code_challenge"] == [_challenge()]
    assert location == "/login?next=" + quote(next_path, safe="")
    # Following "next" once signed in finishes the flow.
    env.http.cookies.set(SESSION_COOKIE, env.sessions.create(ANN, email_verified=True))
    again = env.http.get(next_path)
    assert again.status_code == 302
    assert again.headers["location"].startswith(PEEK_CB + "?code=")


def test_authorize_bad_scope_redirects_error(tmp_path):
    response = _Env(tmp_path).authorize(scope="email profile")
    assert response.status_code == 302
    assert response.headers["location"].startswith(PEEK_CB + "?")
    query = _query(response)
    assert query["error"] == ["invalid_scope"]
    assert query["error_description"]
    assert query["state"] == ["st-1"]
    assert "code" not in query


def test_unsupported_response_type_redirects_error(tmp_path):
    response = _Env(tmp_path).authorize(response_type="token")
    assert _query(response)["error"] == ["unsupported_response_type"]


@pytest.mark.parametrize("overrides", [
    {"code_challenge": None},
    {"code_challenge_method": None},
    {"code_challenge": "short"},
    {"code_challenge": "x" * 129},
    {"code_challenge": "a" * 42 + "!"},
])
def test_authorize_missing_pkce_refused(tmp_path, overrides):
    response = _Env(tmp_path).authorize(**overrides)
    assert response.status_code == 302
    query = _query(response)
    assert query["error"] == ["invalid_request"]
    assert query["error_description"] == ["PKCE with S256 is required."]


def test_authorize_plain_pkce_refused(tmp_path):
    response = _Env(tmp_path).authorize(code_challenge=VERIFIER, code_challenge_method="plain")
    assert _query(response)["error"] == ["invalid_request"]
    assert _query(response)["error_description"] == ["PKCE with S256 is required."]


def test_error_redirect_merges_existing_query_and_omits_absent_state(tmp_path):
    response = _Env(tmp_path).authorize(client_id="grist", redirect_uri=GRIST_CB, scope="email", state=None)
    location = response.headers["location"]
    assert location.startswith(GRIST_CB + "&")
    query = _query(response)
    assert query["tenant"] == ["a"]
    assert query["error"] == ["invalid_scope"]
    assert "state" not in query


def test_authorize_principal_without_email_is_access_denied(tmp_path):
    response = _Env(tmp_path, principal=Principal(id="u-none")).authorize()
    assert _query(response)["error"] == ["access_denied"]
    assert _query(response)["state"] == ["st-1"]


def test_authorize_success_is_no_store_and_keeps_state(tmp_path):
    env = _Env(tmp_path)
    response = env.authorize(client_id="grist", redirect_uri=GRIST_CB, state=None)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["location"].startswith(GRIST_CB + "&code=")
    query = _query(response)
    assert set(query) == {"tenant", "code"}
    assert _query(env.authorize())["state"] == ["st-1"]


def test_authorize_is_get_only(tmp_path):
    assert _Env(tmp_path).http.post("/oidc/authorize").status_code == 405


def test_full_flow_basic_auth(tmp_path, caplog):
    env = _Env(tmp_path)
    with caplog.at_level(logging.INFO, logger="privacyfence.web.routes_oidc"):
        response = env.token(env.code())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 300
    claims = _verify_id_token(env, body["id_token"])
    assert claims["iss"] == ISS
    assert claims["aud"] == "peek"
    assert claims["sub"] == "u-ann"
    assert claims["nonce"] == "n-1"
    expected_at_hash = base64.urlsafe_b64encode(
        hashlib.sha256(body["access_token"].encode()).digest()[:16]
    ).rstrip(b"=").decode()
    assert claims["at_hash"] == expected_at_hash
    assert claims["email"] == "ann@example.com"
    assert claims["email_verified"] is True
    assert claims["name"] == "Ann Example"
    assert claims["exp"] - claims["iat"] == 300
    assert isinstance(claims["auth_time"], int)
    assert jwt.get_unverified_header(body["id_token"])["alg"] == "RS256"
    assert "OIDC sign-in to peek for principal u-ann" in caplog.text
    for secret in (body["access_token"], body["id_token"], "ann@example.com"):
        assert secret not in caplog.text


def test_full_flow_post_auth(tmp_path):
    env = _Env(tmp_path)
    response = env.token(env.code(), basic=False)
    assert response.status_code == 200
    claims = _verify_id_token(env, response.json()["id_token"])
    assert claims["aud"] == "peek"


def test_id_token_has_no_nonce_when_none_was_sent(tmp_path):
    env = _Env(tmp_path)
    claims = _verify_id_token(env, env.token(env.code(nonce=None)).json()["id_token"])
    assert "nonce" not in claims


def test_basic_and_form_secret_together_400(tmp_path):
    env = _Env(tmp_path)
    response = env.token(env.code(), extra={"client_secret": PEEK_SECRET})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}


def test_basic_auth_plus_encoded_secret(tmp_path):
    # openid-client form-encodes the client ID and secret before Basic-encoding them.
    secret = "a+b c:d%e/" + "s" * 32
    env = _Env(tmp_path)
    env.provider._clients["peek"] = oidc_provider.OidcClient("peek", (PEEK_CB,), secret)
    raw = f"peek:{secret.replace('%', '%25').replace('+', '%2B').replace(' ', '+').replace(':', '%3A')}"
    header = "Basic " + base64.b64encode(raw.encode()).decode()
    data = {"grant_type": "authorization_code", "code": env.code(), "redirect_uri": PEEK_CB, "code_verifier": VERIFIER}
    response = env.http.post("/oidc/token", data=data, headers={"Authorization": header})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("basic", [True, False])
def test_non_ascii_secret_is_401_not_500(tmp_path, basic):
    env = _Env(tmp_path)
    response = env.token(env.code(), secret="ünïcødé-" + "x" * 32, basic=basic)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_client"}


@pytest.mark.parametrize("header", [
    "Basic !!!not-base64", "Basic " + base64.b64encode(b"no-colon").decode(),
    "Basic " + base64.b64encode(b"\xff\xfe:x").decode(), "Basic ünï".encode("latin-1"),
])
def test_unreadable_basic_header_is_401(tmp_path, header):
    env = _Env(tmp_path)
    response = env.http.post("/oidc/token", data={"grant_type": "authorization_code"}, headers={"Authorization": header})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_no_client_credentials_is_401(tmp_path):
    response = _Env(tmp_path).http.post("/oidc/token", data={"grant_type": "authorization_code"})
    assert response.status_code == 401


def test_unsupported_grant_type(tmp_path):
    env = _Env(tmp_path)
    response = env.token("x", extra={"grant_type": "refresh_token"})
    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_grant_type"}


def test_code_single_use(tmp_path):
    env = _Env(tmp_path)
    code = env.code()
    assert env.token(code).status_code == 200
    second = env.token(code)
    assert second.status_code == 400
    assert second.json() == {"error": "invalid_grant"}


def test_code_expired(tmp_path):
    env = _Env(tmp_path)
    code = env.code()
    env.clock.now += 61
    assert env.token(code).json() == {"error": "invalid_grant"}


def test_code_wrong_client(tmp_path):
    env = _Env(tmp_path)
    code = env.code()
    response = env.token(code, client_id="grist", secret=GRIST_SECRET)
    assert response.json() == {"error": "invalid_grant"}
    # A failed redemption still consumes the code.
    assert env.token(code).json() == {"error": "invalid_grant"}


def test_code_wrong_redirect(tmp_path):
    env = _Env(tmp_path)
    assert env.token(env.code(), redirect_uri=PEEK_CB + "/other").json() == {"error": "invalid_grant"}


def test_pkce_wrong_verifier(tmp_path):
    env = _Env(tmp_path)
    assert env.token(env.code(), verifier="w" * 50).json() == {"error": "invalid_grant"}


def test_pkce_missing_verifier(tmp_path):
    env = _Env(tmp_path)
    # Even a challenge made from the empty verifier needs a verifier to be sent.
    code = env.code(code_challenge=_challenge(""))
    assert env.token(code, verifier=None).json() == {"error": "invalid_grant"}


def test_token_bad_secret_401(tmp_path):
    env = _Env(tmp_path)
    for basic in (True, False):
        response = env.token(env.code(), secret="z" * 40, basic=basic)
        assert response.status_code == 401
        assert response.json() == {"error": "invalid_client"}
        assert response.headers["www-authenticate"] == "Basic"
    assert env.token("x", client_id="nobody").status_code == 401


def test_token_is_post_only(tmp_path):
    assert _Env(tmp_path).http.get("/oidc/token").status_code == 405


def test_userinfo_ok(tmp_path):
    env = _Env(tmp_path)
    access_token = env.token(env.code()).json()["access_token"]
    expected = {"sub": "u-ann", "email": "ann@example.com", "email_verified": True, "name": "Ann Example"}
    for method in (env.http.get, env.http.post):
        response = method("/oidc/userinfo", headers={"Authorization": f"Bearer {access_token}"})
        assert response.status_code == 200
        assert response.json() == expected
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("header", [None, "Bearer nope", "Basic abc", "Bearer "])
def test_userinfo_without_a_live_token_401(tmp_path, header):
    env = _Env(tmp_path)
    headers = {"Authorization": header} if header else {}
    response = env.http.get("/oidc/userinfo", headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_token"}
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_userinfo_expired_401(tmp_path):
    env = _Env(tmp_path)
    access_token = env.token(env.code()).json()["access_token"]
    env.clock.now += 301
    response = env.http.get("/oidc/userinfo", headers={"Authorization": f"Bearer {access_token}"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_unverified_session_gives_email_verified_false(tmp_path):
    env = _Env(tmp_path, email_verified=False)
    body = env.token(env.code()).json()
    assert _verify_id_token(env, body["id_token"])["email_verified"] is False
    info = env.http.get("/oidc/userinfo", headers={"Authorization": f"Bearer {body['access_token']}"}).json()
    assert info["email_verified"] is False


def test_scope_echoed(tmp_path):
    env = _Env(tmp_path)
    assert env.token(env.code(scope="openid")).json()["scope"] == "openid"
    assert env.token(env.code(scope="email  openid email")).json()["scope"] == "email openid"


def test_code_cap_temporarily_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(oidc_provider, "MAX_LIVE_ENTRIES", 2)
    env = _Env(tmp_path)
    env.code()
    env.code()
    response = env.authorize()
    assert response.status_code == 302
    assert _query(response)["error"] == ["temporarily_unavailable"]
    assert _query(response)["state"] == ["st-1"]
    # Once the earlier codes expire there is room again.
    env.clock.now += 61
    env.code()
