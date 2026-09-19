"""Tests for web/routes_approvals.py -- the approval list/card/decide
routes, exercised against an in-process ASGI test client (no real socket).
Auth middleware, CSRF, and Host/Origin policy each get explicit negative
tests here.

SEC-06: this module's own create_app() no longer takes a shared ``token``
-- it authenticates
against a web/session_auth.py ``LocalSessionStore`` instead (the one-time
``?bootstrap=`` exchange that actually mints a session lives one layer up,
in web/server.py's ``_BootstrapMiddleware`` -- see test_server.py for that).
Every test here that used to authenticate via ``?token=`` now does so the
same way test_routes_org_approvals.py's own ``_signed_in`` does for
OrgSessionStore: create a session directly and set the cookie.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from privacyfence import paths
from privacyfence import webauthn_stepup as wa
from privacyfence.step_up_config import StepUpConfig
from privacyfence.web.routes_approvals import _inject_shim, create_app
from privacyfence.web.session_auth import (
    PROVENANCE_HUMAN,
    PROVENANCE_UNATTESTED,
    SESSION_COOKIE,
    LocalSessionStore,
)
from privacyfence.web_approval_ui import WebApprovalUI

ORIGIN = "http://localhost"


@pytest.fixture
def web_ui():
    return WebApprovalUI()


@pytest.fixture
def sessions():
    return LocalSessionStore()


@pytest.fixture
def client(web_ui, sessions):
    app = create_app(web_ui, sessions=sessions)
    return TestClient(app, base_url="http://localhost")


def _signed_in(client: TestClient, sessions: LocalSessionStore) -> str:
    session_id = sessions.create()
    client.cookies.set(SESSION_COOKIE, session_id)
    return session_id


def _pending_card(web_ui, **kwargs):
    """Starts a blocking show_popup() call on a background thread and
    waits for its card to register -- returns (thread, card). daemon=True:
    if a test's own assertion fails before it resolves the card (or calls
    t.join()), this thread would otherwise block forever on the unresolved
    card's Event -- a non-daemon thread left running like that hangs the
    whole pytest process at interpreter exit, not just fails the one test.

    Waits for a card *this call* registered, by diffing against the ids
    already pending before the thread started, rather than on
    ``web_ui.current() is None``: current() is "the newest pending
    approval", so once anything else is pending that wait finishes
    instantly and hands back the *earlier* card. test_two_pending_cards_
    both_link below is the call site that actually depends on the
    difference -- with the old wait its two cards were frequently the same
    object, and its "the list must show all of them, not just the most
    recent" assertions then both checked the same link and passed without
    ever exercising what the test exists for. (The same bug made
    tests/integration/test_browser_smoke.py's own two-card SSE test fail in
    CI; see that module's _await_new_registration.)
    """
    box = {}

    def run():
        box["result"] = web_ui.show_popup("Send email", {"To": "a@b.com"}, "body text", **kwargs)

    already_pending = {a.id for a in web_ui.deferred_registry.list_pending()}
    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.monotonic() + 2
    card = None
    while time.monotonic() < deadline:
        fresh = [a for a in web_ui.deferred_registry.list_pending() if a.id not in already_pending]
        if fresh:
            card = fresh[0]
            break
        time.sleep(0.01)
    assert card is not None, "card never registered"
    return t, card, box


class TestInjectShim:
    """Regression coverage for a real bug found by actually clicking Allow
    in headless Chromium: a naive html.replace("<body>", ..., 1) lands the
    shim wherever "<body>" first appears in the raw string -- and
    styles.css's own CSS comments genuinely contain that literal substring
    (e.g. "the same-colored rail on <body>") inside the document's one
    <style> block, well before the real tag. The browser then parses the
    injected <script> as inert CSS text, not a script element: it silently
    never runs, so window.webkit stays undefined and clicking any button
    has no effect at all -- no console error, no failed request, nothing.
    _inject_shim must only ever match the real <body>, after </head>
    closes.
    """

    def test_a_body_like_string_inside_a_style_comment_before_head_close_is_not_matched(self):
        html = (
            "<html><head><style>/* mentions <body> in a comment */</style></head>"
            "<body>REAL CONTENT</body></html>"
        )
        shimmed = _inject_shim(html, "<script>SHIM</script>")
        assert shimmed == (
            "<html><head><style>/* mentions <body> in a comment */</style></head>"
            "<body><script>SHIM</script>REAL CONTENT</body></html>"
        )

    def test_lands_after_head_close_and_before_the_real_body_content(self):
        from privacyfence.card_builder import build_card_html

        html = build_card_html(title="t", preview={}, details_text="d", is_read=True, layout="narrow")
        shimmed = _inject_shim(html, "<script>SHIM_MARKER</script>")
        head_close = shimmed.index("</head>")
        style_close = shimmed.index("</style>")
        # Deliberately searched from head_close on, not shimmed.index("<body>")
        # from the start -- the real card template's own styles.css comments
        # contain the literal substring "<body>" earlier in the document
        # (inside the <style> block), which is exactly the bug this test
        # exists to catch; a naive search here would hide it, not prove it.
        body_open = shimmed.index("<body>", head_close)
        shim_pos = shimmed.index("SHIM_MARKER")
        assert style_close < head_close < body_open < shim_pos


class TestAuthentication:
    def test_unauthenticated_list_is_rejected(self, client):
        r = client.get("/approvals")
        assert r.status_code == 401

    def test_valid_session_cookie_authenticates(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert r.status_code == 200

    def test_unknown_session_cookie_is_rejected(self, client):
        client.cookies.set(SESSION_COOKIE, "not-a-real-session-id")
        r = client.get("/approvals")
        assert r.status_code == 401

    def test_session_cookie_alone_authenticates_a_later_request(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert r.status_code == 200

    def test_unauthorized_page_offers_an_on_demand_recovery_command(self, client):
        # A dead/expired link shouldn't just tell a human to restart
        # PrivacyFence -- the control channel (web/control_channel.py,
        # #428 Phase 2) exists precisely so they don't have to, and this
        # page is where that needs to actually be spelled out (see
        # session_auth.unauthorized_html's own docstring). "MINT" is the
        # one substring present on either platform's command (nc -U on
        # POSIX, PowerShell's NamedPipeClientStream on Windows).
        r = client.get("/approvals")
        assert r.status_code == 401
        assert "MINT" in r.text
        assert "api/bootstrap" not in r.text


class TestListApprovals:
    def test_nothing_pending(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert "Nothing is waiting" in r.text

    def test_one_pending_card_links_to_it(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        t, card, box = _pending_card(web_ui)
        r = client.get("/approvals")
        assert f"/approvals/{card.id}" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_response_is_never_cached(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert r.headers.get("cache-control") == "no-store"

    def test_wrapped_in_the_shared_shell(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert "pf-shell-nav" in r.text
        assert 'class="pf-shell-nav-item active" href="/approvals"' in r.text

    def test_notifications_enabled_config_reaches_the_page(self, web_ui, sessions):
        from privacyfence.web.routes_approvals import create_app

        app = create_app(web_ui, sessions=sessions, notifications_enabled=False)
        c = TestClient(app, base_url="http://localhost")
        _signed_in(c, sessions)
        r = c.get("/approvals")
        assert "NOTIFICATIONS_ENABLED = false" in r.text

    def test_empty_state_names_the_first_run_case(self, web_ui, sessions):
        from privacyfence.web.routes_approvals import create_app

        app = create_app(web_ui, sessions=sessions, any_connector_authenticated=lambda: False)
        c = TestClient(app, base_url="http://localhost")
        _signed_in(c, sessions)
        r = c.get("/approvals")
        assert "Nothing is governed yet." in r.text
        assert "/settings/connectors" in r.text

    def test_empty_state_is_re_evaluated_per_request(self, web_ui, sessions):
        # Authenticating a connector has to take effect on the next page
        # load, not the next daemon restart.
        from privacyfence.web.routes_approvals import create_app

        authed = {"value": False}
        app = create_app(
            web_ui, sessions=sessions, any_connector_authenticated=lambda: authed["value"],
        )
        c = TestClient(app, base_url="http://localhost")
        _signed_in(c, sessions)
        assert "Nothing is governed yet." in c.get("/approvals").text
        authed["value"] = True
        assert "Nothing is waiting" in c.get("/approvals").text

    def test_pending_card_row_has_a_deny_button_and_review_link(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        t, card, box = _pending_card(web_ui)
        r = client.get("/approvals")
        assert f'data-deny="{card.id}"' in r.text
        assert f'href="/approvals/{card.id}"' in r.text
        assert ">Allow<" not in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_two_pending_cards_both_link(self, client, sessions, web_ui):
        # P3: several approvals can be pending at once (§6's retirement of
        # _popup_lock's "one dialog at a time") -- the list must show all of
        # them, not just the most recent.
        _signed_in(client, sessions)
        t1, card1, box1 = _pending_card(web_ui)
        t2, card2, box2 = _pending_card(web_ui)
        # Guards this test against silently proving nothing: if the helper
        # ever hands back the same card twice (as it did before it stopped
        # waiting on current() -- see _pending_card), both assertions below
        # would check the same link and pass with only one row ever
        # rendered, which is exactly the regression this test exists for.
        assert card1.id != card2.id, "the helper handed back the same card twice"
        r = client.get("/approvals")
        assert f"/approvals/{card1.id}" in r.text
        assert f"/approvals/{card2.id}" in r.text
        web_ui.resolve(card1.id, "deny")
        web_ui.resolve(card2.id, "deny")
        t1.join(timeout=2)
        t2.join(timeout=2)


class TestServiceWorker:
    """W8 (tier 0/1 notifications, docs/approval-list-ui-ux.md §4): served
    at the origin root with no auth required, so registration never fails
    on a session that hasn't authenticated yet."""

    def test_served_with_no_auth_required(self, client):
        r = client.get("/sw.js")
        assert r.status_code == 200

    def test_correct_content_type_and_scope_header(self, client):
        r = client.get("/sw.js")
        assert "javascript" in r.headers.get("content-type", "")
        assert r.headers.get("service-worker-allowed") == "/"

    def test_no_push_handler(self, client):
        # This phase is tier 0/1 only -- no `push` event handler (that's
        # tier 2, VAPID, org mode/P7+).
        r = client.get("/sw.js")
        assert "addEventListener(\"push\"" not in r.text
        assert "addEventListener('push'" not in r.text


class TestApprovalsStream:
    """GET /api/approvals/stream -- §7.1's SSE counterpart to the list page,
    so it updates live without polling. Only the auth boundary is exercised
    here: the handler's own generator loops until the client disconnects,
    which starlette.testclient.TestClient's synchronous, fully-buffering
    ``stream()`` (it drains an ASGI response before yielding control back,
    even under ``with client.stream(...)``) can't drive without hanging --
    a real streaming HTTP client is what this endpoint actually needs to be
    exercised end-to-end, which is what P0/P1's own manual Chromium checks
    already cover the pattern for, not this test client."""

    def test_requires_auth(self, client):
        r = client.get("/api/approvals/stream")
        assert r.status_code == 401


class TestApprovalPreview:
    """GET /api/approvals/{id}/preview -- the approval binder's read-only
    inline-disclosure fragment (Phase 1): serves the ``preview`` dict
    stamped onto a ``PendingApproval`` at registration, metadata only."""

    def test_requires_auth(self, client, web_ui):
        thread, card, _box = _pending_card(web_ui)
        try:
            r = client.get(f"/api/approvals/{card.id}/preview")
            assert r.status_code == 401
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_unknown_id_is_404(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/api/approvals/nope/preview")
        assert r.status_code == 404

    def test_returns_the_stamped_preview_dict(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        approval, _ = web_ui.deferred_registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="gmail_get_message", gate_kind="review",
            request_id="r1", preview={"From": "alice@example.com", "Subject": "hi"},
        )
        r = client.get(f"/api/approvals/{approval.id}/preview")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == approval.id
        assert body["preview"] == {"From": "alice@example.com", "Subject": "hi"}

    def test_never_carries_details_text_or_html(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        thread, card, _box = _pending_card(web_ui)
        try:
            r = client.get(f"/api/approvals/{card.id}/preview")
            body = r.json()
            assert set(body.keys()) == {"id", "preview"}
            assert "details_text" not in body
            assert "html" not in body
            assert "summary" not in body
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)


class TestShowApproval:
    def test_unknown_id_says_no_longer_pending_not_404(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/approvals/does-not-exist")
        assert r.status_code == 200
        assert "no longer pending" in r.text

    def test_pending_card_renders_with_the_bridge_shim_injected(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        t, card, box = _pending_card(web_ui, connector="gmail")
        r = client.get(f"/approvals/{card.id}")
        assert "window.webkit.messageHandlers.pf" in r.text
        assert f"/api/approvals/{card.id}/decide" in r.text
        assert "Send email" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_unauthenticated_show_is_rejected(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals/{card.id}")
        assert r.status_code == 401
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_no_longer_pending_page_links_back_to_the_list(self, client, sessions):
        _signed_in(client, sessions)
        r = client.get("/approvals/does-not-exist")
        assert 'href="/approvals"' in r.text

    def test_shim_navigates_back_to_the_list_on_success_not_innerhtml(self, client, sessions, web_ui):
        # §3 of docs/approval-list-ui-ux.md: a decision navigates back to
        # /approvals via location.replace (so the back button can't walk
        # into a dead card) with a toast stashed in sessionStorage, instead
        # of rewriting the document body in place.
        _signed_in(client, sessions)
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals/{card.id}")
        assert "location.replace('/approvals')" in r.text
        assert "sessionStorage.setItem('pf_toast'" in r.text
        assert "document.body.innerHTML = r.ok" not in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_shim_handles_the_409_already_decided_case(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals/{card.id}")
        assert "r.status === 409" in r.text
        assert "Already decided elsewhere" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_a_registered_but_not_yet_rendered_card_shows_a_placeholder_not_a_500(self, client, sessions, web_ui):
        # Regression: card HTML is only built inside gate.py's
        # _popup_executor (build_card_html runs from within show_popup/
        # show_read_popup, on that worker thread). Past that executor's
        # worker count, a newly registered approval is listed and
        # decidable but genuinely has card.html == "" until a worker frees
        # up -- _inject_shim's first statement is html.index("</head>"),
        # which raised ValueError on an empty string. This must never 500.
        _signed_in(client, sessions)
        approval, _ = web_ui.deferred_registry.register_or_coalesce(
            dedupe_key="k1", connector="gmail", tool="t", gate_kind="review", request_id="r1",
        )
        assert approval.html == ""
        r = client.get(f"/approvals/{approval.id}")
        assert r.status_code == 200
        assert "Preparing this request" in r.text
        assert 'href="/approvals"' in r.text


class TestDecide:
    def test_happy_path_releases_the_blocked_gate_call(self, client, sessions, web_ui):
        t, card, box = _pending_card(web_ui, accept_all_choices=[("always_allow", "")])
        session_id = _signed_in(client, sessions)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "accept_all", "choice": 0, "csrf": session_id},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        t.join(timeout=2)
        assert box["result"] == ("accept_all", 0)

    def test_missing_csrf_is_rejected(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.post(f"/api/approvals/{card.id}/decide", json={"action": "resolve", "result": "deny"})
        assert r.status_code == 401
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_csrf_not_matching_the_session_cookie_is_rejected(self, client, sessions, web_ui):
        t, card, box = _pending_card(web_ui)
        _signed_in(client, sessions)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "deny", "csrf": "wrong-value"},
        )
        assert r.status_code == 401
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_second_decide_for_the_same_id_is_rejected_not_silently_applied(self, client, sessions, web_ui):
        # Idempotent: the first accepted decision wins (§7.1). Also what
        # protects against a stale tab and a genuine double-submit.
        t, card, box = _pending_card(web_ui)
        session_id = _signed_in(client, sessions)
        first = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "accept", "csrf": session_id},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "deny", "csrf": session_id},
        )
        assert second.status_code == 409
        t.join(timeout=2)
        assert box["result"] == ("accept", None)  # the second POST never overturned the first

    def test_cross_origin_request_is_rejected(self, client, sessions, web_ui):
        t, card, box = _pending_card(web_ui)
        session_id = _signed_in(client, sessions)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "deny", "csrf": session_id},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_unknown_id_is_rejected_without_touching_the_real_pending_card(self, client, sessions, web_ui):
        t, card, box = _pending_card(web_ui)
        session_id = _signed_in(client, sessions)
        r = client.post(
            "/api/approvals/not-the-real-id/decide",
            json={"action": "resolve", "result": "deny", "csrf": session_id},
        )
        assert r.status_code == 409
        web_ui.resolve(card.id, "accept")
        t.join(timeout=2)
        assert box["result"] == ("accept", None)

    def test_malformed_json_body_is_rejected(self, client, sessions, web_ui):
        t, card, box = _pending_card(web_ui)
        _signed_in(client, sessions)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)


