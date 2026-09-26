"""Tests for web/routes_push.py and its wiring into org mode's app (ADR 0081): the manifest and
icons (public, classified), the subscription routes (session + CSRF + Origin), the org-wide off
switch, and local mode, which mounts none of it.

web_push.py's own crypto, allowlist, store and payload are tests/unit/test_web_push.py.
"""
from __future__ import annotations

import json
import struct

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence.design_css import token_value
from privacyfence.org_mode import ConfigurationError, WebPushConfig
from privacyfence.principal import Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, routes_push
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.server import OrgAuth, build_app
from privacyfence.web.state_stream import StateStream
from privacyfence.web_approval_ui import WebApprovalUI
from privacyfence.web_push import PushNotifier, PushSubscriptionStore, b64url_encode

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com")
FCM = "https://fcm.googleapis.com/fcm/send/abc123"
NEW_PATHS = ("/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png", "/api/push/subscription")


def _subscription(endpoint: str = FCM) -> dict:
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    point = key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": b64url_encode(point), "auth": b64url_encode(b"a" * 16)}}


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _org_app(tmp_path, monkeypatch, *, push: bool):
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    web_ui = WebApprovalUI()
    store = PushSubscriptionStore(lambda: tmp_path / "users")
    notifier = PushNotifier(
        store=store, vapid_key=ec.generate_private_key(ec.SECP256R1()), subject=ISSUER,
        registry=web_ui.deferred_registry, post=lambda *a, **k: None,
    ) if push else None
    sessions = org_session.OrgSessionStore()
    org = OrgAuth(
        provider=OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback"),
        sessions=sessions, idp=_idp(), issuer_url=ISSUER,
        push_notifier=notifier, push_store=store if push else None,
    )
    app = build_app(web_ui, org=org, allowed_hosts=frozenset({"pf.example.com"}))
    return TestClient(app, base_url=ISSUER, follow_redirects=False), sessions, store, notifier


def _sign_in(client: TestClient, sessions) -> str:
    session_id = sessions.create(ALICE)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


class TestManifest:
    def test_served_without_a_session(self, tmp_path, monkeypatch):
        client, *_ = _org_app(tmp_path, monkeypatch, push=True)
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/manifest+json")
        body = r.json()
        assert body["start_url"] == "/approvals"
        assert body["display"] == "standalone"
        assert body["theme_color"] == body["background_color"] == token_value("--bg")
        assert body["name"] == "PrivacyFence"

    def test_every_icon_is_served_at_its_declared_size(self, tmp_path, monkeypatch):
        client, *_ = _org_app(tmp_path, monkeypatch, push=True)
        icons = client.get("/manifest.webmanifest").json()["icons"]
        assert {icon["sizes"] for icon in icons} >= {"192x192", "512x512"}
        for icon in icons:
            r = client.get(icon["src"])
            assert r.status_code == 200 and r.headers["content-type"] == "image/png"
            width, height = struct.unpack(">II", r.content[16:24])  # PNG IHDR
            assert icon["sizes"] == f"{width}x{height}"

    def test_the_manifest_carries_no_session_state(self, tmp_path, monkeypatch):
        client, sessions, *_ = _org_app(tmp_path, monkeypatch, push=True)
        anonymous = client.get("/manifest.webmanifest").content
        _sign_in(client, sessions)
        assert client.get("/manifest.webmanifest").content == anonymous

    def test_org_csp_allows_the_manifest_and_its_icons_only(self, tmp_path, monkeypatch):
        client, *_ = _org_app(tmp_path, monkeypatch, push=True)
        csp = client.get("/manifest.webmanifest").headers["content-security-policy"]
        assert "manifest-src 'self'" in csp
        assert f"img-src data: {ISSUER}/icons/;" in csp

    def test_org_pages_link_the_manifest(self, tmp_path, monkeypatch):
        client, sessions, *_ = _org_app(tmp_path, monkeypatch, push=True)
        _sign_in(client, sessions)
        html = client.get("/approvals").text
        assert '<link rel="manifest" href="/manifest.webmanifest">' in html
        assert '<link rel="apple-touch-icon" href="/icons/icon-192.png">' in html


