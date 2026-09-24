"""Tests for web/routes_connect.py: /connect and the GET /oauth/start/
{service}+/oauth/callback/{service} server-redirect flow (P8, `git show 96cd5af4^:docs/https-
connector-refactor-plan.md` §9.3).

httpx's TestClient does not enforce SameSite cookie semantics the way a
real browser does, so it would happily pass a naive implementation that
reads the session cookie at callback time -- see routes_connect.py's own
module docstring on why that would be wrong. TestCallback's own tests
below drive the callback with **no** cookie at all on that specific
request, which is what actually proves the module doesn't depend on one.
That stays true now that the cookie is SameSite=Lax and a real browser
would send it here: not depending on it is the point, since the principal
has to come from the flow that started, not from whoever is signed in.
"""
from __future__ import annotations

import urllib.parse as up

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import apps_script_client, paths
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.principal import Principal
from privacyfence.web import org_session, routes_connect as rc

ISSUER = "https://pf.example.com"

_GOOGLE_ORG = {
    "client_id": "gcid", "client_secret": "gcsecret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
}
_ORG_CONFIG = {
    "google": _GOOGLE_ORG,
    "slack": {"client_id": "scid", "client_secret": "scsecret"},
    "salesforce": {"consumer_key": "sfkey", "consumer_secret": "sfsecret"},
    "atlassian": {"client_id": "acid", "client_secret": "acsecret"},
}


def _registry() -> ConnectorRegistry:
    return ConnectorRegistry(factory=lambda principal: [])


def _app(sessions=None, org_config=None, registry=None):
    sessions = sessions or org_session.OrgSessionStore()
    registry = registry or _registry()
    routes = rc.build_routes(
        sessions=sessions, connector_registry=registry,
        org_config=_ORG_CONFIG if org_config is None else org_config, issuer_url=ISSUER,
    )
    app = Starlette(routes=routes)
    return app, sessions, registry


def _client(app) -> TestClient:
    return TestClient(app, base_url=ISSUER, follow_redirects=False)


def _signed_in(sessions: org_session.OrgSessionStore, principal_id: str = "alice") -> tuple[str, Principal]:
    principal = Principal(id=principal_id, email=f"{principal_id}@example.com")
    session_id = sessions.create(principal)
    return session_id, principal


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------- #
# /connect
# ---------------------------------------------------------------------------- #

