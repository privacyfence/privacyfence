"""Unit tests for web/mcp_auth.py's principal_from_access_token (P6/P7)
-- see test_routes_mcp_principal.py for the wire-level proof that routes_mcp.py
actually calls this per request, and web/test_oauth_provider.py for the
org-mode ``OrgOAuthProvider`` tokens this function is actually built to
read (``subject``/``claims``), end to end.
"""
from __future__ import annotations

from mcp.server.auth.provider import AccessToken

from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.web import mcp_auth
from privacyfence.web.mcp_auth import principal_from_access_token


class TestPrincipalFromAccessToken:
    def test_none_token_resolves_to_local_principal(self):
        assert principal_from_access_token(None) == LOCAL_PRINCIPAL

    def test_local_client_id_resolves_to_local_principal(self):
        token = AccessToken(token="t", client_id="local", scopes=[])
        assert principal_from_access_token(token) == LOCAL_PRINCIPAL

    def test_client_id_is_a_fallback_only_when_no_subject_is_present(self):
        # A hand-rolled/future verifier that doesn't populate subject --
        # PerUserTokenVerifier's own local-mode case is handled above
        # already; OrgOAuthProvider (P7) always sets subject (see the next
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

    def test_unseparated_uses_the_legacy_handoff_path(self, tmp_path, monkeypatch):
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

    def test_delete_legacy_shared_mcp_token_is_a_noop_when_unseparated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: False)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: tmp_path)
        (tmp_path / mcp_auth.MCP_TOKEN_FILE_NAME).write_text("stillhere", encoding="utf-8")

        mcp_auth.delete_legacy_shared_mcp_token()

        assert (tmp_path / mcp_auth.MCP_TOKEN_FILE_NAME).read_text(encoding="utf-8") == "stillhere"

    def test_delete_legacy_shared_mcp_token_removes_it_when_separated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: tmp_path)
        legacy = tmp_path / mcp_auth.MCP_TOKEN_FILE_NAME
        legacy.write_text("shared-secret", encoding="utf-8")

        mcp_auth.delete_legacy_shared_mcp_token()

        assert not legacy.exists()

    def test_delete_legacy_shared_mcp_token_handles_a_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_auth.privilege_separation, "is_enabled", lambda: True)
        monkeypatch.setattr(mcp_auth.paths, "handoff_dir", lambda: tmp_path)

        mcp_auth.delete_legacy_shared_mcp_token()  # must not raise


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