class TestClassification:
    def test_every_route_built_is_classified(self, tmp_path):
        store = PushSubscriptionStore(lambda: tmp_path)
        routes = routes_push.build_routes(sessions=org_session.OrgSessionStore(), store=store)
        assert {r.path for r in routes} == set(routes_push.ROUTE_CLASSIFICATION)
        for auth, reason in routes_push.ROUTE_CLASSIFICATION.values():
            assert auth in (routes_push.PUBLIC, routes_push.SESSION_AND_CSRF) and reason

    def test_an_unclassified_route_stops_the_app_from_building(self, monkeypatch):
        trimmed = dict(routes_push.ROUTE_CLASSIFICATION)
        del trimmed[routes_push.MANIFEST_PATH]
        monkeypatch.setattr(routes_push, "ROUTE_CLASSIFICATION", trimmed)
        with pytest.raises(RuntimeError, match="unclassified"):
            routes_push.build_routes(sessions=org_session.OrgSessionStore(), store=None)

    def test_only_the_subscription_route_needs_a_session(self):
        needing = {p for p, (auth, _) in routes_push.ROUTE_CLASSIFICATION.items() if auth != routes_push.PUBLIC}
        assert needing == {routes_push.SUBSCRIPTION_PATH}


class TestSubscriptionAuth:
    def test_no_session_is_401(self, tmp_path, monkeypatch):
        client, _sessions, store, _ = _org_app(tmp_path, monkeypatch, push=True)
        r = client.post("/api/push/subscription", json={"subscription": _subscription(), "csrf": "x"})
        assert r.status_code == 401
        assert store.list("alice") == []

    def test_missing_or_wrong_csrf_is_403(self, tmp_path, monkeypatch):
        client, sessions, store, _ = _org_app(tmp_path, monkeypatch, push=True)
        _sign_in(client, sessions)
        for body in ({"subscription": _subscription()}, {"subscription": _subscription(), "csrf": "wrong"}):
            assert client.post("/api/push/subscription", json=body).status_code == 403
        assert store.list("alice") == []

    def test_cross_origin_is_403(self, tmp_path, monkeypatch):
        client, sessions, store, _ = _org_app(tmp_path, monkeypatch, push=True)
        csrf = _sign_in(client, sessions)
        r = client.post(
            "/api/push/subscription", json={"subscription": _subscription(), "csrf": csrf},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403
        assert store.list("alice") == []

    def test_subscribe_stores_for_the_signed_in_principal_and_delete_removes(self, tmp_path, monkeypatch):
        client, sessions, store, _ = _org_app(tmp_path, monkeypatch, push=True)
        csrf = _sign_in(client, sessions)
        r = client.post("/api/push/subscription", json={"subscription": _subscription(), "csrf": csrf})
        assert r.status_code == 200 and r.json() == {"subscribed": True}
        assert r.headers["cache-control"] == "no-store"
        assert [s.endpoint for s in store.list("alice")] == [FCM]
        r = client.request("DELETE", "/api/push/subscription", json={"endpoint": FCM, "csrf": csrf})
        assert r.status_code == 200 and r.json() == {"removed": True}
        assert store.list("alice") == []

    @pytest.mark.parametrize("endpoint", ["https://127.0.0.1/x", "http://fcm.googleapis.com/x", "https://intranet/x"])
    def test_an_endpoint_off_the_allowlist_is_400(self, tmp_path, monkeypatch, endpoint):
        client, sessions, store, _ = _org_app(tmp_path, monkeypatch, push=True)
        csrf = _sign_in(client, sessions)
        r = client.post("/api/push/subscription", json={"subscription": _subscription(endpoint), "csrf": csrf})
        assert r.status_code == 400
        assert store.list("alice") == []

    def test_an_oversized_body_is_413(self, tmp_path, monkeypatch):
        client, sessions, _store, _ = _org_app(tmp_path, monkeypatch, push=True)
        csrf = _sign_in(client, sessions)
        body = {"subscription": _subscription(), "csrf": csrf, "pad": "x" * 5000}
        assert client.post("/api/push/subscription", json=body).status_code == 413

    def test_malformed_json_is_400(self, tmp_path, monkeypatch):
        client, sessions, _store, _ = _org_app(tmp_path, monkeypatch, push=True)
        _sign_in(client, sessions)
        r = client.post("/api/push/subscription", content=b"{nope", headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    def test_the_approvals_page_hands_the_browser_the_server_key(self, tmp_path, monkeypatch):
        client, sessions, _store, notifier = _org_app(tmp_path, monkeypatch, push=True)
        csrf = _sign_in(client, sessions)
        html = client.get("/approvals").text
        assert f"var PUSH_PUBLIC_KEY = {json.dumps(notifier.public_key)};" in html
        assert f"var PUSH_CSRF = {json.dumps(csrf)};" in html


class TestOrgWideSwitchOff:
    def test_config_defaults_on_and_can_be_turned_off(self):
        assert WebPushConfig.from_org_config({}).enabled is True
        assert WebPushConfig.from_org_config({"web_push": {"enabled": False}}).enabled is False
        with pytest.raises(ConfigurationError):
            WebPushConfig.from_org_config({"web_push": {"enabled": "no"}})

    def test_off_mounts_no_subscription_route_and_offers_nothing(self, tmp_path, monkeypatch):
        client, sessions, _store, _ = _org_app(tmp_path, monkeypatch, push=False)
        csrf = _sign_in(client, sessions)
        r = client.post("/api/push/subscription", json={"subscription": _subscription(), "csrf": csrf})
        assert r.status_code in (404, 405)
        html = client.get("/approvals").text
        assert 'var PUSH_PUBLIC_KEY = "";' in html
        assert 'var PUSH_CSRF = "";' in html
        # Still installable: the manifest is not push.
        assert client.get("/manifest.webmanifest").status_code == 200

    def test_off_generates_no_key_and_registers_no_listener(self, tmp_path, monkeypatch):
        from privacyfence import daemon_main
        from privacyfence.approvals import PendingApprovalRegistry

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path / "org")
        registry = PendingApprovalRegistry()
        assert daemon_main._start_web_push({"web_push": {"enabled": False}}, registry, issuer_url=ISSUER) == (None, None)
        assert not (tmp_path / "org").exists()
        assert registry._created_listeners == []

    def test_on_generates_the_key_in_org_state_and_registers_the_listener(self, tmp_path, monkeypatch):
        from privacyfence import daemon_main
        from privacyfence.approvals import PendingApprovalRegistry

        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path / "org")
        registry = PendingApprovalRegistry()
        notifier, store = daemon_main._start_web_push({}, registry, issuer_url=ISSUER)
        try:
            assert (tmp_path / "org" / "web_push_vapid_key.pem").exists()
            assert registry._created_listeners == [notifier.on_new_approval]
            assert isinstance(store, PushSubscriptionStore)
        finally:
            notifier.shutdown()


