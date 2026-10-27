"""Tests for web/routes_connect.py: /connect and the GET /oauth/start/
{service}+/oauth/callback/{service} server-redirect flow.

The callback only finishes in the signed-in session that started it (ADR 0178), so every
callback that is expected to succeed sends the starting principal's session cookie, and
TestOAuthCallback drives it with another principal's cookie and with none to prove nothing is
saved.
"""
from __future__ import annotations

import time
import urllib.parse as up

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import json

from privacyfence import (
    apps_script_client,
    grist_auth,
    grist_provisioning,
    miro_auth,
    paths,
    peek_client,
    peek_provisioning,
    slack_client,
)
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
    "miro": {"client_id": "m", "client_secret": "s"},
    "telegram": {"enabled": True},
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

    def test_service_without_a_section_is_not_listed(self):
        app, sessions, _registry = _app(org_config={"slack": _ORG_CONFIG["slack"]})
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
        assert 'class="pf-service-chip">Slack<' in r.text
        for label in ("Gmail", "Jira", "Telegram", "Peek", "Grist"):
            assert f'class="pf-service-chip">{label}<' not in r.text

    def test_enabled_service_that_cannot_be_offered_shows_not_set_up_badge(self):
        app, sessions, _registry = _app(org_config={"telegram": {"enabled": True}})
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


