"""web_shell.py -- the shared header/nav/live-indicator chrome wrapping
/approvals and /settings."""
from __future__ import annotations


import pytest

from privacyfence import web_shell


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
        assert "--accent:" in html
        assert "prefers-color-scheme: dark" in html

    def test_wires_the_state_stream_and_render_dispatch(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert 'new EventSource("/api/state/stream")' in html
        assert "__pfRender" in html
        assert "__pfRenderApprovals" in html

    def test_body_html_is_not_escaped(self):
        # Callers own their own content's escaping (same convention as
        # web/routes_approvals.py's existing HTML-building routes) -- the
        # shell must not double-escape real markup handed to it.
        html = web_shell.wrap('<div id="mine">x</div>', title="t", active="approvals")
        assert '<div id="mine">x</div>' in html

    def test_onerror_tells_a_permanent_close_from_a_retrying_one(self):
        # readyState CLOSED (a non-2xx response, e.g. this route's own 401
        # once the session has expired) never gets an automatic retry per
        # the EventSource spec, unlike a transient network error -- a
        # handler that set the same "reconnecting…" text for both would lie
        # forever on an expired tab.
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
    """The "loud persistent banner" step_up_config.py's
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
    """A second, dismissible strip below
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
    """Tiers 0-1 only: the title badge and a local service-worker notification (ADR 0064)."""

    def test_registers_the_service_worker(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "navigator.serviceWorker.register('/sw.js')" in html

    def test_title_badge_and_aria_live_announcer_present(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "document.title = count > 0" in html
        assert 'id="pf-shell-announcer"' in html
        assert 'aria-live="polite"' in html

    def test_never_asks_for_permission_on_page_load(self):
        # Only window.__pfNotifPrompt, called by approval_list_html.py
        # after a decision -- never invoked unconditionally by this script.
        html = web_shell.wrap("", title="t", active="approvals")
        assert "Notification.requestPermission()" in html
        # The only unconditional call in this document is the *definition*
        # of __pfNotifPrompt, not an invocation of it.
        assert "__pfNotifPrompt();" not in html

    def test_maybe_notify_never_reads_row_fields_directly(self):
        # The hard invariant: the *only* thing allowed to decide what a
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
        # The per-field content allowlist: minimal (or a multi-approval
        # grouped notification, which has no richer copy defined) never
        # touches a row at all;
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


class TestWebsiteHeader:
    """The shell's header is the website's (website/_partials/header.html): brand mark, inline
    links, and the same links in a <details> menu that replaces them when the header is narrow."""

    def test_the_menu_repeats_every_inline_link(self):
        html = web_shell.wrap("", title="t", active="connections", nav_items=web_shell.ORG_NAV_ITEMS)
        inline = html.split('<div class="pf-shell-nav-links">', 1)[1].split("</div>", 1)[0]
        menu = html.split('<div class="pf-shell-menu-panel">', 1)[1].split("</div></details>", 1)[0]
        for _key, label, href in web_shell.ORG_NAV_ITEMS:
            assert f'href="{href}"' in inline and f'href="{href}"' in menu, label
        assert '<details class="pf-shell-menu"><summary>' in html

    def test_the_current_page_is_marked_for_assistive_technology_too(self):
        html = web_shell.wrap("", title="t", active="settings")
        assert 'class="pf-shell-nav-item active" href="/settings" aria-current="page"' in html
        assert html.count('aria-current="page"') == 2  # inline and in the menu

    def test_the_brand_links_home_with_the_shield_mark(self):
        html = web_shell.wrap("", title="t", active="settings")
        assert '<a class="pf-shell-brand" href="/approvals" aria-label="PrivacyFence home">' in html
        assert f'<img src="{web_shell._BRAND_ICON_DATA_URI}" alt=""' in html

    def test_the_nav_collapses_by_container_not_viewport(self):
        # Shared rule 6: the app has no viewport breakpoints.
        assert "@container (max-width: 900px)" in web_shell._SHELL_CSS
        assert "@media" not in web_shell._SHELL_CSS

    def test_the_principal_is_also_in_the_menu(self):
        html = web_shell.wrap(
            "", title="t", active="approvals", nav_items=web_shell.ORG_NAV_ITEMS, principal_label="carol@example.com",
        )
        assert '<div class="pf-shell-menu-principal">Signed in as carol@example.com</div>' in html


class TestNavItems:
    def test_local_mode_nav_is_unchanged_by_default(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert 'href="/approvals"' in html
        assert 'href="/settings"' in html
        assert 'href="/connect"' not in html
        assert 'href="/security"' not in html

    def test_org_nav_items_render_and_mark_the_active_one(self):
        html = web_shell.wrap(
            "", title="t", active="approvals", nav_items=web_shell.ORG_NAV_ITEMS,
        )
        assert 'class="pf-shell-nav-item active" href="/approvals"' in html
        for href in ("/connect", "/security", "/settings"):
            assert f'class="pf-shell-nav-item" href="{href}"' in html


class TestPrincipalLabel:
    def test_absent_by_default(self):
        # Local mode has exactly one principal; naming it would be noise.
        # Asserted on the rendered element rather than the bare class name,
        # since the stylesheet always carries the rule either way.
        assert 'class="pf-shell-principal"' not in web_shell.wrap("", title="t", active="approvals")

    def test_rendered_and_escaped_when_given(self):
        html = web_shell.wrap(
            "", title="t", active="approvals", principal_label="<b>m@acme.example</b>",
        )
        assert 'class="pf-shell-principal"' in html
        assert "<b>m@acme.example</b>" not in html
        assert "&lt;b&gt;m@acme.example&lt;/b&gt;" in html


class TestLiveUpdatesCanBeTurnedOff:
    """The live indicator tells a reviewer whether the queue in front of
    them is current. A mode with no state stream behind it (org mode --
    web/server.py's _build_org_app mounts no GET /api/state/stream) must
    render no indicator rather than one that lies in either direction."""

    def test_indicator_and_stream_script_are_both_dropped(self):
        html = web_shell.wrap("", title="t", active="approvals", live_updates=False)
        assert 'id="pf-shell-live-dot"' not in html
        assert 'id="pf-shell-live-label"' not in html
        assert "EventSource" not in html
        assert "/api/state/stream" not in html

    def test_both_are_present_by_default(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert 'id="pf-shell-live-dot"' in html
        assert 'new EventSource("/api/state/stream")' in html

    def test_stream_url_is_configurable(self):
        # Org mode's approvals page subscribes to its own principal-scoped
        # /api/approvals/stream, since org mode mounts no state stream.
        html = web_shell.wrap("", title="t", active="approvals", stream_url="/api/approvals/stream")
        assert 'id="pf-shell-live-dot"' in html
        assert 'new EventSource("/api/approvals/stream")' in html
        assert "/api/state/stream" not in html

    def test_the_page_is_still_a_complete_document(self):
        # Everything the list page itself depends on has to survive: the
        # toast target its own script writes into, and <main>.
        html = web_shell.wrap("<p>body</p>", title="t", active="approvals", live_updates=False)
        assert html.startswith("<!DOCTYPE html>")
        assert 'id="pf-shell-toast"' in html
        assert "<p>body</p>" in html


class TestBareLinksAreStyled:
    def test_main_content_links_are_not_browser_default_blue(self):
        # Nothing else in this stylesheet styles a bare <a>, so any link a
        # page renders outside the nav/banner/notice classes fell through
        # to #0000ee against a warm grey palette.
        html = web_shell.wrap("", title="t", active="approvals")
        assert ".pf-shell-main :where(a:not(.button)) { color: var(--accent-dark); }" in html

    def test_a_link_drawn_as_a_button_keeps_the_buttons_colours(self):
        # A bare-link colour on a.button.primary would put accent text on the ink fill.
        html = web_shell.wrap("", title="t", active="approvals")
        assert ".pf-shell-main a.button { text-decoration: none; }" in html


class TestPlainPage:
    """The fallback documents (no longer pending, preparing, not authorized, local mode's
    /security) share one helper: a phone lays them out at its own width, and they carry the
    design system and dark mode like every other document."""

    def test_is_a_complete_document_a_phone_lays_out_at_its_own_width(self):
        html = web_shell.plain_page("<p>x</p>", title="t", nonce="n")
        assert html.startswith("<!DOCTYPE html>")
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
        assert '<meta name="color-scheme" content="light dark">' in html

    def test_inlines_the_design_system_first_under_the_nonce(self):
        from privacyfence.design_css import DOCUMENT_CSS

        html = web_shell.plain_page("<p>x</p>", title="t", nonce="abc", page_css=".mine{}")
        style = html.split('<style nonce="abc">', 1)[1].split("</style>", 1)[0]
        assert style.startswith(DOCUMENT_CSS)
        assert style.endswith(".mine{}")
        assert html.count("<style") == 1

    def test_body_sits_in_one_panel_and_head_html_lands_in_head(self):
        html = web_shell.plain_page(
            "<p>body</p>", title="<t>", nonce="n", head_html='<meta http-equiv="refresh" content="2">',
        )
        head, body = html.split("</head>", 1)
        assert '<meta http-equiv="refresh" content="2">' in head
        assert "<title>&lt;t&gt;</title>" in head
        assert '<div class="panel stack pf-plain-panel"><p>body</p></div>' in body
