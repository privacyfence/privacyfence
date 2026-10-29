"""Tests for org_identity.py: the OIDC relying-party helpers shared by
web/oauth_provider.py (Claude's own OAuth 2.1 dance) and web/routes_org_
identity.py (a browser's /login) -- see that module's own docstring for why
they share this code rather than each reimplementing claims-to-Principal
mapping.
"""
from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from privacyfence import org_identity as oi
from privacyfence.org_mode import AuthzPolicyConfig
from privacyfence.principal import Principal


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _idp(**overrides) -> oi.IdpConfig:
    defaults = dict(
        issuer="https://idp.example.com",
        client_id="privacyfence",
        client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        jwks_uri="https://idp.example.com/jwks",
    )
    defaults.update(overrides)
    return oi.IdpConfig(**defaults)


def _sign(claims: dict, private_key) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256")


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


class TestDiscoverIdp:
    @staticmethod
    def _valid_metadata(issuer="https://idp.example.com"):
        return {
            "issuer": issuer, "authorization_endpoint": "https://idp.example.com/authorize",
            "token_endpoint": "https://idp.example.com/token", "jwks_uri": "https://idp.example.com/jwks",
        }

    def test_fetches_the_well_known_document(self, monkeypatch):
        calls = []

        def fake_get(url, timeout):
            calls.append((url, timeout))
            return FakeResponse(self._valid_metadata())

        monkeypatch.setattr(oi.requests, "get", fake_get)
        result = oi.discover_idp("https://idp.example.com")

        assert calls == [("https://idp.example.com/.well-known/openid-configuration", oi._HTTP_TIMEOUT_SECONDS)]
        assert result["jwks_uri"] == "https://idp.example.com/jwks"

    def test_strips_a_trailing_slash_on_the_issuer(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            oi.requests, "get",
            lambda url, timeout: calls.append(url) or FakeResponse(self._valid_metadata()),
        )
        oi.discover_idp("https://idp.example.com/")
        assert calls == ["https://idp.example.com/.well-known/openid-configuration"]

    def test_raises_on_an_error_status(self, monkeypatch):
        monkeypatch.setattr(oi.requests, "get", lambda url, timeout: FakeResponse({}, status_code=500))
        with pytest.raises(requests.HTTPError):
            oi.discover_idp("https://idp.example.com")

    def test_rejects_a_plain_http_issuer(self, monkeypatch):
        monkeypatch.delenv(oi._DEV_ALLOW_INSECURE_IDP_ENV, raising=False)
        with pytest.raises(oi.ConfigurationError, match="issuer"):
            oi.discover_idp("http://idp.example.com")

    def test_dev_override_permits_a_plain_http_issuer(self, monkeypatch):
        monkeypatch.setenv(oi._DEV_ALLOW_INSECURE_IDP_ENV, "1")
        monkeypatch.setattr(
            oi.requests, "get",
            lambda url, timeout: FakeResponse(self._valid_metadata(issuer="http://idp.example.com")),
        )
        result = oi.discover_idp("http://idp.example.com")
        assert result["issuer"] == "http://idp.example.com"

    def test_rejects_a_document_that_is_not_an_object(self, monkeypatch):
        monkeypatch.setattr(oi.requests, "get", lambda url, timeout: FakeResponse(["not", "a", "dict"]))
        with pytest.raises(oi.ConfigurationError, match="JSON object"):
            oi.discover_idp("https://idp.example.com")

    @pytest.mark.parametrize("missing", ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"])
    def test_rejects_a_document_missing_a_required_field(self, monkeypatch, missing):
        metadata = self._valid_metadata()
        del metadata[missing]
        monkeypatch.setattr(oi.requests, "get", lambda url, timeout: FakeResponse(metadata))
        with pytest.raises(oi.ConfigurationError, match=missing):
            oi.discover_idp("https://idp.example.com")

    def test_rejects_a_document_whose_issuer_does_not_match(self, monkeypatch):
        monkeypatch.setattr(
            oi.requests, "get",
            lambda url, timeout: FakeResponse(self._valid_metadata(issuer="https://not-the-idp.example.com")),
        )
        with pytest.raises(oi.ConfigurationError, match="does not match"):
            oi.discover_idp("https://idp.example.com")

    def test_issuer_match_tolerates_a_trailing_slash_mismatch(self, monkeypatch):
        monkeypatch.setattr(
            oi.requests, "get",
            lambda url, timeout: FakeResponse(self._valid_metadata(issuer="https://idp.example.com/")),
        )
        result = oi.discover_idp("https://idp.example.com")
        assert result["issuer"] == "https://idp.example.com/"

    @pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
    def test_rejects_a_plain_http_endpoint_in_the_document(self, monkeypatch, field):
        monkeypatch.delenv(oi._DEV_ALLOW_INSECURE_IDP_ENV, raising=False)
        metadata = self._valid_metadata()
        metadata[field] = metadata[field].replace("https://", "http://")
        monkeypatch.setattr(oi.requests, "get", lambda url, timeout: FakeResponse(metadata))
        with pytest.raises(oi.ConfigurationError, match=field):
            oi.discover_idp("https://idp.example.com")

    def test_dev_override_permits_a_plain_http_endpoint_in_the_document(self, monkeypatch):
        monkeypatch.setenv(oi._DEV_ALLOW_INSECURE_IDP_ENV, "1")
        metadata = self._valid_metadata()
        metadata["jwks_uri"] = "http://idp.example.com/jwks"
        monkeypatch.setattr(oi.requests, "get", lambda url, timeout: FakeResponse(metadata))
        result = oi.discover_idp("https://idp.example.com")
        assert result["jwks_uri"] == "http://idp.example.com/jwks"

    @pytest.mark.parametrize("falsy", ["0", "false", "False", ""])
    def test_dev_override_env_var_falsy_values_do_not_disable_the_check(self, monkeypatch, falsy):
        monkeypatch.setenv(oi._DEV_ALLOW_INSECURE_IDP_ENV, falsy)
        with pytest.raises(oi.ConfigurationError):
            oi.discover_idp("http://idp.example.com")


class TestIdpConfigFromOrgConfig:
    def test_none_when_no_idp_section(self, monkeypatch):
        assert oi.IdpConfig.from_org_config({}) is None

    def test_none_when_issuer_missing(self, monkeypatch):
        assert oi.IdpConfig.from_org_config({"idp": {"client_id": "x"}}) is None

    def test_none_when_client_id_missing(self, monkeypatch):
        assert oi.IdpConfig.from_org_config({"idp": {"issuer": "https://idp.example.com"}}) is None

    def test_builds_config_via_discovery(self, monkeypatch):
        monkeypatch.setattr(
            oi, "discover_idp",
            lambda issuer: {"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"},
        )
        config = oi.IdpConfig.from_org_config({
            "idp": {
                "issuer": "https://idp.example.com", "client_id": "cid", "client_secret": "sec",
                "admin_group_claim": "groups", "admin_group_values": ["admins"],
            },
        })
        assert config == oi.IdpConfig(
            issuer="https://idp.example.com", client_id="cid", client_secret="sec",
            authorization_endpoint="a", token_endpoint="t", jwks_uri="j",
            admin_group_claim="groups", admin_group_values=("admins",),
        )

    def test_admin_group_defaults_are_fail_closed(self, monkeypatch):
        monkeypatch.setattr(
            oi, "discover_idp",
            lambda issuer: {"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"},
        )
        config = oi.IdpConfig.from_org_config({"idp": {"issuer": "https://idp.example.com", "client_id": "cid"}})
        assert config.admin_group_claim == ""
        assert config.admin_group_values == ()


class TestIdpConfigFromEnvironment:
    SECRET = "s3cret-sentinel-value"

    @staticmethod
    def _discovery(monkeypatch):
        monkeypatch.setattr(
            oi, "discover_idp",
            lambda issuer: {"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"},
        )

    def test_builds_config_from_the_environment_alone(self, monkeypatch, caplog):
        self._discovery(monkeypatch)
        monkeypatch.setenv("PRIVACYFENCE_IDP_ISSUER", "https://idp.example.com")
        monkeypatch.setenv("PRIVACYFENCE_IDP_CLIENT_ID", "cid")
        monkeypatch.setenv("PRIVACYFENCE_IDP_CLIENT_SECRET", self.SECRET)
        monkeypatch.setenv("PRIVACYFENCE_IDP_TRUST_UNVERIFIED_EMAIL", "true")
        monkeypatch.setenv("PRIVACYFENCE_IDP_ADMIN_GROUP_VALUES", "admins,ops")
        with caplog.at_level("DEBUG"):
            config = oi.IdpConfig.from_org_config({"mode": "org"})
        assert config == oi.IdpConfig(
            issuer="https://idp.example.com", client_id="cid", client_secret=self.SECRET,
            authorization_endpoint="a", token_endpoint="t", jwks_uri="j",
            admin_group_values=("admins", "ops"), trust_unverified_email=True,
        )
        assert self.SECRET not in caplog.text

    def test_trust_unverified_email_defaults_to_false_and_reads_a_bundle_section(self, monkeypatch):
        self._discovery(monkeypatch)
        base = {"issuer": "https://idp.example.com", "client_id": "cid"}
        assert oi.IdpConfig.from_org_config({"idp": base}).trust_unverified_email is False
        assert oi.IdpConfig.from_org_config({"idp": {**base, "trust_unverified_email": True}}).trust_unverified_email

    def test_a_bundle_idp_wins_over_the_environment(self, monkeypatch):
        self._discovery(monkeypatch)
        monkeypatch.setenv("PRIVACYFENCE_IDP_ISSUER", "https://env.example.com")
        monkeypatch.setenv("PRIVACYFENCE_IDP_CLIENT_ID", "env-client")
        config = oi.IdpConfig.from_org_config({"idp": {"issuer": "https://idp.example.com", "client_id": "cid"}})
        assert (config.issuer, config.client_id) == ("https://idp.example.com", "cid")

    def test_repr_does_not_contain_the_secret(self):
        config = _idp(client_secret=self.SECRET)
        assert self.SECRET not in repr(config)
        assert self.SECRET not in str(config)


class TestGeneratePkcePair:
    def test_challenge_is_derived_from_the_verifier(self):
        verifier, challenge = oi.generate_pkce_pair()
        assert verifier and challenge
        assert verifier != challenge

    def test_two_calls_produce_different_pairs(self):
        v1, c1 = oi.generate_pkce_pair()
        v2, c2 = oi.generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2


class TestBuildAuthorizationUrl:
    def test_carries_every_required_param(self):
        url = oi.build_authorization_url(
            _idp(), redirect_uri="https://pf.example.com/oauth/idp/callback",
            state="st4te", code_challenge="ch4ll", nonce="n0nce",
        )
        assert url.startswith("https://idp.example.com/authorize?")
        for fragment in (
            "response_type=code", "client_id=privacyfence",
            "state=st4te", "code_challenge=ch4ll", "code_challenge_method=S256", "nonce=n0nce",
        ):
            assert fragment in url

    def test_appends_to_an_endpoint_that_already_has_a_query_string(self):
        url = oi.build_authorization_url(
            _idp(authorization_endpoint="https://idp.example.com/authorize?tenant=acme"),
            redirect_uri="https://pf.example.com/cb", state="s", code_challenge="c", nonce="n",
        )
        assert url.startswith("https://idp.example.com/authorize?tenant=acme&")


class TestExchangeCodeForTokens:
    def test_posts_the_expected_form_body(self, monkeypatch):
        captured = {}

        def fake_post(url, data, timeout):
            captured["url"] = url
            captured["data"] = data
            return FakeResponse({"id_token": "abc"})

        monkeypatch.setattr(oi.requests, "post", fake_post)
        result = oi.exchange_code_for_tokens(
            _idp(), code="c0de", redirect_uri="https://pf.example.com/cb", code_verifier="v3rifier",
        )

        assert result == {"id_token": "abc"}
        assert captured["url"] == "https://idp.example.com/token"
        assert captured["data"]["grant_type"] == "authorization_code"
        assert captured["data"]["code"] == "c0de"
        assert captured["data"]["code_verifier"] == "v3rifier"
        assert captured["data"]["client_secret"] == "s3cr3t"

    def test_omits_client_secret_when_the_idp_config_has_none(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(oi.requests, "post", lambda url, data, timeout: captured.update(data) or FakeResponse({}))
        oi.exchange_code_for_tokens(
            _idp(client_secret=""), code="c", redirect_uri="https://pf.example.com/cb", code_verifier="v",
        )
        assert "client_secret" not in captured

    def test_raises_on_an_error_status(self, monkeypatch):
        monkeypatch.setattr(oi.requests, "post", lambda url, data, timeout: FakeResponse({}, status_code=400))
        with pytest.raises(requests.HTTPError):
            oi.exchange_code_for_tokens(_idp(), code="c", redirect_uri="https://pf.example.com/cb", code_verifier="v")


class TestVerifyIdToken:
    def _patch_jwks(self, monkeypatch, public_key):
        monkeypatch.setattr(
            oi.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: SimpleNamespace(key=public_key),
        )

    def test_valid_token_returns_claims(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {
                "sub": "alice", "email": "alice@example.com", "aud": idp.client_id, "iss": idp.issuer,
                "iat": 1, "exp": 9999999999, "nonce": "expected-nonce",
            },
            private_key,
        )

        claims = oi.verify_id_token(idp, token, nonce="expected-nonce")

        assert claims["sub"] == "alice"
        assert claims["email"] == "alice@example.com"

    def test_wrong_audience_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": "someone-else", "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "n"},
            private_key,
        )
        with pytest.raises(jwt.InvalidAudienceError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_wrong_issuer_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": "https://not-the-idp.example.com",
             "iat": 1, "exp": 9999999999, "nonce": "n"},
            private_key,
        )
        with pytest.raises(jwt.InvalidIssuerError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_expired_token_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 1, "nonce": "n"},
            private_key,
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_mismatched_nonce_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "actual"},
            private_key,
        )
        with pytest.raises(jwt.InvalidTokenError):
            oi.verify_id_token(idp, token, nonce="expected")

    def test_signature_forged_with_a_different_key_is_rejected(self, monkeypatch, rsa_keypair):
        _private_key, public_key = rsa_keypair
        other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "n"},
            other_private_key,
        )
        with pytest.raises(jwt.InvalidSignatureError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_missing_sub_claim_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "n"}, private_key,
        )
        with pytest.raises(jwt.MissingRequiredClaimError):
            oi.verify_id_token(idp, token, nonce="n")