def _register(web_ui: WebApprovalUI, *, gate_kind: str = "review", pii_detected: bool = False, dedupe_key: str = "k1"):
    """The local-mode counterpart of test_routes_org_approvals.py's own
    ``_register`` -- no ``principal_scope`` needed, since local mode's
    ``current_principal()`` already defaults to ``LOCAL_PRINCIPAL``."""
    approval, _ = web_ui.deferred_registry.register_or_coalesce(
        dedupe_key=dedupe_key, connector="gmail", tool="gmail_get_message", gate_kind=gate_kind,
        request_id="r1", summary="a message", tool_name="Get message", pii_detected=pii_detected,
    )
    web_ui.deferred_registry.set_html(approval.id, "<!doctype html><html><head></head><body>CARD</body></html>")
    return approval


def _app(*, step_up: StepUpConfig):
    web_ui = WebApprovalUI()
    sessions = LocalSessionStore()
    app = create_app(web_ui, sessions=sessions, step_up=step_up, step_up_origin=ORIGIN)
    return app, sessions, web_ui


def _client(app) -> TestClient:
    return TestClient(app, base_url=ORIGIN)


class TestStepUpScoping:
    """#426 Phase 2: step-up is off by default, and even when enabled,
    applies only to approving decisions on writes (or PII reads, in the
    wider scope) -- never to denies. Mirrors
    test_routes_org_approvals.py's own TestStepUpScoping, minus the IdP
    fallback local mode has no equivalent of."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _enroll(self):
        from privacyfence.principal import LOCAL_PRINCIPAL

        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def test_disabled_step_up_never_blocks_a_write_accept(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=False))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

    def test_deny_never_needs_step_up_even_on_a_write(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 200

    def test_plain_read_never_needs_step_up_in_default_scope(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost", scope="writes"))
        approval = _register(web_ui, gate_kind="review", pii_detected=True)
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

    def test_pii_read_needs_step_up_in_the_wider_scope(self):
        self._enroll()
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", scope="writes_and_pii_reads"),
        )
        approval = _register(web_ui, gate_kind="review", pii_detected=True)
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428

    def test_unflagged_read_needs_step_up_only_in_the_widest_scope(self):
        """The gap ``writes_and_pii_reads`` leaves: a read pii_detector.py
        never flagged is still a disclosure, and ``writes_and_reads`` is
        what covers it. Same approval, same principal, both scopes --
        org mode's own counterpart asserts this pair identically."""
        self._enroll()
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", scope="writes_and_pii_reads"),
        )
        client = _client(app)
        session_id = _signed_in(client, sessions)
        approval = _register(web_ui, gate_kind="review", pii_detected=False)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", scope="writes_and_reads"),
        )
        client = _client(app)
        session_id = _signed_in(client, sessions)
        approval = _register(web_ui, gate_kind="review", pii_detected=False)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428

    def test_denying_an_unflagged_read_in_the_widest_scope_still_needs_nothing(self):
        self._enroll()
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", scope="writes_and_reads"),
        )
        approval = _register(web_ui, gate_kind="review", pii_detected=False)
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 200


