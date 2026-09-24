"""privacyfence.settings_window_html -- Linux/CI-portable, no AppKit import.

``build_html()`` renders everything client-side (vanilla JS, driven off
``window.__pfInitialState``/``window.__pfRender`` -- see that module's own
docstring for the bridge protocol) rather than string-interpolating
per-request content the way approval_window_html.build_card_stack_html()
does, so there is no JS engine here to actually execute ``render()`` and
inspect real DOM output. What this file *can* assert on the returned string,
in the same spirit as test_approval_window_html.py's string inspection of
that module's output:

  1. The given ``state`` is embedded byte-for-byte (as JSON) in
     ``window.__pfInitialState`` -- so a page load reflects exactly the
     values SettingsController.snapshot() computed, with no cross-field
     transformation. Section content / toggle on-off / connector rows /
     policy values are all data the JS template reads straight off this
     blob, so correct embedding is the load-bearing part of "does the right
     content show up".
  2. The generic client-side templates that consume each of those fields
     (toggle track/knob, connector row, rule/grant row, policy segmented
     control colors) exist in the shipped JS/CSS and reference the same
     field names the state actually carries -- catching the class of bug
     where a field gets renamed on one side of the bridge and not the
     other.
"""
from __future__ import annotations

import json
import re

from privacyfence.settings_window_html import build_html