class TestPrincipalFromClaims:
    def test_maps_sub_email_and_name(self):
        p = oi.principal_from_claims({"sub": "alice-id", "email": "alice@example.com", "name": "Alice A."}, _idp())
        assert p == Principal(id="alice-id", email="alice@example.com", display_name="Alice A.")

    def test_falls_back_to_preferred_username_then_email_then_sub_for_display_name(self):
        idp = _idp()
        assert oi.principal_from_claims({"sub": "s", "preferred_username": "aa"}, idp).display_name == "aa"
        assert oi.principal_from_claims({"sub": "s", "email": "a@x.com"}, idp).display_name == "a@x.com"
        assert oi.principal_from_claims({"sub": "s"}, idp).display_name == "s"

    def test_raises_without_a_sub_claim(self):
        with pytest.raises(ValueError):
            oi.principal_from_claims({"email": "a@x.com"}, _idp())

    def test_unsafe_sub_is_hashed_into_a_safe_principal_id(self):
        p = oi.principal_from_claims({"sub": "cn=alice,dc=example,dc=com"}, _idp())
        assert p.id.startswith("idp-")
        assert p.id != "cn=alice,dc=example,dc=com"

    def test_not_admin_when_no_admin_group_claim_configured(self):
        p = oi.principal_from_claims({"sub": "s", "groups": ["admins"]}, _idp())
        assert p.is_admin is False

    def test_admin_when_a_configured_group_is_present(self):
        idp = _idp(admin_group_claim="groups", admin_group_values=("admins", "it"))
        p = oi.principal_from_claims({"sub": "s", "groups": ["engineers", "admins"]}, idp)
        assert p.is_admin is True

    def test_not_admin_when_configured_group_is_absent(self):
        idp = _idp(admin_group_claim="groups", admin_group_values=("admins",))
        p = oi.principal_from_claims({"sub": "s", "groups": ["engineers"]}, idp)
        assert p.is_admin is False

    def test_admin_group_claim_as_a_bare_string_not_a_list(self):
        idp = _idp(admin_group_claim="role", admin_group_values=("admin",))
        p = oi.principal_from_claims({"sub": "s", "role": "admin"}, idp)
        assert p.is_admin is True