class TestStepUpEvadableWithNoPasskeyEnrolled:
    """#426 Phase 2's own deliberate gap: with no passkey enrolled and no
    IdP to fall back to (unlike org mode), the decide endpoint has no
    ceremony left to demand -- the decision goes through unguarded rather
    than deadlocking behind one nobody can complete. Closing this is #426
    Phase 3's ``require_passkey`` enforcement, not this one."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_a_write_accept_with_step_up_enabled_but_nothing_enrolled_still_succeeds(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestRequirePasskeyBanner:
    """#426 Phase 3: the list page carries step_up_config.py's own "loud
    persistent banner" exactly when ``local_enrollment_banner()`` says to --
    see that function's own tests (test_step_up_config.py) for the
    condition itself, and web_shell.py's TestBanner for the markup."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_no_banner_when_step_up_is_off(self):
        app, sessions, _web_ui = _app(step_up=StepUpConfig(enabled=False, require_passkey=True))
        client = _client(app)
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert '<div class="pf-shell-banner"' not in r.text

    def test_banner_shown_when_require_passkey_unmet(self):
        app, sessions, _web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True))
        client = _client(app)
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert '<div class="pf-shell-banner"' in r.text
        assert "/security" in r.text

    def test_no_banner_once_a_credential_is_enrolled(self):
        from privacyfence.principal import LOCAL_PRINCIPAL

        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        app, sessions, _web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True))
        client = _client(app)
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert '<div class="pf-shell-banner"' not in r.text

    def test_disabled_requirement_notice_is_shown(self):
        """#426 Phase 4: webauthn_stepup.observe_step_up_requirement's own
        persistent notice, surfaced the same way the Phase 3 enrollment
        one is -- see test_routes_settings.py's own copy of this test."""
        from privacyfence.principal import LOCAL_PRINCIPAL

        wa.observe_step_up_requirement(LOCAL_PRINCIPAL, enabled=True, require_passkey=True)
        wa.observe_step_up_requirement(LOCAL_PRINCIPAL, enabled=True, require_passkey=False)
        app, sessions, _web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=False))
        client = _client(app)
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert '<div class="pf-shell-banner"' in r.text
        assert "turned off" in r.text


