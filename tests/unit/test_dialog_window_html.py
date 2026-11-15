"""Tests for dialog_window_html.py -- the small confirmation dialog HTML
template.

Pure-function module (same as approval_window_html.py, which this imports
from) -- these assert directly on the generated HTML strings.

The real, injection-relevant escaping coverage this replaces used to live in
test_approval_popup_escaping.py, round-tripping content through a real
`osascript` process to prove AppleScript string-literal breakout was
impossible. That vector doesn't exist anymore -- a webview bridge call takes
a string as a real DOM text value, never source text to be interpreted (see
this module's own docstring) -- so what's worth pinning down here is just
that build_confirmation_html actually HTML-escapes
everything it interpolates, the same defensive posture
build_card_stack_html takes with details_text.
"""
from __future__ import annotations

from privacyfence.approval_window_html import extract_csp_nonce
from privacyfence.dialog_window_html import (
    CONFIRM_WIDTH,
    build_confirmation_html,
)


class TestBuildConfirmationHtml:
    def test_title_and_message_lines_render(self):
        html = build_confirmation_html(
            title="PrivacyFence — Confirm Auto-Accept Rule",
            message_lines=["PrivacyFence will create an auto-accept rule:", "i_am_sender"],
            cancel_label="Cancel",
            confirm_label="Confirm",
        )
        assert "PrivacyFence — Confirm Auto-Accept Rule" in html
        assert "PrivacyFence will create an auto-accept rule:" in html
        assert "i_am_sender" in html

    def test_empty_lines_are_dropped_not_rendered_as_empty_paragraphs(self):
        html = build_confirmation_html(
            title="T", message_lines=["a", "", "b"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert "<p></p>" not in html

    def test_cancel_and_confirm_buttons_present(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Proceed",
        )
        assert 'data-pf-action="cancel"' in html
        assert 'data-pf-action="confirm"' in html
        assert ">Cancel<" in html
        assert ">Proceed<" in html

    def test_only_the_confirm_button_is_marked_primary(self):
        # data-pf-primary is what _JS's keydown handler excludes from
        # Enter/Space activating a focused control -- hitting Enter must
        # never silently accept. Confirmed on exactly the confirm button.
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert html.count('data-pf-primary="1"') == 1
        assert 'data-pf-primary="1" aria-label="Confirm" data-pf-action="confirm"' in html

    def test_buttons_start_disabled_in_markup(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert html.count('role="button" aria-disabled="true"') == 2

    def test_confirm_and_cancel_labels_are_html_escaped(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="<b>Cancel</b>", confirm_label="<i>Go</i>",
        )
        assert "<b>Cancel</b>" not in html
        assert "<i>Go</i>" not in html
        assert "&lt;b&gt;Cancel&lt;/b&gt;" in html
        assert "&lt;i&gt;Go&lt;/i&gt;" in html

    def test_title_is_html_escaped(self):
        html = build_confirmation_html(
            title="<script>alert(1)</script>", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_message_lines_are_html_escaped(self):
        html = build_confirmation_html(
            title="T", message_lines=['x" & do shell script "touch pwned" & "'],
            cancel_label="Cancel", confirm_label="Confirm",
        )
        assert '<p>x&quot; &amp; do shell script &quot;touch pwned&quot; &amp; &quot;</p>' in html

    def test_uses_the_confirm_width(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        # min(...,100%), not a bare fixed width -- see _document's own
        # docstring/comment: a bare `width: {CONFIRM_WIDTH}px` overflows any
        # viewport narrower than that (this document renders inside an
        # ordinary browser tab, not only a native window frame sized to
        # exactly this width) -- caught by
        # tests/integration/test_browser_smoke.py's TestResponsiveLayout.
        assert f"width: min({CONFIRM_WIDTH}px, 100%)" in html

    def test_bridge_script_is_present(self):
        html = build_confirmation_html(
            title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="Confirm",
        )
        assert "window.webkit.messageHandlers.pf.postMessage" in html



class TestCspNonce:
    """The dialog shares approval_window_html.py's own document-shell/
    extraction conventions (see that module's own docstring on the CSP
    nonce)."""

    def test_confirmation_html_style_and_script_share_a_random_nonce(self):
        html = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        nonce = extract_csp_nonce(html)
        assert nonce
        assert f'<style nonce="{nonce}">' in html


    def test_each_call_gets_its_own_nonce(self):
        a = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        b = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        assert extract_csp_nonce(a) != extract_csp_nonce(b)


class TestResponsiveViewport:
    """The dialog is served in an ordinary browser tab by the web approval
    surface -- so it needs the same device-width viewport
    build_card_stack_html declares, for the same reason (see that function's
    own head)."""

    def test_confirmation_declares_a_device_width_viewport(self):
        html = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html