VERIFIED = {"email_verified": True}


class TestPrincipalAdminByEmail:
    @staticmethod
    def _claims(**overrides):
        claims = {"sub": "s", "email": "alice@acme.com", "email_verified": True}
        claims.update(overrides)
        return claims

    def _admin(self, claims, **idp_overrides) -> bool:
        idp = _idp(admin_emails=idp_overrides.pop("admin_emails", ("alice@acme.com",)), **idp_overrides)
        return oi.principal_from_claims(claims, idp).is_admin

    def test_a_verified_listed_email_is_admin(self):
        assert self._admin(self._claims()) is True

    def test_matching_is_case_insensitive_and_strips_whitespace(self):
        assert self._admin(self._claims(email="  Alice@ACME.com ")) is True

    def test_a_configured_email_with_whitespace_or_case_still_matches(self):
        assert self._admin(self._claims(), admin_emails=("  ALICE@Acme.COM  ",)) is True

    @pytest.mark.parametrize("verified", [False, "true", "True", 1, None])
    def test_an_unverified_email_is_not_admin(self, verified):
        assert self._admin(self._claims(email_verified=verified)) is False

    def test_a_missing_email_verified_claim_is_not_admin(self):
        claims = self._claims()
        del claims["email_verified"]
        assert self._admin(claims) is False

    def test_an_unlisted_email_is_not_admin(self):
        assert self._admin(self._claims(email="bob@acme.com")) is False

    @pytest.mark.parametrize("email", [["alice@acme.com"], ("alice@acme.com",), {"a": 1}, 5, None])
    def test_a_non_string_email_claim_is_not_admin(self, email):
        assert self._admin(self._claims(email=email)) is False
        assert oi.principal_from_claims(self._claims(email=email), _idp()).email == ""

    @pytest.mark.parametrize("email", ["alice@acme.com\u212a", "al\u017fce@acme.com", "alice@acme.com\u00e9"])
    def test_a_non_ascii_address_never_matches(self, email):
        assert self._admin(self._claims(email=email), admin_emails=(email,)) is False
        # A look-alike of a listed address (Kelvin sign lower-cases to "k", long s to "s" under casefold).
        assert self._admin(self._claims(email="al\u017fce@acme.com")) is False
        assert self._admin(self._claims(email="BOB@acme.com"), admin_emails=("bo\u212a@acme.com",)) is False
        assert self._admin(self._claims(email="bok@acme.com"), admin_emails=("bo\u212a@acme.com",)) is False

    def test_an_empty_or_malformed_email_never_matches(self):
        for email in ("", " ", "@", "alice@", "@acme.com", "a@b@acme.com"):
            assert self._admin(self._claims(email=email), admin_emails=(email,)) is False

    def test_no_admin_list_means_nobody_is_admin_by_email(self):
        assert self._admin(self._claims(), admin_emails=()) is False

    def test_the_trust_unverified_flag_never_makes_an_email_admin(self):
        claims = self._claims(email_verified=False)
        assert self._admin(claims, trust_unverified_email=True) is False
        del claims["email_verified"]
        assert self._admin(claims, trust_unverified_email=True) is False

    def test_the_group_rule_still_works_alone_and_with_the_email_list(self):
        groups = dict(admin_group_claim="groups", admin_group_values=("admins",))
        by_group = self._claims(email="bob@acme.com", email_verified=False, groups=["admins"])
        assert self._admin(by_group, **groups) is True
        by_email = self._claims(groups=["engineers"])
        assert self._admin(by_email, **groups) is True
        neither = self._claims(email="bob@acme.com", groups=["engineers"])
        assert self._admin(neither, **groups) is False

    GOOGLE = "https://accounts.google.com"

    def test_google_needs_hd_to_equal_the_email_domain(self):
        assert self._admin(self._claims(hd="acme.com"), issuer=self.GOOGLE) is True
        assert self._admin(self._claims(hd="ACME.com"), issuer=self.GOOGLE) is True
        assert self._admin(self._claims(), issuer=self.GOOGLE) is False
        assert self._admin(self._claims(hd="other.com"), issuer=self.GOOGLE) is False
        assert self._admin(self._claims(hd=["acme.com"]), issuer=self.GOOGLE) is False
        assert self._admin(self._claims(hd=""), issuer=self.GOOGLE) is False
        assert self._admin(self._claims(), issuer=self.GOOGLE + "/") is False

    def test_other_issuers_do_not_need_hd(self):
        assert self._admin(self._claims()) is True


