"""Unit tests for web/mcp_auth.py's principal_from_access_token -- see test_routes_mcp_principal.py for the wire-level proof that routes_mcp.py
actually calls this per request, and web/test_oauth_provider.py for the
org-mode ``OrgOAuthProvider`` tokens this function is actually built to
read (``subject``/``claims``), end to end.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from mcp.server.auth.provider import AccessToken, TokenVerifier

from privacyfence import paths, scoped_tokens
from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.scoped_tokens import REVOKED_SCOPE, ScopedTokenPolicy, ScopedTokenStore
from privacyfence.web import mcp_auth
from privacyfence.web.mcp_auth import (
    CompositeTokenVerifier,
    ScopedTokenVerifier,
    principal_from_access_token,
    scope_from_access_token,
)

_TOOL = "gmail_list_messages"


@pytest.fixture
def scoped_store(tmp_path, monkeypatch):
    """A scoped-token store over ``tmp_path``, installed as the module singleton."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    store = ScopedTokenStore(mcp_auth.scoped_token_principals)
    store.load()
    scoped_tokens.set_store(store)
    return store


def _create(store, principal=LOCAL_PRINCIPAL, *, days=90, now=None, **extra):
    return store.create(
        principal, label="sales-sync", days=days, rules=[], tools=[_TOOL],
        policy=ScopedTokenPolicy(enabled=True, max_days=365), now=now, **extra,
    )


class _RecordingVerifier(TokenVerifier):
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.seen.append(token)
        return AccessToken(token=token, client_id="inner", scopes=[])


class TestPrincipalFromAccessToken:
    def test_none_token_resolves_to_local_principal(self):
        assert principal_from_access_token(None) == LOCAL_PRINCIPAL

    def test_local_client_id_resolves_to_local_principal(self):
        token = AccessToken(token="t", client_id="local", scopes=[])
        assert principal_from_access_token(token) == LOCAL_PRINCIPAL

    def test_client_id_is_a_fallback_only_when_no_subject_is_present(self):
        # A hand-rolled/future verifier that doesn't populate subject --
        # PerUserTokenVerifier's own local-mode case is handled above
        # already; OrgOAuthProvider always sets subject (see the next
        # test), so this branch exists for robustness, not as the org-mode
        # path itself.
        token = AccessToken(token="t", client_id="some-oauth-client-id", scopes=[])
        assert principal_from_access_token(token) == Principal(id="some-oauth-client-id")

    def test_email_verified_is_true_only_for_the_boolean_true(self):
        def principal(value):
            return principal_from_access_token(AccessToken(
                token="t", client_id="c", scopes=[], subject="u", claims={"email_verified": value},
            ))
        assert principal(True).email_verified is True
        assert principal("true").email_verified is False
        assert principal(None).email_verified is False
        assert principal_from_access_token(
            AccessToken(token="t", client_id="c", scopes=[], subject="u", claims={}),
        ).email_verified is False

    def test_subject_is_preferred_over_client_id(self):
        # client_id identifies *which Claude installation* registered via
        # DCR, not *which human* is using it -- subject is the org IdP's
        # own resolved identity (OrgOAuthProvider._mint_tokens).
        token = AccessToken(token="t", client_id="claude-desktop-install-1", scopes=[], subject="alice")
        assert principal_from_access_token(token).id == "alice"

    def test_claims_populate_email_display_name_and_is_admin(self):
        token = AccessToken(
            token="t", client_id="c", scopes=[], subject="alice",
            claims={"email": "alice@example.com", "display_name": "Alice A.", "is_admin": True},
        )
        principal = principal_from_access_token(token)
        assert principal == Principal(id="alice", email="alice@example.com", display_name="Alice A.", is_admin=True)

    def test_missing_claims_default_to_empty_not_admin(self):
        token = AccessToken(token="t", client_id="c", scopes=[], subject="alice")
        principal = principal_from_access_token(token)
        assert principal == Principal(id="alice")

    def test_local_client_id_with_a_non_local_subject_resolves_to_that_principal(self):
        # ADR 0008: PerUserTokenVerifier sets client_id="local" for every
        # token it issues (local mode's own scheme identifier), but subject
        # is the presenting principal -- a second OS user's own token, not
        # the owner's.
        token = AccessToken(token="t", client_id="local", scopes=[], subject="os-1002")
        principal = principal_from_access_token(token)
        assert principal == Principal(id="os-1002")

    def test_local_client_id_with_the_local_subject_is_the_local_principal_object(self):
        token = AccessToken(token="t", client_id="local", scopes=[], subject="local")
        assert principal_from_access_token(token) == LOCAL_PRINCIPAL
        assert principal_from_access_token(token) is LOCAL_PRINCIPAL


    def test_a_local_scoped_token_resolves_to_the_local_principal(self):
        token = AccessToken(
            token="pfst_x", client_id="scoped:st-1", scopes=["scoped"], subject=LOCAL_PRINCIPAL.id,
            claims={"email": "", "display_name": "", "is_admin": False, "scoped_token_id": "st-1"},
        )
        assert principal_from_access_token(token) is LOCAL_PRINCIPAL

    def test_an_org_scoped_token_resolves_to_its_owner_with_claims(self):
        token = AccessToken(
            token="pfst_x", client_id="scoped:st-1", scopes=["scoped"], subject="alice",
            claims={"email": "alice@example.com", "display_name": "Alice A.", "is_admin": False,
                    "scoped_token_id": "st-1"},
        )
        assert principal_from_access_token(token) == Principal(
            id="alice", email="alice@example.com", display_name="Alice A.", is_admin=False,
        )


