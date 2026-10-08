"""Tests for dialog_window_html.py -- the small confirmation/list-picker
HTML template dialog_window.py's DialogWindowController renders.

Pure-function module, no AppKit (same as approval_window_html.py, which this
imports from) -- these assert directly on the generated HTML strings, no
macOS/PyObjC required.

The real, injection-relevant escaping coverage this replaces used to live in
test_approval_popup_escaping.py, round-tripping content through a real
`osascript` process to prove AppleScript string-literal breakout was
impossible. That vector doesn't exist anymore -- a webview bridge call takes
a string as a real DOM text value, never source text to be interpreted (see
this module's own docstring) -- so what's worth pinning down here is just
that build_confirmation_html/build_choice_html actually HTML-escape
everything they interpolate, the same defensive posture
build_card_stack_html takes with details_text.
"""
from __future__ import annotations

from privacyfence.approval_window_html import extract_csp_nonce
from privacyfence.dialog_window_html import (
    CONFIRM_WIDTH,
    PICKER_WIDTH,
    build_choice_html,
    build_confirmation_html,
    build_plugin_approval_html,
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


class TestConfirmationBodyBlocks:
    """``body_blocks``: a plugin confirmation's typed preview, rendered by the
    card's own preview renderer between the message and the buttons."""

    def _html(self, blocks):
        return build_confirmation_html(
            title="Today: Publish", message_lines=["m"], cancel_label="Deny", confirm_label="Approve",
            body_blocks=blocks,
        )

    def test_blocks_render_between_message_and_buttons(self):
        html = self._html([
            {"type": "heading", "label": "Note"},
            {"type": "field", "label": "To", "value": "team"},
            {"type": "code", "text": "x = 1", "language": ""},
            {"type": "diff", "text": "+added"},
        ])
        assert 'class="pf-dialog-blocks"' in html
        assert html.index('class="pf-dialog-message"') < html.index('class="pf-dialog-blocks"')
        assert html.index('class="pf-dialog-blocks"') < html.index('class="pf-btn-row"')
        assert ">Note</div>" in html
        assert '<span class="pf-preview-label">To:</span>' in html
        assert '<pre class="pf-code"><code>x = 1</code></pre>' in html
        assert '<span class="pf-diff-add">+added</span>' in html

    def test_blocks_are_escaped(self):
        html = self._html([
            {"type": "text", "text": "<script>window.pwned=1</script>"},
            {"type": "field", "label": "<b>L</b>", "value": "<img src=x onerror=alert(1)>"},
            {"type": "table", "headers": ["<i>h</i>"], "rows": [["<u>c</u>"]]},
        ])
        assert "window.pwned=1</script>" not in html
        assert "<img" not in html
        for tag in ("<b>", "<i>", "<u>"):
            assert tag not in html
        assert "&lt;script&gt;window.pwned=1&lt;/script&gt;" in html

    def test_no_blocks_renders_no_blocks_container(self):
        assert 'class="pf-dialog-blocks"' not in self._html(None)
        assert 'class="pf-dialog-blocks"' not in self._html([])


class TestBuildChoiceHtml:
    def test_title_and_prompt_render(self):
        html = build_choice_html(
            title="PrivacyFence — Choose Auto-Accept Rule",
            prompt="More than one rule could be created from this item — choose one:",
            options=["i_am_owner", "approved_folder: f1"],
        )
        assert "PrivacyFence — Choose Auto-Accept Rule" in html
        assert "More than one rule could be created from this item — choose one:" in html

    def test_every_option_renders_as_its_own_row_with_its_index(self):
        html = build_choice_html(
            title="T", prompt="p", options=["i_am_owner", "approved_folder: f1"],
        )
        assert 'data-pf-action="choice" data-pf-index="0">i_am_owner<' in html
        assert 'data-pf-action="choice" data-pf-index="1">approved_folder: f1<' in html

    def test_cancel_button_present_and_not_primary(self):
        html = build_choice_html(title="T", prompt="p", options=["a", "b"])
        assert 'data-pf-action="cancel"' in html
        # Scoped to the `="1"`-valued attribute, not a bare "data-pf-primary"
        # substring search -- _JS's own embedded script text (inlined into
        # every document regardless of shape) references the bare attribute
        # name too, in its keydown-handler selector -- same distinction
        # test_approval_window.py's own data-pf-primary tests draw.
        assert 'data-pf-primary="1"' not in html

    def test_options_are_html_escaped(self):
        # The real bug this replaces: settings_controller._osascript_pick
        # used to interpolate an unescaped option (e.g. a live Atlassian
        # accessible-resources URL) directly into AppleScript source text.
        html = build_choice_html(
            title="T", prompt="p",
            options=['https://x.atlassian.net/" with administrator privileges'],
        )
        assert 'https://x.atlassian.net/" with administrator privileges' not in html
        assert "&quot;" in html

    def test_prompt_is_html_escaped(self):
        html = build_choice_html(title="T", prompt="<script>alert(1)</script>", options=["a"])
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_empty_options_renders_no_rows(self):
        html = build_choice_html(title="T", prompt="p", options=[])
        assert 'data-pf-action="choice"' not in html
        assert 'data-pf-action="cancel"' in html

    def test_uses_the_picker_width(self):
        html = build_choice_html(title="T", prompt="p", options=["a"])
        # See TestBuildConfirmationHtml.test_uses_the_confirm_width's own
        # comment -- same min(...,100%) responsive shape, same reason.
        assert f"width: min({PICKER_WIDTH}px, 100%)" in html

    def test_buttons_start_disabled_in_markup(self):
        html = build_choice_html(title="T", prompt="p", options=["a", "b"])
        # Two option rows + the Cancel button, all start disabled.
        assert html.count('role="button" aria-disabled="true"') == 3


class TestCspNonce:
    """Both shapes share approval_window_html.py's own document-shell/
    extraction conventions (see that module's own docstring on the CSP
    nonce)."""

    def test_confirmation_html_style_and_script_share_a_random_nonce(self):
        html = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        nonce = extract_csp_nonce(html)
        assert nonce
        assert f'<style nonce="{nonce}">' in html

    def test_choice_html_style_and_script_share_a_random_nonce(self):
        html = build_choice_html(title="T", prompt="p", options=["a"])
        nonce = extract_csp_nonce(html)
        assert nonce
        assert f'<style nonce="{nonce}">' in html

    def test_each_call_gets_its_own_nonce(self):
        a = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        b = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        assert extract_csp_nonce(a) != extract_csp_nonce(b)


class TestResponsiveViewport:
    """Both dialog shapes are served in an ordinary browser tab by the web
    approval surface, not only inside a fixed native frame -- so both need
    the same device-width viewport build_card_stack_html declares, for the
    same reason (see that function's own head)."""

    def test_confirmation_declares_a_device_width_viewport(self):
        html = build_confirmation_html(title="T", message_lines=["m"], cancel_label="Cancel", confirm_label="OK")
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html

    def test_choice_declares_a_device_width_viewport(self):
        html = build_choice_html(title="T", prompt="p", options=["a"])
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html


class TestBuildPluginApprovalHtml:
    FIELDS = [
        ("Plugin", "Data Lake (datalake)"),
        ("Kind", "processor-code"),
        ("Subject", "pipeline/clean.py"),
        ("Digest", "sha256:" + "a" * 64),
    ]

    def _html(self, **overrides) -> str:
        kwargs = dict(
            title="Data Lake: Approve the cleaner", fields=self.FIELDS,
            body_blocks=[{"type": "text", "text": "Cleans rows"}],
            frame_src="/plugins/datalake/approval?pf_approval=abc", frame_title="Page from Data Lake",
        )
        kwargs.update(overrides)
        return build_plugin_approval_html(**kwargs)

    def test_fields_render_with_the_full_digest_in_code_style(self):
        html = self._html()
        for label, value in self.FIELDS:
            assert f'<th scope="row">{label}</th>' in html
            assert value in html
        assert f'<code class="pf-code">sha256:{"a" * 64}</code>' in html

    def test_fields_are_escaped(self):
        html = self._html(
            title="<b>t</b>", fields=[("Plugin", "<img src=x onerror=alert(1)>"), ("Subject", '"><script>')],
        )
        assert "<img src=x" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "&quot;&gt;&lt;script&gt;" in html
        assert "<b>t</b>" not in html

    def test_fields_and_blocks_come_before_the_frame(self):
        html = self._html()
        fields_at = html.index("pf-approval-fields")
        blocks_at = html.index("Cleans rows")
        frame_at = html.index("<iframe")
        buttons_at = html.index('data-pf-action="confirm"')
        assert fields_at < blocks_at < frame_at < buttons_at
        # The fields are never inside the frame: the frame element is empty.
        assert html[frame_at:].startswith("<iframe") and "></iframe>" in html[frame_at:frame_at + 400]
        assert "pf-approval-fields" not in html[frame_at:]

    def test_frame_attributes_are_exact(self):
        html = self._html()
        assert (
            '<iframe class="pf-plugin-frame" sandbox="allow-scripts" '
            'src="/plugins/datalake/approval?pf_approval=abc" referrerpolicy="no-referrer" '
            'title="Page from Data Lake" loading="eager"></iframe>'
        ) in html
        assert "allow-same-origin" not in html
        assert html.count("<iframe") == 1

    def test_frame_src_and_title_are_escaped(self):
        html = self._html(frame_src='/plugins/x/a"onload="alert(1)&b', frame_title='"><x>')
        assert 'src="/plugins/x/a&quot;onload=&quot;alert(1)&amp;b"' in html
        assert 'title="&quot;&gt;&lt;x&gt;"' in html

    def test_no_frame_without_frame_src(self):
        html = self._html(frame_src="")
        assert "<iframe" not in html
        assert "pf-plugin-frame" not in html.split("</style>")[1]

    def test_deny_and_approve_buttons_with_approve_primary(self):
        body = self._html().split("<body>")[1].split("<script")[0]
        assert ">Deny<" in body and ">Approve<" in body
        assert body.count("data-pf-primary") == 1
        assert body.index("data-pf-primary") > body.index('data-pf-action="cancel"')

    def test_no_blocks_renders_no_blocks_container(self):
        assert 'class="pf-dialog-blocks"' not in self._html(body_blocks=[])

    def test_style_and_script_share_a_nonce(self):
        html = self._html()
        nonce = extract_csp_nonce(html)
        assert nonce and f'<style nonce="{nonce}">' in html and f'<script nonce="{nonce}">' in html