class TestReservedSubs:
    @pytest.mark.parametrize("sub", ["local", "single-user", "os-alice", "os-", "LOCAL", "Single-User", "OS-x"])
    def test_a_reserved_form_is_hashed(self, sub):
        import hashlib

        p = oi.principal_from_claims({"sub": sub}, _idp())
        assert p.id == "idp-" + hashlib.sha256(sub.encode()).hexdigest()[:32]

    @pytest.mark.parametrize("sub", ["locally", "single-users", "pos-1", "alice"])
    def test_a_near_miss_is_left_alone(self, sub):
        assert oi.principal_from_claims({"sub": sub}, _idp()).id == sub


class TestCheckAuthzPolicy:
    """Allowed-domain and required-group authorization policy enforcement."""

    def _principal(self, **overrides) -> Principal:
        defaults = dict(id="alice-id", email="alice@acme.com", display_name="Alice")
        defaults.update(overrides)
        return Principal(**defaults)

    def test_disabled_policy_admits_everyone(self):
        oi.check_authz_policy(self._principal(email=""), {}, AuthzPolicyConfig())  # must not raise

    def test_allowed_domain_admits(self):
        policy = AuthzPolicyConfig(allowed_domains=("acme.com",))
        oi.check_authz_policy(self._principal(email="alice@acme.com"), VERIFIED, policy)  # must not raise

    def test_allowed_domain_is_case_insensitive(self):
        policy = AuthzPolicyConfig(allowed_domains=("acme.com",))
        oi.check_authz_policy(self._principal(email="alice@ACME.COM"), VERIFIED, policy)  # must not raise

    def test_disallowed_domain_is_denied(self):
        policy = AuthzPolicyConfig(allowed_domains=("acme.com",))
        with pytest.raises(oi.AuthorizationDenied):
            oi.check_authz_policy(self._principal(email="mallory@evil.example.com"), {}, policy)

    def test_no_email_is_denied_when_domains_are_restricted(self):
        policy = AuthzPolicyConfig(allowed_domains=("acme.com",))
        with pytest.raises(oi.AuthorizationDenied):
            oi.check_authz_policy(self._principal(email=""), {}, policy)

    def test_required_group_present_admits(self):
        policy = AuthzPolicyConfig(groups_claim="groups", required_groups=("privacyfence-users",))
        claims = {"groups": ["engineers", "privacyfence-users"]}
        oi.check_authz_policy(self._principal(), claims, policy)  # must not raise

    def test_required_group_absent_is_denied(self):
        policy = AuthzPolicyConfig(groups_claim="groups", required_groups=("privacyfence-users",))
        claims = {"groups": ["engineers"]}
        with pytest.raises(oi.AuthorizationDenied):
            oi.check_authz_policy(self._principal(), claims, policy)

    def test_required_group_claim_missing_from_token_is_denied(self):
        policy = AuthzPolicyConfig(groups_claim="groups", required_groups=("privacyfence-users",))
        with pytest.raises(oi.AuthorizationDenied):
            oi.check_authz_policy(self._principal(), {}, policy)

    def test_groups_claim_as_a_bare_string_not_a_list(self):
        policy = AuthzPolicyConfig(groups_claim="role", required_groups=("member",))
        oi.check_authz_policy(self._principal(), {"role": "member"}, policy)  # must not raise

    def test_both_checks_must_pass(self):
        policy = AuthzPolicyConfig(
            allowed_domains=("acme.com",), groups_claim="groups", required_groups=("privacyfence-users",),
        )
        # Right domain, wrong group -> still denied.
        with pytest.raises(oi.AuthorizationDenied):
            oi.check_authz_policy(
                self._principal(email="alice@acme.com"), {**VERIFIED, "groups": ["engineers"]}, policy,
            )
        # Both satisfied -> admitted.
        oi.check_authz_policy(
            self._principal(email="alice@acme.com"), {**VERIFIED, "groups": ["privacyfence-users"]}, policy,
        )


