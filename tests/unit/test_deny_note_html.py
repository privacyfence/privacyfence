"""Tests for deny_note_html.py -- the "Deny with a note…" panel's markup and script (ADR 0082).

The panel's behaviour in a real page (the chips clearing, the counter, Cancel, Ctrl+Enter) is
tests/integration/test_browser_smoke.py's ``TestDenyNote``; this is the markup, and the promise
that nothing but the person's typing ever fills the note.
"""
from __future__ import annotations

import inspect
import re
from html.parser import HTMLParser

from privacyfence import deny_note_html
from privacyfence.deny_feedback import INTENTS, MAX_NOTE_CHARS
from privacyfence.deny_note_html import PANEL_JS, batch_panel_html, panel_html

# The markup a card's content could carry, if anything ever copied it into the panel.
HOSTILE = '</textarea><script>alert(1)</script>" value="PWNED" \' onfocus="x()"'


class _Elements(HTMLParser):
    """Every start tag, with its attributes, and the text inside each <textarea>."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.textarea_text: list[str] = []
        self._in_textarea = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "textarea":
            self._in_textarea = True
            self.textarea_text.append("")

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._in_textarea = False

    def handle_data(self, data):
        if self._in_textarea:
            self.textarea_text[-1] += data


def _parse(html: str) -> _Elements:
    parser = _Elements()
    parser.feed(html)
    return parser


class TestMarkup:
    def test_the_panel_is_one_hidden_section_named_by_the_prefix(self):
        html = panel_html("pf-deny-note", "Deny and send")
        assert html.startswith('<section id="pf-deny-note" ')
        assert html.endswith("</section>")
        section = _parse(html).tags[0]
        assert section == ("section", {
            "id": "pf-deny-note", "class": "card stack pf-deny-note",
            "aria-labelledby": "pf-deny-note-title", "hidden": None,
        })

    def test_every_id_starts_with_the_prefix(self):
        ids = [a["id"] for _tag, a in _parse(panel_html("pf-x", "Send")).tags if "id" in a]
        assert ids and all(i.startswith("pf-x") for i in ids), ids

    def test_the_kicker_help_and_placeholder_are_the_plans_copy(self):
        html = panel_html("p", "Deny and send")
        assert ">Tell the agent why, or what to do instead (optional)</div>" in html
        assert 'placeholder="e.g. Send it only to Anna, not the whole team."' in html
        assert ("Only the agent that made this request receives this note. "
                "It is not kept in the audit log.") in html

    def test_one_chip_per_intent_in_order_in_a_radiogroup(self):
        parsed = _parse(panel_html("p", "Deny and send"))
        radios = [a for tag, a in parsed.tags if tag == "input"]
        assert [r["value"] for r in radios] == list(INTENTS)
        assert all(r["type"] == "radio" and r["name"] == "p-intent" for r in radios)
        assert [a["class"] for tag, a in parsed.tags if tag == "label"] == ["chip"] * len(INTENTS)
        assert ("div", {"class": "cluster", "role": "radiogroup", "aria-labelledby": "p-title"}) in parsed.tags
        html = panel_html("p", "Deny and send")
        for label, _guidance in INTENTS.values():
            assert f"<span>{label.replace(chr(39), '&#x27;')}</span>" in html

    def test_the_textarea_is_a_field_capped_at_the_servers_limit_with_a_counter(self):
        parsed = _parse(panel_html("p", "Deny and send"))
        (textarea,) = [a for tag, a in parsed.tags if tag == "textarea"]
        assert textarea["class"] == "field"
        assert textarea["maxlength"] == str(MAX_NOTE_CHARS) == "500"
        assert textarea["aria-describedby"] == "p-count p-help"
        assert ("div", {"class": "field-help", "id": "p-count", "aria-live": "polite"}) in parsed.tags
        assert f">0 / {MAX_NOTE_CHARS}</div>" in panel_html("p", "Deny and send")

    def test_cancel_is_secondary_and_submit_is_the_outlined_danger_button(self):
        parsed = _parse(panel_html("p", "Deny and send"))
        buttons = [a for tag, a in parsed.tags if tag == "button"]
        assert buttons == [
            {"type": "button", "class": "button secondary", "data-pf-note-cancel": None},
            {"type": "button", "class": "button danger", "data-pf-note-submit": None},
        ]
        assert ">Cancel</button>" in panel_html("p", "Deny and send")
        assert ">Deny and send</button>" in panel_html("p", "Deny and send")

    def test_the_panel_carries_no_decision_attribute(self):
        # The host's Escape and enableButtons() look for data-pf-action; the panel's submit must
        # never be taken for the plain Deny.
        assert "data-pf-action" not in panel_html("p", "Deny and send")

    def test_the_submit_label_is_escaped(self):
        html = panel_html("p", "<b>Deny</b>")
        assert "<b>" not in html and "&lt;b&gt;Deny&lt;/b&gt;" in html

    def test_no_inline_style_and_no_colour_literal(self):
        html = panel_html("p", "Deny and send")
        assert "style=" not in html
        assert re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", html) is None


class TestNothingButTypingFillsTheNote:
    """panel_html has no argument for content, so no request data can reach the note or the
    chips. The whole card (a preview, a summary and a stated reason all carrying a marker and
    hostile markup) is tests/unit/test_approval_window_html.py's TestDenyNotePanel."""

    def test_panel_html_takes_only_its_prefix_and_submit_label(self):
        assert list(inspect.signature(panel_html).parameters) == ["prefix", "submit_label"]

    def test_the_textarea_renders_empty_and_no_chip_is_checked(self):
        parsed = _parse(panel_html("p", "Deny and send"))
        assert parsed.textarea_text == [""]
        (textarea,) = [a for tag, a in parsed.tags if tag == "textarea"]
        assert "value" not in textarea
        assert all("checked" not in a for tag, a in parsed.tags if tag == "input")

    def test_the_script_reads_only_its_own_panel(self):
        # Every element PANEL_JS touches is found by the prefix or inside the panel it found; it
        # never queries the whole document for anything else.
        assert re.findall(r"document\.\w+\(", PANEL_JS) == [
            "document.getElementById(", "document.getElementById(", "document.getElementById(",
        ]
        assert "innerHTML" not in PANEL_JS and ".value =" not in PANEL_JS

    def test_every_string_the_panel_shows_is_a_constant(self):
        for name in ("KICKER", "PLACEHOLDER", "HELP", "CANCEL_LABEL"):
            assert isinstance(getattr(deny_note_html, name), str)


