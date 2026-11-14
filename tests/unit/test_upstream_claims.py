"""Tests for the signed caller claim sent to upstream MCP servers."""

from __future__ import annotations

import jwt
import pytest
from jwt.algorithms import RSAAlgorithm
import json

import httpx2

from privacyfence import agent_identity, oidc_provider, paths, scoped_tokens, upstream_claims
from privacyfence.agent_identity import AgentIdentity, AgentSource
from privacyfence.upstream_claims import CallerClaim, ClaimAuth, ClaimSigner
from privacyfence.upstream_mcp_config import UpstreamServer

ISSUER = "https://pf.example/upstream-mcp"
AUD = "https://kh.example/mcp"


@pytest.fixture
def key(tmp_path):
    return oidc_provider.load_or_create_signing_key(tmp_path / "k.pem")


@pytest.fixture
def signer(key):
    return ClaimSigner(issuer=ISSUER, key=key)


@pytest.fixture
def upstream():
    return UpstreamServer(id="kh", label="KH", url="https://kh.example/mcp", audience=AUD, tools=())


@pytest.fixture
def caller():
    return CallerClaim(
        sub="u1",
        email="a@b.example",
        email_verified=True,
        domains=("z.example", "b.example"),
        act={"type": "ai_client", "attested": False, "name": "", "claimed_name": ""},
    )


def _decode(signer, token, **kw):
    jwk = signer.jwks()["keys"][0]
    pub = RSAAlgorithm.from_jwk(json.dumps(jwk))
    return jwt.decode(token, pub, algorithms=["RS256"], audience=kw.pop("audience", AUD), issuer=ISSUER, **kw)


def test_mint_claims_and_header(signer, upstream, caller, key):
    token = signer.mint(upstream=upstream, caller=caller, tool="search", now=1000.7)
    header = jwt.get_unverified_header(token)
    assert header["typ"] == upstream_claims.CLAIM_TYPE
    assert header["kid"] == key.kid
    assert header["alg"] == "RS256"
    claims = _decode(signer, token, options={"verify_exp": False, "verify_nbf": False, "verify_iat": False})
    assert claims["sub"] == "u1"
    assert claims["email"] == "a@b.example"
    assert claims["email_verified"] is True
    assert claims["domains"] == ["b.example", "z.example"]
    assert claims["act"] == caller.act
    assert claims["pf_upstream"] == "kh"
    assert claims["pf_tool"] == "search"
    assert claims["iat"] == claims["nbf"] == 1000
    assert claims["exp"] == claims["iat"] + 60


def test_mint_defaults_to_now_and_null_tool(signer, upstream, caller):
    claims = _decode(signer, signer.mint(upstream=upstream, caller=caller, tool=None))
    assert claims["pf_tool"] is None


def test_two_mints_differ_in_jti(signer, upstream, caller):
    a = _decode(signer, signer.mint(upstream=upstream, caller=caller, tool=None))
    b = _decode(signer, signer.mint(upstream=upstream, caller=caller, tool=None))
    assert a["jti"] != b["jti"]


def test_unverified_email_is_empty(signer, upstream):
    caller = CallerClaim(sub="u", email="", email_verified=False, domains=(), act={"type": "service"})
    claims = _decode(signer, signer.mint(upstream=upstream, caller=caller, tool=None))
    assert claims["email"] == ""
    assert claims["email_verified"] is False


def test_wrong_audience_is_rejected(signer, upstream, caller):
    token = signer.mint(upstream=upstream, caller=caller, tool=None)
    with pytest.raises(jwt.InvalidAudienceError):
        _decode(signer, token, audience="https://other.example")


def test_load_signer(tmp_path, monkeypatch, upstream, caller):
    monkeypatch.setattr(paths, "org_dir", lambda: tmp_path)
    s = upstream_claims.load_signer("https://pf.example/")
    assert (tmp_path / upstream_claims.KEY_FILENAME).exists()
    assert _decode(s, s.mint(upstream=upstream, caller=caller, tool=None))["iss"] == ISSUER


def _scoped():
    return scoped_tokens.ScopedToken(
        id="st-abc",
        label="nightly",
        digest="d",
        principal_id="u",
        principal_email="",
        principal_display_name="",
        created_at=0.0,
        expires_at=1.0,
        tools=frozenset(),
    )


def test_actor_scoped_token_wins():
    with (
        scoped_tokens.token_scope(_scoped()),
        agent_identity.agent_scope(AgentIdentity(id="x", name="Bot", version="", source=AgentSource.OVERRIDE)),
    ):
        assert upstream_claims.actor_from_context() == {"type": "workflow", "token_id": "st-abc", "label": "nightly"}


def test_actor_attested_agent():
    with agent_identity.agent_scope(AgentIdentity(id="x", name="Claude", version="", source=AgentSource.OAUTH_CLIENT)):
        assert upstream_claims.actor_from_context() == {
            "type": "ai_client",
            "attested": True,
            "name": "Claude",
            "claimed_name": "",
        }


def test_actor_claimed_agent():
    with agent_identity.agent_scope(AgentIdentity(id="x", name="Evil", version="", source=AgentSource.CLIENT_INFO)):
        assert upstream_claims.actor_from_context() == {
            "type": "ai_client",
            "attested": False,
            "name": "",
            "claimed_name": "Evil",
        }


def test_actor_no_agent():
    assert upstream_claims.actor_from_context() == {
        "type": "ai_client",
        "attested": False,
        "name": "",
        "claimed_name": "",
    }


def test_service_caller():
    c = upstream_claims.service_caller()
    assert (c.sub, c.email, c.email_verified, c.domains, c.act) == ("privacyfence", "", False, (), {"type": "service"})


def test_claim_auth_fresh_bearer_per_request(signer, upstream, caller):
    auth = ClaimAuth(signer, upstream, caller, "search")
    bearers = []
    for _ in range(2):
        req = httpx2.Request("POST", upstream.url)
        assert next(auth.auth_flow(req)) is req
        header = req.headers["Authorization"]
        assert header.startswith("Bearer ")
        bearers.append(header[len("Bearer ") :])
    assert bearers[0] != bearers[1]
    assert _decode(signer, bearers[0])["pf_tool"] == "search"
