"""Tests for the OIDC clients read from the environment, the signing key and the provider."""
from __future__ import annotations

import base64
import hashlib
import os
import stat

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from privacyfence import oidc_provider
from privacyfence.org_mode import ConfigurationError
from privacyfence.principal import Principal

SECRET = "x" * 40


def _client_env(monkeypatch, names="peek", uris="https://peek.example.com/cb", secret=SECRET, stem="PEEK"):
    monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENTS", names)
    if uris is not None:
        monkeypatch.setenv(f"PRIVACYFENCE_OIDC_CLIENT_{stem}_REDIRECT_URIS", uris)
    if secret is not None:
        monkeypatch.setenv(f"PRIVACYFENCE_OIDC_CLIENT_{stem}_SECRET", secret)


class TestClientsFromEnv:
    def test_unset_and_empty_give_no_clients(self, monkeypatch):
        assert oidc_provider.clients_from_env() == {}
        monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENTS", " ")
        assert oidc_provider.clients_from_env() == {}

    def test_two_clients_are_parsed(self, monkeypatch, tmp_path):
        secret_file = tmp_path / "grist"
        secret_file.write_text(SECRET + "\n")
        _client_env(monkeypatch, names=" Peek, my-grist ", uris="https://a/cb, http://localhost:3000/cb")
        monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENT_MY_GRIST_REDIRECT_URIS", "http://[::1]:8484/cb")
        monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENT_MY_GRIST_SECRET_FILE", str(secret_file))
        clients = oidc_provider.clients_from_env()
        assert set(clients) == {"peek", "my-grist"}
        assert clients["peek"].redirect_uris == ("https://a/cb", "http://localhost:3000/cb")
        assert clients["my-grist"].redirect_uris == ("http://[::1]:8484/cb",)
        assert clients["my-grist"].secret == SECRET

    def test_the_secret_is_not_in_repr(self, monkeypatch):
        _client_env(monkeypatch)
        assert SECRET not in repr(oidc_provider.clients_from_env()["peek"])

    @pytest.mark.parametrize("name", ["1peek", "Pe_ek", "a" * 33, "pe ek"])
    def test_invalid_name(self, monkeypatch, name):
        monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENTS", name)
        with pytest.raises(ConfigurationError) as err:
            oidc_provider.clients_from_env()
        assert str(err.value) == f"PRIVACYFENCE_OIDC_CLIENTS has an invalid name: {name.lower()}"

    def test_no_redirect_uris(self, monkeypatch):
        _client_env(monkeypatch, uris=" , ")
        with pytest.raises(ConfigurationError) as err:
            oidc_provider.clients_from_env()
        assert str(err.value) == "PRIVACYFENCE_OIDC_CLIENT_PEEK_REDIRECT_URIS must list at least one address"

    @pytest.mark.parametrize("uri", [
        "http://peek.example.com/cb", "https://user@peek.example.com/cb", "https://u:p@peek.example.com/cb",
        "https://peek.example.com/cb#frag", "https://peek.example.com/cb#", "ftp://peek.example.com/cb",
        "/relative", "https:///nohost",
    ])
    def test_bad_redirect_uri(self, monkeypatch, uri):
        _client_env(monkeypatch, uris=uri)
        with pytest.raises(ConfigurationError) as err:
            oidc_provider.clients_from_env()
        assert str(err.value) == (
            "PRIVACYFENCE_OIDC_CLIENT_PEEK_REDIRECT_URIS has an address that is not https:// "
            f"(http:// only for localhost) or has a user name or fragment: {uri}"
        )

    def test_secret_and_secret_file_together(self, monkeypatch, tmp_path):
        _client_env(monkeypatch)
        monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE", str(tmp_path / "f"))
        with pytest.raises(ConfigurationError) as err:
            oidc_provider.clients_from_env()
        assert str(err.value) == (
            "Set PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET or PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE, not both."
        )

    def test_unreadable_secret_file_never_quotes_the_reason(self, monkeypatch, tmp_path):
        _client_env(monkeypatch, secret=None)
        bad = tmp_path / "bad"
        bad.write_bytes(b"\xff\xfe\xfd")
        for path in (bad, tmp_path / "missing"):
            monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE", str(path))
            with pytest.raises(ConfigurationError) as err:
                oidc_provider.clients_from_env()
            assert str(err.value) == "PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE could not be read."

    @pytest.mark.parametrize("secret", [None, "short", "x" * 31])
    def test_short_or_missing_secret(self, monkeypatch, secret):
        _client_env(monkeypatch, secret=secret)
        with pytest.raises(ConfigurationError) as err:
            oidc_provider.clients_from_env()
        assert str(err.value) == "PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET must be at least 32 characters"

    def test_secret_file_is_stripped_before_the_length_check(self, monkeypatch, tmp_path):
        _client_env(monkeypatch, secret=None)
        path = tmp_path / "s"
        path.write_text("  " + "y" * 31 + "  \n")
        monkeypatch.setenv("PRIVACYFENCE_OIDC_CLIENT_PEEK_SECRET_FILE", str(path))
        with pytest.raises(ConfigurationError):
            oidc_provider.clients_from_env()


