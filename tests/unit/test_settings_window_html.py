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
import shutil
import subprocess

import pytest

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
        "peek_auth": {"step": None, "error": "", "user_code": "", "verification_url": "", "pinned_base_url": ""},
        "grist_signin": "api_key",
        "grist_server_url_pinned": "",
        "grist_auth": {"error": ""},
        "auto_accept": {
            "rules": [
                {"id": "r-abc123", "sentence": "Gmail - sender: allow read", "connector": "gmail",
                 "connector_label": "Gmail", "scope_type": "gmail.sender", "scope": "gmail.sender",
                 "scope_verbs": ["read", "download", "move"], "value": "", "value_ids": [],
                 "verbs": [{"verb": "read", "family": "read"}], "covered_tools": ["gmail_get_message"],
                 "match_count": 0, "last_matched": "", "never_matched": True},
            ],
            "scope_groups": [
                {"id": "drive.folder", "label": "Drive — folder", "connector": "drive", "needs_value": True,
                 "value_hint": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms", "verbs": ["read", "download"]},
                {"id": "gmail.sender", "label": "Gmail — sender", "connector": "gmail", "needs_value": False,
                 "value_hint": "", "verbs": ["read", "download", "move"]},
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
        assert '<div id="app" class="shell pf-settings"></div>' in html

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

    HOSTILE = "</script><script>alert(1)</script>"

    @pytest.mark.parametrize("embed", ["state", "ai_client", "initial_section", "scoped_rule"])
    def test_html_special_characters_in_state_do_not_break_the_script_tag(self, embed):
        # json.dumps leaves "</script>" literal, which would end the embedding
        # <script> tag early: every embedded value must be escaped.
        state = _make_state()
        kwargs: dict = {}
        if embed == "state":
            state["general"]["org_installed_date"] = self.HOSTILE
        elif embed == "ai_client":
            kwargs["ai_client"] = {"mcp_url": self.HOSTILE}
        elif embed == "initial_section":
            kwargs["initial_section"] = self.HOSTILE
        else:
            data = json.loads(json.dumps(TestScopedTokensSection.DATA))
            data["tokens"][0]["rules"][0]["sentence"] = self.HOSTILE
            kwargs["scoped_tokens"] = data
        html = build_html(state, **kwargs)
        assert "</script><script>alert(1)" not in html
        assert html.count("</script>") == html.count("<script")
        if embed == "state":
            assert _extract_initial_state(html)["general"]["org_installed_date"] == self.HOSTILE


class TestOptionalValueField:
    def test_value_input_is_offered_for_an_optional_value_group(self):
        html = build_html(_make_state())
        assert "group.needs_value || group.value_optional" in html
        assert "Value, optional (comma-separated for more than one)" in html


class TestToggleTemplate:
    def test_switches_are_the_toggle_component(self):
        # app.css's toggle: a real checkbox with role=switch inside a <label> tap target, the
        # data-action on the input itself (one tap, one post).
        html = build_html(_make_state())
        assert "<label class=\"toggle pf-toggle\"><input type=\"checkbox\" role=\"switch\"" in html
        assert "<span class=\"toggle-track\"></span></label>" in html
        assert ".toggle-track" in html  # the component's own CSS, from app.css

    def test_toggle_bridge_actions_referenced(self):
        html = build_html(_make_state())
        for action in (
            "toggle_pii_detection", "toggle_pii_category",
            "toggle_update_check", "toggle_update_check_beta",
            # No single toggle_connector -- both branches of the directional ternary are
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
    """``initial_section`` -- web/routes_settings.py's
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
        # The literal fallback is capability-aware (org mode's
        # own non-admin principal falls back to Auto-accept instead, see
        # TestOrgCapabilities below) -- local mode's own capabilities
        # (CAPS.sections.general always true) still resolve this to
        # 'general', which is what this test is actually about.
        html = build_html(_make_state())
        assert "section: (window.__pfInitialSection || (CAPS.sections.general ? 'general' : 'auto_accept'))" in html


class TestWelcomeBanner:
    """The un-onboarded welcome banner on the Connectors page --
    client-rendered, so this only asserts on the shipped
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
    """The single filterable Auto-accept page (policy v2)."""

    def test_rule_row_fields_wired(self):
        html = build_html(_make_state())
        assert "renderAutoAccept" in html
        assert "remove_policy_rule" in html
        assert "data-aa-expand" in html

    def test_update_rule_notice_and_button_are_wired_to_missing_tools(self):
        """The row shows the notice and the "Update rule" button only when ``missing_tools`` is
        non-empty; the DOM behaviour is proved in tests/integration/test_settings_update_rule_browser.py."""
        html = build_html(_make_state())
        assert "r.missing_tools && r.missing_tools.length" in html
        assert "These verbs also cover tools this rule doesn\\'t allow yet" in html
        assert "dataAttr('update_policy_rule', { rule_id: r.id })" in html

    def test_update_rule_confirm_names_the_tools(self):
        html = build_html(_make_state())
        assert "data-confirm" in html
        assert "'Add ' + missingNames + ' to this rule?" in html
        assert "window.confirm(confirmText)" in html

    def test_add_rule_form_fields_wired(self):
        html = build_html(_make_state())
        assert "add_policy_rule" in html
        assert "data-aa-group-select" in html
        assert "data-aa-value" in html
        assert "data-aa-verb-toggle" in html
        assert "data-aa-add" in html

    def test_enter_in_the_new_token_value_field_adds_the_rule(self):
        html = build_html(_make_state())
        assert "if (el.hasAttribute('data-st-new-value')) { addNewScopedRule(); return; }" in html

    def test_move_needs_two_ids_hint_and_block(self):
        html = build_html(_make_state())
        assert "function aaPairHint(group)" in html
        assert "['move'].filter" in html
        assert "['move', 'copy']" not in html
        assert "Enter at least two ids, separated by a comma." in html
        assert "pf-aa-pair-hint" in html
        # The invalid state disables the button and the submit path refuses too.
        assert "aaHint && aaHint.invalid ? ' aria-disabled=\"true\"'" in html
        assert "if (submitHint && submitHint.invalid) return;" in html

    def test_the_move_pair_rule_names_exactly_drive_folder_and_tasks_list(self):
        """One constant lists the scopes whose move has a source and a destination; the add form's
        hint and the Edit verbs toggle both go through the helper that reads it."""
        html = build_html(_make_state())
        assert "var AA_MOVE_PAIR_SCOPES = ['drive.folder', 'tasks.list'];" in html
        assert "function aaMoveNeedsPair(scopeId)" in html
        assert "!aaMoveNeedsPair(group.id)) return null;" in html
        assert "aaMoveNeedsPair(r.scope) && r.value_ids.length < 2" in html

    def test_edit_verbs_hooks_and_confirm_text_are_present(self):
        html = build_html(_make_state())
        assert 'data-aa-edit="' in html
        assert "data-aa-edit-verb" in html
        assert "data-aa-edit-save" in html
        assert "data-aa-edit-cancel" in html
        assert "dataAttr('set_policy_rule_verbs', { rule_id: r.id, verbs: selected })" in html
        assert "Change this rule to allow ' + selected.join(', ') + '? Added verbs cover all of their tools. " in html
        assert "Tools that only removed verbs cover stop being auto-accepted." in html
        # Custom rows (scope null) get no link; Save is inert while no verb is selected.
        assert "r.scope && r.scope_verbs && r.scope_verbs.length" in html
        assert "aria-label=\"Save verbs\" aria-disabled=\"true\"" in html

    def test_rule_form_has_no_personal_data_checkbox(self):
        html = build_html(_make_state())
        assert "data-aa-personal-data" not in html
        assert "personal_data_label" not in html
        assert "aaPersonalData" not in html
        assert "verbs: checked });" in html

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
        # Rule attribution and staleness: every row's match count/last-matched/never-matched
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
    def test_policy_colors_are_the_status_tokens(self):
        # allow / redact / block take the success / warning / danger status colours (app.css),
        # which have a dark-mode value, never a literal of their own.
        html = build_html(_make_state())
        assert ".pf-seg-btn.policy-allow { color: var(--success); background: var(--success-soft)" in html
        assert ".pf-seg-btn.policy-redact { color: var(--warning); background: var(--warning-soft)" in html
        assert ".pf-seg-btn.policy-block { color: var(--danger); background: var(--danger-soft)" in html

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
    optional 2FA password). See TestTelegramStartAuth/TestTelegramSubmitCode/
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


class TestPeekSignInModal:
    def test_script_carries_the_two_step_modal(self):
        html = build_html(_make_state())
        assert "data-peek-auth" in html
        assert 'data-peek-field="base_url"' in html or "data-peek-field=\"base_url\"" in html
        assert "'peek_start_login'" in html
        assert "'peek_finish_login'" in html
        assert "'peek_cancel_login'" in html
        assert "Approve in Peek" in html

    def test_state_embeds_peek_auth_key(self):
        state = _make_state(peek_auth={
            "step": "approve", "error": "", "user_code": "ABCD-1234",
            "verification_url": "https://peek.example.com/cli", "pinned_base_url": "",
        })
        embedded = _extract_initial_state(build_html(state))
        assert embedded["peek_auth"]["user_code"] == "ABCD-1234"


class TestGristApiKeyForm:
    def test_script_carries_the_api_key_form(self):
        html = build_html(_make_state())
        assert "data-grist-auth" in html
        assert 'type="password" autocomplete="off"' in html
        assert "'grist_connect'" in html
        assert "'grist_cancel_auth'" in html
        assert "Account settings → Developer → API Key." in html

    def test_oauth_signin_keeps_the_generic_authenticate_connector(self):
        html = build_html(_make_state(grist_signin="oauth"))
        assert "'authenticate_connector'" in html
        assert embedded_signin(html) == "oauth"

    def test_state_embeds_the_grist_keys(self):
        embedded = _extract_initial_state(build_html(_make_state(grist_auth={"error": "bad key"})))
        assert embedded["grist_auth"] == {"error": "bad key"}
        assert embedded["grist_signin"] == "api_key"


def embedded_signin(html):
    return _extract_initial_state(html)["grist_signin"]


class TestNotificationsCard:
    """renderNotificationsCard's own comment: web.notifications.detail
    (settings.yaml.example; how much a notification body may say, ADR 0064) is a real,
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
    """The General page's Security card has a one-directional "turn
    step-up on" control (ADR 0068) -- string-level checks only, same reasoning as this
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


    def test_scope_control_is_wired_to_set_step_up_scope(self):
        html = build_html(_make_state())
        assert "renderStepUpScopeRow(g)" in self._general_fn(html)
        start = html.index("function renderStepUpScopeRow")
        fn = html[start:html.index("function renderGeneral")]
        assert "dataAttr('set_step_up_scope'" in fn
        # A rung below the configured minimum carries no data-action at all.
        assert "locked ? ' aria-disabled=\"true\"'" in fn
        for field in ("g.step_up_scope_floor", "g.step_up_scope"):
            assert field in fn

    def test_pii_card_is_local_mode_only(self):
        # On an organization server the PII gate is an Admin section, not a Settings one.
        fn = self._general_fn(build_html(_make_state()))
        guard = fn.index("CAPS.mode !== 'org')")
        assert guard < fn.index("piiGateCardHtml(g)")
        assert "CAPS.is_admin" not in fn[guard:fn.index("piiGateCardHtml(g)")]
        assert "'PII Detection Gate'" not in fn


class TestAiClientSection:
    """The "Connect an AI client" section (ADR 0104): drawn in local mode
    when the caller has an /mcp URL to show, never in org mode, and never
    carrying a token -- the page only ever asks the companion to copy one."""

    AI_CLIENT = {"mcp_url": "http://127.0.0.1:8765/mcp"}

    def _caps(self, html: str) -> dict:
        match = re.search(r"window\.__pfCapabilities = (\{.*?\});</script>", html, re.DOTALL)
        assert match
        return json.loads(match.group(1))

    def test_local_mode_with_an_mcp_url_shows_the_section(self):
        html = build_html(_make_state(), ai_client=self.AI_CLIENT)
        assert self._caps(html)["sections"]["ai_client"] is True
        assert 'window.__pfAiClient = {"mcp_url": "http://127.0.0.1:8765/mcp"};' in html

    def test_org_mode_never_shows_it(self):
        for is_admin in (True, False):
            html = build_html(_make_state(), mode="org", is_admin=is_admin, ai_client=self.AI_CLIENT)
            assert self._caps(html)["sections"]["ai_client"] is False
            assert "window.__pfAiClient = " not in html

    def test_state_is_unaffected(self):
        state = _make_state()
        assert _extract_initial_state(build_html(state, ai_client=self.AI_CLIENT)) == state

    def test_the_template_carries_the_header_format_steps_and_both_actions(self):
        html = build_html(_make_state(), ai_client=self.AI_CLIENT)
        assert "['ai_client', 'AI clients']" in html
        assert "Connect an AI client" in html
        assert "Authorization: Bearer &lt;token&gt;" in html
        assert "claude mcp add --transport http --scope user privacyfence" in html
        assert "dataAttr('copy_mcp_token', {})" in html
        assert "dataAttr('rotate_mcp_token', {})" in html
        assert "connect-claude-code/" in html
        assert "Copy AI Client Token" in html

    def test_the_template_carries_chatgpt_desktops_steps(self):
        html = build_html(_make_state(), ai_client=self.AI_CLIENT)
        assert "ChatGPT desktop" in html
        assert "Settings → MCP servers → Add server" in html
        assert "connect-chatgpt/#chatgpt-desktop" in html


class TestScopedTokensSection:
    """The "Scoped tokens" section: drawn in local mode when the caller has
    page data for it, never in org mode, and every value in it escaped."""

    DATA = {
        "delivery": "companion", "max_days": 365, "default_days": 90, "warn_days": 14,
        "tokens": [
            {"id": "st-1", "label": "sales-sync", "created_at": 0, "expires_at": 1798675200, "days_left": 5,
             "expiring_soon": True, "expired": False,
             "rules": [{"id": "r1", "title": "Gmail — anything", "sentence": "Allow reads </script><b>x</b>",
                        "tools": ["gmail_get_message"]}],
             "tools": ["gmail_get_message"]},
            {"id": "st-2", "label": "old", "created_at": 0, "expires_at": 1, "days_left": 0,
             "expiring_soon": False, "expired": True, "rules": [], "tools": []},
        ],
        "rules": [{"id": "r1", "title": "Gmail — anything", "verbs": ["read"], "sentence": "Allow reads",
                   "tools": ["gmail_get_message", "gmail_list_messages"]}],
        "tools": [{"connector": "gmail", "tools": ["gmail_get_message", "gmail_list_messages"]}],
        "auto_tools": ["gmail_list_messages"],
        "scope_groups": [
            {"id": "gmail.label", "label": "Gmail — label </script><i>g</i>", "connector": "gmail", "needs_value": True,
             "value_optional": False, "value_hint": "Label </script><u>h</u>", "verbs": ["read", "update"],
             "verb_tools": {"read": ["gmail_get_message"], "update": ["gmail_modify_message"]}},
        ],
    }

    def _caps(self, html: str) -> dict:
        match = re.search(r"window\.__pfCapabilities = (\{.*?\});</script>", html, re.DOTALL)
        assert match
        return json.loads(match.group(1))

    def test_local_mode_with_an_mcp_url_shows_the_section(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert self._caps(html)["sections"]["scoped_tokens"] is True
        assert "window.__pfScopedTokens = " in html
        assert "['scoped_tokens', 'Scoped tokens']" in html
        assert "</script><b>" not in html
        for mode_kwargs in ({}, {"mode": "org", "is_admin": True}):
            hidden = build_html(_make_state(), **mode_kwargs)
            assert self._caps(hidden)["sections"]["scoped_tokens"] is False
            assert "window.__pfScopedTokens = " not in hidden

    def test_org_mode_shows_the_section_only_when_enabled(self):
        # The org route passes page data only once the administrator has enabled the feature.
        data = {**self.DATA, "delivery": "once", "max_days": 30}
        for is_admin in (True, False):
            shown = build_html(_make_state(), mode="org", is_admin=is_admin, scoped_tokens=data)
            assert self._caps(shown)["sections"]["scoped_tokens"] is True
            assert '"delivery": "once"' in shown
            assert '"max_days": 30' in shown
            assert "</script><b>" not in shown
            hidden = build_html(_make_state(), mode="org", is_admin=is_admin)
            assert self._caps(hidden)["sections"]["scoped_tokens"] is False
            assert "window.__pfScopedTokens = " not in hidden

    def test_org_mode_uses_the_one_time_dialog(self):
        html = build_html(_make_state(), mode="org", scoped_tokens={**self.DATA, "delivery": "once"})
        assert "window.__pfShowScopedSecret = function (secret, onClose)" in html
        assert "Copy this token now. It is not stored and cannot be shown again." in html
        assert "navigator.clipboard.writeText(field.value)" in html
        assert "A new token is shown once and cannot be shown again." in html
        # The secret goes into a read-only field's value, never into markup, and is wiped on close.
        assert "field.readOnly = true;" in html
        assert "field.value = secret;" in html
        assert "field.value = '';" in html
        assert "innerHTML = secret" not in html

    def test_each_token_shows_its_last_use_and_last_denial(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "pf-st-activity" in html
        assert "'Never used'" in html
        assert "'Last used ' + stamp(t.last_used_at)" in html
        assert "Last denied" in html
        assert "esc(scopedExpiryText(t)) + activityHtml + detail" in html

    def test_expiring_and_expired_tokens_are_flagged(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "Expires in ' + esc(t.days_left)" in html
        assert ">Expired</span>'" in html
        assert "'Expires ' + day + ' (in '" in html
        assert "dataAttr('revoke_scoped_token', { token_id: t.id })" in html
        assert "dataAttr('revoke_all_scoped_tokens', {})" in html

    def test_the_form_offers_rules_tools_and_the_add_covered_tools_button(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "data-st-label" in html
        assert "data-st-rule" in html
        assert "data-st-tool" in html
        assert "Add the tools these rules cover" in html
        assert "post('create_scoped_token'" in html
        assert "max=\"' +\n      esc(SCOPED.max_days)" in html

    def test_the_form_offers_the_personal_data_checkbox_and_the_list_shows_its_badge(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "Let exports, downloads and page uploads carry personal data" in html
        assert "Only for calls made with this token, and only for Drive downloads delivered as a file, Grist, " in html
        assert "Other calls still stop for review when personal data is found." in html
        assert "allow_personal_data: !!ui.stPersonalData" in html
        assert "personal data allowed" in html

    def test_reads_checkbox_label_and_help_are_rendered(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "Let reads return personal data" in html
        assert 'data-st-personal-data-reads="1"' in html
        assert "Applies only to calls made with this token, and only to Gmail message and thread reads and Slack " in html
        assert "reads that one of the token\\'s rules covers. The personal-data scan still runs" in html
        assert "but the AI system receives the content." in html
        assert "ui.stPersonalDataReads = !ui.stPersonalDataReads" in html

    def test_reads_badge_is_rendered(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "if (t.allow_personal_data_reads)" in html
        assert "personal data in reads allowed</span>" in html

    def test_create_payload_carries_the_reads_flag(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "allow_personal_data_reads: !!ui.stPersonalDataReads" in html
        assert "ui.stPersonalDataReads = !!t.allow_personal_data_reads;" in html
        assert "ui.stPersonalDataReads = false;" in html

    def test_rules_show_their_resource_with_the_verbs_as_a_detail(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "withValuesHtml(r.title_parts, r.value_items, r.title)" in html
        assert "'allows ' + r.verbs.join(', ')" in html

    def test_each_tool_shows_whether_a_selected_rule_covers_it(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "text: 'no rule needed'" in html
        assert "text: 'covered by ' + coverage[name].join('; ')" in html
        assert "text: 'no selected rule covers it', uncovered: true" in html
        assert "(SCOPED.auto_tools || []).indexOf(name)" in html

    def test_ticked_tools_no_rule_covers_are_listed_in_a_warning(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert 'data-st-uncovered="1">No selected rule covers' in html
        assert "will be denied. Select a rule that covers" in html

    def test_no_notice_for_a_missing_or_limited_rule(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "a rule that no longer exists" not in html
        assert "limited to" not in html
        assert "data-st-dup-missing" not in html
        assert "data-st-dup-limited" not in html

    def test_the_rules_block_has_its_hint_sub_titles_and_button(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert ("Rules belong to this token only: they never auto-accept anything for your other AI clients, ' +\n"
                "      'and changing or deleting an Auto-accept rule later does not change the token. ' +\n"
                "      'A tool that needs approval works only where one of the token\\'s rules covers it.") in html
        assert "New rule for this token" in html
        assert "Copy from my Auto-accept rules" in html
        assert "From ' + esc(dupOf.label)" in html
        assert "Add rule to token" in html
        assert "A copy: later changes to the Auto-accept rule do not reach the token." in html
        assert "You have no Auto-accept rules to copy." in html
        for attr in ("data-st-new-group", "data-st-new-value", "data-st-new-verb", "data-st-new-add",
                     "data-st-new-remove", "data-st-new-list", "data-st-dup-rule"):
            assert attr in html

    def test_the_form_says_so_when_no_scope_group_is_offered(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "No connector is enabled, so there is no scope to make a rule for." in html

    def test_the_create_request_carries_rule_requests_not_rule_ids(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "rule_ids" not in html
        assert "{kind: 'new', group: nr.group, value: nr.value, verbs: nr.verbs}" in html
        assert "{kind: 'copy', rule_id: id}" in html
        assert "{kind: 'duplicate', token_id: ui.stDuplicateOf, rule_id: r.id}" in html
        assert "rules: rules, tools: tools" in html

    def test_the_pair_hint_is_shared_with_the_auto_accept_page(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "function pairHint(group, checkedVerbs, value)" in html
        assert "return pairHint(group, ui.aaCheckedVerbs, ui.aaValue);" in html
        assert "pairHint(newGroup, ui.stNewVerbs, ui.stNewValue)" in html

    def test_scope_group_labels_and_value_hints_are_escaped(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "</script><i>g</i>" not in html
        assert "</script><u>h</u>" not in html

    def test_resource_values_toggle_between_ids_and_names_on_both_pages(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert 'data-show-ids-toggle="1"' in html
        assert "withValuesHtml(r.sentence_parts, r.value_items, r.sentence)" in html
        assert "pf-val-invalid" in html
        assert "Shown in red: not found, it may have been deleted." in html

    def test_a_token_can_be_duplicated_and_the_form_cleared(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert 'data-st-duplicate="' in html
        assert "aria-label=\"Duplicate ' + esc(t.label)" in html
        assert "data-st-clear" in html
        assert ">Clear form</div>" in html
        assert "/^(.*) (\\d+)$/" in html

    def test_a_token_can_be_renamed(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert 'data-st-rename="' in html
        assert "aria-label=\"Rename ' + esc(t.label)" in html
        assert "rename_scoped_token" in html

    def test_tools_follow_the_chosen_rules(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "data-st-other-tools" in html
        assert "Tools that need no rule, from other connectors (" in html
        assert "Select a rule to see the tools it covers, or pick a tool that needs no rule." in html

    def test_no_rule_selected_opens_the_tools_that_need_no_rule(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "'<details data-st-other-tools=\"1\"' + (anyRule ? '' : ' open')" in html
        assert "'Tools that need no rule (') + otherTools.length" in html

    def test_rule_selected_keeps_the_other_connectors_disclosure_closed(self):
        html = build_html(_make_state(), scoped_tokens=self.DATA)
        assert "anyRule ? 'Tools that need no rule, from other connectors (' : " in html


class TestScopedTokenProposal:
    """The review panel for an AI-proposed token: embedded escaped, with its review strings in the page."""

    HOSTILE = "</script><img src=x onerror=alert(1)>"
    PROPOSAL = {
        "id": "p1", "label": HOSTILE, "days": 30, "tools": ["gmail_get_message"],
        "allow_personal_data": True, "allow_personal_data_reads": False,
        "rules": [{"request": {"kind": "new", "group": "gmail.anything", "value": "", "verbs": ["read"]},
                   "sentence": HOSTILE}],
        "agent_label": HOSTILE, "reason": HOSTILE, "source": "explicit", "uncovered_tools": [],
        "expires_at": 1798675200,
    }

    def test_build_html_embeds_the_proposal_escaped(self):
        html = build_html(_make_state(), initial_section="scoped_tokens",
                          scoped_tokens=TestScopedTokensSection.DATA, scoped_proposal=self.PROPOSAL)
        assert "window.__pfScopedProposal = " in html
        assert self.HOSTILE not in html
        assert html.count("</script>") == html.count("<script")

    def test_review_strings_present(self):
        html = build_html(_make_state(), scoped_tokens=TestScopedTokensSection.DATA, scoped_proposal=self.PROPOSAL)
        for text in (
            "Review proposed token",
            "An AI client proposed this token. Untick anything the job does not need. "
            "You can only narrow it here; to add more, use New token.",
            "The AI client reports this name itself; PrivacyFence does not verify it.",
            "The AI client asked for this.",
            "This token proposal has expired, was declined or was already used.",
            "No rule covers these tools, so the token\\'s calls to them will be denied: ",
            "Delegate access to this token?",
            "You are delegating access. Anything that holds this token can act as you within these rules, for ",
            "Revoke it in Settings → Scoped tokens if it leaks or the job is retired.",
            "This token may also return or upload personal data.",
            "Type the token name to confirm",
            "decline_scoped_token_proposal",
            "create_proposed_scoped_token",
        ):
            assert text in html

    def test_delegate_button_text(self):
        html = build_html(_make_state(), scoped_tokens=TestScopedTokensSection.DATA, scoped_proposal=self.PROPOSAL)
        assert "btn(' primary', 'Delegate access')" in html
        assert "btn('', 'Cancel')" in html

    def test_waiting_list_renders_when_proposals_exist(self):
        data = {**TestScopedTokensSection.DATA,
                "proposals": [{"id": "p1", "label": "nightly", "agent_label": "Claude", "expires_at": 1798675200}],
                "proposals_enabled": True}
        html = build_html(_make_state(), initial_section="scoped_tokens", scoped_tokens=data)
        assert "Waiting for review" in html
        assert "' — proposed by '" in html
        assert "/settings/scoped_tokens/proposals/" in html
        assert '"proposals": [{"id": "p1"' in html


class TestOrgCapabilities:
    """window.__pfCapabilities is a separate embedded global from
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
        # Stats is org-only, and Connect an AI client needs an /mcp URL the caller did not pass;
        # every other section stays.
        assert caps["sections"]["stats"] is False
        assert caps["sections"]["ai_client"] is False
        assert caps["sections"]["scoped_tokens"] is False
        assert all(v for k, v in caps["sections"].items() if k not in ("stats", "ai_client", "scoped_tokens"))
        assert caps["not_applicable_actions"] == []

    def test_local_state_is_unaffected_by_capabilities(self):
        # The byte-for-byte invariant (TestStateEmbedding) must hold
        # regardless of mode/is_admin -- capabilities never leak into state.
        state = _make_state()
        html = build_html(state, mode="org", is_admin=True)
        assert _extract_initial_state(html) == state

    def test_org_admin_sees_general_but_not_privacy(self):
        # The install-wide Privacy Filter is an Admin section: an administrator's Settings is
        # the same page as anyone else's.
        caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=True))
        assert caps["mode"] == "org"
        assert caps["is_admin"] is True
        assert caps["sections"]["general"] is True
        assert caps["sections"]["privacy"] is False
        assert caps["sections"]["auto_accept"] is True
        assert caps["sections"]["about"] is True

    def test_org_non_admin_sees_general_but_not_privacy(self):
        # General carries every principal's own passkey scope (ADR 0091).
        caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=False))
        assert caps["sections"]["general"] is True
        assert caps["sections"]["privacy"] is False
        # Per-principal, not admin-gated -- every org principal keeps this.
        assert caps["sections"]["auto_accept"] is True

    def test_org_mode_never_shows_connectors(self):
        for is_admin in (True, False):
            caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=is_admin))
            assert caps["sections"]["connectors"] is False

    def test_org_mode_shows_audit_to_every_principal_without_its_local_only_controls(self):
        # Each principal's own recent decisions, read-only -- export and
        # log level have no org route, so the page must not draw them.
        for is_admin in (True, False):
            caps = self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=is_admin))
            assert caps["sections"]["audit"] is True
            assert {"export_audit_log", "set_log_level"} <= set(caps["not_applicable_actions"])

    def test_there_is_no_ai_systems_page(self):
        assert "agents" not in self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=True))["sections"]
        assert "agents" not in self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=False))["sections"]
        assert "agents" not in self._extract_capabilities(build_html(_make_state(), mode="local"))["sections"]

    def test_stats_is_not_a_settings_section_for_anyone(self):
        for is_admin in (True, False):
            assert self._extract_capabilities(build_html(_make_state(), mode="org", is_admin=is_admin))["sections"]["stats"] is False

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
        # The install-wide privacy actions are the Admin dispatcher's now.
        assert {"toggle_pii_detection", "toggle_pii_category", "set_default_policy", "set_category_policy"} <= not_applicable
        # Actions org mode genuinely keeps must never be marked inapplicable.
        assert not {"add_policy_rule", "remove_policy_rule", "update_policy_rule", "set_policy_rule_verbs"} & not_applicable

    def test_nav_filters_hidden_sections(self):
        html_non_admin = build_html(_make_state(), mode="org", is_admin=False)
        nav_fn_start = html_non_admin.index("function renderNav")
        nav_fn_end = html_non_admin.index("function renderNotificationsDetailControl")
        nav_fn = html_non_admin[nav_fn_start:nav_fn_end]
        assert "CAPS.sections[key] === false" in nav_fn


class TestPersonalTransferCard:
    """"Move your settings" (Auto-accept) is drawn client-side only when the page is told the
    routes behind it exist: the capability is the switch, the card text lives in the JS."""

    @staticmethod
    def _caps(html: str) -> dict:
        match = re.search(r"window\.__pfCapabilities = (\{.*?\});</script>", html, re.DOTALL)
        assert match
        return json.loads(match.group(1))

    @pytest.mark.parametrize("mode", ["local", "org"])
    def test_capability_is_on_when_requested(self, mode):
        html = build_html(_make_state(), mode=mode, is_admin=True, personal_transfer=True)
        assert self._caps(html)["personalTransfer"] is True
        assert "if (CAPS.personalTransfer)" in html
        assert "Move your settings" in html
        assert 'data-pf-personal-export="1"' in html
        assert 'data-pf-personal-import="1"' in html
        assert "Importing only adds; it never removes or narrows anything." in html

    @pytest.mark.parametrize("mode", ["local", "org"])
    def test_capability_is_off_by_default(self, mode):
        html = build_html(_make_state(), mode=mode)
        assert self._caps(html)["personalTransfer"] is False

    def test_card_sits_between_the_subtitle_and_the_filter_bar(self):
        html = build_html(_make_state(), personal_transfer=True)
        body = html.split("function renderAutoAccept", 1)[1]
        assert body.index("pf-page-subtitle") < body.index("CAPS.personalTransfer") < body.index("pf-aa-filterbar")


# --------------------------------------------------------------------------- #
# The Admin surface
# --------------------------------------------------------------------------- #

def _admin_caps(html: str) -> dict:
    match = re.search(r"window\.__pfCapabilities = (\{.*?\});</script>", html, re.DOTALL)
    assert match
    return json.loads(match.group(1))


def _row(key, label, *, offered=True, app_source="builtin", client_id="", relay=False, options=None):
    return {
        "key": key, "label": label, "offered": offered, "app_source": app_source, "client_id": client_id,
        "relay": relay, "options": options or {},
    }


def _admin_state(*rows, **extra):
    state = {
        "services": list(rows), "rooms": {"count": 0, "synced_at": ""}, "unattended_sessions": {},
        "restart_needed": [],
    }
    state.update(extra)
    return state


class TestAdminSurfaceCapabilities:
    def test_the_settings_surface_names_itself_and_is_otherwise_unchanged(self):
        for mode in ("local", "org"):
            caps = _admin_caps(build_html(_make_state(), mode=mode, is_admin=True))
            assert caps["surface"] == "settings"
            assert "services" not in caps["sections"] and "org_config" not in caps["sections"]

    def test_local_mode_gets_services_and_the_bundle_upload(self):
        caps = _admin_caps(build_html(_admin_state(), surface="admin"))
        assert (caps["surface"], caps["mode"]) == ("admin", "local")
        assert caps["sections"] == {"services": True, "features": True, "org_config": True}

    def test_local_mode_suppresses_the_org_only_admin_actions(self):
        caps = _admin_caps(build_html(_admin_state(), surface="admin"))
        suppressed = set(caps["not_applicable_actions"])
        assert {"set_auto_provisioning", "set_web_push", "set_authz", "tighten_step_up"} <= suppressed
        assert not suppressed & {"offer_service", "withdraw_service", "set_server_address", "clear_rooms"}

    def test_an_org_admin_gets_every_section_and_nothing_is_suppressed(self):
        caps = _admin_caps(build_html(_admin_state(), surface="admin", mode="org", is_admin=True))
        assert caps["sections"] == dict.fromkeys(
            ("services", "features", "delivery", "access", "step_up", "tokens", "audit", "privacy", "stats"), True,
        )
        assert caps["not_applicable_actions"] == []

    def test_an_org_non_admin_gets_no_section(self):
        caps = _admin_caps(build_html(_admin_state(), surface="admin", mode="org", is_admin=False))
        assert not any(caps["sections"].values())

    def test_settings_only_inputs_do_not_reach_the_admin_surface(self):
        html = build_html(
            _admin_state(), surface="admin", ai_client={"mcp_url": "http://x"}, scoped_tokens={"a": 1},
            personal_transfer=True,
        )
        caps = _admin_caps(html)
        assert "ai_client" not in caps["sections"] and "personalTransfer" not in caps
        assert "window.__pfAiClient" not in html.split("<script", 4)[0] and "__pfScopedTokens = " not in html

    def test_initial_section_services_is_embedded(self):
        html = build_html(_admin_state(), surface="admin", initial_section="services")
        assert 'window.__pfInitialSection = "services";' in html

    def test_render_branches_to_the_admin_renderer_first(self):
        html = build_html(_admin_state(), surface="admin")
        body = html.split("function render(state) {", 1)[1]
        assert body.index("renderAdmin(state); return;") < body.index("renderNav(state)")
        assert "function renderServices" in html
        admin = html.split("function renderAdmin(state) {", 1)[1].split("function renderSection(state)", 1)[0]
        assert "renderNav(" not in admin and "renderSection(" not in admin

    def test_the_admin_nav_keys_and_labels_are_in_order(self):
        html = build_html(_admin_state(), surface="admin")
        nav = html.split("var ADMIN_NAV_ITEMS = [", 1)[1].split("];", 1)[0]
        pairs = re.findall(r"\['(\w+)', '([^']+)'\]", nav)
        assert pairs == [
            ("services", "Services"), ("features", "Features"), ("org_config", "Organization configuration"),
            ("delivery", "File delivery"), ("access", "Sign-in"), ("step_up", "Step-up"),
            ("tokens", "Scoped tokens"), ("audit", "Audit forwarding"), ("privacy", "Privacy Filter"),
            ("stats", "Stats"),
        ]


_RENDER_HARNESS = r"""
const vm = require('vm');
const scripts = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const app = { innerHTML: '' };
const document = {
  getElementById: () => app, querySelector: () => null, querySelectorAll: () => [],
  addEventListener: (name, fn) => { if (name === 'DOMContentLoaded') fn(); },
  body: { addEventListener: () => {} }, activeElement: null,
};
const window = { addEventListener: () => {} };
window.window = window;
vm.runInNewContext(scripts.join('\n'), { window, document, console, Date, JSON, Object, Array, String, parseInt });
console.log(app.innerHTML);
"""


def _render_admin(state, *, mode="local", is_admin=False, section="services"):
    """The Admin page's markup as the browser would draw it: the page's own scripts run in node
    against a stub document that keeps what the renderer writes."""
    return _render(build_html(state, surface="admin", initial_section=section, mode=mode, is_admin=is_admin))


def _render(html):
    scripts = re.findall(r"<script nonce=\"[^\"]+\">(.*?)</script>", html, re.DOTALL)
    done = subprocess.run(
        ["node", "-e", _RENDER_HARNESS], input=json.dumps(scripts),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


@needs_node
class TestServicesSection:
    def test_the_nav_shows_only_the_sections_the_capabilities_include(self):
        local = _render_admin(_admin_state(_row("google", "Google")))
        assert re.findall(r'aria-label="([^"]+)" data-nav', local) == ["Services", "Features", "Organization configuration"]
        org = _render_admin(_admin_state(_row("google", "Google")), mode="org", is_admin=True)
        assert re.findall(r'aria-label="([^"]+)" data-nav', org) == [
            "Services", "Features", "File delivery", "Sign-in", "Step-up", "Scoped tokens", "Audit forwarding",
            "Privacy Filter", "Stats",
        ]
        assert 'aria-selected="true"' in org

    def test_a_bundle_app_names_its_client_and_warns_about_reconnecting(self):
        page = _render_admin(_admin_state(_row("slack", "Slack", app_source="bundle", client_id="slack-id")))
        assert "Uses your organization&#39;s own app (client slack-id), from the configuration file." in page \
            or "Uses your organization's own app (client slack-id), from the configuration file." in page
        assert "Changing the app makes everyone who connected Slack connect again." in page
        assert "Sign-in codes for this service pass through" not in page

    def test_a_relayed_builtin_app_says_so(self):
        page = _render_admin(
            _admin_state(_row("atlassian", "Atlassian", relay=True)), mode="org", is_admin=True,
        )
        assert "Uses the built-in PrivacyFence app through oauth.privacyfence.eu." in page
        assert "Changing the app makes everyone who connected Atlassian connect again." in page
        assert "without PKCE" not in page

    @pytest.mark.parametrize("key,label", [("slack", "Slack"), ("miro", "Miro")])
    def test_relayed_slack_and_miro_warn_that_codes_pass_through_the_relay_without_pkce(self, key, label):
        page = _render_admin(_admin_state(_row(key, label, relay=True)), mode="org", is_admin=True)
        assert (
            "Sign-in codes for this service pass through oauth.privacyfence.eu without PKCE. "
            "Use your own app to avoid this."
        ) in page

    def test_an_own_app_for_slack_has_no_pkce_warning(self):
        page = _render_admin(_admin_state(_row("slack", "Slack", app_source="bundle", client_id="x")), mode="org", is_admin=True)
        assert "without PKCE" not in page

    def test_an_offered_service_with_no_app_says_how_to_add_one(self):
        page = _render_admin(_admin_state(_row("salesforce", "Salesforce", app_source="none")))
        assert "No app is available for this service." in page
        assert "Add your organization" in page and "to the configuration file." in page
        assert "Changing the app makes everyone" not in page

    def test_a_service_that_is_not_offered_says_so_and_offers_a_switch(self):
        page = _render_admin(_admin_state(_row("google", "Google", offered=False, app_source="none")))
        assert "Not offered." in page
        assert "No app is available" not in page
        assert '"offer_service"' in page.replace("&quot;", '"') or "data-action=\"offer_service\"" in page

    def test_the_switch_posts_offer_or_withdraw_for_the_service(self):
        off = _render_admin(_admin_state(_row("google", "Google", offered=False)))
        on = _render_admin(_admin_state(_row("google", "Google", offered=True)))
        assert 'data-action="offer_service"' in off and 'data-action="withdraw_service"' not in off
        assert 'data-action="withdraw_service"' in on and 'data-action="offer_service"' not in on
        assert "{&quot;service&quot;:&quot;google&quot;}" in on or '{"service":"google"}' in on

    def test_each_row_links_to_its_provider_setup_doc(self):
        keys = ["google", "slack", "salesforce", "atlassian", "miro", "telegram", "grist", "peek"]
        page = _render_admin(_admin_state(*[_row(k, k.title()) for k in keys]))
        for key, slug in zip(keys, [
            "google-cloud-setup", "slack-setup", "salesforce-setup", "atlassian-setup", "miro-setup",
            "telegram-setup", "grist-setup", "peek-setup",
        ], strict=True):
            assert f'href="https://privacyfence.eu/docs/{slug}/"' in page, key
        assert page.count("How to use your own app") == len(keys)

    def test_the_initial_section_is_services(self):
        page = _render_admin(_admin_state(_row("google", "Google")))
        assert '<h1 class="pf-page-title">Services</h1>' in page

    def test_a_pending_restart_is_announced(self):
        page = _render_admin(_admin_state(_row("google", "Google"), restart_needed=["Web push", "Step-up"]))
        assert "Saved. Restart PrivacyFence for these changes to take effect: Web push, Step-up." in page
        assert "Saved. Restart" not in _render_admin(_admin_state(_row("google", "Google")))

    def test_the_controller_error_is_shown(self):
        page = _render_admin(_admin_state(_row("google", "Google"), error="org_config.json carries settings ... are ignored"))
        assert "are ignored" in page and "pf-error-banner" in page


@needs_node
class TestRelayStatusLine:
    def _line(self, status):
        page = _render_admin(_admin_state(_row("google", "Google"), relay_status=status), mode="org", is_admin=True)
        match = re.search(r'<div class="pf-row-desc pf-relay-status">(.*?)</div>', page)
        assert match, page
        return match.group(1)

    def test_registered(self):
        line = self._line({"last_ok_at": 1_700_000_000, "last_error": "", "needed": True})
        assert line.startswith("Relay registration: registered ") and "Relay terms" in line

    def test_failed(self):
        line = self._line({"last_ok_at": None, "last_error": "unreachable", "needed": True})
        assert line.startswith(
            "Relay registration failed: unreachable. Built-in apps cannot sign in until it succeeds."
        )

    def test_not_needed(self):
        line = self._line({"last_ok_at": None, "last_error": "", "needed": False})
        assert line.startswith("Relay registration: not needed.")

    def test_every_variant_ends_with_the_relay_terms_link(self):
        for status in (
            {"last_ok_at": 1_700_000_000, "last_error": "", "needed": True},
            {"last_ok_at": None, "last_error": "unreachable", "needed": True},
            {"last_ok_at": None, "last_error": "", "needed": False},
        ):
            assert self._line(status).endswith(
                '<a href="https://privacyfence.eu/relay-terms/" target="_blank" rel="noopener">Relay terms</a>'
            )

    def test_local_mode_has_no_relay_line(self):
        assert "pf-relay-status" not in _render_admin(_admin_state(_row("google", "Google")))


def _server_row(key, *, source, address="", locked=None, auto=None):
    field = "base_url" if key == "peek" else "server_url"
    options = {
        field: address, "address_source": source, "locked": (source != "admin") if locked is None else locked,
        "auto_provision": auto or {
            "enabled": False, "trust_unverified_email": False, "available": True, "reason": "", "key_set": True,
        },
    }
    return _row(key, key.title(), app_source="server", options=options)


@needs_node
class TestServerAndAutomaticAccounts:
    def _org(self, *rows):
        return _render_admin(_admin_state(*rows), mode="org", is_admin=True)

    def test_a_server_from_the_environment_names_the_variable_and_has_no_address_form(self):
        page = self._org(_server_row("peek", source="environment", address="https://peek.example.com"))
        assert "Server: https://peek.example.com (set by PRIVACYFENCE_PEEK_BASE_URL on the server)." in page
        assert "data-admin-address" not in page
        page = self._org(_server_row("grist", source="environment", address="https://grist.example.com"))
        assert "(set by PRIVACYFENCE_GRIST_SERVER_URL on the server)." in page

    def test_no_server_in_org_mode_asks_for_one_and_shows_the_form(self):
        page = self._org(_server_row("peek", source="admin"))
        assert "Set the server address below." in page
        assert 'data-admin-address="peek"' in page and 'data-admin-address-save="peek"' in page

    def test_a_server_that_is_not_offered_has_no_server_line_or_form(self):
        row = _server_row("peek", source="admin")
        row["offered"] = False
        page = self._org(row)
        assert "Set the server address below." not in page and "data-admin-address" not in page
        assert "Automatic accounts" not in page

    def test_a_locked_imported_or_bundle_address_has_no_form(self):
        page = self._org(_server_row("peek", source="imported", address="https://p.example.com"))
        assert "Server: https://p.example.com" in page and "data-admin-address" not in page
        page = self._org(_server_row("grist", source="bundle", address="https://g.example.com"))
        assert "data-admin-address" not in page

    def test_automatic_accounts_on_shows_the_switch_and_the_trust_checkbox(self):
        auto = {"enabled": True, "trust_unverified_email": False, "available": True, "reason": "", "key_set": True}
        page = self._org(_server_row("peek", source="environment", address="https://p.example.com", auto=auto))
        assert "Automatic accounts: on. Each person who signs in gets a Peek account connected for them." in page
        assert 'data-action="set_auto_provisioning"' in page
        assert "Trust email addresses the identity provider does not mark verified" in page
        assert "is not set on the server" not in page
        grist = self._org(_server_row("grist", source="environment", address="https://g.example.com", auto=auto))
        assert "gets a Grist key connected for them." in grist

    def test_the_switch_and_the_checkbox_post_the_whole_section(self):
        auto = {"enabled": True, "trust_unverified_email": False, "available": True, "reason": "", "key_set": True}
        page = self._org(_server_row("peek", source="environment", address="https://p.example.com", auto=auto))
        payloads = [p.replace("&quot;", '"') for p in re.findall(r"data-payload='([^']*)'", page)]
        assert '{"service":"peek","enabled":false,"trust_unverified_email":false}' in payloads
        assert '{"service":"peek","enabled":true,"trust_unverified_email":true}' in payloads

    def test_automatic_accounts_on_without_the_key_says_which_variable_is_missing(self):
        auto = {"enabled": True, "trust_unverified_email": True, "available": True, "reason": "", "key_set": False}
        page = self._org(_server_row("peek", source="environment", address="https://p.example.com", auto=auto))
        assert "Automatic accounts: on, but PRIVACYFENCE_PEEK_PROVISIONING_KEY is not set on the server." in page
        grist = self._org(_server_row("grist", source="environment", address="https://g.example.com", auto=auto))
        assert "PRIVACYFENCE_GRIST_PROVISIONING_KEY is not set on the server." in grist

    def test_unavailable_automatic_accounts_show_the_reason_and_no_switch(self):
        reason = (
            "Automatic accounts need the Peek address from PRIVACYFENCE_PEEK_BASE_URL on the server, so the "
            "provisioning key only goes where the server's operator points it."
        )
        auto = {"enabled": False, "trust_unverified_email": False, "available": False, "reason": reason, "key_set": False}
        page = self._org(_server_row("peek", source="admin", address="https://p.example.com", auto=auto))
        assert "Automatic accounts need the Peek address from PRIVACYFENCE_PEEK_BASE_URL" in page
        assert 'data-action="set_auto_provisioning"' not in page
        assert "Trust email addresses" not in page

    def test_a_bundle_grist_oauth_app_shows_its_reason(self):
        reason = "Automatic accounts work only with API keys; this server uses a Grist OAuth app."
        auto = {"enabled": False, "trust_unverified_email": False, "available": False, "reason": reason, "key_set": False}
        row = _server_row("grist", source="bundle", address="https://g.example.com", auto=auto)
        row["client_id"] = "grist-client"
        page = self._org(row)
        assert "Automatic accounts work only with API keys; this server uses a Grist OAuth app." in page
        assert "data-admin-address" not in page
        assert "client grist-client" in page

    def test_local_mode_has_the_address_form_but_no_automatic_accounts(self):
        auto = {
            "enabled": False, "trust_unverified_email": False, "available": False, "key_set": False,
            "reason": "Automatic accounts are available only on an organization server.",
        }
        page = _render_admin(_admin_state(_server_row("peek", source="admin", auto=auto)))
        assert 'data-admin-address="peek"' in page
        assert "Automatic accounts" not in page
        assert "Set the server address below." not in page


def _features_state(**overrides):
    state = _admin_state(
        _row("atlassian", "Atlassian", options={"assets": True, "assets_workspace_id": "ws-1"}),
        _row("grist", "Grist", app_source="server", options={"server_url": "https://grist.example.com", "locked": False}),
        _row("peek", "Peek", app_source="server", options={"base_url": "https://peek.example.com", "locked": False}),
        rooms={"count": 3, "synced_at": "2026-10-01T08:00:00Z"}, unattended_sessions={"enabled": True},
    )
    state.update(overrides)
    return state


@needs_node
class TestFeaturesSection:
    def _page(self, state=None, **kwargs):
        return _render_admin(state or _features_state(), section="features", **kwargs)

    def test_each_form_is_prefilled_from_the_state(self):
        page = self._page()
        assert 'data-admin-assets-enabled="1" checked' in page
        assert 'value="ws-1" data-admin-assets-workspace="1"' in page
        assert 'value="https://grist.example.com" data-admin-address="grist"' in page
        assert 'value="https://peek.example.com" data-admin-address="peek"' in page
        assert 'aria-checked="true"' in page and "set_unattended_sessions" in page

    def test_the_unattended_switch_posts_the_opposite_value(self):
        assert '"enabled":false' in self._page()
        off = self._page(_features_state(unattended_sessions={"enabled": False}))
        assert '"enabled":true' in off

    def test_a_locked_server_has_no_form(self):
        state = _features_state()
        for row in state["services"]:
            if row["key"] in ("grist", "peek"):
                row["options"]["locked"] = True
        page = self._page(state)
        assert "data-admin-address" not in page
        assert "cannot be changed here" in page
        state = _features_state()
        state["services"][1]["options"]["locked"] = True
        page = self._page(state)
        assert 'data-admin-address="grist"' not in page and 'data-admin-address="peek"' in page

    def test_the_rooms_control_uploads_and_clears(self):
        page = self._page()
        assert "3 rooms, synced 2026-10-01T08:00:00Z." in page
        assert 'data-action="upload_rooms"' in page
        assert 'data-action="clear_rooms"' in page
        empty = self._page(_features_state(rooms={"count": 0, "synced_at": ""}))
        assert "No room directory uploaded." in empty
        assert 'data-action="upload_rooms"' in empty and "clear_rooms" not in empty

    def test_org_mode_shows_the_same_section(self):
        page = self._page(mode="org", is_admin=True)
        assert "data-admin-assets-save" in page and 'data-action="upload_rooms"' in page


@needs_node
class TestOrganizationConfigurationCard:
    def test_the_admin_surface_has_the_card_with_the_new_description(self):
        page = _render_admin(_admin_state(), section="org_config")
        assert "Organization Configuration" in page
        assert "Your own app credentials, and the server and identity-provider settings, from your IT administrator." in page
        assert 'data-action="install_org_config"' in page

    def test_the_error_banner_shows_when_the_state_has_one(self):
        page = _render_admin(_admin_state(error="Settings in this file are ignored."), section="org_config")
        assert "Settings in this file are ignored." in page

    def test_the_local_settings_surface_no_longer_has_the_card(self):
        page = _render(build_html(_make_state(), initial_section="general"))
        assert "Check for Updates" in page
        assert "Organization Configuration" not in page and "install_org_config" not in page


_SENTINEL = "sk-SENTINEL-secret-value"


def _org_state(**overrides):
    state = _features_state(
        restart_needed=[],
        download_delivery={
            "inline_max_bytes": 262144, "link_ttl_seconds": 900, "allow_disk_staging": True,
            "agent_links": False, "allow_exports": True,
        },
        web_push={"enabled": True},
        authz={"allowed_domains": ["example.com", "example.org"], "groups_claim": "groups", "required_groups": ["pf-users"]},
        step_up={"enabled": True, "require_passkey": False, "scope": "writes_and_pii_reads"},
        scoped_tokens={"enabled": True, "max_days": 30, "allow_ai_proposals": True},
        audit_forwarding={
            "enabled": True, "kind": "http", "syslog": {"host": "logs.example.com", "port": 6514, "protocol": "tcp"},
            "http": {"url": "https://siem.example.com/in", "bearer_token_env": "SIEM_TOKEN"},
        },
        privacy={
            "groups": [{"key": "privacy", "label": "Gmail"}],
            "default_policy": {"privacy": "block"},
            "categories": {"privacy": [{"key": "body", "label": "Message body", "policy": "redact"}]},
            "pii_enabled": True, "pii_ip": True, "pii_financial": False,
        },
        stats={
            "users": {"total": 4, "with_connectors": 3, "with_rules": 1},
            "connectors": [{"service": "gmail", "label": "Gmail", "configured": True, "users": 3}],
            "rules": {"total": 2, "by_connector": []},
        },
    )
    state.update(overrides)
    return state


def _render_org(section, state=None):
    return _render_admin(state or _org_state(), mode="org", is_admin=True, section=section)


@needs_node
class TestOrgAdminSections:
    def test_the_org_admin_surface_has_all_of_the_org_sections(self):
        caps = _admin_caps(build_html(_org_state(), surface="admin", mode="org", is_admin=True))
        keys = [k for k, v in caps["sections"].items() if v]
        assert keys == ["services", "features", "delivery", "access", "step_up", "tokens", "audit", "privacy", "stats"]
        page = _render_org("services")
        for label in ("File delivery", "Sign-in", "Step-up", "Scoped tokens", "Audit forwarding", "Privacy Filter", "Stats"):
            assert f'aria-label="{label}"' in page

    def test_the_local_admin_surface_has_none_of_them(self):
        caps = _admin_caps(build_html(_admin_state(), surface="admin"))
        assert not {"delivery", "access", "step_up", "tokens", "audit", "privacy", "stats"} & set(caps["sections"])

    def test_the_delivery_form_is_prefilled(self):
        page = _render_org("delivery")
        assert 'data-f="inline_max_bytes" data-t="int" aria-label="Largest file sent inline (bytes)" value="262144"' in page
        assert 'data-f="link_ttl_seconds" data-t="int" aria-label="Download link lifetime (seconds)" value="900"' in page
        assert 'data-f="allow_disk_staging" data-t="bool" checked' in page
        assert 'data-f="agent_links" data-t="bool" aria-label' in page
        assert 'data-f="allow_exports" data-t="bool" checked' in page
        assert 'data-admin-form-save="delivery"' in page

    def test_the_sign_in_form_is_prefilled_with_one_item_per_line(self):
        page = _render_org("access")
        assert ">example.com\nexample.org</textarea>" in page
        assert 'data-f="groups_claim" data-t="text" aria-label="Groups claim" value="groups"' in page
        assert ">pf-users</textarea>" in page

    def test_the_step_up_form_offers_nothing_looser_than_the_current_value(self):
        page = _render_org("step_up")
        options = re.findall(r'<option value="(\w+)"', page)
        assert options == ["writes_and_pii_reads", "writes_and_reads"]
        assert 'data-f="enabled" data-t="bool" checked disabled' in page
        assert 'data-f="require_passkey" data-t="bool" aria-label' in page and "disabled" not in page.split('data-f="require_passkey"')[1].split(">")[0]
        assert "Relaxing step-up is a file edit (ADR 0068)." in page

    def test_the_widest_step_up_scope_offers_only_itself(self):
        state = _org_state(step_up={"enabled": False, "require_passkey": False, "scope": "writes_and_reads"})
        page = _render_org("step_up", state)
        assert re.findall(r'<option value="(\w+)"', page) == ["writes_and_reads"]
        assert 'data-f="enabled" data-t="bool" aria-label' in page

    def test_the_scoped_tokens_form_carries_the_ai_proposals_checkbox(self):
        page = _render_org("tokens")
        assert "Let AI clients propose a scoped token for a person to review and create." in page
        assert 'data-f="allow_ai_proposals" data-t="bool" checked' in page
        assert 'data-f="max_days" data-t="int" aria-label="Longest lifetime (days)" value="30"' in page
        assert "Needs scoped tokens turned on." not in page
        off = _render_org("tokens", _org_state(scoped_tokens={"enabled": False, "max_days": 30, "allow_ai_proposals": False}))
        assert "Needs scoped tokens turned on." in off

    def test_the_audit_form_is_prefilled_and_the_bearer_token_name_has_no_input(self):
        page = _render_org("audit")
        assert '<option value="http" selected>' in page and '<option value="tcp" selected>' in page
        assert 'data-f="syslog_host" data-t="text" aria-label="Syslog host" value="logs.example.com"' in page
        assert 'data-f="syslog_port" data-t="int" aria-label="Syslog port" value="6514"' in page
        assert 'data-f="http_url" data-t="text" aria-label="HTTP URL" value="https://siem.example.com/in"' in page
        assert "SIEM_TOKEN" in page and "Set by editing org/admin_config.json on the server." in page
        assert "bearer" not in page.split("data-f=")[-1].split(">")[0].lower()
        assert not re.search(r'data-f="[^"]*bearer', page) and 'value="SIEM_TOKEN"' not in page

    def test_privacy_has_the_pii_gate_toggles_and_the_policy_editor(self):
        page = _render_org("privacy")
        assert 'aria-label="PII Detection Gate"' in page and "toggle_pii_detection" in page
        assert "toggle_pii_category" in page and "detect_financial_figures" in page
        assert "set_default_policy" in page and "set_category_policy" in page

    def test_stats_renders_in_the_admin_surface(self):
        page = _render_org("stats")
        assert "data-stat-tile" in page and 'data-stat-connector="gmail"' in page

    def test_the_web_push_switch_is_on_features_in_org_mode_only(self):
        assert "set_web_push" in _render_org("features")
        assert "set_web_push" not in _render_admin(_features_state(), section="features")

    def test_the_restart_banner_renders(self):
        page = _render_org("delivery", _org_state(restart_needed=["File delivery", "Step-up"]))
        assert "Saved. Restart PrivacyFence for these changes to take effect: File delivery, Step-up." in page
        assert "Restart PrivacyFence" not in _render_org("delivery")

    def test_no_rendered_html_contains_a_secret_placed_in_the_state(self):
        state = _org_state()
        state["audit_forwarding"]["http"]["bearer_token"] = _SENTINEL
        state["audit_forwarding"]["bearer_token_value"] = _SENTINEL
        state["step_up"]["secret"] = _SENTINEL
        state["authz"]["client_secret"] = _SENTINEL
        for section in ("delivery", "access", "step_up", "tokens", "audit", "privacy", "stats", "features", "services"):
            assert _SENTINEL not in _render_org(section, state)


_SAVE_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const posted = [];
const handlers = {};
const field = (f, t, extra) => Object.assign({ getAttribute: (n) => ({ 'data-f': f, 'data-t': t })[n] }, extra);
const root = { querySelectorAll: () => input.fields.map((x) => field(x[0], x[1], { value: x[2], checked: x[2] })) };
const saveButton = { getAttribute: () => input.form };
const document = {
  getElementById: () => ({ innerHTML: '' }), querySelectorAll: () => [],
  querySelector: (sel) => (sel === '[data-admin-form="' + input.form + '"]' ? root : null),
  addEventListener: (name, fn) => { if (name === 'DOMContentLoaded') fn(); },
  body: { addEventListener: (name, fn) => { handlers[name] = fn; } }, activeElement: null,
};
const window = { addEventListener: () => {}, webkit: { messageHandlers: { pf: { postMessage: (m) => posted.push(m) } } } };
window.window = window;
vm.runInNewContext(input.scripts.join('\n'), { window, document, console, Date, JSON, Object, Array, String, parseInt });
handlers.click({ target: { closest: (sel) => (sel === '[data-admin-form-save]' ? saveButton : null) } });
console.log(JSON.stringify(posted));
"""


@needs_node
class TestAdminFormSave:
    def _save(self, form, fields):
        html = build_html(_org_state(), surface="admin", mode="org", is_admin=True, initial_section=form)
        scripts = re.findall(r"<script nonce=\"[^\"]+\">(.*?)</script>", html, re.DOTALL)
        done = subprocess.run(
            ["node", "-e", _SAVE_HARNESS], input=json.dumps({"scripts": scripts, "form": form, "fields": fields}),
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    def test_a_form_posts_its_whole_section_with_typed_values(self):
        posted = self._save("tokens", [["enabled", "bool", True], ["max_days", "int", "45"], ["allow_ai_proposals", "bool", False]])
        assert posted == [{"action": "set_scoped_tokens", "enabled": True, "max_days": 45, "allow_ai_proposals": False}]

    def test_a_number_field_that_is_not_a_number_is_sent_as_typed(self):
        posted = self._save("delivery", [["inline_max_bytes", "int", "lots"], ["allow_exports", "bool", True]])
        assert posted == [{"action": "set_download_delivery", "inline_max_bytes": "lots", "allow_exports": True}]

    def test_each_form_names_its_action(self):
        actions = {
            "delivery": "set_download_delivery", "access": "set_authz", "step_up": "tighten_step_up",
            "tokens": "set_scoped_tokens", "audit": "set_audit_forwarding",
        }
        for form, action in actions.items():
            assert self._save(form, [])[0]["action"] == action


@needs_node
def test_a_fresh_server_shows_the_defaults_in_force():
    page = _render_org("delivery", _org_state(download_delivery={}))
    assert 'value="100000"' in page and 'value="300"' in page and 'data-f="agent_links" data-t="bool" checked' in page
    assert 'value="90"' in _render_org("tokens", _org_state(scoped_tokens={}))
    assert 'value="6514"' in _render_org("audit", _org_state(audit_forwarding={}))
