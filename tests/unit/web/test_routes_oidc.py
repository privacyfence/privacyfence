"""Tests for the OIDC discovery and key-set routes."""
from __future__ import annotations

import jwt
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import oidc_provider
from privacyfence.web import routes_oidc
from privacyfence.web.org_session import OrgSessionStore


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