class TestScript:
    def test_defines_the_panel_factory_and_its_api(self):
        assert "window.pfDenyNotePanel = function (prefix, options)" in PANEL_JS
        assert "return {open: open, close: close, isOpen: isOpen, getFeedback: getFeedback};" in PANEL_JS

    def test_feedback_omits_empty_values(self):
        assert "if (text.value.trim()) feedback.note = text.value;" in PANEL_JS
        assert "if (chosen && chosen.checked) feedback.intent = chosen.value;" in PANEL_JS

    def test_ctrl_or_cmd_enter_submits_and_plain_enter_does_not(self):
        assert "e.key === 'Enter' && (e.ctrlKey || e.metaKey)" in PANEL_JS


class TestBatchPanel:
    """batch_panel_html: the approval list's panel, whose note goes to every selected request. It
    is panel_html's panel plus one help line, and takes no content argument either."""

    def test_is_the_card_panel_plus_the_same_note_line(self):
        batch = batch_panel_html("pf-deny-note", "Deny selected and send")
        single = panel_html("pf-deny-note", "Deny selected and send")
        line = '<div class="field-help" id="pf-deny-note-help-batch">The same note goes to every selected request.</div>'
        assert line in batch and line not in single
        assert batch.replace(line, "").replace(
            'aria-describedby="pf-deny-note-count pf-deny-note-help pf-deny-note-help-batch"',
            'aria-describedby="pf-deny-note-count pf-deny-note-help"',
        ) == single

    def test_the_textarea_is_described_by_the_extra_line_too(self):
        parsed = _parse(batch_panel_html("p", "Deny 2 and send"))
        (textarea,) = [a for tag, a in parsed.tags if tag == "textarea"]
        assert textarea["aria-describedby"] == "p-count p-help p-help-batch"

    def test_takes_only_its_prefix_and_submit_label_and_renders_empty(self):
        assert list(inspect.signature(batch_panel_html).parameters) == ["prefix", "submit_label"]
        parsed = _parse(batch_panel_html("p", "Deny 2 and send"))
        assert parsed.textarea_text == [""]
        assert all("checked" not in a for tag, a in parsed.tags if tag == "input")
        assert isinstance(deny_note_html.BATCH_HELP, str)

    def test_carries_no_decision_attribute_and_keeps_the_submit_hook(self):
        html = batch_panel_html("p", "Deny 2 and send")
        assert "data-pf-action" not in html
        assert '<button type="button" class="button danger" data-pf-note-submit>Deny 2 and send</button>' in html