class TestConnectPage:
    def test_unauthenticated_redirects_to_login(self):
        app, _sessions, _registry = _app()
        r = _client(app).get("/connect")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/connect"

    def test_authenticated_lists_every_configured_service(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 200
        for label in (
            "Gmail", "Drive", "Calendar", "Contacts", "Tasks", "Apps Script", "Slack", "Salesforce", "Jira", "Confluence",
        ):
            assert label in r.text
        assert "Connect" in r.text  # nothing authorized yet

    def test_unconfigured_service_shows_not_set_up_badge(self):
        app, sessions, _registry = _app(org_config={})
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert "Not set up by your organization" in r.text

    def test_already_connected_service_shows_connected_badge(self, tmp_path):
        app, sessions, _registry = _app()
        session_id, principal = _signed_in(sessions)
        token_file = paths.user_dir(principal) / "credentials" / "slack_token.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("{}")
        r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert "Connected" in r.text
        assert "Reconnect" in r.text

    def test_footer_links_back_to_the_rest_of_the_org_mode_surface(self):
        # Every other org-mode page (approvals/security/settings) links back
        # here or onward; this page previously had no way back to any of
        # them short of editing the URL bar.
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert 'href="/approvals"' in r.text
        assert 'href="/security"' in r.text
        assert 'href="/settings"' in r.text

    def test_the_way_back_is_the_persistent_shell_nav_not_a_footer(self):
        # web_shell.wrap() (web_shell.ORG_NAV_ITEMS) replaced the old
        # centred one-line footer that only ever rendered here -- the same
        # header/nav now carries across /approvals, /connect, /security and
        # /settings, so it survives navigating into any of them rather than
        # disappearing the moment you leave /approvals.
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert 'class="pf-shell-nav-item active" href="/connect"' in r.text
        for href in ("/approvals", "/security", "/settings"):
            assert f'class="pf-shell-nav-item" href="{href}"' in r.text
        assert '<p style="text-align:center">' not in r.text

    def test_signed_in_principal_is_shown_in_the_shell_header(self):
        app, sessions, _registry = _app()
        session_id, principal = _signed_in(sessions, "alice")
        r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert f'<div class="pf-shell-principal">{principal.email}</div>' in r.text


# ---------------------------------------------------------------------------- #
# /oauth/start/{service}
# ---------------------------------------------------------------------------- #

class TestOAuthStart:
    def test_unauthenticated_redirects_to_login(self):
        app, _sessions, _registry = _app()
        r = _client(app).get("/oauth/start/slack")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/connect"

    def test_unknown_service_is_404(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/not-a-service", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 404

    def test_unconfigured_service_redirects_back_with_error(self):
        app, sessions, _registry = _app(org_config={})
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=slack"

    def test_slack_redirects_to_slacks_own_authorize_endpoint_with_state(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 302
        location = r.headers["location"]
        assert location.startswith("https://slack.com/oauth/v2/authorize?")
        qs = dict(up.parse_qsl(up.urlparse(location).query))
        assert qs["redirect_uri"] == f"{ISSUER}/oauth/callback/slack"
        assert "state" in qs

    def test_google_service_redirects_with_pkce_challenge(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/gmail", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 302
        location = r.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/auth?")
        qs = dict(up.parse_qsl(up.urlparse(location).query))
        assert qs["redirect_uri"] == f"{ISSUER}/oauth/callback/gmail"
        assert "code_challenge" in qs

    def test_apps_script_redirects_to_google_with_its_own_scopes_and_callback(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/apps_script", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 302
        location = r.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/auth?")
        qs = dict(up.parse_qsl(up.urlparse(location).query))
        assert qs["redirect_uri"] == f"{ISSUER}/oauth/callback/apps_script"
        assert qs["scope"].split() == apps_script_client.SCOPES

    def test_apps_script_without_a_google_section_redirects_back_with_error(self):
        app, sessions, _registry = _app(org_config={"slack": _ORG_CONFIG["slack"]})
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/apps_script", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=apps_script"

    def test_atlassian_covers_both_jira_and_confluence(self):
        # Jira and Confluence are one Atlassian OAuth app under the hood
        # (one grant, one token -- see _GRANT_KEY), and Atlassian's OAuth
        # 2.0 (3LO) apps accept only a single registered callback URL
        # (unlike Slack/Salesforce/Google). So both must redirect through
        # the exact same URL regardless of which one the user clicked --
        # a per-service URL here would mean whichever one isn't registered
        # on the Atlassian app always fails with redirect_uri_mismatch.
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        for service in ("jira", "confluence"):
            r = _client(app).get(f"/oauth/start/{service}", cookies={org_session.SESSION_COOKIE: session_id})
            assert r.status_code == 302
            location = r.headers["location"]
            assert location.startswith("https://auth.atlassian.com/authorize?")
            qs = dict(up.parse_qsl(up.urlparse(location).query))
            assert qs["redirect_uri"] == f"{ISSUER}/oauth/callback/atlassian"


# ---------------------------------------------------------------------------- #
# /oauth/callback/{service}
# ---------------------------------------------------------------------------- #

class TestOAuthCallback:
    def test_missing_state_is_rejected(self):
        app, _sessions, _registry = _app()
        r = _client(app).get("/oauth/callback/slack?code=abc")
        assert r.status_code == 400

    def test_unknown_state_is_rejected(self):
        app, _sessions, _registry = _app()
        r = _client(app).get("/oauth/callback/slack?code=abc&state=does-not-exist")
        assert r.status_code == 400

    def test_provider_declining_redirects_back_with_error(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        r = client.get(f"/oauth/callback/slack?error=access_denied&state={state}")
        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=slack"

    def test_happy_path_saves_token_evicts_registry_and_needs_no_session_cookie(self, monkeypatch):
        app, sessions, registry = _app()
        session_id, principal = _signed_in(sessions)
        client = _client(app)

        # Step 1: a same-site nav the user's own click made -- the cookie
        # *is* present here.
        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        def fake_exchange_code(client_id, client_secret, code, redirect_uri):
            assert client_id == "scid"
            assert redirect_uri == f"{ISSUER}/oauth/callback/slack"
            return {"access_token": "xoxp-fake", "team_name": "Acme"}

        monkeypatch.setattr(rc.slack_client, "exchange_code", fake_exchange_code)
        evicted = []
        monkeypatch.setattr(registry, "evict", lambda pid: evicted.append(pid))

        # Step 2: the real-world cross-site redirect landing -- deliberately
        # NO cookie on this request at all. Under the SameSite=Strict this
        # module was written against, that mirrored what a browser really
        # sends; under Lax the cookie would now arrive, and withholding it
        # here keeps proving the thing that matters either way -- the
        # principal is resolved from `state`, never from the session.
        r = client.get(f"/oauth/callback/slack?code=auth-code-1&state={state}")

        assert r.status_code == 302
        assert r.headers["location"] == "/connect?connected=slack"
        token_file = paths.user_dir(principal) / "credentials" / "slack_token.json"
        assert token_file.exists()
        assert evicted == [principal.id]

    def test_apps_script_callback_saves_its_own_token_with_its_own_scopes(self, monkeypatch):
        app, sessions, registry = _app()
        session_id, principal = _signed_in(sessions)
        client = _client(app)
        start = client.get("/oauth/start/apps_script", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        exchanged = []

        def fake_exchange_code(client_config, scopes, redirect_uri, code, code_verifier):
            exchanged.append((scopes, redirect_uri, code))
            return "fake-creds"

        saved = []
        monkeypatch.setattr(rc.google_oauth, "exchange_code", fake_exchange_code)
        monkeypatch.setattr(rc.google_oauth, "save_credentials", lambda token_file, creds: saved.append((token_file, creds)))
        monkeypatch.setattr(registry, "evict", lambda pid: None)

        r = client.get(f"/oauth/callback/apps_script?code=auth-code-1&state={state}")

        assert r.status_code == 302
        assert r.headers["location"] == "/connect?connected=apps_script"
        assert exchanged == [(apps_script_client.SCOPES, f"{ISSUER}/oauth/callback/apps_script", "auth-code-1")]
        assert saved == [(str(paths.user_dir(principal) / "credentials" / "apps_script_token.json"), "fake-creds")]

    def test_state_is_single_use(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]
        monkeypatch.setattr(
            rc.slack_client, "exchange_code",
            lambda *a, **kw: {"access_token": "xoxp-fake", "team_name": "Acme"},
        )

        first = client.get(f"/oauth/callback/slack?code=c&state={state}")
        second = client.get(f"/oauth/callback/slack?code=c&state={state}")

        assert first.status_code == 302
        assert second.status_code == 400

    def test_atlassian_callback_lands_on_the_shared_grant_url_not_a_per_service_one(self, monkeypatch):
        app, sessions, registry = _app()
        session_id, principal = _signed_in(sessions)
        client = _client(app)

        start = client.get("/oauth/start/jira", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        def fake_exchange_code(client_id, client_secret, code, redirect_uri, code_verifier):
            assert client_id == "acid"
            assert redirect_uri == f"{ISSUER}/oauth/callback/atlassian"
            return {"access_token": "tok", "refresh_token": "ref"}

        monkeypatch.setattr(rc.atlassian_oauth, "exchange_code", fake_exchange_code)
        monkeypatch.setattr(
            rc.atlassian_oauth, "resolve_resource_and_save",
            lambda token_file, access_token, refresh_token, pick_resource: pick_resource([{"id": "site1"}]),
        )
        evicted = []
        monkeypatch.setattr(registry, "evict", lambda pid: evicted.append(pid))

        # The provider redirects back to the *grant's* callback URL
        # (/oauth/callback/atlassian), never /oauth/callback/jira -- that's
        # the whole point of building the redirect_uri from _GRANT_KEY.
        r = client.get(f"/oauth/callback/atlassian?code=auth-code&state={state}")

        assert r.status_code == 302
        assert r.headers["location"] == "/connect?connected=jira"
        assert evicted == [principal.id]

    def test_atlassian_callback_on_the_wrong_grant_url_is_rejected(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        start = client.get("/oauth/start/jira", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        # A confused/forged request hitting the per-service path the
        # provider was never actually told about.
        r = client.get(f"/oauth/callback/jira?code=auth-code&state={state}")

        assert r.status_code == 400

    def test_exchange_failure_redirects_back_with_error_not_a_500(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        def boom(*a, **kw):
            raise rc.slack_client.SlackClientError("bad code")

        monkeypatch.setattr(rc.slack_client, "exchange_code", boom)
        r = client.get(f"/oauth/callback/slack?code=c&state={state}")
        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=slack"


# ---------------------------------------------------------------------------- #
# Telegram: /connect/telegram/*
# ---------------------------------------------------------------------------- #

class TestTelegramFlow:
    def test_start_without_csrf_is_rejected(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).post(
            "/connect/telegram/start", data={"phone": "+123"}, cookies={org_session.SESSION_COOKIE: session_id},
        )
        assert r.status_code == 401

    def test_happy_path_advances_to_code_step(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        monkeypatch.setattr(rc, "telegram_app_credentials", lambda: (123, "apihash"))

        async def fake_send_code(phone, session_file, api_id, api_hash):
            assert phone == "+1234567890"
            return "hash-abc"

        monkeypatch.setattr(rc.telegram_auth, "send_code", fake_send_code)

        r = client.post(
            "/connect/telegram/start", data={"phone": "+1234567890", "csrf": session_id},
            cookies={org_session.SESSION_COOKIE: session_id},
        )
        assert r.status_code == 303
        page = client.get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert "Verification code" in page.text or "verification code" in page.text.lower()

    def test_not_configured_shows_an_error_without_calling_telegram(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        monkeypatch.setattr(rc, "telegram_app_credentials", lambda: None)
        called = []
        monkeypatch.setattr(rc.telegram_auth, "send_code", lambda *a, **kw: called.append(1))

        client.post(
            "/connect/telegram/start", data={"phone": "+1", "csrf": session_id},
            cookies={org_session.SESSION_COOKIE: session_id},
        )

        assert called == []