def _starlette(app):
    while not hasattr(app, "routes"):
        app = app._app
    return app


def _route_set(routes, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for route in routes:
        if isinstance(route, Mount):
            out |= _route_set(route.routes, prefix + route.path)
        else:
            for method in sorted(getattr(route, "methods", None) or {"*"}):
                out.add(f"{method} {prefix}{route.path}")
    return out


# Local mode's full route set as it was before web push existed, captured from this same build.
LOCAL_ROUTES_BEFORE_PUSH = {
    "* /mcp",
    "GET /", "HEAD /",
    "GET /api/approvals/stream", "HEAD /api/approvals/stream",
    "GET /api/approvals/{id}/preview", "HEAD /api/approvals/{id}/preview",
    "GET /api/state/stream", "HEAD /api/state/stream",
    "GET /approvals", "HEAD /approvals",
    "GET /approvals/{id}", "HEAD /approvals/{id}",
    "GET /security", "HEAD /security",
    "GET /sw.js", "HEAD /sw.js",
    "POST /api/approvals/batch/decide",
    "POST /api/approvals/{id}/decide",
    "POST /api/security/webauthn/register/options",
    "POST /api/security/webauthn/register/verify",
    "POST /security/credentials/{credential_id}/delete",
    "POST /security/recover",
}


class TestLocalModeHasNoPush:
    def _local_app(self):
        return build_app(
            WebApprovalUI(), mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token="t",
            step_up=StepUpConfig(rp_id="localhost"), step_up_issuer_url="http://localhost:8765",
            state_stream=StateStream(settings_snapshot=lambda: {}, list_pending=lambda: []),
        )

    def test_route_set_is_unchanged(self):
        assert _route_set(_starlette(self._local_app()).routes) == LOCAL_ROUTES_BEFORE_PUSH

    def test_none_of_the_push_paths_answer(self):
        client = TestClient(self._local_app(), base_url="http://localhost")
        for path in NEW_PATHS:
            assert client.get(path).status_code == 404
        assert client.post("/api/push/subscription", json={}).status_code == 404

    def test_csp_has_no_manifest_source(self):
        client = TestClient(self._local_app(), base_url="http://localhost")
        csp = client.get("/sw.js").headers["content-security-policy"]
        assert "manifest-src" not in csp
        assert "img-src data:;" in csp

    def test_local_pages_link_no_manifest_and_offer_no_push(self):
        from privacyfence import web_shell

        html = web_shell.wrap("<p>x</p>", title="t", active="approvals", nonce="n")
        assert "manifest" not in html and "apple-touch-icon" not in html
        assert 'var PUSH_PUBLIC_KEY = "";' in html


def test_a_bare_route_list_serves_the_manifest(tmp_path):
    # routes_push has no dependency on the rest of the org app.
    app = Starlette(routes=routes_push.build_routes(sessions=org_session.OrgSessionStore(), store=None))
    assert TestClient(app).get("/manifest.webmanifest").json()["id"] == "/approvals"