class TestAllowedDomainsNeedAVerifiedAddress:
    POLICY = AuthzPolicyConfig(allowed_domains=("acme.com",))

    @staticmethod
    def _check(policy, claims, idp_issuer="https://login.example.com", trust=False, email="alice@acme.com"):
        idp = _idp(issuer=idp_issuer, trust_unverified_email=trust)
        bound = oi.IdpAuthzPolicy.bind(policy, idp)
        oi.check_authz_policy(Principal(id="p", email=email, display_name="P"), claims, bound)

    @pytest.mark.parametrize("verified", [False, "true", None, 1])
    def test_an_unverified_address_is_denied(self, verified):
        with pytest.raises(oi.AuthorizationDenied):
            self._check(self.POLICY, {"email_verified": verified})

    def test_a_missing_email_verified_claim_is_denied_by_default(self):
        with pytest.raises(oi.AuthorizationDenied):
            self._check(self.POLICY, {})

    def test_a_plain_policy_without_an_idp_is_strict(self):
        with pytest.raises(oi.AuthorizationDenied):
            oi.check_authz_policy(Principal(id="p", email="a@acme.com", display_name="P"), {}, self.POLICY)

    def test_entra_style_claims_are_allowed_with_the_trust_flag(self):
        self._check(self.POLICY, {}, trust=True)
        self._check(self.POLICY, {"email_verified": False}, trust=True)

    def test_the_trust_flag_does_not_admit_the_wrong_domain(self):
        with pytest.raises(oi.AuthorizationDenied):
            self._check(self.POLICY, {}, trust=True, email="mallory@evil.example")

    @pytest.mark.parametrize("email", ["alice@acme.com\u212a", "alice@ac\u212ae.com", "alice@acme.com\u00e9"])
    def test_a_non_ascii_address_is_denied(self, email):
        policy = AuthzPolicyConfig(allowed_domains=("acme.com", "ackme.com"))
        with pytest.raises(oi.AuthorizationDenied):
            self._check(policy, VERIFIED, email=email)
        with pytest.raises(oi.AuthorizationDenied):
            self._check(policy, VERIFIED, email="alice@ac\u212ame.com", trust=True)

    def test_a_malformed_address_is_denied(self):
        for email in ("", "acme.com", "@acme.com", "a@b@acme.com"):
            with pytest.raises(oi.AuthorizationDenied):
                self._check(self.POLICY, VERIFIED, email=email)

    def test_google_needs_hd_even_for_a_verified_address(self):
        google = "https://accounts.google.com"
        self._check(self.POLICY, {"email_verified": True, "hd": "acme.com"}, idp_issuer=google)
        for claims in ({"email_verified": True}, {"email_verified": True, "hd": "other.com"}):
            with pytest.raises(oi.AuthorizationDenied):
                self._check(self.POLICY, claims, idp_issuer=google)

    def test_google_hd_still_applies_with_the_trust_flag(self):
        with pytest.raises(oi.AuthorizationDenied):
            self._check(self.POLICY, {}, idp_issuer="https://accounts.google.com", trust=True)

    def test_a_disabled_policy_still_admits_everyone(self):
        self._check(AuthzPolicyConfig(), {}, email="")

    def test_bind_carries_the_policy_fields(self):
        policy = AuthzPolicyConfig(allowed_domains=("a.com",), groups_claim="g", required_groups=("x",))
        bound = oi.IdpAuthzPolicy.bind(policy, _idp(trust_unverified_email=True))
        assert (bound.allowed_domains, bound.groups_claim, bound.required_groups) == (("a.com",), "g", ("x",))
        assert bound.trust_unverified_email is True and bound.issuer == "https://idp.example.com"
        assert bound.enabled


class TestAdminEmailsFromConfig:
    def test_the_section_list_is_normalized_and_non_ascii_or_non_string_entries_are_dropped(self, monkeypatch):
        monkeypatch.setattr(
            oi, "discover_idp",
            lambda issuer: {"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"},
        )
        section = {
            "issuer": "https://idp.example.com", "client_id": "cid",
            "admin_emails": ["  Alice@Acme.COM ", "bo\u212a@acme.com", 5, None, ["x@y.com"], ""],
        }
        assert oi.IdpConfig.from_org_config({"idp": section}).admin_emails == ("alice@acme.com",)

    def test_the_environment_list_reaches_the_config(self, monkeypatch):
        monkeypatch.setattr(
            oi, "discover_idp",
            lambda issuer: {"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"},
        )
        monkeypatch.setenv("PRIVACYFENCE_IDP_ISSUER", "https://idp.example.com")
        monkeypatch.setenv("PRIVACYFENCE_IDP_CLIENT_ID", "cid")
        monkeypatch.setenv("PRIVACYFENCE_ADMIN_EMAILS", "Alice@acme.com, bob@acme.com")
        config = oi.IdpConfig.from_org_config({"mode": "org"})
        assert config.admin_emails == ("alice@acme.com", "bob@acme.com")