class TestStepUpOffNotice:
    """B23 of the 4.1.0 action plan: the list page carries step_up_
    config.py's own ``off_notice()`` as a dismissible strip (web_shell.
    wrap's ``dismissible_notice_html``) exactly when step-up isn't
    genuinely required -- see that function's own tests
    (test_step_up_config.py) for the condition, and web_shell.py's
    TestDismissibleNotice for the markup."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_notice_shown_on_the_ordinary_default(self):
        app, sessions, _web_ui = _app(step_up=StepUpConfig())
        client = _client(app)
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert '<div class="pf-shell-notice"' in r.text
        assert "/settings" in r.text

    def test_no_notice_when_step_up_is_genuinely_required(self):
        app, sessions, _web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True))
        client = _client(app)
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert '<div class="pf-shell-notice"' not in r.text

    def test_notice_and_banner_can_both_render(self):
        # local_enrollment_banner (required but nothing enrolled yet) and
        # off_notice are mutually exclusive by construction -- required
        # implies off_notice() is None -- so this documents that instead:
        # the disabled-requirement banner (also fires only when NOT
        # currently required) and the off notice can appear together.
        from privacyfence.principal import LOCAL_PRINCIPAL

        wa.observe_step_up_requirement(LOCAL_PRINCIPAL, enabled=True, require_passkey=True)
        wa.observe_step_up_requirement(LOCAL_PRINCIPAL, enabled=True, require_passkey=False)
        app, sessions, _web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=False))
        client = _client(app)
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert '<div class="pf-shell-banner"' in r.text
        assert '<div class="pf-shell-notice"' in r.text


class TestRequirePasskeyHardFail:
    """#426 Phase 3: with ``require_passkey`` on, the one deliberate gap
    TestStepUpEvadableWithNoPasskeyEnrolled documents above is closed --
    nothing enrolled means a hard ``403``, never a silent pass-through.
    Mirrors test_routes_org_approvals.py's own
    test_no_credential_hard_fails_with_no_idp_fallback, minus the (local
    mode has none) IdP angle."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_no_credential_hard_fails_instead_of_releasing_the_write(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "passkey_enrollment_required"
        assert body["enroll_url"] == "/security"
        stored = web_ui.deferred_registry.get(approval.id)
        assert stored is not None
        assert not stored.event.is_set()

    def test_with_a_credential_still_offers_the_normal_webauthn_challenge(self):
        from privacyfence.principal import LOCAL_PRINCIPAL

        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        assert "webauthn_options" in r.json()

    def test_deny_never_hard_fails_even_with_nothing_enrolled(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 200


class TestStepUpWebAuthnFlow:
    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _enroll(self):
        from privacyfence.principal import LOCAL_PRINCIPAL

        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def test_first_attempt_with_a_credential_offers_webauthn_options_and_no_idp_url(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" in body
        assert "idp_stepup_url" not in body

    def test_valid_assertion_completes_the_decision(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        first = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert first.status_code == 428

        fake_verified = type(
            "V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False},
        )()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post(f"/api/approvals/{approval.id}/decide", json={
                "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert second.json() == {"status": "ok"}

    def test_an_assertion_for_a_different_decision_is_rejected(self):
        # Fingerprint binding (§10.6): a challenge minted for "accept"
        # cannot be reused to authorize "accept_all" on the same approval.
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        r = client.post(f"/api/approvals/{approval.id}/decide", json={
            "result": "accept_all", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
        })
        assert r.status_code == 400

    def test_an_assertion_with_no_matching_pending_challenge_is_rejected(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        # No prior 428 round-trip -- nothing pending in the challenge store.
        r = client.post(f"/api/approvals/{approval.id}/decide", json={
            "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
        })
        assert r.status_code == 400

    def test_a_failed_assertion_does_not_release_the_decision(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            r = client.post(f"/api/approvals/{approval.id}/decide", json={
                "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert r.status_code == 401
        stored = web_ui.deferred_registry.get(approval.id)
        assert stored is not None
        assert not stored.event.is_set()  # decision was never released


class TestStepUpBridgeShim:
    """The card document itself carries the WebAuthn ceremony helpers and
    the 428-handling branch of the shim, regardless of whether step-up is
    actually enabled for this install (see _bridge_shim's own docstring)."""

    def test_show_approval_wraps_the_webauthn_helper_js_in_a_script_tag(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        t, card, box = _pending_card(web_ui, connector="gmail")
        r = client.get(f"/approvals/{card.id}")
        assert "pfWebauthnGet" in r.text
        assert "webauthn_assertion" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)


class TestBatchDecide:
    """Phase 2 of the approval binder plan: ``POST /api/approvals/batch/
    decide`` -- approve or deny a selected set in one request. This
    covers only the plain batch mechanics and their own auth posture,
    against an app with step-up off (the ``client``/``sessions``/``web_ui``
    fixtures' default ``create_app`` call, no ``step_up``) -- see
    TestBatchStepUp below for the passkey gate itself."""

    def test_a_mixed_batch_applies_each_item_and_reports_its_own_outcome(self, client, sessions, web_ui):
        session_id = _signed_in(client, sessions)
        accept_me = _register(web_ui, dedupe_key="k1")
        deny_me = _register(web_ui, dedupe_key="k2")
        already_decided = _register(web_ui, dedupe_key="k3")
        web_ui.resolve(already_decided.id, "accept")  # decided elsewhere, before the batch is submitted

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [
                {"id": accept_me.id, "result": "accept"},
                {"id": deny_me.id, "result": "deny"},
                {"id": already_decided.id, "result": "accept"},
                {"id": "does-not-exist", "result": "accept"},
            ],
        })

        assert r.status_code == 200
        body = r.json()
        assert body["results"] == [
            {"id": accept_me.id, "outcome": "applied"},
            {"id": deny_me.id, "outcome": "applied"},
            {"id": already_decided.id, "outcome": "already_decided"},
            {"id": "does-not-exist", "outcome": "unknown"},
        ]
        assert accept_me.result == "accept"
        assert deny_me.result == "deny"
        # Audit provenance (Phase 2): every item this batch actually
        # applied is stamped with the same server-minted batch id --
        # already_decided/unknown items were never touched by it.
        assert accept_me.decided_via == "binder"
        assert deny_me.decided_via == "binder"
        assert accept_me.batch_id == deny_me.batch_id == body["batch_id"]

    def test_a_client_supplied_batch_id_is_never_recorded_verbatim(self, client, sessions, web_ui):
        # B27: batch_id is documented (audit_log.py) as server-minted. With
        # no step-up in play there is never a live challenge to prove a
        # submitted batch_id against, so a forged one must be replaced
        # rather than trusted straight into the audit trail.
        session_id = _signed_in(client, sessions)
        approval = _register(web_ui)

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept"}],
            "batch_id": "attacker-forged-batch-id",
        })

        assert r.status_code == 200
        assert r.json()["batch_id"] != "attacker-forged-batch-id"
        assert approval.batch_id == r.json()["batch_id"]
        assert approval.batch_id != "attacker-forged-batch-id"

    def test_a_non_batchable_item_reports_not_batchable_and_is_left_untouched(self, client, sessions, web_ui):
        session_id = _signed_in(client, sessions)
        confirm = web_ui.deferred_registry.register_confirm()

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": confirm.id, "result": "accept"}],
        })

        assert r.status_code == 200
        assert r.json()["results"] == [{"id": confirm.id, "outcome": "not_batchable"}]
        assert not confirm.event.is_set()

    def test_wrong_csrf_is_unauthorized(self, client, sessions, web_ui):
        _signed_in(client, sessions)
        approval = _register(web_ui)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": "not-the-real-token", "items": [{"id": approval.id, "result": "accept"}],
        })
        assert r.status_code == 401
        assert not approval.event.is_set()

    def test_cross_origin_request_is_rejected(self, client, sessions, web_ui):
        session_id = _signed_in(client, sessions)
        approval = _register(web_ui)
        r = client.post(
            "/api/approvals/batch/decide",
            json={"csrf": session_id, "items": [{"id": approval.id, "result": "accept"}]},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        assert not approval.event.is_set()

    def test_oversize_batch_is_rejected_with_nothing_applied(self):
        # A small max_pending (default caps are 20/50, too many
        # approvals to register just to exceed it) makes the size check
        # itself easy to hit without also tripping the per-principal cap.
        from privacyfence.approvals import PendingApprovalRegistry

        registry = PendingApprovalRegistry(max_pending=3, max_pending_per_principal=3)
        web_ui = WebApprovalUI(registry=registry)
        sessions = LocalSessionStore()
        app = create_app(web_ui, sessions=sessions, step_up=StepUpConfig())
        client = _client(app)
        session_id = _signed_in(client, sessions)
        approvals = [_register(web_ui, dedupe_key=f"k{i}") for i in range(registry.max_pending)]
        items = [{"id": a.id, "result": "accept"} for a in approvals] + [{"id": "extra", "result": "accept"}]

        r = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})

        assert r.status_code == 400
        assert all(not a.event.is_set() for a in approvals)

    def test_missing_items_is_rejected(self, client, sessions, web_ui):
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": []})
        assert r.status_code == 400

    def test_an_invalid_item_result_is_rejected(self, client, sessions, web_ui):
        session_id = _signed_in(client, sessions)
        approval = _register(web_ui)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept_all"}],
        })
        assert r.status_code == 400
        assert not approval.event.is_set()

    def test_unauthenticated_request_is_rejected(self, client, web_ui):
        approval = _register(web_ui)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": "whatever", "items": [{"id": approval.id, "result": "accept"}],
        })
        assert r.status_code == 401
        assert not approval.event.is_set()


