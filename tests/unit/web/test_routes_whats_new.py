"""Tests for web/routes_whats_new.py and the "What's new" strip on org mode's pages (ADR 0238):
the page and its seen endpoint (session, Origin and CSRF checks), what the page lists for a person
who upgraded, and local mode, which mounts none of it.

whats_new.py's own parsing and per-person state is tests/unit/test_whats_new.py.
"""
from __future__ import annotations

import functools

import pytest
from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence import whats_new
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web import org_session, routes_whats_new
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.server import OrgAuth, build_app
from privacyfence.web.state_stream import StateStream
from privacyfence.web_approval_ui import WebApprovalUI

pytestmark = pytest.mark.unit

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com")
STRIP = 'id="pf-shell-whats-new"'


def _notes(version: str, date: str = "2026-10-01") -> whats_new.ReleaseNotes:
    return whats_new.ReleaseNotes(version=version, date=date, body=f"### Fixed\n\n- **Fix in {version}.** Text.")


CHANGELOG = (
    whats_new.ReleaseNotes(version=whats_new.UNRELEASED, date="", body="### Added\n\n- **Upcoming thing.** Text."),
    _notes("6.10.0"), _notes("6.9.1"), _notes("6.8.2"),
)


def _set_changelog(monkeypatch, sections) -> None:
    # An lru_cache wrapper, not a bare lambda: conftest's _reset() calls load_changelog.cache_clear().
    monkeypatch.setattr(whats_new, "load_changelog", functools.lru_cache(maxsize=1)(lambda: tuple(sections)))


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


@pytest.fixture
def org(tmp_path, monkeypatch):
    """A real org app with a connector registry, so /approvals, /settings and /connect all mount,
    running 6.10.0 with a fixture changelog and ALICE last seeing 6.8.2."""
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    monkeypatch.setattr("privacyfence.web.oauth_provider._refresh_store_path", lambda: str(tmp_path / "refresh.json"))
    monkeypatch.setattr(whats_new, "running_version", lambda: "6.10.0")
    _set_changelog(monkeypatch, CHANGELOG)
    sessions = org_session.OrgSessionStore()
    auth = OrgAuth(
        provider=OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback"),
        sessions=sessions, idp=_idp(), issuer_url=ISSUER,
        connector_registry=ConnectorRegistry(factory=lambda principal: []), org_config={},
    )
    app = build_app(WebApprovalUI(), org=auth, allowed_hosts=frozenset({"pf.example.com"}))
    client = TestClient(app, base_url=ISSUER, follow_redirects=False)
    whats_new.mark_seen(ALICE, "6.8.2")
    return client, sessions


def _sign_in(client: TestClient, sessions) -> str:
    session_id = sessions.create(ALICE)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


def _seen(client: TestClient, csrf: str, **kwargs):
    return client.post(routes_whats_new.SEEN_PATH, json={"csrf": csrf}, **kwargs)


class TestPage:
    def test_page_redirects_to_login_without_a_session(self, org):
        client, _ = org
        r = client.get("/whats-new")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/whats-new"
        assert r.headers["cache-control"] == "no-store"

    def test_page_shows_every_section_since_last_seen_and_marks_seen(self, org):
        client, sessions = org
        _sign_in(client, sessions)
        r = client.get("/whats-new")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        assert "What&#x27;s new in PrivacyFence" in r.text or "What's new in PrivacyFence" in r.text
        assert "You are using version 6.10.0." in r.text
        assert "Fix in 6.10.0." in r.text and "Fix in 6.9.1." in r.text
        assert "Fix in 6.8.2." not in r.text
        assert "Upcoming thing." not in r.text
        assert whats_new.read_last_seen(ALICE) == "6.10.0"

    def test_page_with_nothing_pending_shows_the_running_versions_section(self, org):
        client, sessions = org
        whats_new.mark_seen(ALICE, "6.10.0")
        _sign_in(client, sessions)
        r = client.get("/whats-new")
        assert "Fix in 6.10.0." in r.text
        assert "Fix in 6.9.1." not in r.text

    def test_page_caps_at_max_sections_and_says_so(self, org, monkeypatch):
        client, sessions = org
        twelve = tuple(_notes(f"6.{n}.0") for n in range(20, 8, -1))
        _set_changelog(monkeypatch, twelve)
        monkeypatch.setattr(whats_new, "running_version", lambda: "6.20.0")
        _sign_in(client, sessions)
        r = client.get("/whats-new")
        assert r.text.count('class="pf-whats-new-release"') == whats_new.MAX_SECTIONS
        assert "Older changes are in the full changelog." in r.text
        assert "Fix in 6.10.0." not in r.text

    def test_page_shows_the_empty_state_without_a_changelog(self, org, monkeypatch):
        client, sessions = org
        _set_changelog(monkeypatch, ())
        _sign_in(client, sessions)
        r = client.get("/whats-new")
        assert "No release notes are available for this version." in r.text
        assert "Older changes are in the full changelog." not in r.text
        assert routes_whats_new.CHANGELOG_URL in r.text

    def test_page_titles_unreleased_as_in_this_pre_release(self, org, monkeypatch):
        client, sessions = org
        monkeypatch.setattr(whats_new, "running_version", lambda: "6.11.0rc1")
        _sign_in(client, sessions)
        r = client.get("/whats-new")
        assert "In this pre-release" in r.text
        assert r.text.index("Upcoming thing.") < r.text.index("Fix in 6.10.0.")

    def test_a_section_without_a_date_has_no_date_span(self, org, monkeypatch):
        client, sessions = org
        _set_changelog(monkeypatch, [_notes("6.10.0", date="")])
        _sign_in(client, sessions)
        r = client.get("/whats-new")
        assert "Fix in 6.10.0." in r.text
        assert '<span class="pf-whats-new-date">' not in r.text


