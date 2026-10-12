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


def _create(store, principal=LOCAL_PRINCIPAL, *, days=90, now=None):
    return store.create(
        principal, label="sales-sync", days=days, rule_ids=[], tools=[_TOOL],
        policy=ScopedTokenPolicy(enabled=True, max_days=365), now=now,
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
            "scoped_token_id": token.id,
        }

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
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: False)
        (tmp_path / "users" / "alice").mkdir(parents=True)
        (tmp_path / "users" / "not a principal!").mkdir()
        (tmp_path / "users" / "stray-file").write_text("", encoding="utf-8")

        assert [p.id for p in mcp_auth.scoped_token_principals()] == [LOCAL_PRINCIPAL.id, "alice"]

    def test_no_users_folder_yields_only_the_local_principal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        assert list(mcp_auth.scoped_token_principals()) == [LOCAL_PRINCIPAL]


class TestPerUserTokenVerifier:
    """ADR 0008: a ``{sha256(token): principal_id}`` map replacing the one
    shared secret every caller used to resolve to ``LOCAL_PRINCIPAL``."""

    async def test_unregistered_token_does_not_verify(self):
        verifier = mcp_auth.PerUserTokenVerifier()
        assert await verifier.verify_token("nope") is None

    async def test_empty_token_does_not_verify(self):
        verifier = mcp_auth.PerUserTokenVerifier()
        verifier.register("realtoken", "local")
        assert await verifier.verify_token("") is None

    async def test_registered_token_resolves_to_its_principal(self):
        verifier = mcp_auth.PerUserTokenVerifier()
        verifier.register("alice-token", "local")
        verifier.register("bob-token", "os-1002")

        alice = await verifier.verify_token("alice-token")
        bob = await verifier.verify_token("bob-token")

        assert alice is not None and alice.client_id == "local" and alice.subject == "local"
        assert bob is not None and bob.client_id == "local" and bob.subject == "os-1002"

    async def test_one_principals_token_never_verifies_as_another(self):
        verifier = mcp_auth.PerUserTokenVerifier()
        verifier.register("alice-token", "local")
        verifier.register("bob-token", "os-1002")

        assert await verifier.verify_token("bob-token") is not None
        result = await verifier.verify_token("alice-token")
        assert result is not None and result.subject != "os-1002"

    async def test_unregister_revokes_immediately(self):
        verifier = mcp_auth.PerUserTokenVerifier()
        verifier.register("sometoken", "os-1002")
        verifier.unregister("sometoken")

        assert await verifier.verify_token("sometoken") is None

    async def test_single_token_verifier_registers_exactly_one_token(self):
        verifier = mcp_auth.single_token_verifier("onlytoken")

        result = await verifier.verify_token("onlytoken")

        assert result is not None and result.subject == LOCAL_PRINCIPAL.id
        assert await verifier.verify_token("anythingelse") is None


