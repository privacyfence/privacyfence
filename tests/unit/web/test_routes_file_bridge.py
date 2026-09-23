"""Tests for web/routes_file_bridge.py: the local file bridge's own
PUT /mcp-files/uploads/{slot} and GET /mcp-files/downloads/{token} routes
(ADR 0007), authenticated exactly like /mcp."""
from __future__ import annotations

import hashlib

import pytest
from starlette.testclient import TestClient

from privacyfence import paths
from privacyfence.download_staging import get_download_staging_store
from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.upload_staging import get_upload_staging_store
from privacyfence.web import routes_file_bridge
from privacyfence.web.routes_file_bridge import (
    build_file_bridge_asgi_app,
    mount_capability_routes,
    mount_file_bridge,
)

TOKEN = "s3cr3t-mcp-token"  # nosec B105 -- test fixture value, not a real credential
BOB = Principal(id="bob", email="bob@example.com")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _client() -> TestClient:
    app = build_file_bridge_asgi_app(token=TOKEN)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _capability_client() -> TestClient:
    from starlette.applications import Starlette

    app = Starlette(routes=mount_capability_routes())
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestAuthRequired:
    def test_upload_with_no_authorization_header_is_refused(self):
        r = _client().put("/uploads/anything", content=b"data")
        assert r.status_code == 401

    def test_download_with_no_authorization_header_is_refused(self):
        r = _client().get("/downloads/anything")
        assert r.status_code == 401

    def test_wrong_token_is_refused(self):
        r = _client().get("/downloads/anything", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_a_cookie_alone_is_refused(self):
        client = _client()
        client.cookies.set("privacyfence_session", "whatever")
        r = client.get("/downloads/anything")
        assert r.status_code == 401


class TestDownloadRoute:
    def test_claims_a_staged_download_and_returns_the_bytes(self):
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"file bytes", "report.pdf", "application/pdf")
        slot = _b64(token)
        r = _client().get(f"/downloads/{slot}", headers=_auth())
        assert r.status_code == 200
        assert r.content == b"file bytes"
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-sha256"] == hashlib.sha256(b"file bytes").hexdigest()

    def test_single_use(self):
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"data", "f.txt", "text/plain")
        slot = _b64(token)
        client = _client()
        assert client.get(f"/downloads/{slot}", headers=_auth()).status_code == 200
        assert client.get(f"/downloads/{slot}", headers=_auth()).status_code == 404

    def test_expired_token_is_404(self):
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"data", "f.txt", "text/plain", ttl_seconds=-1.0)
        slot = _b64(token)
        r = _client().get(f"/downloads/{slot}", headers=_auth())
        assert r.status_code == 404

    def test_wrong_principal_is_404(self):
        # PerUserTokenVerifier always resolves this single-token caller to LOCAL_PRINCIPAL
        # -- stage for a different principal to exercise the mismatch path
        # directly against the store, same shape test_routes_downloads.py
        # uses for the org-mode route.
        store = get_download_staging_store()
        token = store.stage(BOB, b"data", "f.txt", "text/plain")
        slot = _b64(token)
        r = _client().get(f"/downloads/{slot}", headers=_auth())
        assert r.status_code == 404

    def test_malformed_token_is_404_not_500(self):
        r = _client().get("/downloads/a", headers=_auth())
        assert r.status_code == 404


class TestUploadRoute:
    def test_fills_a_slot_and_the_daemon_can_claim_it(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "~/report.pdf", max_bytes=1000)
        slot = _b64(token)
        r = _client().put(f"/uploads/{slot}", content=b"uploaded bytes", headers=_auth())
        assert r.status_code == 204
        assert r.headers["cache-control"] == "no-store"
        assert store.claim(token, LOCAL_PRINCIPAL.id) == b"uploaded bytes"

    def test_unknown_slot_is_404(self):
        r = _client().put("/uploads/" + _b64(b"\x00" * 32), content=b"data", headers=_auth())
        assert r.status_code == 404

    def test_malformed_slot_is_404_not_500(self):
        r = _client().put("/uploads/a", content=b"data", headers=_auth())
        assert r.status_code == 404

    def test_second_fill_is_409(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "~/f.txt", max_bytes=1000)
        slot = _b64(token)
        client = _client()
        assert client.put(f"/uploads/{slot}", content=b"first", headers=_auth()).status_code == 204
        assert client.put(f"/uploads/{slot}", content=b"second", headers=_auth()).status_code == 409

    def test_oversized_upload_is_413(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "~/f.txt", max_bytes=4)
        slot = _b64(token)
        r = _client().put(f"/uploads/{slot}", content=b"way more than 4 bytes", headers=_auth())
        assert r.status_code == 413

    def test_expired_slot_is_404(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "~/f.txt", max_bytes=1000, ttl_seconds=-1.0)
        slot = _b64(token)
        r = _client().put(f"/uploads/{slot}", content=b"data", headers=_auth())
        assert r.status_code == 404