class TestConnectPageLook:
    """The page is built from the shared design system: every service is a card, its state a
    badge whose text names the state (never colour alone), its action a shared button, and no
    colour of its own (tests/unit/test_design_system.py checks that for every renderer)."""

    def _page(self, *, connected: bool = False, org_config=None) -> str:
        principal = Principal(id="alice", email="alice@example.com")
        if connected:
            token_file = paths.user_dir(principal) / "credentials" / "slack_token.json"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text("{}")
        return rc._render_connect_page(
            principal=principal, org_config=_ORG_CONFIG if org_config is None else org_config,
            telegram_state=rc._TelegramState(), flash_connected="", flash_error="", csrf="c", nonce="n",
        )

    def test_each_state_is_a_named_badge(self):
        html = self._page(connected=True)
        assert '<span class="pf-service-chip">Slack</span><span class="badge badge-success">Connected</span>' in html
        assert '<span class="pf-service-chip">Gmail</span><span class="badge">Not connected</span>' in html
        assert 'class="button primary" href="/oauth/start/gmail">Connect<' in html
        assert 'class="button secondary" href="/oauth/start/slack">Reconnect<' in html
        unconfigured = self._page(org_config={"telegram": {"enabled": True}})
        assert '<span class="badge badge-dashed">Not set up by your organization</span>' in unconfigured

    def _assets_page(self, *, assets: bool = True, scopes=None, atlassian=None) -> str:
        principal = Principal(id="alice", email="alice@example.com")
        if scopes is not None:
            token_file = paths.user_dir(principal) / "credentials" / "atlassian_token.json"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(json.dumps({"access_token": "a", "scopes": scopes}))
        section = _ORG_CONFIG["atlassian"] if atlassian is None else atlassian
        return self._page(org_config={**_ORG_CONFIG, "atlassian": {**section, "assets": assets}})

    def test_assets_row_is_listed_only_when_the_bundle_switches_assets_on(self):
        assert '<span class="pf-service-chip">Jira Assets</span>' in self._assets_page(assets=True)
        assert "Jira Assets" not in self._assets_page(assets=False)
        assert "Jira Assets" not in self._page(org_config={"slack": _ORG_CONFIG["slack"]})

    def test_assets_row_without_an_atlassian_sign_in_points_at_jira_and_confluence(self):
        html = self._assets_page()
        assert '<span class="pf-service-chip">Jira Assets</span><span class="badge">Not connected</span>' in html
        assert "Connect either one to turn it on." in html
        assert 'href="/oauth/start/assets"' not in html

    def test_assets_row_is_connected_when_the_sign_in_carries_the_assets_scopes(self):
        html = self._assets_page(scopes=["read:jira-work", "read:cmdb-object:jira"])
        assert '<span class="pf-service-chip">Jira Assets</span>' + rc._CONNECTED_BADGE in html
        assert "Needs reconnect" not in html

    def test_assets_row_asks_for_a_reconnect_when_the_sign_in_predates_assets(self):
        for scopes in (["read:jira-work"], "not-a-list"):
            html = self._assets_page(scopes=scopes)
            assert '<span class="pf-service-chip">Jira Assets</span><span class="badge">Needs reconnect</span>' in html
            assert 'class="button primary" href="/oauth/start/jira">Reconnect<' in html

    def test_assets_row_asks_for_a_reconnect_when_the_saved_token_cannot_be_read(self):
        principal = Principal(id="alice", email="alice@example.com")
        token_file = paths.user_dir(principal) / "credentials" / "atlassian_token.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        for content in ("{not json", "[]"):
            token_file.write_text(content)
            assert '<span class="badge">Needs reconnect</span>' in self._assets_page()

    def test_assets_row_without_an_atlassian_app_is_not_set_up(self):
        html = self._assets_page(atlassian={"site": "x"})
        assert (
            '<span class="pf-service-chip">Jira Assets</span>'
            '<span class="badge badge-dashed">Not set up by your organization</span>'
        ) in html

    def test_rows_are_cards_and_nothing_is_styled_inline(self):
        html = self._page()
        # Eleven OAuth services and Telegram (enabled, but no app credentials on this install); Peek and
        # Grist have no section in this config, so they are not listed.
        assert html.count('<li class="service card cluster">') == 12
        body = html.split('<div class="pf-connect stack">', 1)[1]
        assert "style=" not in body


class TestTelegramFormOnAPhone:
    """Each step of the Telegram sign-in asks a phone for the right keyboard (``inputmode``)
    and tells the browser what it may fill in (``autocomplete``)."""

    @staticmethod
    def _html(monkeypatch, step: str | None, error: str = "") -> str:
        monkeypatch.setattr(rc, "telegram_app_credentials", lambda: (123, "apihash"))
        return rc._telegram_box_html(
            Principal(id="alice"), _ORG_CONFIG, rc._TelegramState(step=step, error=error), "csrf-token",
        )

    def test_phone_number_gets_the_phone_pad(self, monkeypatch):
        html = self._html(monkeypatch, None)
        assert '<input class="field" type="tel" name="phone" inputmode="tel" autocomplete="tel"' in html
        assert '<span class="field-label">Phone number</span>' in html

    def test_code_gets_the_digit_pad_and_the_one_time_code_suggestion(self, monkeypatch):
        html = self._html(monkeypatch, "code")
        assert 'name="code" inputmode="numeric" autocomplete="one-time-code"' in html
        # Cancel posts its own form but sits beside Confirm, not nested in its form.
        assert '<form id="pf-telegram-cancel" method="post" action="/connect/telegram/cancel">' in html
        assert 'form="pf-telegram-cancel">Cancel</button>' in html
        assert html.count("<form") == 2 and html.index("</form>") < html.index('<form id="pf-telegram-cancel"')

    def test_two_step_password_is_a_saved_password(self, monkeypatch):
        html = self._html(monkeypatch, "password")
        assert '<input class="field" type="password" name="password" autocomplete="current-password" required>' in html

    def test_an_error_is_an_alert_with_its_text(self, monkeypatch):
        html = self._html(monkeypatch, "code", error="That code has expired.")
        assert '<p class="card card-danger" role="alert">That code has expired.</p>' in html


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

    def test_slack_requests_the_bundles_user_scopes(self):
        slack_org = {**_ORG_CONFIG["slack"], "user_scopes": ["channels:read", "users:read"]}
        app, sessions, _registry = _app(org_config={"slack": slack_org})
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        assert qs["user_scope"] == "channels:read,users:read"

    def test_slack_without_bundle_scopes_requests_the_defaults(self):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        assert qs["user_scope"] == ",".join(slack_client.DEFAULT_USER_SCOPES)

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


    @pytest.mark.parametrize(("assets_flag", "expect_assets"), [(False, False), (True, True)])
    def test_atlassian_authorize_url_carries_assets_scopes_only_when_the_bundle_says_so(
        self, assets_flag, expect_assets,
    ):
        org_config = dict(_ORG_CONFIG)
        org_config["atlassian"] = {**_ORG_CONFIG["atlassian"], "assets": assets_flag}
        app, sessions, _registry = _app(org_config=org_config)
        session_id, _principal = _signed_in(sessions)
        r = _client(app).get("/oauth/start/jira", cookies={org_session.SESSION_COOKIE: session_id})
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        scopes = qs["scope"].split(" ")
        assert ("read:cmdb-object:jira" in scopes) is expect_assets
        assert "read:jira-work" in scopes


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

        r = client.get(f"/oauth/callback/slack?error=access_denied&state={state}", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=slack"

    def test_happy_path_saves_token_and_evicts_registry(self, monkeypatch):
        app, sessions, registry = _app()
        session_id, principal = _signed_in(sessions)
        client = _client(app)

        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        def fake_exchange_code(client_id, client_secret, code, redirect_uri):
            assert client_id == "scid"
            assert redirect_uri == f"{ISSUER}/oauth/callback/slack"
            return {"access_token": "xoxp-fake", "team_name": "Acme"}

        monkeypatch.setattr(rc.slack_client, "exchange_code", fake_exchange_code)
        evicted = []
        monkeypatch.setattr(registry, "evict", lambda pid: evicted.append(pid))

        # The provider's redirect back is a top-level GET, so the Lax session cookie rides along.
        r = client.get(
            f"/oauth/callback/slack?code=auth-code-1&state={state}",
            cookies={org_session.SESSION_COOKIE: session_id},
        )

        assert r.status_code == 302
        assert r.headers["location"] == "/connect?connected=slack"
        token_file = paths.user_dir(principal) / "credentials" / "slack_token.json"
        assert token_file.exists()
        assert evicted == [principal.id]

    def test_callback_with_a_different_signed_in_principal_is_rejected(self, monkeypatch):
        app, sessions, registry = _app()
        alice_session, alice = _signed_in(sessions, "alice")
        bob_session, bob = _signed_in(sessions, "bob")
        client = _client(app)
        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: alice_session})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]
        exchanged = []
        monkeypatch.setattr(rc.slack_client, "exchange_code", lambda *a, **kw: exchanged.append(1))
        evicted = []
        monkeypatch.setattr(registry, "evict", lambda pid: evicted.append(pid))

        r = client.get(
            f"/oauth/callback/slack?code=c&state={state}", cookies={org_session.SESSION_COOKIE: bob_session},
        )

        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=slack"
        assert r.headers["cache-control"] == "no-store"
        assert exchanged == [] and evicted == []
        assert not (paths.user_dir(alice) / "credentials" / "slack_token.json").exists()
        assert not (paths.user_dir(bob) / "credentials" / "slack_token.json").exists()

    def test_callback_with_no_session_is_rejected(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, principal = _signed_in(sessions)
        client = _client(app)
        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]
        exchanged = []
        monkeypatch.setattr(rc.slack_client, "exchange_code", lambda *a, **kw: exchanged.append(1))

        r = client.get(f"/oauth/callback/slack?code=c&state={state}")

        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=slack"
        assert exchanged == []
        assert not (paths.user_dir(principal) / "credentials" / "slack_token.json").exists()

    def test_callback_after_ttl_is_rejected(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, principal = _signed_in(sessions)
        client = _client(app)
        start = client.get("/oauth/start/slack", cookies={org_session.SESSION_COOKIE: session_id})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]
        exchanged = []
        monkeypatch.setattr(rc.slack_client, "exchange_code", lambda *a, **kw: exchanged.append(1))
        now = time.time()
        monkeypatch.setattr(rc.time, "time", lambda: now + rc._PENDING_TTL_SECONDS + 1)

        r = client.get(
            f"/oauth/callback/slack?code=c&state={state}", cookies={org_session.SESSION_COOKIE: session_id},
        )

        assert r.status_code == 400
        assert exchanged == []
        assert not (paths.user_dir(principal) / "credentials" / "slack_token.json").exists()

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

        r = client.get(f"/oauth/callback/apps_script?code=auth-code-1&state={state}", cookies={org_session.SESSION_COOKIE: session_id})

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

        first = client.get(f"/oauth/callback/slack?code=c&state={state}", cookies={org_session.SESSION_COOKIE: session_id})
        second = client.get(f"/oauth/callback/slack?code=c&state={state}", cookies={org_session.SESSION_COOKIE: session_id})

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
        r = client.get(f"/oauth/callback/atlassian?code=auth-code&state={state}", cookies={org_session.SESSION_COOKIE: session_id})

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
        r = client.get(f"/oauth/callback/jira?code=auth-code&state={state}", cookies={org_session.SESSION_COOKIE: session_id})

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
        r = client.get(f"/oauth/callback/slack?code=c&state={state}", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=slack"


# ---------------------------------------------------------------------------- #
# Telegram: /connect/telegram/*
# ---------------------------------------------------------------------------- #

class TestTelegramNotEnabled:
    def test_start_is_refused_without_the_org_section(self, monkeypatch):
        monkeypatch.setattr(rc, "telegram_app_credentials", lambda: (123, "apihash"))
        sent = []
        monkeypatch.setattr(rc.telegram_auth, "send_code", lambda *a: sent.append(a))
        app, sessions, _registry = _app(org_config={"slack": _ORG_CONFIG["slack"]})
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        r = client.post(
            "/connect/telegram/start", data={"csrf": session_id, "phone": "+15550100"},
            cookies={org_session.SESSION_COOKIE: session_id},
        )
        assert r.status_code == 303
        assert sent == []


class TestTelegramFlow:
    @pytest.mark.parametrize("step", ["start", "code", "2fa", "cancel"])
    def test_a_post_after_the_session_ended_lands_on_the_signed_out_page(self, step):
        """Not /login?next=/connect: /login redirects on to the IdP, which the page's form-action
        does not let a form submission reach (ADR 0082)."""
        app, _sessions, _registry = _app()
        r = _client(app).post(f"/connect/telegram/{step}", data={"csrf": "stale"})
        assert r.status_code == 303
        assert r.headers["location"] == "/signed-out"

    @staticmethod
    def _record_telegram_calls(monkeypatch):
        calls: list[str] = []

        async def send_code(*a, **kw):
            calls.append("send_code")

        async def sign_in(*a, **kw):
            calls.append("sign_in")

        async def sign_in_2fa(*a, **kw):
            calls.append("sign_in_2fa")

        monkeypatch.setattr(rc.telegram_auth, "send_code", send_code)
        monkeypatch.setattr(rc.telegram_auth, "sign_in", sign_in)
        monkeypatch.setattr(rc.telegram_auth, "sign_in_2fa", sign_in_2fa)
        return calls

    @pytest.mark.parametrize("step", ["start", "code", "2fa", "cancel"])
    def test_post_without_csrf_is_rejected(self, monkeypatch, step):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        calls = self._record_telegram_calls(monkeypatch)
        r = _client(app).post(
            f"/connect/telegram/{step}", data={"csrf": "wrong", "phone": "+123", "code": "1", "password": "p"},
            cookies={org_session.SESSION_COOKIE: session_id},
        )
        assert r.status_code == 401
        assert calls == []

    @pytest.mark.parametrize("step", ["start", "code", "2fa", "cancel"])
    def test_post_from_another_origin_is_rejected(self, monkeypatch, step):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        calls = self._record_telegram_calls(monkeypatch)
        r = _client(app).post(
            f"/connect/telegram/{step}", data={"csrf": session_id, "phone": "+123", "code": "1", "password": "p"},
            cookies={org_session.SESSION_COOKIE: session_id}, headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403
        assert calls == []

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


# ---------------------------------------------------------------------------- #
# Peek
# ---------------------------------------------------------------------------- #

_PEEK_SERVER = "https://peek.example.com"
_PEEK_ORG = {**_ORG_CONFIG, "peek": {"base_url": _PEEK_SERVER}}
_PEEK_SECRET_CODE = "device-code-secret"
_PEEK_SECRET_TOKEN = "peek-token-secret"


def _peek_token_path(principal: Principal):
    return paths.user_dir(principal) / "credentials" / "peek_token.json"


def _write_peek_token(principal: Principal, base_url: str = _PEEK_SERVER) -> None:
    path = _peek_token_path(principal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base_url": base_url, "token": _PEEK_SECRET_TOKEN}))


def _login(expires_in: float = 600.0) -> peek_client.PeekDeviceLogin:
    return peek_client.PeekDeviceLogin(
        device_code=_PEEK_SECRET_CODE, user_code="ABCD-1234",
        verification_url=f"{_PEEK_SERVER}/cli/verify?code=ABCD-1234", interval=2, expires_at=time.time() + expires_in,
    )


class TestPeekBox:
    def _page(self, principal, org_config) -> str:
        return rc._render_connect_page(
            principal=principal, org_config=org_config, telegram_state=rc._TelegramState(),
            flash_connected="", flash_error="", csrf="c", nonce="n",
        )

    def _peek_row(self, html: str) -> str:
        return html.split('<li class="service peek card stack">', 1)[1].split("</li>", 1)[0]

    def test_no_section_is_not_listed(self):
        principal = Principal(id="alice", email="alice@example.com")
        html = self._page(principal, _ORG_CONFIG)
        assert 'class="pf-service-chip">Peek<' not in html
        assert "service peek" not in html

    def test_section_without_a_token_is_not_connected(self):
        principal = Principal(id="alice", email="alice@example.com")
        row = self._peek_row(self._page(principal, _PEEK_ORG))
        assert "Not connected" in row
        assert f"Your organization uses {_PEEK_SERVER}." in row
        assert "Connect Peek" in row and "Reconnect Peek" not in row

    def test_token_for_the_bundle_server_is_connected(self):
        principal = Principal(id="alice", email="alice@example.com")
        _write_peek_token(principal)
        row = self._peek_row(self._page(principal, _PEEK_ORG))
        assert "Connected" in row and "Not connected" not in row
        assert "Reconnect Peek" in row

    def test_provisioned_file_shows_the_automatic_line(self):
        principal = Principal(id="alice", email="alice@example.com")
        peek_client.save_token_file(
            str(_peek_token_path(principal)), _PEEK_SERVER, _PEEK_SECRET_TOKEN, source="provisioned", expires_at=1,
        )
        row = self._peek_row(self._page(principal, _PEEK_ORG))
        assert "Your organization connected your Peek account automatically." in row
        assert "Reconnect Peek" in row

    def test_device_file_does_not_show_the_automatic_line(self):
        principal = Principal(id="alice", email="alice@example.com")
        _write_peek_token(principal)
        row = self._peek_row(self._page(principal, _PEEK_ORG))
        assert "connected your Peek account automatically" not in row

    def test_token_for_another_server_is_not_connected(self):
        principal = Principal(id="alice", email="alice@example.com")
        _write_peek_token(principal, "https://other.example.com")
        row = self._peek_row(self._page(principal, _PEEK_ORG))
        assert "Not connected" in row

    def test_is_connected_follows_the_token_file(self):
        principal = Principal(id="alice", email="alice@example.com")
        assert rc._is_connected(principal, "peek") is False
        _write_peek_token(principal)
        assert rc._is_connected(principal, "peek") is True

    def test_is_configured_needs_a_usable_section(self):
        assert rc._is_configured(_PEEK_ORG, "peek") is True
        assert rc._is_configured(_ORG_CONFIG, "peek") is False
        assert rc._is_configured({"peek": {"base_url": "ftp://x"}}, "peek") is False


class TestPeekFlow:
    @pytest.mark.parametrize("step", ["start", "finish", "cancel"])
    def test_a_post_after_the_session_ended_lands_on_the_signed_out_page(self, step):
        app, _sessions, _registry = _app(org_config=_PEEK_ORG)
        r = _client(app).post(f"/connect/peek/{step}", data={"csrf": "stale"})
        assert r.status_code == 303
        assert r.headers["location"] == "/signed-out"

    @pytest.mark.parametrize("step", ["start", "finish", "cancel"])
    def test_without_csrf_is_rejected(self, step):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        session_id, _principal = _signed_in(sessions)
        r = _client(app).post(f"/connect/peek/{step}", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 401

    @pytest.mark.parametrize("step", ["start", "finish", "cancel"])
    def test_cross_origin_is_rejected(self, step):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        session_id, _principal = _signed_in(sessions)
        r = _client(app).post(
            f"/connect/peek/{step}", data={"csrf": session_id},
            cookies={org_session.SESSION_COOKIE: session_id}, headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403

    def _post(self, client, session_id, step):
        return client.post(
            f"/connect/peek/{step}", data={"csrf": session_id}, cookies={org_session.SESSION_COOKIE: session_id},
        )

    def _page(self, client, session_id) -> str:
        return client.get("/connect", cookies={org_session.SESSION_COOKIE: session_id}).text

    def _start(self, monkeypatch, app, sessions):
        session_id, principal = _signed_in(sessions)
        client = _client(app)
        monkeypatch.setattr(peek_client, "start_device_login", lambda base: _login())
        r = self._post(client, session_id, "start")
        assert r.status_code == 303 and r.headers["location"] == "/connect"
        return client, session_id, principal

    def test_not_configured_shows_its_error_without_calling_peek(self, monkeypatch):
        app, sessions, _registry = _app()
        session_id, _principal = _signed_in(sessions)
        called = []
        monkeypatch.setattr(peek_client, "start_device_login", lambda base: called.append(base))
        client = _client(app)
        self._post(client, session_id, "start")
        assert called == []
        assert 'class="pf-service-chip">Peek<' not in self._page(client, session_id)

    def test_start_shows_the_code_and_the_open_link(self, monkeypatch):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        client, session_id, _principal = self._start(monkeypatch, app, sessions)
        page = self._page(client, session_id)
        assert "ABCD-1234" in page
        assert f'href="{_PEEK_SERVER}/cli/verify?code=ABCD-1234" target="_blank" rel="noopener noreferrer">Open Peek</a>' in page
        assert _PEEK_SECRET_CODE not in page

    def test_start_failure_shows_the_error(self, monkeypatch):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        session_id, _principal = _signed_in(sessions)

        def boom(base):
            raise peek_client.PeekClientError("Could not reach Peek at peek.example.com: ConnectionError")

        monkeypatch.setattr(peek_client, "start_device_login", boom)
        client = _client(app)
        self._post(client, session_id, "start")
        assert "Could not reach Peek at peek.example.com" in self._page(client, session_id)

    def test_finish_approved_saves_the_token_and_evicts(self, monkeypatch):
        evicted = []
        registry = _registry()
        monkeypatch.setattr(registry, "evict", lambda principal_id: evicted.append(principal_id))
        app, sessions, _unused = _app(org_config=_PEEK_ORG, registry=registry)
        client, session_id, principal = self._start(monkeypatch, app, sessions)
        seen = []

        def poll(base, device_code):
            seen.append((base, device_code))
            return peek_client.PeekLoginPoll(status="approved", token=_PEEK_SECRET_TOKEN)

        monkeypatch.setattr(peek_client, "poll_device_login", poll)
        r = self._post(client, session_id, "finish")
        assert r.status_code == 303
        assert seen == [(_PEEK_SERVER, _PEEK_SECRET_CODE)]
        saved = json.loads(_peek_token_path(principal).read_text())
        assert saved == {"base_url": _PEEK_SERVER, "token": _PEEK_SECRET_TOKEN}
        assert evicted == [principal.id]
        page = self._page(client, session_id)
        assert "Connected" in page.split('<li class="service peek card stack">', 1)[1]
        assert _PEEK_SECRET_TOKEN not in page and _PEEK_SECRET_CODE not in page

    def test_finish_saves_the_token_under_the_principal_lock(self, monkeypatch):
        app, sessions, _unused = _app(org_config=_PEEK_ORG)
        client, session_id, principal = self._start(monkeypatch, app, sessions)
        events = []

        class RecordingLock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, *exc):
                events.append("exit")

        locked_ids = []
        monkeypatch.setattr(
            peek_provisioning, "principal_lock", lambda principal_id: locked_ids.append(principal_id) or RecordingLock(),
        )
        real_save = peek_client.save_token_file

        def save(*args, **kwargs):
            events.append("save")
            return real_save(*args, **kwargs)

        monkeypatch.setattr(peek_client, "save_token_file", save)
        monkeypatch.setattr(
            peek_client, "poll_device_login",
            lambda base, code: peek_client.PeekLoginPoll(status="approved", token=_PEEK_SECRET_TOKEN),
        )
        self._post(client, session_id, "finish")
        assert locked_ids == [principal.id]
        assert events == ["enter", "save", "exit"]

    @pytest.mark.parametrize("status,message", [
        ("pending", "Peek has not recorded your approval yet."),
        ("denied", "The sign-in was denied in Peek."),
        ("expired", "The sign-in code expired. Start again."),
        ("consumed", "The sign-in code expired. Start again."),
    ])
    def test_finish_not_approved_shows_its_message(self, monkeypatch, status, message):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        client, session_id, principal = self._start(monkeypatch, app, sessions)
        monkeypatch.setattr(peek_client, "poll_device_login", lambda base, code: peek_client.PeekLoginPoll(status=status))
        self._post(client, session_id, "finish")
        page = self._page(client, session_id)
        assert message in page
        assert not _peek_token_path(principal).exists()
        assert (_PEEK_SECRET_CODE in page) is False
        # Pending keeps the approval step; the others start over.
        assert ("ABCD-1234" in page) is (status == "pending")

    def test_finish_poll_error_keeps_the_step(self, monkeypatch):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        client, session_id, _principal = self._start(monkeypatch, app, sessions)

        def boom(base, code):
            raise peek_client.PeekClientError("Peek answered with a redirect (HTTP 302). Check the Peek server address.")

        monkeypatch.setattr(peek_client, "poll_device_login", boom)
        self._post(client, session_id, "finish")
        page = self._page(client, session_id)
        assert "Peek answered with a redirect" in page and "ABCD-1234" in page

    def test_finish_past_expiry_resets(self, monkeypatch):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        session_id, _principal = _signed_in(sessions)
        client = _client(app)
        monkeypatch.setattr(peek_client, "start_device_login", lambda base: _login(expires_in=-1))
        polled = []
        monkeypatch.setattr(peek_client, "poll_device_login", lambda *a: polled.append(a))
        self._post(client, session_id, "start")
        self._post(client, session_id, "finish")
        assert polled == []
        assert "The sign-in code expired. Start again." in self._page(client, session_id)

    def test_finish_without_a_started_sign_in_just_redirects(self):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        session_id, _principal = _signed_in(sessions)
        r = self._post(_client(app), session_id, "finish")
        assert r.status_code == 303 and r.headers["location"] == "/connect"

    def test_cancel_clears_the_sign_in(self, monkeypatch):
        app, sessions, _registry = _app(org_config=_PEEK_ORG)
        client, session_id, _principal = self._start(monkeypatch, app, sessions)
        self._post(client, session_id, "cancel")
        page = self._page(client, session_id)
        assert "ABCD-1234" not in page and "Connect Peek" in page


# ---------------------------------------------------------------------------- #
# Grist
# ---------------------------------------------------------------------------- #

_GRIST_SERVER = "https://grist.example.com"
_GRIST_OAUTH = {"server_url": _GRIST_SERVER, "client_id": "gid", "client_secret": "gsecret"}
_GRIST_ENDPOINTS = grist_auth.GristOAuthEndpoints(
    authorization_endpoint="https://login.example.com/oidc/auth",
    token_endpoint="https://login.example.com/oidc/token",
    client_secret_basic=True,
)


def _grist_page(grist_section, *, token_record=None):
    org_config = {} if grist_section is None else {"grist": grist_section}
    app, sessions, _registry = _app(org_config=org_config)
    session_id, principal = _signed_in(sessions)
    if token_record is not None:
        token_file = paths.user_dir(principal) / "credentials" / "grist_token.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps(token_record))
    r = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id})
    assert r.status_code == 200
    return r.text


def _grist_box(html):
    return html.split("Grist</span>", 1)[1].split("</li>", 1)[0]


class TestGristBox:
    def test_without_a_section_it_is_not_listed(self):
        page = _grist_page(None)
        assert 'class="pf-service-chip">Grist<' not in page

    def test_a_section_the_bundle_cannot_use_is_a_not_set_up_row(self):
        box = _grist_box(_grist_page({"client_id": "x"}))
        assert "Not set up by your organization" in box
        assert "/oauth/start/grist" not in box

    def test_a_server_only_section_names_the_server_and_asks_for_a_key(self):
        box = _grist_box(_grist_page({"server_url": _GRIST_SERVER}))
        assert "Your organization uses https://grist.example.com." in box
        assert 'action="/connect/grist"' in box
        assert '<input class="field" type="password" name="api_key" autocomplete="off"' in box
        assert "/oauth/start/grist" not in box
        assert "Not set up by your organization" not in box

    def test_a_provisioned_key_shows_the_automatic_connection_line(self):
        record = {
            "auth": "api_key", "server_url": _GRIST_SERVER, "api_key": "k",
            "source": "provisioned", "managed": True, "expires_at": 1,
        }
        box = _grist_box(_grist_page({"server_url": _GRIST_SERVER}, token_record=record))
        assert "Your organization connected your Grist account automatically." in box
        assert "Connected to https://grist.example.com." in box

    def test_a_key_file_that_cannot_be_read_still_renders_the_box(self, monkeypatch):
        record = {"auth": "api_key", "server_url": _GRIST_SERVER, "api_key": "k"}

        def unreadable(_path):
            raise rc.grist_auth.GristClientError("unreadable")

        monkeypatch.setattr(rc, "_is_connected", lambda *a, **k: True)
        monkeypatch.setattr(rc.grist_auth, "load_token_file", unreadable)
        box = _grist_box(_grist_page({"server_url": _GRIST_SERVER}, token_record=record))
        assert "Connected to https://grist.example.com." in box
        assert "connected your Grist account automatically" not in box

    def test_a_manual_key_does_not_show_the_automatic_connection_line(self):
        record = {"auth": "api_key", "server_url": _GRIST_SERVER, "api_key": "k"}
        box = _grist_box(_grist_page({"server_url": _GRIST_SERVER}, token_record=record))
        assert "connected your Grist account automatically" not in box
        assert "Connected to https://grist.example.com." in box

    def test_an_oauth_section_offers_connect(self):
        box = _grist_box(_grist_page(_GRIST_OAUTH))
        assert 'href="/oauth/start/grist"' in box
        assert ">Connect</a>" in box

    def test_a_valid_oauth_credential_shows_connected_and_reconnect(self):
        record = {
            "auth": "oauth", "server_url": _GRIST_SERVER, "access_token": "a", "refresh_token": "r", "expires_at": 1.0,
        }
        box = _grist_box(_grist_page(_GRIST_OAUTH, token_record=record))
        assert "Connected" in box
        assert ">Reconnect</a>" in box

    def test_an_api_key_left_over_from_before_oauth_is_not_connected(self):
        record = {"auth": "api_key", "server_url": _GRIST_SERVER, "api_key": "k"}
        box = _grist_box(_grist_page(_GRIST_OAUTH, token_record=record))
        assert "Not connected" in box
        assert ">Connect</a>" in box

    def test_a_credential_for_another_server_is_not_connected(self):
        record = {
            "auth": "oauth", "server_url": "https://other.example.com",
            "access_token": "a", "refresh_token": "r", "expires_at": 1.0,
        }
        box = _grist_box(_grist_page(_GRIST_OAUTH, token_record=record))
        assert "Not connected" in box


class TestGristOAuth:
    def _start(self, monkeypatch, org_config):
        app, sessions, registry = _app(org_config=org_config)
        session_id, principal = _signed_in(sessions)
        monkeypatch.setattr(grist_auth, "discover", lambda auth_url, server_url: _GRIST_ENDPOINTS)
        client = _client(app)
        r = client.get("/oauth/start/grist", cookies={org_session.SESSION_COOKIE: session_id})
        client.cookies.set(org_session.SESSION_COOKIE, session_id)
        return client, r, registry, principal

    def test_start_redirects_to_the_discovered_endpoint_with_pkce_and_scopes(self, monkeypatch):
        _client_, r, _registry, _principal = self._start(monkeypatch, {"grist": _GRIST_OAUTH})
        assert r.status_code == 302
        location = r.headers["location"]
        assert location.startswith("https://login.example.com/oidc/auth?")
        qs = dict(up.parse_qsl(up.urlparse(location).query))
        assert qs["client_id"] == "gid"
        assert qs["redirect_uri"] == f"{ISSUER}/oauth/callback/grist"
        assert qs["prompt"] == "consent"
        assert qs["code_challenge_method"] == "S256" and qs["code_challenge"]
        assert qs["scope"] == grist_auth.GRIST_SCOPES

    def test_start_passes_the_bundles_servers_to_discovery(self, monkeypatch):
        seen = []
        app, sessions, _registry = _app(org_config={"grist": {**_GRIST_OAUTH, "auth_server_url": "https://auth.example.com"}})
        session_id, _principal = _signed_in(sessions)
        monkeypatch.setattr(
            grist_auth, "discover", lambda auth_url, server_url: seen.append((auth_url, server_url)) or _GRIST_ENDPOINTS,
        )
        _client(app).get("/oauth/start/grist", cookies={org_session.SESSION_COOKIE: session_id})
        assert seen == [("https://auth.example.com", _GRIST_SERVER)]

    def test_start_without_an_app_is_not_configured(self, monkeypatch):
        _client_, r, _registry, _principal = self._start(monkeypatch, {"grist": {"server_url": _GRIST_SERVER}})
        assert r.status_code == 302
        assert r.headers["location"] == "/connect?error=grist"

    def test_start_with_unusable_sign_in_settings_redirects_back_with_error(self, monkeypatch):
        app, sessions, _registry = _app(org_config={"grist": _GRIST_OAUTH})
        session_id, _principal = _signed_in(sessions)

        def boom(auth_url, server_url):
            raise grist_auth.GristClientError("not usable")

        monkeypatch.setattr(grist_auth, "discover", boom)
        r = _client(app).get("/oauth/start/grist", cookies={org_session.SESSION_COOKIE: session_id})
        assert r.headers["location"] == "/connect?error=grist"

    def test_callback_exchanges_the_code_and_saves_the_credential(self, monkeypatch):
        client, start, registry, principal = self._start(monkeypatch, {"grist": _GRIST_OAUTH})
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]
        calls = []

        def fake_exchange(config, endpoints, code, redirect_uri, code_verifier):
            calls.append((config.client_id, code, redirect_uri, bool(code_verifier)))
            return {
                "auth": "oauth", "server_url": config.server_url,
                "access_token": "at", "refresh_token": "rt", "expires_at": 9.0,
            }

        monkeypatch.setattr(grist_auth, "exchange_code", fake_exchange)
        evicted = []
        monkeypatch.setattr(registry, "evict", lambda pid: evicted.append(pid))

        r = client.get(f"/oauth/callback/grist?code=c1&state={state}")

        assert r.headers["location"] == "/connect?connected=grist"
        assert calls == [("gid", "c1", f"{ISSUER}/oauth/callback/grist", True)]
        saved = json.loads((paths.user_dir(principal) / "credentials" / "grist_token.json").read_text())
        assert saved["auth"] == "oauth" and saved["refresh_token"] == "rt"
        assert evicted == [principal.id]


class TestMiroOAuth:
    def _start(self, org_config=None):
        app, sessions, registry = _app(org_config=_ORG_CONFIG if org_config is None else org_config)
        session_id, principal = _signed_in(sessions)
        client = _client(app)
        r = client.get("/oauth/start/miro", cookies={org_session.SESSION_COOKIE: session_id})
        client.cookies.set(org_session.SESSION_COOKIE, session_id)
        return client, r, registry, principal, session_id

    def test_row_is_not_configured_without_a_section_and_connectable_with_one(self):
        app, sessions, _registry = _app(org_config={"slack": _ORG_CONFIG["slack"]})
        session_id, _principal = _signed_in(sessions)
        page = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id}).text
        assert "/oauth/start/miro" not in page and "Miro" not in page

        app, sessions, _registry = _app(org_config={"miro": _ORG_CONFIG["miro"]})
        session_id, _principal = _signed_in(sessions)
        page = _client(app).get("/connect", cookies={org_session.SESSION_COOKIE: session_id}).text
        assert 'class="pf-service-chip">Miro<' in page
        assert 'href="/oauth/start/miro"' in page

    def test_start_redirects_to_miro_with_state_and_no_scope(self):
        _client_, r, _registry, _principal, _sid = self._start()
        assert r.status_code == 302
        location = r.headers["location"]
        assert location.startswith("https://miro.com/oauth/authorize?")
        qs = dict(up.parse_qsl(up.urlparse(location).query))
        assert qs["client_id"] == "m"
        assert qs["redirect_uri"] == f"{ISSUER}/oauth/callback/miro"
        assert qs["state"]
        assert "scope" not in qs and "code_challenge" not in qs

    def test_start_without_a_section_is_not_configured(self):
        _client_, r, _registry, _principal, _sid = self._start({"slack": _ORG_CONFIG["slack"]})
        assert r.headers["location"] == "/connect?error=miro"

    def test_callback_exchanges_the_code_and_saves_the_token(self, monkeypatch):
        client, start, registry, principal, _sid = self._start()
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]
        calls = []

        def fake_exchange(config, code, redirect_uri):
            calls.append((config.client_id, code, redirect_uri))
            return {
                "access_token": "at", "refresh_token": "rt", "expires_at": 9.0, "team_id": "t1",
                "user_id": "u1", "scope": "boards:read boards:write",
            }

        monkeypatch.setattr(miro_auth, "exchange_code", fake_exchange)
        evicted = []
        monkeypatch.setattr(registry, "evict", lambda pid: evicted.append(pid))

        r = client.get(f"/oauth/callback/miro?code=c1&state={state}")

        assert r.headers["location"] == "/connect?connected=miro"
        assert calls == [("m", "c1", f"{ISSUER}/oauth/callback/miro")]
        saved = json.loads((paths.user_dir(principal) / "credentials" / "miro_token.json").read_text())
        assert saved["refresh_token"] == "rt" and saved["team_id"] == "t1"
        assert evicted == [principal.id]

    def test_a_failed_exchange_takes_the_sign_in_failed_path(self, monkeypatch):
        client, start, _registry, principal, _sid = self._start()
        state = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))["state"]

        def boom(config, code, redirect_uri):
            raise miro_auth.MiroClientError("Miro sign-in failed: bad code")

        monkeypatch.setattr(miro_auth, "exchange_code", boom)
        r = client.get(f"/oauth/callback/miro?code=c1&state={state}")
        assert r.headers["location"] == "/connect?error=miro"
        assert not (paths.user_dir(principal) / "credentials" / "miro_token.json").exists()


