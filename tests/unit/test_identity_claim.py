"""The v1 identity claim, the canonical request hash and the signer (plan 3.1 to 3.3)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from privacyfence import identity_claim as ic
from privacyfence import upstream_claim_keys as ucw
from privacyfence.upstream_claim_keys import KeySet

ISSUER = "https://pf.example.org"
AUD = "https://hub.example.org/mcp"
VECTORS = json.loads(
    (Path(__file__).resolve().parents[2] / "contrib" / "verifier" / "req_test_vectors.json").read_text("utf-8")
)["vectors"]


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def keys(tmp_path, clock) -> KeySet:
    made = KeySet(tmp_path / ucw.FILENAME, overlap_seconds=3600, clock=clock)
    made.ensure()
    return made


@pytest.fixture
def signer(keys, clock) -> ic.ClaimSigner:
    return ic.ClaimSigner(issuer=ISSUER, keys=keys, clock=clock)


def _decode(token: str, jwks: dict) -> tuple[dict, dict]:
    header = jwt.get_unverified_header(token)
    entry = next(k for k in jwks["keys"] if k["kid"] == header["kid"])
    raw = base64.urlsafe_b64decode(entry["x"] + "=" * (-len(entry["x"]) % 4))
    payload = jwt.decode(
        token, Ed25519PublicKey.from_public_bytes(raw), algorithms=["EdDSA"], audience=AUD, issuer=ISSUER,
        options={"verify_exp": False, "verify_iat": False},
    )
    return header, payload


PERSON = ic.IdentityCaller(
    sub="p-1", principal_type="claude_client", email="a@x.example", audiences=("mgmt", "all"),
    tools=("search", "get_doc"), act={"sub": "client-9", "agent": "claude-desktop"},
)
WORKFLOW = ic.IdentityCaller(
    sub="n8n-token:t1", principal_type="n8n_workflow", audiences=("all",), tools=("search",),
    approved_by="p-admin", token_id="t1",
)
SERVICE = ic.IdentityCaller(sub="privacyfence", principal_type="service")


class TestRequestHash:
    @pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
    def test_vector(self, vector):
        assert ic.canonical_request(vector["tool"], vector["arguments"]) == vector["canonical"].encode("utf-8")
        assert ic.request_hash(vector["tool"], vector["arguments"]) == vector["req"]

    def test_nan_is_refused(self):
        with pytest.raises(ValueError):
            ic.canonical_request("search", {"x": float("nan")})


class TestMint:
    def test_claude_client(self, signer):
        minted = signer.mint(audience=AUD, caller=PERSON, req="R", lifetime=30, now=1_000_000)
        header, payload = _decode(minted.token, signer.jwks())
        assert header["alg"] == "EdDSA" and header["typ"] == ic.CLAIM_TYPE and header["kid"]
        assert payload["claim_version"] == 1
        assert payload["iss"] == ISSUER and payload["aud"] == AUD
        assert payload["sub"] == "p-1" and payload["principal_type"] == "claude_client"
        assert payload["email"] == "a@x.example"
        assert payload["act"] == {"sub": "client-9", "agent": "claude-desktop"}
        assert payload["audiences"] == ["all", "mgmt"]
        assert payload["tools"] == ["get_doc", "search"]
        assert payload["req"] == "R"
        assert payload["exp"] - payload["iat"] == 30
        assert payload["jti"] == minted.jti and minted.req == "R"
        assert "approved_by" not in payload and "token_id" not in payload

    def test_n8n_workflow(self, signer):
        _, payload = _decode(signer.mint(audience=AUD, caller=WORKFLOW, req=None, lifetime=60).token, signer.jwks())
        assert payload["sub"] == "n8n-token:t1" and payload["principal_type"] == "n8n_workflow"
        assert payload["approved_by"] == "p-admin" and payload["token_id"] == "t1"
        assert "email" not in payload and "act" not in payload and "req" not in payload

    def test_service(self, signer):
        _, payload = _decode(signer.mint(audience=AUD, caller=SERVICE, req=None, lifetime=60).token, signer.jwks())
        assert payload["sub"] == "privacyfence" and payload["principal_type"] == "service"
        assert payload["audiences"] == [] and payload["tools"] == []
        assert not ({"email", "act", "req", "approved_by", "token_id"} & set(payload))

    @pytest.mark.parametrize("lifetime", [4, 61, 0, -1])
    def test_lifetime_out_of_range(self, signer, lifetime):
        with pytest.raises(ValueError):
            signer.mint(audience=AUD, caller=PERSON, req=None, lifetime=lifetime)

    def test_repr_hides_the_token(self, signer):
        minted = signer.mint(audience=AUD, caller=PERSON, req=None, lifetime=30)
        assert minted.token not in repr(minted)

    def test_no_usable_key(self, tmp_path, clock):
        keyset = KeySet(tmp_path / ucw.FILENAME, clock=clock)
        keyset.ensure()
        # A clock before the only key's signs_from leaves no usable signing key.
        broken = ic.ClaimSigner(issuer=ISSUER, keys=keyset, clock=lambda: clock.now - 10_000)
        with pytest.raises(ic.ClaimSigningError):
            broken.mint(audience=AUD, caller=PERSON, req=None, lifetime=30)

    def test_rotation(self, signer, keys, clock):
        before = signer.mint(audience=AUD, caller=PERSON, req=None, lifetime=30)
        keys.rotate()
        clock.now += ucw.JWKS_CACHE_SECONDS + 1
        after = signer.mint(audience=AUD, caller=PERSON, req=None, lifetime=30)
        jwks = signer.jwks()
        h1, _ = _decode(before.token, jwks)
        h2, _ = _decode(after.token, jwks)
        assert h1["kid"] != h2["kid"]


def _tools_call(name="search", arguments=None):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


def _send(auth: ic.IdentityClaimAuth, body, headers=None) -> httpx2.Request:
    """Run one request through the auth flow and return the request the transport saw."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    import asyncio

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=auth) as client:
            await client.post("https://hub.example.org/mcp", content=json.dumps(body), headers=headers or {})

    asyncio.run(run())
    return seen[0]