class TestSeen:
    def test_seen_requires_session(self, org):
        client, _ = org
        r = _seen(client, "x")
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    def test_seen_requires_origin(self, org):
        client, sessions = org
        csrf = _sign_in(client, sessions)
        r = _seen(client, csrf, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert r.json() == {"error": "origin mismatch"}
        assert whats_new.read_last_seen(ALICE) == "6.8.2"

    def test_seen_requires_csrf(self, org):
        client, sessions = org
        _sign_in(client, sessions)
        r = _seen(client, "not-the-session")
        assert r.status_code == 403
        assert r.json() == {"error": "csrf"}
        assert whats_new.read_last_seen(ALICE) == "6.8.2"

    def test_seen_rejects_a_non_object_body(self, org):
        client, sessions = org
        _sign_in(client, sessions)
        r = client.post(routes_whats_new.SEEN_PATH, content="[]")
        assert r.status_code == 403
        assert r.json() == {"error": "csrf"}

    def test_seen_rejects_oversized_body(self, org):
        client, sessions = org
        csrf = _sign_in(client, sessions)
        r = client.post(routes_whats_new.SEEN_PATH, json={"csrf": csrf, "pad": "x" * 2000})
        assert r.status_code == 413
        assert whats_new.read_last_seen(ALICE) == "6.8.2"

    def test_seen_rejects_invalid_json(self, org):
        client, sessions = org
        _sign_in(client, sessions)
        r = client.post(routes_whats_new.SEEN_PATH, content="{nope")
        assert r.status_code == 400
        assert r.json() == {"error": "invalid JSON"}

    def test_seen_records_the_running_version_not_the_clients(self, org):
        client, sessions = org
        csrf = _sign_in(client, sessions)
        r = client.post(routes_whats_new.SEEN_PATH, json={"csrf": csrf, "version": "99.0.0"})
        assert r.status_code == 200
        assert r.json() == {"seen": "6.10.0"}
        assert r.headers["cache-control"] == "no-store"
        assert whats_new.read_last_seen(ALICE) == "6.10.0"


class TestStripOnOrgPages:
    def test_approvals_shows_strip_after_upgrade_and_not_after_seen(self, org):
        client, sessions = org
        csrf = _sign_in(client, sessions)
        page = client.get("/approvals")
        assert STRIP in page.text
        assert "PrivacyFence was updated to 6.10.0." in page.text
        assert _seen(client, csrf).status_code == 200
        assert STRIP not in client.get("/approvals").text

    def test_first_visit_shows_no_strip_and_records_the_version(self, org, tmp_path):
        client, sessions = org
        bob = Principal(id="bob", email="bob@example.com")
        client.cookies.set(org_session.SESSION_COOKIE, sessions.create(bob))
        assert whats_new.read_last_seen(bob) is None
        assert STRIP not in client.get("/approvals").text
        assert whats_new.read_last_seen(bob) == "6.10.0"

    def test_settings_shows_the_strip_after_upgrade(self, org):
        client, sessions = org
        _sign_in(client, sessions)
        r = client.get("/settings")
        assert r.status_code == 200
        assert STRIP in r.text

    def test_connect_shows_the_strip_after_upgrade(self, org):
        client, sessions = org
        _sign_in(client, sessions)
        r = client.get("/connect")
        assert r.status_code == 200
        assert STRIP in r.text


def test_build_routes_refuses_an_unclassified_route(monkeypatch):
    monkeypatch.setattr(
        routes_whats_new, "ROUTE_CLASSIFICATION",
        {k: v for k, v in routes_whats_new.ROUTE_CLASSIFICATION.items() if k != routes_whats_new.SEEN_PATH},
    )
    with pytest.raises(RuntimeError, match="unclassified"):
        routes_whats_new.build_routes(sessions=org_session.OrgSessionStore())


def test_local_mode_mounts_no_whats_new_route():
    app = build_app(
        WebApprovalUI(), mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token="t",
        step_up=StepUpConfig(rp_id="localhost"), step_up_issuer_url="http://localhost:8765",
        state_stream=StateStream(settings_snapshot=lambda: {}, list_pending=lambda: []),
    )
    client = TestClient(app, base_url="http://localhost")
    assert client.get("/whats-new").status_code == 404
    assert client.post(routes_whats_new.SEEN_PATH, json={}).status_code == 404
