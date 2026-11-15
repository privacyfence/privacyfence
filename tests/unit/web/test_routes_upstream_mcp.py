"""GET /upstream-mcp/jwks serves the claim signer's Ed25519 key set."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence.identity_claim import ClaimSigner
from privacyfence.upstream_claim_keys import KeySet
from privacyfence.web import routes_upstream_mcp


def _signer(tmp_path):
    keys = KeySet(tmp_path / "keys.json")
    keys.ensure()
    return ClaimSigner(issuer="https://pf.example", keys=keys), keys


def test_jwks_is_the_signers_key_set_with_a_five_minute_cache(tmp_path):
    signer, keys = _signer(tmp_path)
    client = TestClient(Starlette(routes=routes_upstream_mcp.build_routes(signer)))
    response = client.get("/upstream-mcp/jwks")
    assert response.status_code == 200
    assert response.json() == signer.jwks()
    assert [k["kid"] for k in response.json()["keys"]] == [keys.signing_key().kid]
    assert all(
        {k["kty"], k["crv"], k["alg"], k["use"]} == {"OKP", "Ed25519", "EdDSA", "sig"} for k in response.json()["keys"]
    )
    assert response.headers["cache-control"] == "max-age=300"


def test_only_get_is_routed(tmp_path):
    client = TestClient(Starlette(routes=routes_upstream_mcp.build_routes(_signer(tmp_path)[0])))
    assert client.post("/upstream-mcp/jwks").status_code == 405
