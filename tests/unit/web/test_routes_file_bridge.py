"""Tests for web/routes_file_bridge.py: the capability pair, PUT /mcp-files/slots/{slot} and
GET /mcp-files/fetch/{token} (ADR 0028, ADR 0304), authenticated by the token in the URL alone."""
from __future__ import annotations

import hashlib
import json

import pytest
from starlette.testclient import TestClient

from privacyfence import paths
from privacyfence.agent_identity import AgentIdentity, AgentSource, agent_scope
from privacyfence.audit_log import current_week, init_audit_logger, released_request_scope, set_released_request_id
from privacyfence.download_staging import get_download_staging_store
from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.upload_staging import get_upload_staging_store
from privacyfence.web import routes_file_bridge
from privacyfence.web.routes_file_bridge import mount_capability_routes

BOB = Principal(id="bob", email="bob@example.com")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


def _capability_client() -> TestClient:
    from starlette.applications import Starlette

    app = Starlette(routes=mount_capability_routes())
    return TestClient(app, base_url="http://127.0.0.1:8765")


class TestCapabilityUploadRoute:
    """PUT /mcp-files/slots/{slot} -- ADR 0007's "Clients without
    the bridge" section, ADR 0028. No Authorization header at all, unlike
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
    """GET /mcp-files/fetch/{token} -- no Authorization header,
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

    def test_served_row_names_the_releasing_request_and_agent(self, tmp_path):
        """ADR 0093: the fetch carries no bearer, so the row is tied to its
        request through what stage() captured."""
        init_audit_logger(str(tmp_path / "audit"))
        agent = AgentIdentity(id="claude-code", name="Claude Code", version="2.1", source=AgentSource.CLIENT_INFO)
        with released_request_scope(), agent_scope(agent):
            set_released_request_id("req-1")
            token = get_download_staging_store().stage(LOCAL_PRINCIPAL, b"data", "f.txt", "text/plain")

        assert _capability_client().get(f"/mcp-files/fetch/{_b64(token)}").status_code == 200

        week_file = tmp_path / "audit" / f"{current_week()}.jsonl"
        [row] = [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]
        assert row["decision"] == "bridge_download_served"
        assert row["request_id"] == "req-1"
        assert (row["agent_id"], row["agent_name"]) == ("claude-code", "Claude Code")


class TestMount:
    def test_mount_capability_routes_returns_one_mount_under_the_prefix(self):
        [mount] = mount_capability_routes()
        assert mount.path == routes_file_bridge.FILE_BRIDGE_PREFIX

    def test_the_removed_bearer_routes_are_not_served(self):
        client = _capability_client()
        assert client.put("/mcp-files/uploads/anything", content=b"d").status_code == 404
        assert client.get("/mcp-files/downloads/anything").status_code == 404


class TestAuditLoggingNeverBlocks:
    def test_upload_succeeds_even_if_the_audit_log_write_fails(self, monkeypatch):
        monkeypatch.setattr(
            routes_file_bridge, "get_audit_logger",
            lambda: (_ for _ in ()).throw(RuntimeError("audit log unavailable")),
        )
        store = get_upload_staging_store()
        token = store.create_slot(LOCAL_PRINCIPAL, "~/f.txt", max_bytes=1000)
        r = _capability_client().put(f"/mcp-files/slots/{_b64(token)}", content=b"data")
        assert r.status_code == 204

    def test_download_succeeds_even_if_the_audit_log_write_fails(self, monkeypatch):
        monkeypatch.setattr(
            routes_file_bridge, "get_audit_logger",
            lambda: (_ for _ in ()).throw(RuntimeError("audit log unavailable")),
        )
        store = get_download_staging_store()
        token = store.stage(LOCAL_PRINCIPAL, b"data", "f.txt", "text/plain")
        r = _capability_client().get(f"/mcp-files/fetch/{_b64(token)}")
        assert r.status_code == 200
        assert r.content == b"data"


def _b64(token: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
