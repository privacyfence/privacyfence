"""web_shell.py -- the shared header/nav/live-indicator chrome wrapping
/approvals and /settings."""
from __future__ import annotations

from pathlib import Path

import pytest

from privacyfence import web_shell
from privacyfence.approval_window_html import _STYLES_CSS


class TestWrap:
    def test_returns_one_well_formed_document(self):
        html = web_shell.wrap("<p>hello</p>", title="Test Page", active="approvals")
        assert html.startswith("<!DOCTYPE html>")
        assert "<title>Test Page</title>" in html
        assert "<p>hello</p>" in html

    def test_title_is_escaped(self):
        html = web_shell.wrap("<p>x</p>", title="<script>evil()</script>", active="approvals")
        assert "<script>evil()</script>" not in html
        assert "&lt;script&gt;evil()&lt;/script&gt;" in html

    def test_active_nav_item_is_marked(self):
        html = web_shell.wrap("", title="t", active="settings")
        assert 'class="pf-shell-nav-item active" href="/settings"' in html
        assert 'class="pf-shell-nav-item" href="/approvals"' in html

    def test_embeds_the_shared_tokens(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "--color-accent:" in html
        assert "prefers-color-scheme: dark" in html

    def test_wires_the_state_stream_and_render_dispatch(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "new EventSource('/api/state/stream')" in html
        assert "__pfRender" in html
        assert "__pfRenderApprovals" in html

    def test_body_html_is_not_escaped(self):
        # Callers own their own content's escaping (same convention as
        # web/routes_approvals.py's existing HTML-building routes) -- the
        # shell must not double-escape real markup handed to it.
        html = web_shell.wrap('<div id="mine">x</div>', title="t", active="approvals")
        assert '<div id="mine">x</div>' in html

    def test_onerror_tells_a_permanent_close_from_a_retrying_one(self):
        # Issue #423 part 2: readyState CLOSED (a non-2xx response, e.g.
        # this route's own 401 once the session has expired) never gets an
        # automatic retry per the EventSource spec, unlike a transient
        # network error -- the old handler set the same "reconnecting…"
        # text for both, which lied forever on an expired tab.
        html = web_shell.wrap("", title="t", active="approvals")
        start = html.index("es.onerror = function ()")
        end = html.index("es.addEventListener('settings'")
        onerror_fn = html[start:end]
        assert "EventSource.CLOSED" in onerror_fn
        assert "session expired" in onerror_fn
        assert "reconnecting…" in onerror_fn

    def test_favicon_is_the_bundled_shield_icon_as_a_data_uri(self):
        # No extra unauthenticated route/asset file for the browser's
        # automatic GET /favicon.ico -- same embedded-data-URI approach
        # approval_icons.py already uses for the card-stack documents.
        html = web_shell.wrap("", title="t", active="approvals")
        assert '<link rel="icon" href="data:image/png;base64,' in html


class TestBanner:
    """#426 Phase 3: the "loud persistent banner" step_up_config.py's
    StepUpConfig.local_enrollment_banner() drives -- see that function's
    own docstring for when it fires."""

    def test_no_banner_by_default(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert '<div class="pf-shell-banner"' not in html

    def test_banner_renders_between_header_and_main_when_given(self):
        html = web_shell.wrap("<p>body</p>", title="t", active="approvals", banner_html="Passkey required")
        assert '<div class="pf-shell-banner" role="alert">Passkey required</div>' in html
        assert html.index("pf-shell-banner") < html.index("<p>body</p>")

    def test_banner_html_is_not_escaped(self):
        # Same convention as body_html itself (test_body_html_is_not_escaped
        # above) -- callers own their own escaping; step_up_config.py's own
        # banner text carries a real <a href> link.
        html = web_shell.wrap("", title="t", active="approvals", banner_html='<a href="/security">add one</a>')
        assert '<a href="/security">add one</a>' in html


class TestDismissibleNotice:
    """B23 of the 4.1.0 action plan: a second, dismissible strip below
    banner_html -- see step_up_config.py's own off_notice() for the one
    real caller today."""

    def test_no_notice_by_default(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert '<div class="pf-shell-notice"' not in html

    def test_notice_renders_between_header_and_main_when_given(self):
        html = web_shell.wrap(
            "<p>body</p>", title="t", active="approvals",
            dismissible_notice_html="Not passkey-protected", dismissible_notice_key="pf_test_key",
        )
        assert '<div class="pf-shell-notice"' in html
        assert "Not passkey-protected" in html
        assert 'data-dismiss-key="pf_test_key"' in html
        assert html.index("pf-shell-notice") < html.index("<p>body</p>")

    def test_notice_html_is_not_escaped(self):
        html = web_shell.wrap(
            "", title="t", active="approvals",
            dismissible_notice_html='<a href="/settings">turn on</a>', dismissible_notice_key="k",
        )
        assert '<a href="/settings">turn on</a>' in html

    def test_carries_a_dismiss_button(self):
        html = web_shell.wrap(
            "", title="t", active="approvals", dismissible_notice_html="x", dismissible_notice_key="k",
        )
        assert "data-dismiss-notice" in html

    def test_missing_key_is_rejected(self):
        with pytest.raises(ValueError):
            web_shell.wrap("", title="t", active="approvals", dismissible_notice_html="x")

    def test_dismiss_wiring_reads_the_element_and_key(self):
        html = web_shell.wrap(
            "", title="t", active="approvals", dismissible_notice_html="x", dismissible_notice_key="k",
        )
        assert "getElementById('pf-shell-notice')" in html
        assert "data-dismiss-notice" in html
        assert "localStorage" in html


class TestNotifications:
    """W8, docs/approval-list-ui-ux.md §4: tiers 0-1 only."""

    def test_registers_the_service_worker(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "navigator.serviceWorker.register('/sw.js')" in html

    def test_title_badge_and_aria_live_announcer_present(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "document.title = count > 0" in html
        assert 'id="pf-shell-announcer"' in html
        assert 'aria-live="polite"' in html

    def test_never_asks_for_permission_on_page_load(self):
        # §4.4: only window.__pfNotifPrompt, called by approval_list_html.py
        # after a decision -- never invoked unconditionally by this script.
        html = web_shell.wrap("", title="t", active="approvals")
        assert "Notification.requestPermission()" in html
        # The only unconditional call in this document is the *definition*
        # of __pfNotifPrompt, not an invocation of it.
        assert "__pfNotifPrompt();" not in html

    def test_maybe_notify_never_reads_row_fields_directly(self):
        # §4.3's hard invariant: the *only* thing allowed to decide what a
        # notification says is notificationBody()'s own per-level
        # allowlist (see the test class below) -- maybeNotify just calls
        # it and hands the result to showNotification, never reaching into
        # a row itself.
        html = web_shell.wrap("", title="t", active="approvals")
        start = html.index("function maybeNotify")
        end = html.index("function onApprovalsEvent")
        notify_fn_body = html[start:end]
        assert "showNotification" in notify_fn_body
        assert "notificationBody(count, rows)" in notify_fn_body
        for forbidden in (".connector", ".tool_name", ".summary", "row."):
            assert forbidden not in notify_fn_body, forbidden

    def test_notification_body_allowlist_by_detail_level(self):
        # P5's per-field content allowlist (docs/approval-list-ui-ux.md
        # §4.3): minimal (or a multi-approval grouped notification, which
        # has no richer copy defined -- §4.2) never touches a row at all;
        # `summary` -- the one field that can carry real gated content
        # (approvals.PendingApproval.summary -- see gate.py's call sites)
        # -- is read exactly once, and only inside the `detailed` branch.
        html = web_shell.wrap("", title="t", active="approvals")
        start = html.index("function notificationBody")
        end = html.index("function maybeNotify")
        fn = html[start:end]

        early_return = fn.index("return countBody(count);")
        row_ref = fn.index("var row = rows[0];")
        assert early_return < row_ref, "minimal/grouped case must return before ever touching `row`"

        # Both reads of row.summary (the guard's own condition, and the
        # string it appends) live on the one line gated by the `detailed`
        # check -- nowhere else in this function reads it.
        assert fn.count("row.summary") == 2
        detailed_guard = fn.index("NOTIFICATIONS_DETAIL === 'detailed'")
        assert detailed_guard < fn.index("row.summary")

        # standard's own fields are safe by construction (see module
        # comment above _STREAM_JS): gate_kind names a category, never
        # gated content; connector/tool_name are the same bare/curated
        # strings approval_list_html.py's own row kicker already shows
        # unescaped.
        assert "row.connector" in fn
        assert "row.tool_name" in fn
        assert "row.gate_kind" in fn

    def test_notifications_detail_defaults_to_minimal(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert 'NOTIFICATIONS_DETAIL = "minimal"' in html

    def test_notifications_detail_is_threaded_through_as_a_json_string(self):
        html = web_shell.wrap("", title="t", active="approvals", notifications_detail="detailed")
        assert 'NOTIFICATIONS_DETAIL = "detailed"' in html

    def test_notifications_enabled_exposed_globally_for_the_settings_card(self):
        # settings_window_html.py's renderNotificationsCard reads this off
        # `window` -- see that module's own comment on why (shared JS with
        # the native settings window, which never loads this script).
        # NOTIFICATIONS_DETAIL has no equivalent global: the card's own
        # detail-level control is sourced from state.general.
        # notifications_detail (a real, mutable setting -- see
        # SettingsController.set_notifications_detail) instead of this
        # per-page-load constant, which stays local to notificationBody().
        html = web_shell.wrap("", title="t", active="approvals")
        assert "window.__pfNotificationsEnabled = NOTIFICATIONS_ENABLED;" in html
        assert "window.__pfNotificationsDetail" not in html

    def test_rate_limited_to_one_notification_per_five_seconds(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "5000" in html

    def test_no_notification_while_the_tab_is_focused(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "document.hasFocus()" in html


class TestTokensCssStaysInSyncWithTheApprovalCard:
    """resources/tokens.css is a manual export of approval_window/styles.css's
    own :root block (see that file's docstring for why it isn't a single
    shared @import source) -- this guards against the two silently drifting
    apart the next time either palette changes."""

    def test_every_token_value_in_tokens_css_appears_in_the_approval_stylesheet(self):
        tokens_css = (Path(web_shell.__file__).parent / "resources" / "tokens.css").read_text()
        for line in tokens_css.splitlines():
            line = line.strip()
            if not line.startswith("--") or ":" not in line:
                continue
            value = line.split(":", 1)[1].strip().rstrip(";")
            assert value in _STYLES_CSS, f"tokens.css value {value!r} ({line!r}) not found in approval styles.css"