class TestSigningKey:
    def test_created_once_with_mode_0600_and_reused(self, tmp_path):
        path = tmp_path / "sub" / "key.pem"
        first = oidc_provider.load_or_create_signing_key(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert first.private_key.key_size == 2048
        content = path.read_bytes()
        second = oidc_provider.load_or_create_signing_key(path)
        assert path.read_bytes() == content
        assert second.kid == first.kid
        assert list(path.parent.iterdir()) == [path]

    def test_kid_is_16_chars_of_unpadded_base64url(self, tmp_path):
        key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
        assert len(key.kid) == 16
        assert set(key.kid) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    def test_repr_hides_the_private_key(self, tmp_path):
        key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
        assert "private_key" not in repr(key)

    @pytest.mark.parametrize("content", [b"", b"not pem", b"\xff\x00"])
    def test_unusable_file_raises_and_is_left_alone(self, tmp_path, content):
        path = tmp_path / "key.pem"
        path.write_bytes(content)
        with pytest.raises(ConfigurationError) as err:
            oidc_provider.load_or_create_signing_key(path)
        assert str(err.value) == (
            f"{path} is not a usable RSA signing key of at least 2048 bits; delete it to make a new one."
        )
        assert path.read_bytes() == content

    def test_directory_in_place_of_the_file_raises(self, tmp_path):
        with pytest.raises(ConfigurationError):
            oidc_provider.load_or_create_signing_key(tmp_path)

    def test_non_rsa_and_small_keys_raise(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric import ec

        for private in (ec.generate_private_key(ec.SECP256R1()), rsa.generate_private_key(65537, 1024)):
            path = tmp_path / f"{type(private).__name__}.pem"
            path.write_bytes(private.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
            ))
            with pytest.raises(ConfigurationError):
                oidc_provider.load_or_create_signing_key(path)

    def test_jwks_matches_the_key(self, tmp_path):
        key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
        (jwk,) = oidc_provider.jwks(key)["keys"]
        numbers = key.private_key.public_key().public_numbers()

        def decode(value):
            return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")

        assert decode(jwk["n"]) == numbers.n
        assert decode(jwk["e"]) == numbers.e == 65537
        assert "=" not in jwk["n"]
        assert {k: jwk[k] for k in ("kty", "use", "alg", "kid")} == {
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": key.kid,
        }


class TestProvider:
    def test_client_lookup_and_documents(self, tmp_path):
        key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
        client = oidc_provider.OidcClient("peek", ("https://p/cb",), SECRET)
        provider = oidc_provider.OidcProvider(issuer="https://pf.example.com/oidc", clients={"peek": client}, key=key)
        assert provider.issuer == "https://pf.example.com/oidc"
        assert provider.client("peek") is client
        assert provider.client("nope") is None
        assert provider.jwks() == oidc_provider.jwks(key)
        assert provider.discovery()["issuer"] == "https://pf.example.com/oidc"


class _Clock:
    def __init__(self) -> None:
        self.now = 5_000.0

    def __call__(self) -> float:
        return self.now


VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
ANN = Principal(id="u1", email="ann@example.com", display_name="Ann")


def _provider(tmp_path, clock):
    key = oidc_provider.load_or_create_signing_key(tmp_path / "key.pem")
    client = oidc_provider.OidcClient("peek", ("https://p/cb",), SECRET)
    return oidc_provider.OidcProvider(issuer="https://pf/oidc", clients={"peek": client}, key=key, clock=clock)


def _code(provider):
    return provider.issue_code(
        client_id="peek", redirect_uri="https://p/cb", principal=ANN, email_verified=True, nonce=None,
        code_challenge=CHALLENGE, scope="openid", auth_time=1,
    )


def _redeem(provider, code):
    return provider.redeem_code(code, client_id="peek", redirect_uri="https://p/cb", code_verifier=VERIFIER)


class TestProviderState:
    def test_authenticate_compares_bytes(self, tmp_path):
        provider = _provider(tmp_path, _Clock())
        assert provider.authenticate("peek", SECRET).client_id == "peek"
        assert provider.authenticate("peek", "x" * 39) is None
        assert provider.authenticate("peek", "ü" * 40) is None
        assert provider.authenticate("nobody", SECRET) is None

    def test_redeem_returns_the_grant_once(self, tmp_path):
        provider = _provider(tmp_path, _Clock())
        code = _code(provider)
        grant = _redeem(provider, code)
        assert grant.principal == ANN
        assert grant.expires_at == 5_000.0 + 60
        assert _redeem(provider, code) is None

    def test_code_expiry_with_an_injected_clock(self, tmp_path):
        clock = _Clock()
        provider = _provider(tmp_path, clock)
        live, expired = _code(provider), _code(provider)
        clock.now += 59
        assert _redeem(provider, live) is not None
        clock.now += 1
        assert _redeem(provider, expired) is None

    def test_token_expiry_with_an_injected_clock(self, tmp_path):
        clock = _Clock()
        provider = _provider(tmp_path, clock)
        token = provider.issue_tokens(_redeem(provider, _code(provider)))["access_token"]
        clock.now += 299
        assert provider.userinfo(token) == {"sub": "u1", "email": "ann@example.com", "email_verified": True, "name": "Ann"}
        clock.now += 1
        assert provider.userinfo(token) is None
        assert provider.userinfo(token) is None

    def test_expired_entries_are_pruned_on_insert(self, tmp_path):
        clock = _Clock()
        provider = _provider(tmp_path, clock)
        _code(provider)
        provider.issue_tokens(_redeem(provider, _code(provider)))
        assert (len(provider._codes), len(provider._tokens)) == (1, 1)
        clock.now += 301
        _code(provider)
        assert (len(provider._codes), len(provider._tokens)) == (1, 0)
        clock.now += 61
        provider.issue_tokens(_redeem(provider, _code(provider)))
        assert (len(provider._codes), len(provider._tokens)) == (0, 1)

    def test_code_cap_refuses_new_codes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oidc_provider, "MAX_LIVE_ENTRIES", 3)
        clock = _Clock()
        provider = _provider(tmp_path, clock)
        codes = [_code(provider) for _ in range(3)]
        assert None not in codes
        assert _code(provider) is None
        clock.now += 60
        assert _code(provider) is not None

    def test_access_token_cap_drops_the_oldest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oidc_provider, "MAX_LIVE_ENTRIES", 2)
        clock = _Clock()
        provider = _provider(tmp_path, clock)
        tokens = []
        for _ in range(3):
            tokens.append(provider.issue_tokens(_redeem(provider, _code(provider)))["access_token"])
            clock.now += 1
        assert provider.userinfo(tokens[0]) is None
        assert provider.userinfo(tokens[1]) is not None
        assert provider.userinfo(tokens[2]) is not None

    def test_token_response_and_id_token(self, tmp_path):
        provider = _provider(tmp_path, _Clock())
        grant = _redeem(provider, provider.issue_code(
            client_id="peek", redirect_uri="https://p/cb", principal=ANN, email_verified=False, nonce="n",
            code_challenge=CHALLENGE, scope="openid email", auth_time=4_000,
        ))
        body = provider.issue_tokens(grant)
        assert set(body) == {"access_token", "token_type", "expires_in", "scope", "id_token"}
        assert (body["token_type"], body["expires_in"], body["scope"]) == ("Bearer", 300, "openid email")
        claims = jwt.decode(body["id_token"], options={"verify_signature": False})
        assert claims["iat"] == 5_000 and claims["exp"] == 5_300 and claims["auth_time"] == 4_000
        assert claims["nonce"] == "n" and claims["email_verified"] is False
        assert jwt.get_unverified_header(body["id_token"])["kid"] == provider._key.kid