class TestBuildAndMount:
    def test_needs_either_token_or_verifier(self):
        with pytest.raises(ValueError, match="token or verifier"):
            build_file_bridge_asgi_app()

    def test_mount_file_bridge_returns_one_mount_under_the_prefix(self):
        [mount] = mount_file_bridge(token=TOKEN)
        assert mount.path == routes_file_bridge.FILE_BRIDGE_PREFIX

    def test_mount_capability_routes_returns_one_mount_under_the_prefix(self):
        [mount] = mount_capability_routes()
        assert mount.path == routes_file_bridge.FILE_BRIDGE_PREFIX


class TestCapabilityUploadRoute:
    """Phase 4's PUT /mcp-files/slots/{slot} -- ADR 0007's "Clients without
    the bridge" section. No Authorization header at all, unlike
    TestUploadRoute above -- the capability token in the URL is the whole
    credential."""

    def test_fills_a_slot_with_no_auth_header_and_the_daemon_can_claim_it(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "report.pdf", max_bytes=1000)
        slot = _b64(token)
        r = _capability_client().put(f"/mcp-files/slots/{slot}", content=b"uploaded bytes")
        assert r.status_code == 204
        assert r.headers["cache-control"] == "no-store"
        # The principal-checked claim still works -- capability upload
        # doesn't skip the principal check overall, only at fill time.
        assert store.claim(token, LOCAL_PRINCIPAL.id) == b"uploaded bytes"

    def test_wrong_principal_claim_still_fails_after_a_capability_fill(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "report.pdf", max_bytes=1000)
        slot = _b64(token)
        assert _capability_client().put(f"/mcp-files/slots/{slot}", content=b"data").status_code == 204
        assert store.claim(token, BOB.id) is None

    def test_unknown_slot_is_404(self):
        r = _capability_client().put("/mcp-files/slots/" + _b64(b"\x00" * 32), content=b"data")
        assert r.status_code == 404

    def test_malformed_slot_is_404_not_500(self):
        r = _capability_client().put("/mcp-files/slots/a", content=b"data")
        assert r.status_code == 404

    def test_second_fill_is_409(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "f.txt", max_bytes=1000)
        slot = _b64(token)
        client = _capability_client()
        assert client.put(f"/mcp-files/slots/{slot}", content=b"first").status_code == 204
        assert client.put(f"/mcp-files/slots/{slot}", content=b"second").status_code == 409

    def test_oversized_upload_is_413(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "f.txt", max_bytes=4)
        slot = _b64(token)
        r = _capability_client().put(f"/mcp-files/slots/{slot}", content=b"way more than 4 bytes")
        assert r.status_code == 413

    def test_expired_slot_is_404(self):
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "f.txt", max_bytes=1000, ttl_seconds=-1.0)
        slot = _b64(token)
        r = _capability_client().put(f"/mcp-files/slots/{slot}", content=b"data")
        assert r.status_code == 404


class TestCapabilityDownloadRoute:
    """Phase 4's GET /mcp-files/fetch/{token} -- no Authorization header,
    same reasoning as TestCapabilityUploadRoute above."""

    def test_claims_a_staged_download_with_no_auth_header(self):
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"file bytes", "report.pdf", "application/pdf")
        slot = _b64(token)
        r = _capability_client().get(f"/mcp-files/fetch/{slot}")
        assert r.status_code == 200
        assert r.content == b"file bytes"
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-sha256"] == hashlib.sha256(b"file bytes").hexdigest()

    def test_single_use(self):
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"data", "f.txt", "text/plain")
        slot = _b64(token)
        client = _capability_client()
        assert client.get(f"/mcp-files/fetch/{slot}").status_code == 200
        assert client.get(f"/mcp-files/fetch/{slot}").status_code == 404

    def test_expired_token_is_404(self):
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"data", "f.txt", "text/plain", ttl_seconds=-1.0)
        slot = _b64(token)
        r = _capability_client().get(f"/mcp-files/fetch/{slot}")
        assert r.status_code == 404

    def test_works_regardless_of_which_principal_staged_it(self):
        # No principal is checked here at all -- possession of the token is
        # the whole authorization, unlike the bearer-authenticated route.
        store = get_download_staging_store()
        token = store.stage(BOB, b"data", "f.txt", "text/plain")
        slot = _b64(token)
        r = _capability_client().get(f"/mcp-files/fetch/{slot}")
        assert r.status_code == 200

    def test_malformed_token_is_404_not_500(self):
        r = _capability_client().get("/mcp-files/fetch/a")
        assert r.status_code == 404


class TestAuditLoggingNeverBlocks:
    def test_upload_succeeds_even_if_the_audit_log_write_fails(self, monkeypatch):
        monkeypatch.setattr(
            routes_file_bridge, "get_audit_logger",
            lambda: (_ for _ in ()).throw(RuntimeError("audit log unavailable")),
        )
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "~/f.txt", max_bytes=1000)
        r = _client().put(f"/uploads/{_b64(token)}", content=b"data", headers=_auth())
        assert r.status_code == 204

    def test_download_succeeds_even_if_the_audit_log_write_fails(self, monkeypatch):
        monkeypatch.setattr(
            routes_file_bridge, "get_audit_logger",
            lambda: (_ for _ in ()).throw(RuntimeError("audit log unavailable")),
        )
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"data", "f.txt", "text/plain")
        r = _client().get(f"/downloads/{_b64(token)}", headers=_auth())
        assert r.status_code == 200
        assert r.content == b"data"


def _b64(token: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