class TestIdentityClaimAuth:
    def _auth(self, signer, caller=PERSON):
        return ic.IdentityClaimAuth(signer, AUD, caller, 30)

    def test_tools_call_binds_req(self, signer):
        auth = self._auth(signer)
        sent = _send(auth, _tools_call("search", {"query": "x"}))
        _, payload = _decode(sent.headers[ic.CLAIM_HEADER], signer.jwks())
        assert payload["req"] == ic.request_hash("search", {"query": "x"})
        assert payload["audiences"] == ["all", "mgmt"] and payload["tools"] == ["get_doc", "search"]
        assert auth.last_call_req == payload["req"] and auth.last_call_jti == payload["jti"]
        assert auth.minted_count == 1

    def test_absent_arguments_hash_as_empty(self, signer):
        sent = _send(self._auth(signer), _tools_call("search"))
        _, payload = _decode(sent.headers[ic.CLAIM_HEADER], signer.jwks())
        assert payload["req"] == ic.request_hash("search", {})

    @pytest.mark.parametrize(
        "body",
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            [_tools_call()],
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 5}},
        ],
        ids=["initialize", "initialized", "batch", "bad-name"],
    )
    def test_not_a_call_strips_audiences(self, signer, body):
        auth = self._auth(signer)
        sent = _send(auth, body)
        _, payload = _decode(sent.headers[ic.CLAIM_HEADER], signer.jwks())
        assert payload["audiences"] == [] and payload["tools"] == [] and "req" not in payload
        assert payload["sub"] == "p-1"
        assert auth.last_call_req is None and auth.last_call_jti is None and auth.minted_count == 1

    def test_unparseable_body(self, signer):
        seen: list[httpx2.Request] = []

        async def run() -> None:
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(lambda r: (seen.append(r), httpx2.Response(200))[1]),
                auth=self._auth(signer),
            ) as client:
                await client.post("https://hub.example.org/mcp", content=b"\xff not json")

        import asyncio

        asyncio.run(run())
        _, payload = _decode(seen[0].headers[ic.CLAIM_HEADER], signer.jwks())
        assert payload["audiences"] == [] and "req" not in payload

    def test_authorization_untouched(self, signer):
        sent = _send(self._auth(signer), _tools_call(), headers={"Authorization": "Bearer upstream-own"})
        assert sent.headers["Authorization"] == "Bearer upstream-own"
        bare = _send(self._auth(signer), _tools_call())
        assert "Authorization" not in bare.headers

    def test_each_request_has_its_own_jti(self, signer):
        a = _send(self._auth(signer), _tools_call())
        b = _send(self._auth(signer), _tools_call())
        ja = _decode(a.headers[ic.CLAIM_HEADER], signer.jwks())[1]["jti"]
        jb = _decode(b.headers[ic.CLAIM_HEADER], signer.jwks())[1]["jti"]
        assert ja != jb

    def test_signing_failure_propagates(self, tmp_path, clock):
        keyset = KeySet(tmp_path / ucw.FILENAME, clock=clock)
        keyset.ensure()
        broken = ic.ClaimSigner(issuer=ISSUER, keys=keyset, clock=lambda: clock.now - 10_000)
        with pytest.raises(ic.ClaimSigningError):
            _send(ic.IdentityClaimAuth(broken, AUD, PERSON, 30), _tools_call())


def test_load_signer(tmp_path, monkeypatch):
    from privacyfence import paths

    monkeypatch.setattr(paths, "org_dir", lambda: tmp_path)
    signer = ic.load_signer(ISSUER + "/", overlap_seconds=3600)
    minted = signer.mint(audience=AUD, caller=SERVICE, req=None, lifetime=60)
    _, payload = _decode(minted.token, signer.jwks())
    assert payload["iss"] == ISSUER
    assert (tmp_path / ucw.FILENAME).exists()