class TestPerPrincipalTokenStorage:
    """ADR 0008: each principal's own persisted token, and where it lives
    depending on whether this install is separated."""

    def test_unseparated_uses_the_handoff_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: tmp_path)

        token = mcp_auth.load_or_create_mcp_token()

        assert (tmp_path / mcp_auth.MCP_TOKEN_FILE_NAME).read_text(encoding="utf-8") == token

    def test_separated_uses_authority_dir_per_principal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(
            mcp_auth.paths, "authority_dir",
            lambda principal=None: (tmp_path / (principal.id if principal else "local")),
        )
        for principal_id in ("local", "os-1002"):
            (tmp_path / principal_id).mkdir(parents=True, exist_ok=True)

        local_token = mcp_auth.load_or_create_mcp_token(LOCAL_PRINCIPAL)
        other_token = mcp_auth.load_or_create_mcp_token(Principal(id="os-1002"))

        assert local_token != other_token
        assert (tmp_path / "local" / mcp_auth.MCP_TOKEN_FILE_NAME).read_text(encoding="utf-8") == local_token
        assert (tmp_path / "os-1002" / mcp_auth.MCP_TOKEN_FILE_NAME).read_text(encoding="utf-8") == other_token

    def test_load_or_create_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: tmp_path)

        first = mcp_auth.load_or_create_mcp_token()
        second = mcp_auth.load_or_create_mcp_token()

        assert first == second

    async def test_rotate_replaces_the_token_and_unregisters_the_old_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: tmp_path)
        verifier = mcp_auth.PerUserTokenVerifier()

        original = mcp_auth.load_or_create_mcp_token()
        verifier.register(original, LOCAL_PRINCIPAL.id)

        rotated = mcp_auth.rotate_mcp_token(verifier=verifier)

        assert rotated != original
        assert await verifier.verify_token(original) is None

    def test_separated_never_reads_a_handoff_mcp_token(self, tmp_path, monkeypatch):
        # ADR 0041: a separated install's tokens live only under
        # authority_dir(). A file at the unseparated handoff path is neither
        # read nor touched -- it is not this daemon's state.
        handoff = tmp_path / "handoff"
        authority = tmp_path / "authority"
        handoff.mkdir()
        authority.mkdir()
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: handoff)
        monkeypatch.setattr(mcp_auth.paths, "authority_dir", lambda principal=None: authority)
        stray = handoff / mcp_auth.MCP_TOKEN_FILE_NAME
        stray.write_text("shared-secret", encoding="utf-8")

        token = mcp_auth.load_or_create_mcp_token()

        assert token != "shared-secret"
        assert stray.read_text(encoding="utf-8") == "shared-secret"


class TestPreloadVerifier:
    def test_preloads_the_local_principals_token(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: tmp_path)
        token = mcp_auth.load_or_create_mcp_token()

        verifier = mcp_auth.PerUserTokenVerifier()
        mcp_auth.preload_verifier(verifier)

        import asyncio

        result = asyncio.run(verifier.verify_token(token))
        assert result is not None and result.subject == LOCAL_PRINCIPAL.id

    def test_preloads_every_already_provisioned_principal_when_separated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(mcp_auth.paths, "data_dir", lambda: tmp_path)

        def fake_authority_dir(principal=None):
            pid = principal.id if principal is not None else LOCAL_PRINCIPAL.id
            d = tmp_path / "authority" if pid == LOCAL_PRINCIPAL.id else tmp_path / "users" / pid / "authority"
            d.mkdir(parents=True, exist_ok=True)
            return d

        monkeypatch.setattr(mcp_auth.paths, "authority_dir", fake_authority_dir)
        (tmp_path / "users" / "os-1002").mkdir(parents=True)
        local_token = mcp_auth.load_or_create_mcp_token(LOCAL_PRINCIPAL)
        other_token = mcp_auth.load_or_create_mcp_token(Principal(id="os-1002"))

        verifier = mcp_auth.PerUserTokenVerifier()
        mcp_auth.preload_verifier(verifier)

        import asyncio

        local_result = asyncio.run(verifier.verify_token(local_token))
        other_result = asyncio.run(verifier.verify_token(other_token))
        assert local_result is not None and local_result.subject == LOCAL_PRINCIPAL.id
        assert other_result is not None and other_result.subject == "os-1002"

    def test_a_separated_install_with_no_users_dir_yet_is_a_noop(self, tmp_path, monkeypatch):
        # A fresh separated install where no second principal has ever been
        # provisioned -- data_dir()/users/ doesn't exist at all yet.
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(mcp_auth.paths, "data_dir", lambda: tmp_path)

        verifier = mcp_auth.PerUserTokenVerifier()
        mcp_auth.preload_verifier(verifier)  # must not raise

    def test_a_non_principal_entry_under_users_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(mcp_auth.paths, "data_dir", lambda: tmp_path)
        users_root = tmp_path / "users"
        users_root.mkdir(parents=True)
        # A plain file (not a directory) and a directory name safe_principal_id
        # would hash rather than pass through unchanged -- neither is a real
        # principal's own storage root, so preload_verifier() must not try
        # to load a token from either.
        (users_root / "not-a-directory").write_text("", encoding="utf-8")
        (users_root / "has a space").mkdir(parents=True)

        verifier = mcp_auth.PerUserTokenVerifier()
        mcp_auth.preload_verifier(verifier)  # must not raise, and registers nothing from either entry