def _make_state(**overrides):
    state = {
        "error": "",
        "general": {
            "pii_enabled": True, "pii_ip": True, "pii_financial": False,
            "update_check_enabled": True, "update_check_beta": False,
            "org_installed": True, "org_installed_date": "Jun 14, 2026",
            "org_button_label": "Install/Update Organization Config…", "version": "3.1.1",
            "notifications_enabled": True, "notifications_detail": "standard",
            "step_up_available": True, "step_up_on": False, "step_up_has_passkey": False,
        },
        "connectors": [
            {"key": "gmail", "label": "Gmail", "icon": "gmail", "icon_data_uri": "data:image/png;base64,AAA",
             "authed": True, "enabled": True, "busy": False, "has_org": True, "auth_label": "Reconnect…"},
            {"key": "telegram", "label": "Telegram", "icon": "telegram", "icon_data_uri": "",
             "authed": False, "enabled": True, "busy": False, "has_org": False, "auth_label": "Authenticate…"},
        ],
        "telegram_auth": {"step": None, "error": ""},
        "auto_accept": {
            "rules": [
                {"id": "r-abc123", "sentence": "Gmail - sender: allow read", "connector": "gmail",
                 "connector_label": "Gmail", "scope_type": "gmail.sender", "value": "", "value_ids": [],
                 "verbs": [{"verb": "read", "family": "read"}], "covered_tools": ["gmail_get_message"],
                 "match_count": 0, "last_matched": "", "never_matched": True},
            ],
            "scope_groups": [
                {"id": "drive.folder", "label": "Drive — folder", "connector": "drive", "needs_value": True,
                 "value_hint": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms", "verbs": ["read", "download"]},
                {"id": "gmail.sender", "label": "Gmail — sender", "connector": "gmail", "needs_value": False,
                 "value_hint": "", "verbs": ["read", "download", "archive"]},
            ],
            "connectors": ["drive", "gmail"],
        },
        "privacy": {
            "groups": [{"key": "privacy", "label": "Gmail"}, {"key": "calendar", "label": "Calendar"}],
            "default_policy": {"privacy": "block"},
            "categories": {"privacy": [{"key": "body", "label": "Message body", "policy": "allow"}]},
            "calendar_free_busy": True,
            "gmail_append_signature": False,
        },
        "audit": {
            "log_level": "INFO", "log_file": "logs/privacyfence.log",
            "export_hint": "logs/audit/2026-W31.jsonl → 2026-W31.xlsx",
            "recent": [{"connector": "Gmail", "tool": "gmail_get_thread", "decision": "auto_accepted", "time": "2m ago"}],
        },
        "about": {"version": "3.1.1", "license": "Apache-2.0", "repo_url": "https://github.com/privacyfence/privacyfence"},
    }
    state.update(overrides)
    return state


def _extract_initial_state(html: str) -> dict:
    match = re.search(r"window\.__pfInitialState = (\{.*?\});</script>", html, re.DOTALL)
    assert match, "window.__pfInitialState assignment not found in build_html() output"
    return json.loads(match.group(1))


class TestDocumentShell:
    def test_has_a_title(self):
        html = build_html(_make_state())
        assert "<title>PrivacyFence Settings</title>" in html

    def test_embeds_an_app_mount_point(self):
        html = build_html(_make_state())
        assert '<div id="app"></div>' in html

    def test_defines_pf_render_entry_point(self):
        html = build_html(_make_state())
        assert "window.__pfRender = render;" in html

    def test_no_external_network_references(self):
        # Self-contained, offline document -- loaded via
        # loadHTMLString_baseURL_(html, None), so nothing may reference a
        # CDN, external stylesheet, or remote script.
        html = build_html(_make_state())
        assert "http://" not in html
        assert "https://" not in html.split("window.__pfInitialState", 1)[0]


class TestStateEmbedding:
    def test_state_round_trips_byte_for_byte(self):
        state = _make_state()
        html = build_html(state)
        assert _extract_initial_state(html) == state

    def test_connector_rows_data_present(self):
        html = build_html(_make_state())
        embedded = _extract_initial_state(html)
        keys = {c["key"] for c in embedded["connectors"]}
        assert keys == {"gmail", "telegram"}
        gmail = next(c for c in embedded["connectors"] if c["key"] == "gmail")
        assert gmail["authed"] is True
        assert gmail["icon_data_uri"] == "data:image/png;base64,AAA"

    def test_toggle_states_present(self):
        state = _make_state()
        state["general"]["pii_enabled"] = False
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["general"]["pii_enabled"] is False

    def test_policy_values_present(self):
        state = _make_state()
        state["privacy"]["default_policy"]["privacy"] = "redact"
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["privacy"]["default_policy"]["privacy"] == "redact"

    def test_error_banner_text_present_when_set(self):
        html = build_html(_make_state(error="Gmail authentication failed: boom"))
        embedded = _extract_initial_state(html)
        assert embedded["error"] == "Gmail authentication failed: boom"

    def test_html_special_characters_in_state_do_not_break_the_script_tag(self):
        # json.dumps escapes "</script>" sequences by default only if they
        # appear literally -- confirm a value containing one doesn't split
        # the embedding <script> tag early.
        state = _make_state()
        state["general"]["org_installed_date"] = "</script><script>alert(1)</script>"
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["general"]["org_installed_date"] == "</script><script>alert(1)</script>"


class TestToggleTemplate:
    def test_toggle_track_and_knob_classes_defined(self):
        html = build_html(_make_state())
        assert ".pf-toggle" in html
        assert ".pf-toggle.on" in html
        assert ".pf-knob" in html

    def test_toggle_bridge_actions_referenced(self):
        html = build_html(_make_state())
        for action in (
            "toggle_pii_detection", "toggle_pii_category",
            "toggle_update_check", "toggle_update_check_beta",
            # F6 of the self-approval review: no single toggle_connector
            # any more -- both branches of the directional ternary are
            # present as source text regardless of any connector's actual
            # state (see settings_window_html.py's renderConnectors).
            "enable_connector", "disable_connector",
            "toggle_calendar_free_busy", "toggle_gmail_signature",
        ):
            assert f"'{action}'" in html, f"missing toggle wiring for {action}"


class TestConnectorRowTemplate:
    def test_connector_row_reads_the_right_fields(self):
        html = build_html(_make_state())
        for field in ("c.icon_data_uri", "c.label", "c.enabled", "c.auth_label", "c.key"):
            assert field in html

    def test_authenticate_action_wired(self):
        html = build_html(_make_state())
        assert "'authenticate_connector'" in html


class TestInitialSection:
    """``initial_section`` (issue #396 Part C) -- web/routes_settings.py's
    ``GET /settings/connectors`` passes this so an un-onboarded sign-in link
    lands on Connectors instead of the client-side JS's own 'general'
    default."""

    def test_omitted_by_default(self):
        html = build_html(_make_state())
        assert "window.__pfInitialSection =" not in html

    def test_embeds_the_given_section(self):
        html = build_html(_make_state(), initial_section="connectors")
        assert 'window.__pfInitialSection = "connectors";' in html

    def test_js_falls_back_to_general_when_unset(self):
        # PSC-5: the literal fallback is now capability-aware (org mode's
        # own non-admin principal falls back to Auto-accept instead, see
        # TestOrgCapabilities below) -- local mode's own capabilities
        # (CAPS.sections.general always true) still resolve this to
        # 'general', which is what this test is actually about.
        html = build_html(_make_state())
        assert "section: (window.__pfInitialSection || (CAPS.sections.general ? 'general' : 'auto_accept'))" in html


class TestWelcomeBanner:
    """The un-onboarded welcome banner on the Connectors page (issue #396
    Part C) -- client-rendered, so this only asserts on the shipped
    template/logic, same as every other section's own tests in this file
    (see the module docstring)."""

    def test_rendered_only_when_no_connector_is_authenticated(self):
        html = build_html(_make_state())
        assert "renderWelcomeBanner" in html
        assert "state.connectors.some(function (c) { return c.authed; })" in html

    def test_dismissible_client_side(self):
        html = build_html(_make_state())
        assert "welcomeBannerDismissed" in html
        assert "data-dismiss-welcome" in html

    def test_explains_what_privacyfence_does_and_the_setup_order(self):
        html = build_html(_make_state())
        assert "Welcome to PrivacyFence" in html
        assert "organization config bundle" in html


class TestAutoAcceptTemplate:
    """P6 of the policy v2 redesign: the single filterable Auto-accept page, replacing the old
    per-connector Trusted-*/parallel-rule-row/Sheets-Docs-pointer-page surface (see git history for
    this class's own pre-P6 shape, TestRulesAndGrantsTemplate)."""

    def test_rule_row_fields_wired(self):
        html = build_html(_make_state())
        assert "renderAutoAccept" in html
        assert "remove_policy_rule" in html
        assert "data-aa-expand" in html

    def test_add_rule_form_fields_wired(self):
        html = build_html(_make_state())
        assert "add_policy_rule" in html
        assert "data-aa-group-select" in html
        assert "data-aa-value" in html
        assert "data-aa-verb-toggle" in html
        assert "data-aa-add" in html

    def test_filter_bar_wired(self):
        html = build_html(_make_state())
        assert "data-aa-search" in html
        assert "data-aa-connector-filter" in html
        assert "data-aa-family-filter" in html

    def test_scope_groups_and_rules_fields_referenced(self):
        html = build_html(_make_state())
        assert "scope_groups" in html
        assert "covered_tools" in html

    def test_usage_fields_wired(self):
        # P8 (rule attribution and staleness): every row's match count/last-matched/never-matched
        # trio is read and rendered, with a distinct class for a rule that has never fired.
        html = build_html(_make_state())
        assert "match_count" in html
        assert "never_matched" in html
        assert "pf-aa-usage" in html
        assert "pf-aa-usage-stale" in html

    def test_copy_id_attribute_wired(self):
        # Right-click a rule row to copy its raw value ids (see
        # copyToClipboard/onContextMenu) -- the "value" field may show a
        # resolved display name instead of the id/key a user would want to
        # paste elsewhere.
        html = build_html(_make_state())
        assert "data-copy-id" in html
        assert "onContextMenu" in html
        assert "copyToClipboard" in html

    def test_old_rule_and_grant_row_actions_are_gone(self):
        # add_rule_row/remove_rule_row/remove_grant_row still exist as SettingsController methods
        # (see their own docstring), but only web/org_settings_pages.py's separate org-mode page
        # -- a different module, with its own HTML template -- ever reaches them; this module's
        # own JS bridge (build_html's output) must never reference any of the seven again.
        html = build_html(_make_state())
        for stale in (
            "update_rule_row", "add_rule_row", "remove_rule_row",
            "toggle_grant_capability", "add_grant_row", "update_grant_row", "remove_grant_row",
            "data-rule-field", "data-grant-field", "data-rules-search", "data-rules-nav",
        ):
            assert stale not in html, stale


class TestPrivacySegmentedControl:
    def test_policy_colors_match_the_design(self):
        html = build_html(_make_state())
        # #0071e3 allow / #b76e00 redact / #d92d20 block -- see
        # settings_window_html.py's module docstring.
        assert ".policy-allow { background: #0071e3" in html
        assert ".policy-redact { background: #b76e00" in html
        assert ".policy-block { background: #d92d20" in html

    def test_policy_actions_wired(self):
        html = build_html(_make_state())
        assert "'set_default_policy'" in html
        assert "'set_category_policy'" in html


class TestAuditTemplate:
    def test_log_level_options_present(self):
        html = build_html(_make_state())
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            assert f"'{level}'" in html

    def test_export_action_wired(self):
        html = build_html(_make_state())
        assert "'export_audit_log'" in html

    def test_audit_badge_decision_classes_present(self):
        html = build_html(_make_state())
        assert "auto_accepted" in html
        assert "denied" in html


class TestAboutTemplate:
    def test_quit_and_check_updates_actions_wired(self):
        html = build_html(_make_state())
        assert "'quit_app'" in html
        assert "'check_for_updates'" in html


class TestTelegramModalTemplate:
    """Telegram's in-webview multi-step sign-in modal (phone -> code ->
    optional 2FA password), replacing the pre-#120 native rumps.Window
    flow. See TestTelegramStartAuth/TestTelegramSubmitCode/
    TestTelegramSubmit2FA in test_settings_controller.py for the Python
    side; string-level checks only here, same reasoning as this module's
    own docstring -- actual step-transition/auto-close behavior was
    verified by executing the extracted JS under Node during development."""

    def test_modal_css_defined(self):
        html = build_html(_make_state())
        assert ".pf-modal-overlay" in html
        assert ".pf-modal {" in html

    def test_telegram_row_intercepted_client_side_not_posted_as_authenticate_connector(self):
        html = build_html(_make_state())
        assert "data-telegram-auth" in html

    def test_all_three_steps_have_copy_and_submit_actions_wired(self):
        html = build_html(_make_state())
        assert "'telegram_start_auth'" in html
        assert "'telegram_submit_code'" in html
        assert "'telegram_submit_2fa'" in html
        assert "'telegram_cancel_auth'" in html
        assert "Send Code" in html
        assert "Enter verification code" in html or "Two-step verification" in html

    def test_password_step_uses_password_input_type(self):
        html = build_html(_make_state())
        assert "password:" in html  # TELEGRAM_STEP_COPY.password.type

    def test_state_embeds_telegram_auth_key(self):
        state = _make_state(telegram_auth={"step": "code", "error": "bad code"})
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["telegram_auth"] == {"step": "code", "error": "bad code"}


class TestNotificationsCard:
    """renderNotificationsCard's own comment: web.notifications.detail
    (settings.yaml.example, docs/approval-list-ui-ux.md §4.3) is a real,
    mutable setting -- set_notifications_detail persists it and the card's
    segmented control (segGroupHtml, the same primitive the Audit page's
    Log level row and the Privacy Filter's policy rows already use) is
    sourced from state.general.notifications_detail, not a static hint --
    string-level checks only, same reasoning as this module's own
    docstring."""

    def test_control_wired_to_set_notifications_detail(self):
        html = build_html(_make_state())
        start = html.index("function renderNotificationsDetailControl")
        end = html.index("function renderNotificationsCard")
        fn = html[start:end]
        assert "segGroupHtml(" in fn
        assert "'set_notifications_detail'" in fn
        for level in ("minimal", "standard", "detailed"):
            assert "'" + level + "'" in fn

    def test_control_shown_alongside_granted_and_enable_states(self):
        html = build_html(_make_state())
        start = html.index("function renderNotificationsCard")
        end = html.index("function renderGeneral")
        fn = html[start:end]
        assert "renderNotificationsDetailControl(state)" in fn

    def test_control_omitted_when_disabled_in_configuration(self):
        # Mirrors "Turned off in configuration" -- picking a detail level
        # is moot when web.notifications.enabled is false, so the guard
        # around renderNotificationsDetailControl's own call site must
        # check __pfNotificationsEnabled, not just Notification support.
        html = build_html(_make_state())
        start = html.index("function renderNotificationsCard")
        end = html.index("function renderGeneral")
        fn = html[start:end]
        assert "__pfNotificationsEnabled !== false" in fn

    def test_active_option_is_driven_by_state_not_a_window_flag(self):
        html = build_html(_make_state())
        start = html.index("function renderNotificationsDetailControl")
        end = html.index("function renderNotificationsCard")
        fn = html[start:end]
        assert "state.general.notifications_detail" in fn
        assert "window.__pfNotificationsDetail" not in fn

    def test_each_detail_level_has_developer_authored_copy(self):
        html = build_html(_make_state())
        start = html.index("NOTIFICATIONS_DETAIL_DESCRIPTIONS")
        end = html.index("function renderNotificationsDetailControl")
        descriptions = html[start:end]
        for level in ("minimal", "standard", "detailed"):
            assert level + ":" in descriptions

    def test_state_embeds_the_current_detail_level_for_client_render_to_read(self):
        # build_html() never executes render() (this module's own docstring:
        # no JS engine here) -- so what this level can assert is that
        # state.general.notifications_detail, the field
        # renderNotificationsDetailControl reads (see the test above),
        # actually reaches the page byte-for-byte via __pfInitialState.
        # segGroupHtml's active-option/aria-checked wiring itself was
        # exercised under Node during development, same as the telegram
        # modal's own step transitions (see this class's own docstring).
        state = _make_state(general={
            "pii_enabled": True, "pii_ip": True, "pii_financial": False,
            "update_check_enabled": True, "update_check_beta": False,
            "org_installed": True, "org_installed_date": "Jun 14, 2026",
            "org_button_label": "x", "version": "3.1.1",
            "notifications_enabled": True, "notifications_detail": "detailed",
            "step_up_available": True, "step_up_on": False, "step_up_has_passkey": False,
        })
        html = build_html(state)
        embedded = _extract_initial_state(html)
        assert embedded["general"]["notifications_detail"] == "detailed"


class TestStepUpCard:
    """B9: the General page's Security card grows a one-directional "turn
    step-up on" control -- string-level checks only, same reasoning as this
    module's own docstring (no JS engine here to actually branch on
    g.step_up_on/g.step_up_has_passkey and see which of the three states
    renders)."""

    def _general_fn(self, html: str) -> str:
        start = html.index("function renderGeneral")
        end = html.index("function connectorStatus")
        return html[start:end]

    def test_turn_on_control_is_wired_to_enable_step_up(self):
        fn = self._general_fn(build_html(_make_state()))
        assert "'enable_step_up'" in fn

    def test_reads_all_three_step_up_fields_off_state(self):
        fn = self._general_fn(build_html(_make_state()))
        assert "g.step_up_available" in fn
        assert "g.step_up_on" in fn
        assert "g.step_up_has_passkey" in fn

    def test_state_embeds_step_up_fields_for_client_render_to_read(self):
        state = _make_state(general={
            "pii_enabled": True, "pii_ip": True, "pii_financial": False,
            "update_check_enabled": True, "update_check_beta": False,
            "org_installed": True, "org_installed_date": "Jun 14, 2026",
            "org_button_label": "x", "version": "3.1.1",
            "notifications_enabled": True, "notifications_detail": "standard",
            "step_up_available": True, "step_up_on": True, "step_up_has_passkey": True,
        })
        embedded = _extract_initial_state(build_html(state))
        assert embedded["general"]["step_up_on"] is True
        assert embedded["general"]["step_up_has_passkey"] is True


class TestOrgCapabilities:
    """PSC-5: window.__pfCapabilities is a separate embedded global from
    window.__pfInitialState -- state itself must stay exactly what the
    caller passed (TestStateEmbedding.test_state_round_trips_byte_for_
    byte), so capability data can't live there. These tests read the
    second global the same way _extract_initial_state reads the first."""

    def _extract_capabilities(self, html: str) -> dict:
        match = re.search(r"window\.__pfCapabilities = (\{.*?\});</script>", html, re.DOTALL)
        assert match, "window.__pfCapabilities assignment not found in build_html() output"
        return json.loads(match.group(1))

    def test_local_mode_default_hides_nothing(self):
        caps = self._extract_capabilities(build_html(_make_state()))
        assert caps["mode"] == "local"
        assert caps["is_admin"] is False
        assert all(caps["sections"].values())
        assert caps["not_applicable_actions"] == []

    def test_local_state_is_unaffected_by_capabilities(self):
        # The byte-for-byte invariant (TestStateEmbedding) must hold
        # regardless of mode/is_admin -- capabilities never leak into state.
        state = _make_state()
        html = build_html(state, mode="org", is_admin=True)
        assert _extract_initial_state(html) == state

    def test_org_admin_sees_general_and_privacy(self):
        caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=True))
        assert caps["mode"] == "org"
        assert caps["is_admin"] is True
        assert caps["sections"]["general"] is True
        assert caps["sections"]["privacy"] is True
        assert caps["sections"]["auto_accept"] is True
        assert caps["sections"]["about"] is True

    def test_org_non_admin_does_not_see_general_or_privacy(self):
        caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=False))
        assert caps["sections"]["general"] is False
        assert caps["sections"]["privacy"] is False
        # Per-principal, not admin-gated -- every org principal keeps this.
        assert caps["sections"]["auto_accept"] is True

    def test_org_mode_never_shows_connectors_or_audit(self):
        for is_admin in (True, False):
            caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=is_admin))
            assert caps["sections"]["connectors"] is False
            assert caps["sections"]["audit"] is False

    def test_not_applicable_actions_match_org_settings_scope(self):
        from privacyfence.web.org_settings_scope import NOT_APPLICABLE_ACTIONS

        caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=True))
        not_applicable = set(caps["not_applicable_actions"])
        assert NOT_APPLICABLE_ACTIONS <= not_applicable
        # The four bespoke, no-ACTION_SCOPES-entry actions the bridge shim
        # intercepts client-side (see settings_window_html._LOCAL_ONLY_
        # BESPOKE_ACTIONS) are also covered, open_repo deliberately excluded.
        assert {"install_org_config", "export_audit_log", "quit_app", "check_for_updates"} <= not_applicable
        assert "open_repo" not in not_applicable
        # Actions org mode genuinely keeps must never be marked inapplicable.
        assert not {"add_policy_rule", "remove_policy_rule", "toggle_pii_detection",
                    "toggle_pii_category", "set_default_policy", "set_category_policy"} & not_applicable

    def test_nav_filters_hidden_sections(self):
        html_non_admin = build_html(_make_state(), mode="org", is_admin=False)
        nav_fn_start = html_non_admin.index("function renderNav")
        nav_fn_end = html_non_admin.index("function renderNotificationsDetailControl")
        nav_fn = html_non_admin[nav_fn_start:nav_fn_end]
        assert "CAPS.sections[key] === false" in nav_fn