class TestGristApiKey:
    _KEY = "sekret-key-123"

    def _setup(self, org_config=None):
        app, sessions, registry = _app(org_config={"grist": {"server_url": _GRIST_SERVER}} if org_config is None else org_config)
        session_id, principal = _signed_in(sessions)
        return _client(app), session_id, principal, registry

    def _post(self, client, session_id, **data):
        return client.post(
            "/connect/grist", data={"csrf": session_id, **data}, cookies={org_session.SESSION_COOKIE: session_id},
        )

    def _page(self, client, session_id):
        return client.get("/connect", cookies={org_session.SESSION_COOKIE: session_id}).text

    @staticmethod
    def _token_file(principal):
        return paths.user_dir(principal) / "credentials" / "grist_token.json"

    def test_a_post_after_the_session_ended_lands_on_the_signed_out_page(self):
        app, _sessions, _registry = _app()
        r = _client(app).post("/connect/grist", data={"csrf": "stale", "api_key": "k"})
        assert r.status_code == 303
        assert r.headers["location"] == "/signed-out"

    def test_without_csrf_is_rejected(self):
        client, session_id, principal, _registry = self._setup()
        r = client.post("/connect/grist", data={"api_key": "k"}, cookies={org_session.SESSION_COOKIE: session_id})
        assert r.status_code == 401
        assert not self._token_file(principal).exists()

    def test_cross_origin_is_rejected(self):
        client, session_id, principal, _registry = self._setup()
        r = client.post(
            "/connect/grist", data={"csrf": session_id, "api_key": "k"},
            cookies={org_session.SESSION_COOKIE: session_id}, headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert not self._token_file(principal).exists()

    @pytest.mark.parametrize("section", [None, {}, {"client_id": "x"}])
    def test_no_usable_section_writes_nothing(self, monkeypatch, section):
        org_config = {} if section is None else {"grist": section}
        client, session_id, principal, _registry = self._setup(org_config)
        monkeypatch.setattr(rc.GristClient, "check_connection", lambda self: pytest.fail("must not be called"))
        r = self._post(client, session_id, api_key=self._KEY)
        assert r.status_code == 303 and r.headers["location"] == "/connect"
        if section:  # the row is listed, so the error shows under it; without a section Grist isn't listed
            assert "Grist is not set up by your organization." in self._page(client, session_id)
        assert not self._token_file(principal).exists()

    def test_an_oauth_app_in_the_bundle_refuses_the_key(self, monkeypatch):
        client, session_id, principal, _registry = self._setup({"grist": _GRIST_OAUTH})
        monkeypatch.setattr(rc.GristClient, "check_connection", lambda self: pytest.fail("must not be called"))
        self._post(client, session_id, api_key=self._KEY)
        page = self._page(client, session_id)
        assert "Your organization connects to Grist with OAuth. Use Connect." in page
        assert not self._token_file(principal).exists()

    def test_an_empty_key_shows_its_error_on_the_next_page(self):
        client, session_id, principal, _registry = self._setup()
        self._post(client, session_id, api_key="   ")
        assert "Enter your Grist API key." in self._page(client, session_id)
        assert not self._token_file(principal).exists()

    def test_success_saves_the_key_evicts_connectors_and_shows_connected(self, monkeypatch):
        client, session_id, principal, registry = self._setup()
        monkeypatch.setattr(rc.GristClient, "check_connection", lambda self: "grist.example.com")
        evicted = []
        monkeypatch.setattr(registry, "evict", evicted.append)
        r = self._post(client, session_id, api_key=f" {self._KEY} ")
        assert r.status_code == 303 and r.headers["location"] == "/connect"
        assert r.headers["cache-control"] == "no-store"
        assert json.loads(self._token_file(principal).read_text()) == {
            "auth": "api_key", "server_url": _GRIST_SERVER, "api_key": self._KEY,
        }
        assert evicted == [principal.id]
        page = self._page(client, session_id)
        assert f"Connected to {_GRIST_SERVER}" in page
        assert ">Reconnect</button>" in page
        assert self._KEY not in page and self._KEY not in r.text

    def test_the_save_happens_under_the_principal_lock(self, monkeypatch):
        client, session_id, principal, _registry = self._setup()
        monkeypatch.setattr(rc.GristClient, "check_connection", lambda self: "grist.example.com")
        events = []

        class RecordingLock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, *exc):
                events.append("exit")

        asked = []
        monkeypatch.setattr(
            grist_provisioning, "principal_lock", lambda principal_id: asked.append(principal_id) or RecordingLock(),
        )
        real_save = grist_auth.save_api_key

        def save(*args, **kwargs):
            events.append("save")
            return real_save(*args, **kwargs)

        monkeypatch.setattr(grist_auth, "save_api_key", save)
        self._post(client, session_id, api_key=self._KEY)
        assert asked == [principal.id]
        assert events == ["enter", "save", "exit"]
        assert self._token_file(principal).exists()

    def test_a_failed_check_writes_nothing_and_shows_the_error(self, monkeypatch):
        client, session_id, principal, _registry = self._setup()

        def refuse(self):
            raise grist_auth.GristClientError("Grist rejected the API key.")

        monkeypatch.setattr(rc.GristClient, "check_connection", refuse)
        r = self._post(client, session_id, api_key=self._KEY)
        assert r.headers["location"] == "/connect"
        page = self._page(client, session_id)
        assert "Grist rejected the API key." in page
        assert self._KEY not in page and self._KEY not in r.text
        assert not self._token_file(principal).exists()

    def test_a_later_success_clears_the_error(self, monkeypatch):
        client, session_id, _principal, _registry = self._setup()
        self._post(client, session_id, api_key="")
        monkeypatch.setattr(rc.GristClient, "check_connection", lambda self: "grist.example.com")
        self._post(client, session_id, api_key=self._KEY)
        assert "Enter your Grist API key." not in self._page(client, session_id)