class TestBatchStepUp:
    """Phase 3 of the approval binder plan: the batch decide endpoint's own
    passkey gate -- one WebAuthn assertion bound to the whole submitted
    set (webauthn_stepup.batch_decision_fingerprint), not one per item."""

    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def _enroll(self):
        from privacyfence.principal import LOCAL_PRINCIPAL

        wa.add_credential(LOCAL_PRINCIPAL, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))

    def _verified_assertion(self):
        return type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()

    def test_deny_only_batch_never_steps_up(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "deny"}],
            "batch_id": "attacker-forged-batch-id",
        })
        assert r.status_code == 200
        # B27: _batch_needs_step_up() never runs verify_step_up() here, so a
        # client-supplied batch_id must not survive into the audit trail.
        assert r.json()["batch_id"] != "attacker-forged-batch-id"
        assert approval.batch_id == r.json()["batch_id"]

    def test_no_assertion_offers_a_428_with_a_batch_id(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept"}],
        })
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" in body
        assert body["batch_id"]
        assert not approval.event.is_set()

    def test_valid_assertion_applies_the_whole_batch(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        accept_me = _register(web_ui, gate_kind="popup", dedupe_key="k1")
        deny_me = _register(web_ui, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        items = [{"id": accept_me.id, "result": "accept"}, {"id": deny_me.id, "result": "deny"}]
        first = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})
        assert first.status_code == 428
        batch_id = first.json()["batch_id"]

        with patch.object(wa.webauthn, "verify_authentication_response", return_value=self._verified_assertion()):
            second = client.post("/api/approvals/batch/decide", json={
                "csrf": session_id, "items": items, "batch_id": batch_id,
                "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert second.json()["results"] == [
            {"id": accept_me.id, "outcome": "applied"}, {"id": deny_me.id, "outcome": "applied"},
        ]
        assert accept_me.result == "accept"
        assert deny_me.result == "deny"

    def test_an_assertion_for_a_smaller_set_is_rejected(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        accept_me = _register(web_ui, gate_kind="popup", dedupe_key="k1")
        extra = _register(web_ui, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        first = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": accept_me.id, "result": "accept"}],
        })
        batch_id = first.json()["batch_id"]

        # Resubmits with an extra item beyond what the challenge was bound to.
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "batch_id": batch_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            "items": [{"id": accept_me.id, "result": "accept"}, {"id": extra.id, "result": "deny"}],
        })
        assert r.status_code == 400
        assert not accept_me.event.is_set()
        assert not extra.event.is_set()

    def test_a_failed_assertion_does_not_release_the_batch(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        items = [{"id": approval.id, "result": "accept"}]
        first = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})
        batch_id = first.json()["batch_id"]

        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            r = client.post("/api/approvals/batch/decide", json={
                "csrf": session_id, "batch_id": batch_id, "webauthn_assertion": {"id": "Y3JlZC0x"}, "items": items,
            })
        assert r.status_code == 401
        assert not approval.event.is_set()

    def test_an_assertion_with_a_flipped_result_is_rejected(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        accept_me = _register(web_ui, gate_kind="popup", dedupe_key="k1")
        other = _register(web_ui, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        items = [{"id": accept_me.id, "result": "accept"}, {"id": other.id, "result": "deny"}]
        first = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})
        batch_id = first.json()["batch_id"]

        flipped = [{"id": accept_me.id, "result": "accept"}, {"id": other.id, "result": "accept"}]
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "batch_id": batch_id, "webauthn_assertion": {"id": "Y3JlZC0x"}, "items": flipped,
        })
        assert r.status_code == 400
        assert not accept_me.event.is_set()
        assert not other.event.is_set()

    def test_replay_after_the_challenge_was_consumed_is_rejected(self):
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        items = [{"id": approval.id, "result": "accept"}]
        first = client.post("/api/approvals/batch/decide", json={"csrf": session_id, "items": items})
        batch_id = first.json()["batch_id"]

        with patch.object(wa.webauthn, "verify_authentication_response", return_value=self._verified_assertion()):
            second = client.post("/api/approvals/batch/decide", json={
                "csrf": session_id, "batch_id": batch_id, "webauthn_assertion": {"id": "Y3JlZC0x"}, "items": items,
            })
        assert second.status_code == 200

        # A second decide for a fresh approval, replaying the same
        # already-consumed batch id/assertion, must not be accepted.
        another = _register(web_ui, gate_kind="popup", dedupe_key="k2")
        third = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "batch_id": batch_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            "items": [{"id": another.id, "result": "accept"}],
        })
        assert third.status_code == 400
        assert not another.event.is_set()

    def test_an_assertion_with_no_matching_pending_challenge_is_rejected(self):
        # No prior 428 round-trip -- nothing pending in the challenge
        # store for this (made-up) batch_id. StepUpChallengeStore's own
        # TTL expiry (test_webauthn_stepup.py's TestStepUpChallengeStore)
        # raises through this exact same path once a real challenge ages
        # out, since ``pop()`` treats "never existed" and "expired" alike.
        self._enroll()
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "batch_id": "made-up-batch-id",
            "webauthn_assertion": {"id": "Y3JlZC0x"}, "items": [{"id": approval.id, "result": "accept"}],
        })
        assert r.status_code == 400
        assert not approval.event.is_set()

    def test_require_passkey_with_nothing_enrolled_hard_fails_the_whole_batch(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", require_passkey=True),
        )
        accept_me = _register(web_ui, gate_kind="popup", dedupe_key="k1")
        deny_me = _register(web_ui, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [{"id": accept_me.id, "result": "accept"}, {"id": deny_me.id, "result": "deny"}],
        })
        assert r.status_code == 403
        body = r.json()
        assert body["error"] == "passkey_enrollment_required"
        assert body["enroll_url"] == "/security"
        assert not accept_me.event.is_set()
        assert not deny_me.event.is_set()  # nothing applied -- not even the deny

    def test_require_passkey_off_with_nothing_enrolled_lets_it_through(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="localhost"))
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "accept"}],
            "batch_id": "attacker-forged-batch-id",
        })
        assert r.status_code == 200
        assert r.json()["results"] == [{"id": approval.id, "outcome": "applied"}]
        # B27: this fallthrough (no enrolled credential, require_passkey off)
        # never verified the assertion, so the client-supplied batch_id must
        # not reach the audit trail either.
        assert r.json()["batch_id"] != "attacker-forged-batch-id"
        assert approval.batch_id == r.json()["batch_id"]

    def test_per_item_mode_refuses_the_batch_with_nothing_applied(self):
        self._enroll()
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", batch="per_item"),
        )
        accept_me = _register(web_ui, gate_kind="popup", dedupe_key="k1")
        deny_me = _register(web_ui, dedupe_key="k2")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [{"id": accept_me.id, "result": "accept"}, {"id": deny_me.id, "result": "deny"}],
        })
        assert r.status_code == 400
        assert not accept_me.event.is_set()
        assert not deny_me.event.is_set()

    def test_per_item_mode_does_not_affect_a_deny_only_batch(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="localhost", batch="per_item"),
        )
        approval = _register(web_ui, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions)
        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id, "items": [{"id": approval.id, "result": "deny"}],
        })
        assert r.status_code == 200