class TestScopedTokenVerifier:
    def test_a_stored_token_verifies_with_its_scoped_client_id_subject_and_expiry(self, scoped_store):
        alice = Principal(id="alice", email="alice@example.com", display_name="Alice A.")
        token, secret = _create(scoped_store, alice)

        access = asyncio.run(ScopedTokenVerifier().verify_token(secret))

        assert access is not None
        assert access.client_id == f"scoped:{token.id}"
        assert access.subject == "alice"
        assert access.expires_at == int(token.expires_at)
        assert access.scopes == ["scoped"]
        assert access.claims == {
            "email": "alice@example.com", "display_name": "Alice A.", "is_admin": False,
            "email_verified": False, "scoped_token_id": token.id,
        }

    def test_the_claims_carry_the_owners_verified_bit_the_token_was_made_with(self, scoped_store):
        alice = Principal(id="alice", email="alice@example.com", display_name="Alice A.")
        _token, secret = _create(scoped_store, alice, principal_email_verified=True)

        access = asyncio.run(ScopedTokenVerifier().verify_token(secret))

        assert access is not None
        assert access.claims["email_verified"] is True
        assert principal_from_access_token(access).email_verified is True

    def test_an_unknown_or_revoked_token_does_not_verify(self, scoped_store):
        token, secret = _create(scoped_store)
        verifier = ScopedTokenVerifier()
        assert asyncio.run(verifier.verify_token(scoped_tokens.mint_secret())) is None

        scoped_store.revoke(LOCAL_PRINCIPAL, token.id)

        assert asyncio.run(verifier.verify_token(secret)) is None

    def test_an_expired_token_does_not_verify(self, scoped_store):
        _, secret = _create(scoped_store, days=1, now=time.time() - 2 * 86400)
        assert asyncio.run(ScopedTokenVerifier().verify_token(secret)) is None


class TestCompositeTokenVerifier:
    def test_a_prefixed_token_never_reaches_the_inner_verifier(self, scoped_store):
        _, secret = _create(scoped_store)
        inner = _RecordingVerifier()
        composite = CompositeTokenVerifier(ScopedTokenVerifier(), inner)

        assert asyncio.run(composite.verify_token(secret)) is not None
        # An unknown prefixed token is refused, not handed to the inner verifier.
        assert asyncio.run(composite.verify_token(scoped_tokens.mint_secret())) is None
        assert inner.seen == []

    def test_an_unprefixed_token_never_reaches_the_scoped_store(self):
        scoped = _RecordingVerifier()
        inner = _RecordingVerifier()
        composite = CompositeTokenVerifier(scoped, inner)

        access = asyncio.run(composite.verify_token("a" * 64))

        assert access is not None and access.client_id == "inner"
        assert scoped.seen == []
        assert inner.seen == ["a" * 64]


class TestScopeFromAccessToken:
    def test_scope_from_access_token_returns_revoked_scope_when_the_record_is_gone(self, scoped_store):
        token, secret = _create(scoped_store)
        access = asyncio.run(ScopedTokenVerifier().verify_token(secret))
        assert scope_from_access_token(access) == token

        scoped_store.revoke(LOCAL_PRINCIPAL, token.id)

        assert scope_from_access_token(access) is REVOKED_SCOPE

    def test_a_scoped_client_id_without_a_token_id_is_the_revoked_scope(self, scoped_store):
        access = AccessToken(token="pfst_x", client_id="scoped:st-1", scopes=["scoped"], subject="local")
        assert scope_from_access_token(access) is REVOKED_SCOPE

    def test_an_unscoped_token_has_no_scope(self):
        assert scope_from_access_token(None) is None
        assert scope_from_access_token(AccessToken(token="t", client_id="local", scopes=[], subject="local")) is None
        assert scope_from_access_token(AccessToken(token="t", client_id="dcr-client", scopes=[], subject="alice")) is None


class TestScopedTokenPrincipals:
    def test_walks_every_user_folder_even_when_unseparated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        (tmp_path / "users" / "alice").mkdir(parents=True)
        (tmp_path / "users" / "not a principal!").mkdir()
        (tmp_path / "users" / "stray-file").write_text("", encoding="utf-8")

        assert [p.id for p in mcp_auth.scoped_token_principals()] == [LOCAL_PRINCIPAL.id, "alice"]

    def test_no_users_folder_yields_only_the_local_principal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        assert list(mcp_auth.scoped_token_principals()) == [LOCAL_PRINCIPAL]
