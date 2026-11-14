"""GET /upstream-mcp/jwks serves the claim signer's key set."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import oidc_provider
from privacyfence.upstream_claims import ClaimSigner
from privacyfence.web import routes_upstream_mcp


def test_jwks_is_the_signers_key_set_with_a_five_minute_cache(tmp_path):
    key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
    signer = ClaimSigner(issuer="https://pf.example/upstream-mcp", key=key)
    client = TestClient(Starlette(routes=routes_upstream_mcp.build_routes(signer)))
    response = client.get("/upstream-mcp/jwks")
    assert response.status_code == 200
    assert response.json() == signer.jwks()
    assert [k["kid"] for k in response.json()["keys"]] == [key.kid]
    assert response.headers["cache-control"] == "max-age=300"


def test_only_get_is_routed(tmp_path):
    key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
    client = TestClient(Starlette(routes=routes_upstream_mcp.build_routes(
        ClaimSigner(issuer="https://pf.example/upstream-mcp", key=key),
    )))
    assert client.post("/upstream-mcp/jwks").status_code == 405