class TestHumanSessionRequiredToApprove:
    """The self-approval plan's Phase 2. ADR 0002 decision 6 names three
    ways a local process reaches a ``pf_session``, all by design; until now
    all three produced the same object with the same authority, so an agent
    holding one could release the write it had itself requested.

    ``require_human_session`` is what web/server.py turns on for a
    privilege-separated install -- the only kind where the guarantee is real
    and the companion that mints an attested session is guaranteed present
    (ADR 0003 decisions 3-6).
    """

    def _app(self):
        web_ui = WebApprovalUI()
        sessions = LocalSessionStore()
        app = create_app(web_ui, sessions=sessions, require_human_session=True)
        return TestClient(app, base_url=ORIGIN), sessions, web_ui

    def _sign_in(self, client, sessions, provenance):
        session_id = sessions.create(provenance=provenance)
        client.cookies.set(SESSION_COOKIE, session_id)
        return session_id

    def test_an_unattested_session_cannot_approve(self):
        client, sessions, web_ui = self._app()
        session_id = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)
        t, card, box = _pending_card(web_ui)

        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "accept", "csrf": session_id},
        )

        assert r.status_code == 403
        assert r.json()["error"] == "human_session_required"
        # The gate refuses rather than silently resolving: the call is still
        # blocked, which is what makes this a refusal and not a deny.
        assert box == {}
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_an_attested_session_approves_exactly_as_before(self):
        client, sessions, web_ui = self._app()
        session_id = self._sign_in(client, sessions, PROVENANCE_HUMAN)
        t, card, box = _pending_card(web_ui)

        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "accept", "csrf": session_id},
        )

        assert r.status_code == 200
        t.join(timeout=2)
        assert box["result"] == ("accept", None)

    def test_denying_is_not_gated(self):
        """Same line this module already draws for step-up
        (``_STEP_UP_RESULTS``): denying leaks nothing, and an agent that can
        only deny cannot release anything it asked for."""
        client, sessions, web_ui = self._app()
        session_id = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)
        t, card, box = _pending_card(web_ui)

        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "deny", "csrf": session_id},
        )

        assert r.status_code == 200
        t.join(timeout=2)
        assert box["result"] == ("deny", None)

    def test_a_batch_containing_an_accept_is_refused_whole(self):
        client, sessions, web_ui = self._app()
        session_id = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)
        accept_me = _register(web_ui, dedupe_key="k1")

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [{"id": accept_me.id, "result": "accept"}],
        })

        assert r.status_code == 403
        assert r.json()["error"] == "human_session_required"
        assert web_ui.deferred_registry.get(accept_me.id) is not None

    def test_a_batch_of_denies_is_not_gated(self):
        client, sessions, web_ui = self._app()
        session_id = self._sign_in(client, sessions, PROVENANCE_UNATTESTED)
        deny_me = _register(web_ui, dedupe_key="k1")

        r = client.post("/api/approvals/batch/decide", json={
            "csrf": session_id,
            "items": [{"id": deny_me.id, "result": "deny"}],
        })

        assert r.status_code == 200

    def test_the_gate_is_off_unless_the_install_asks_for_it(self, client, sessions, web_ui):
        """The default -- and what an unseparated build-from-source install
        keeps. There is no companion to mint an attested session there, and
        an agent that can rewrite the credential store directly (ADR 0002
        decision 6) gains nothing from a session check anyway."""
        session_id = _signed_in(client, sessions)
        t, card, box = _pending_card(web_ui)

        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "accept", "csrf": session_id},
        )

        assert r.status_code == 200
        t.join(timeout=2)
        assert box["result"] == ("accept", None)
