"""The vendorable verifier is tested against the written contract, with tokens minted here."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "pf_claim_verify", _ROOT / "contrib" / "verifier" / "pf_claim_verify.py",
)
assert _SPEC and _SPEC.loader
pfv = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = pfv
_SPEC.loader.exec_module(pfv)

ISSUER = "https://pf.example.org"
AUD = "https://hub.example.org/mcp"
NOW = 1_800_000_000
_SEED_A = bytes(range(32))
_SEED_B = bytes(range(1, 33))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Key:
    def __init__(self, seed: bytes, kid: str) -> None:
        self.kid = kid
        self.private = Ed25519PrivateKey.from_private_bytes(seed)
        raw = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        self.jwk = {"kty": "OKP", "crv": "Ed25519", "x": _b64(raw), "use": "sig", "alg": "EdDSA", "kid": kid}


KEY_A = Key(_SEED_A, "key-a")
KEY_B = Key(_SEED_B, "key-b")


def _body(method: str, **params) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()


def _call_body(tool: str = "search", arguments=None) -> bytes:
    return _body("tools/call", name=tool, arguments={"query": "policy"} if arguments is None else arguments)


def _claims(jti: str = "jti-1", **over) -> dict:
    payload = {
        "claim_version": 1, "iss": ISSUER, "aud": AUD, "sub": "person-1", "principal_type": "claude_client",
        "email": "a@x.example", "act": {"sub": "client-1"}, "audiences": ["mgmt", "all"],
        "tools": ["search"], "iat": NOW, "exp": NOW + 60, "jti": jti,
        "req": pfv.request_hash("search", {"query": "policy"}),
    }
    payload.update(over)
    return {k: v for k, v in payload.items() if v is not None}


def _token(payload=None, key: Key = KEY_A, *, headers=None, algorithm: str = "EdDSA", secret=None) -> str:
    head = {"kid": key.kid, "typ": pfv.CLAIM_TYPE}
    head.update(headers or {})
    return jwt.encode(
        _claims() if payload is None else payload, key.private if secret is None else secret,
        algorithm=algorithm, headers=head,
    )


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def verifier(clock):
    return pfv.ClaimVerifier(issuer=ISSUER, audience=AUD, jwks={"keys": [KEY_A.jwk]}, clock=clock)


def _code(verifier, token, body=b"") -> str:
    with pytest.raises(pfv.ClaimError) as info:
        verifier.verify(token, body=body)
    return info.value.code


class TestValid:
    def test_call_claim_returns_principal(self, verifier):
        principal = verifier.verify(_token(), body=_call_body())
        assert principal == pfv.Principal(
            sub="person-1", principal_type="claude_client", email="a@x.example",
            audiences=("mgmt", "all"), tools=("search",), approved_by=None, token_id=None,
            act={"sub": "client-1"}, jti="jti-1", expires_at=NOW + 60, claim_version=1,
        )

    def test_verify_tool_call(self, verifier):
        principal = verifier.verify_tool_call(_token(), "search", {"query": "policy"})
        assert principal.audiences == ("mgmt", "all")

    def test_workflow_claim_fields(self, verifier):
        token = _token(_claims(
            sub="n8n-token:t1", principal_type="n8n_workflow", email=None, act=None,
            approved_by="person-9", token_id="t1",
        ))
        principal = verifier.verify(token, body=_call_body())
        assert (principal.approved_by, principal.token_id, principal.email, principal.act) == (
            "person-9", "t1", None, None,
        )

    def test_not_a_call_returns_no_audiences(self, verifier):
        token = _token(_claims(req=None))
        principal = verifier.verify(token, body=_body("initialize"))
        assert principal.audiences == () and principal.tools == ()

    def test_empty_body_is_not_a_call(self, verifier):
        assert verifier.verify(_token(_claims(req=None))).audiences == ()


class TestRejections:
    def test_wrong_signature(self, verifier):
        other = Key(_SEED_B, "key-a")
        assert _code(verifier, _token(key=other), _call_body()) == "bad_signature"

    def test_tampered_payload(self, verifier):
        head, payload, sig = _token().split(".")
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        data["audiences"] = ["finance"]
        forged = _b64(json.dumps(data).encode())
        assert _code(verifier, ".".join((head, forged, sig)), _call_body()) == "bad_signature"

    def test_expired(self, verifier, clock):
        clock.now = NOW + 60 + 6
        assert _code(verifier, _token(), _call_body()) == "expired"

    def test_within_leeway_is_accepted(self, verifier, clock):
        clock.now = NOW + 60 + 5
        assert verifier.verify(_token(), body=_call_body()).sub == "person-1"

    def test_not_yet_valid(self, verifier, clock):
        clock.now = NOW - 6
        assert _code(verifier, _token(), _call_body()) == "not_yet_valid"

    def test_wrong_audience(self, verifier):
        assert _code(verifier, _token(_claims(aud="https://other.example")), _call_body()) == "bad_audience"

    def test_wrong_issuer(self, verifier):
        assert _code(verifier, _token(_claims(iss="https://evil.example")), _call_body()) == "bad_issuer"

    def test_wrong_typ(self, verifier):
        assert _code(verifier, _token(headers={"typ": "JWT"}), _call_body()) == "bad_type"

    def test_alg_none(self, verifier):
        token = jwt.encode(_claims(), None, algorithm="none", headers={"kid": "key-a", "typ": pfv.CLAIM_TYPE})
        assert _code(verifier, token, _call_body()) == "bad_algorithm"

    def test_hs256(self, verifier):
        token = _token(algorithm="HS256", secret="s" * 32)
        assert _code(verifier, token, _call_body()) == "bad_algorithm"

    def test_rs256(self, verifier):
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(_claims(), rsa_key, algorithm="RS256", headers={"kid": "key-a", "typ": pfv.CLAIM_TYPE})
        assert _code(verifier, token, _call_body()) == "bad_algorithm"

    def test_unknown_kid(self, verifier):
        assert _code(verifier, _token(key=KEY_B), _call_body()) == "unknown_key"

    def test_missing_kid(self, verifier):
        token = jwt.encode(_claims(), KEY_A.private, algorithm="EdDSA", headers={"typ": pfv.CLAIM_TYPE})
        assert _code(verifier, token, _call_body()) == "unknown_key"

    def test_lifetime_over_60(self, verifier):
        assert _code(verifier, _token(_claims(exp=NOW + 61)), _call_body()) == "bad_lifetime"

    def test_version_2(self, verifier):
        assert _code(verifier, _token(_claims(claim_version=2)), _call_body()) == "bad_version"

    def test_unknown_principal_type(self, verifier):
        assert _code(verifier, _token(_claims(principal_type="robot")), _call_body()) == "bad_principal_type"

    def test_audiences_must_be_strings(self, verifier):
        assert _code(verifier, _token(_claims(audiences=[1])), _call_body()) == "malformed"

    def test_garbage_token(self, verifier):
        assert _code(verifier, "not-a-jwt", _call_body()) == "malformed"

    def test_replay(self, verifier):
        token = _token()
        verifier.verify(token, body=_call_body())
        assert _code(verifier, token, _call_body()) == "replayed"

    def test_replay_cache_none_allows_repeat(self, clock):
        verifier = pfv.ClaimVerifier(
            issuer=ISSUER, audience=AUD, jwks={"keys": [KEY_A.jwk]}, clock=clock, replay_cache=None,
        )
        token = _token()
        verifier.verify(token, body=_call_body())
        assert verifier.verify(token, body=_call_body()).jti == "jti-1"

    def test_custom_replay_store(self, clock):
        class Store:
            def add_if_new(self, jti, expires_at):
                return False

        verifier = pfv.ClaimVerifier(
            issuer=ISSUER, audience=AUD, jwks={"keys": [KEY_A.jwk]}, clock=clock, replay_cache=Store(),
        )
        assert _code(verifier, _token(), _call_body()) == "replayed"


class TestRequestBinding:
    def test_reused_with_different_arguments(self, verifier):
        assert _code(verifier, _token(), _call_body(arguments={"query": "salaries"})) == "request_mismatch"

    def test_reused_for_a_different_tool(self, verifier):
        assert _code(verifier, _token(), _call_body("get_doc", {"query": "policy"})) == "request_mismatch"

    def test_verify_tool_call_mismatch(self, verifier):
        with pytest.raises(pfv.ClaimError) as info:
            verifier.verify_tool_call(_token(), "search", {"query": "other"})
        assert info.value.code == "request_mismatch"

    def test_call_without_req(self, verifier):
        assert _code(verifier, _token(_claims(req=None)), _call_body()) == "request_required"

    def test_req_on_initialize(self, verifier):
        assert _code(verifier, _token(), _body("initialize")) == "request_unexpected"

    def test_batch_body(self, verifier):
        assert _code(verifier, _token(), b"[" + _call_body() + b"]") == "batch_not_allowed"

    def test_duplicate_keys_in_body(self, verifier):
        body = b'{"method":"tools/call","method":"initialize","params":{"name":"search"}}'
        assert _code(verifier, _token(), body) == "bad_body"

    def test_duplicate_argument_keys(self, verifier):
        body = b'{"method":"tools/call","params":{"name":"search","arguments":{"q":1,"q":2}}}'
        assert _code(verifier, _token(), body) == "bad_body"

    def test_invalid_json_body(self, verifier):
        assert _code(verifier, _token(), b"{nope") == "bad_body"

    def test_nan_in_body(self, verifier):
        body = b'{"method":"tools/call","params":{"name":"search","arguments":{"n":NaN}}}'
        assert _code(verifier, _token(), body) == "bad_body"

    def test_call_without_a_name(self, verifier):
        assert _code(verifier, _token(), _body("tools/call", arguments={})) == "bad_body"

    def test_absent_arguments_hash_as_empty_object(self, verifier):
        token = _token(_claims(req=pfv.request_hash("ping", {})))
        assert verifier.verify(token, body=_body("tools/call", name="ping")).sub == "person-1"


class TestKeys:
    def _verifier(self, clock, documents, fetches):
        def fetch(url):
            fetches.append(url)
            return json.dumps(documents[0]).encode()

        return pfv.ClaimVerifier(
            issuer=ISSUER, audience=AUD, jwks_url="https://pf.example.org/upstream-mcp/jwks",
            fetch=fetch, clock=clock, replay_cache=None,
        )

    def test_both_keys_verify_during_a_rotation(self, clock):
        verifier = self._verifier(clock, [{"keys": [KEY_A.jwk, KEY_B.jwk]}], [])
        assert verifier.verify(_token(key=KEY_A), body=_call_body()).sub == "person-1"
        assert verifier.verify(_token(key=KEY_B), body=_call_body()).sub == "person-1"

    def test_removed_key_is_unknown_after_cache_expiry(self, clock):
        documents = [{"keys": [KEY_A.jwk, KEY_B.jwk]}]
        fetches: list = []
        verifier = self._verifier(clock, documents, fetches)
        verifier.verify(_token(key=KEY_B), body=_call_body())
        documents[0] = {"keys": [KEY_A.jwk]}
        clock.now += 100  # inside the 300 s cache: the old key set still answers
        assert verifier.verify(_token(key=KEY_B, payload=_claims(iat=NOW + 100, exp=NOW + 160)), body=_call_body())
        clock.now += 300
        token = _token(_claims(iat=NOW + 400, exp=NOW + 460), key=KEY_B)
        assert _code(verifier, token, _call_body()) == "unknown_key"
        assert len(fetches) == 2

    def test_unknown_kid_refetches_at_most_every_30_seconds(self, clock):
        documents = [{"keys": [KEY_A.jwk]}]
        fetches: list = []
        verifier = self._verifier(clock, documents, fetches)
        assert _code(verifier, _token(key=KEY_B), _call_body()) == "unknown_key"
        assert len(fetches) == 1  # the first load
        assert _code(verifier, _token(key=KEY_B), _call_body()) == "unknown_key"
        assert len(fetches) == 1  # asked again at once: no refetch
        clock.now += 31
        documents[0] = {"keys": [KEY_A.jwk, KEY_B.jwk]}
        token = _token(_claims(iat=NOW + 31, exp=NOW + 91), key=KEY_B)
        assert verifier.verify(token, body=_call_body()).sub == "person-1"
        assert len(fetches) == 2  # one refetch found the new key

    def test_failed_fetch_keeps_the_last_key_set(self, clock):
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) > 1:
                raise OSError("down")
            return json.dumps({"keys": [KEY_A.jwk]})

        verifier = pfv.ClaimVerifier(
            issuer=ISSUER, audience=AUD, jwks_url="https://x.example/jwks", fetch=fetch, clock=clock,
            replay_cache=None,
        )
        verifier.verify(_token(), body=_call_body())
        clock.now += 400
        token = _token(_claims(iat=NOW + 400, exp=NOW + 460))
        assert verifier.verify(token, body=_call_body()).sub == "person-1"

    def test_only_okp_ed25519_keys_are_used(self, clock):
        rsa_like = {"kty": "RSA", "kid": "key-a", "n": "AQAB", "e": "AQAB"}
        verifier = pfv.ClaimVerifier(issuer=ISSUER, audience=AUD, jwks={"keys": [rsa_like]}, clock=clock)
        assert _code(verifier, _token(), _call_body()) == "unknown_key"

    def test_constructor_needs_exactly_one_key_source(self):
        with pytest.raises(ValueError):
            pfv.ClaimVerifier(issuer=ISSUER, audience=AUD)
        with pytest.raises(ValueError):
            pfv.ClaimVerifier(issuer=ISSUER, audience=AUD, jwks_url="https://x", jwks={"keys": []})

    def test_leeway_above_5_is_refused(self):
        with pytest.raises(ValueError):
            pfv.ClaimVerifier(issuer=ISSUER, audience=AUD, jwks={"keys": []}, leeway=6)


class TestVectors:
    _FILE = _ROOT / "contrib" / "verifier" / "req_test_vectors.json"

    def test_every_vector_hashes_as_listed(self):
        vectors = json.loads(self._FILE.read_text(encoding="utf-8"))["vectors"]
        assert len(vectors) == 6
        for vector in vectors:
            canonical = pfv.canonical_request(vector["tool"], vector["arguments"])
            assert canonical.decode("utf-8") == vector["canonical"], vector["name"]
            assert pfv.request_hash(vector["tool"], vector["arguments"]) == vector["req"], vector["name"]

    def test_nan_cannot_be_canonicalised(self):
        with pytest.raises(ValueError):
            pfv.canonical_request("search", {"n": float("nan")})


def test_module_is_valid_python_39_and_uses_only_allowed_imports():
    import ast

    source = (_ROOT / "contrib" / "verifier" / "pf_claim_verify.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    allowed = {
        "__future__", "base64", "hashlib", "hmac", "json", "threading", "time", "urllib", "dataclasses",
        "typing", "jwt", "cryptography",
    }
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= allowed, roots - allowed


def test_claim_from_privacyfence_signer_verifies(tmp_path):
    from privacyfence.identity_claim import ClaimSigner, IdentityCaller, request_hash
    from privacyfence.upstream_claim_keys import KeySet

    keys = KeySet(tmp_path / "keys.json")
    keys.ensure()
    signer = ClaimSigner(issuer=ISSUER, keys=keys)
    caller = IdentityCaller(
        sub="person-1", principal_type="claude_client", email="a@x.example",
        audiences=("mgmt", "all"), tools=("search",), act={"sub": "client-1", "agent": "claude-code"},
    )
    arguments = {"query": "Größe", "limit": 5}
    minted = signer.mint(audience=AUD, caller=caller, req=request_hash("search", arguments), lifetime=60)
    verifier = pfv.ClaimVerifier(issuer=ISSUER, audience=AUD, jwks=signer.jwks(), clock=time.time)
    body = _call_body("search", arguments)
    principal = verifier.verify(minted.token, body=body)
    assert principal.sub == "person-1"
    assert principal.audiences == ("all", "mgmt")
    assert principal.act == {"sub": "client-1", "agent": "claude-code"}
    assert principal.jti == minted.jti
    # A claim minted for a request that is not a call carries nothing.
    bare = signer.mint(
        audience=AUD, caller=IdentityCaller(sub="person-1", principal_type="claude_client"),
        req=None, lifetime=60,
    )
    assert verifier.verify(bare.token, body=_body("initialize")).audiences == ()
