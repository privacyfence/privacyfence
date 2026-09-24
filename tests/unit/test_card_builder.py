"""Tests for card_builder.py -- the pure (no AppKit) translation from
gate.py's show_popup/show_read_popup argument shapes into
approval_window_html.build_card_stack_html()'s own shape. Mirrors what
approval_window.py's ApprovalWindowController does for the native host; see
that controller's docstring for the same reasoning these mirror.
"""
from __future__ import annotations

import html
import re

from privacyfence import card_builder
from privacyfence.approval_window_html import NARROW, WIDE


class TestReadingTimeLabel:
    def test_short_text_is_seconds(self):
        assert card_builder._reading_time_label("just a couple words") == "~1 sec read"

    def test_long_text_is_minutes(self):
        label = card_builder._reading_time_label(" ".join(["word"] * 400))
        assert label == "~2 min read"

    def test_empty_text_floors_at_one_second(self):
        assert card_builder._reading_time_label("") == "~1 sec read"


class TestSeenCountText:
    def test_singular(self):
        assert card_builder._seen_count_text(1) == "Seen 1 time this week"

    def test_plural(self):
        assert card_builder._seen_count_text(3) == "Seen 3 times this week"

    def test_first_time_is_stated_rather_than_left_blank(self):
        # An absent line is indistinguishable from a card that never
        # carries one, so "not seen before" read as "no information" -- on
        # the card's main guard against rubber-stamping a request that has
        # quietly become routine.
        assert card_builder._seen_count_text(0) == "First time this week"


class TestDisclosureRows:
    def test_write_calls_never_get_disclosure_rows(self):
        assert card_builder._disclosure_rows(False, {"To": "a@b.com"}, {"Body": "allow"}) == []

    def test_new_info_comes_before_visibility_derived_rows(self):
        rows = card_builder._disclosure_rows(True, {"Location": "Room 1"}, {"Description": "allow"})
        assert rows[0] == ("Location", "Room 1")
        assert rows[1][0] == "Description"

    def test_no_new_info_or_visibility_yields_no_rows(self):
        assert card_builder._disclosure_rows(True, None, None) == []


class TestBuildCardHtml:
    def _kwargs(self, **overrides):
        kwargs = dict(
            title="Read Message",
            preview={"From": "someone@example.com"},
            details_text="The message body.",
            is_read=True,
            layout=NARROW,
        )
        kwargs.update(overrides)
        return kwargs

    def test_renders_a_full_document(self):
        html = card_builder.build_card_html(**self._kwargs())
        assert html.startswith("<!DOCTYPE html>")
        assert "The message body." not in html  # NARROW has no preview pane

    def test_wide_layout_renders_the_details_text_in_the_preview_pane(self):
        html = card_builder.build_card_html(**self._kwargs(layout=WIDE))
        assert "The message body." in html
        assert "~1 sec read" in html

    def test_accept_all_label_uses_the_short_hint(self):
        html = card_builder.build_card_html(
            **self._kwargs(accept_all_choices=[("sender_domain", "this sender domain")])
        )
        assert "Always allow — this sender domain" in html

    def test_accept_all_label_falls_back_to_plain_when_no_hint(self):
        html = card_builder.build_card_html(**self._kwargs(accept_all_choices=[("always_allow", "")]))
        assert "Always allow" in html
        assert "Always allow — " not in html

    def test_write_call_never_shows_pii_categories_card(self):
        # Write calls carry write_content_flags, not pii_categories -- see
        # gate.py's own module docstring for why these are mutually
        # exclusive and rendered differently.
        html = card_builder.build_card_html(
            **self._kwargs(is_read=False, write_content_flags=["Email address"])
        )
        assert "Email address" in html
        assert "Review carefully before approving" not in html

    def test_read_call_with_pii_gets_the_read_risk_card(self):
        html = card_builder.build_card_html(**self._kwargs(pii_categories=["Email address"]))
        assert "Review carefully before approving" in html

    def test_temp_accept_eligible_shows_the_disclosure_caption(self):
        html = card_builder.build_card_html(**self._kwargs(temp_accept_eligible=True))
        assert "Approving this also allows further calls like this" in html

    def test_not_temp_accept_eligible_hides_the_disclosure_caption(self):
        html = card_builder.build_card_html(**self._kwargs(temp_accept_eligible=False))
        assert "Approving this also allows further calls like this" not in html

    def test_pdf_bytes_render_as_an_embed_data_uri(self):
        html = card_builder.build_card_html(**self._kwargs(layout=WIDE, pdf_bytes=b"%PDF-1.4 fake"))
        assert "data:application/pdf;base64," in html

    def test_image_bytes_render_as_an_img_data_uri(self):
        html = card_builder.build_card_html(
            **self._kwargs(layout=WIDE, preview_bytes=b"\x89PNG fake", preview_mime_type="image/png")
        )
        assert "data:image/png;base64," in html

    def test_table_only_suppresses_details_text_when_a_table_is_present(self):
        html = card_builder.build_card_html(
            **self._kwargs(
                layout=WIDE, table_only=True,
                preview_tables=[{"headers": ["A"], "rows": [["1"]]}],
            )
        )
        assert "The message body." not in html
        assert "<table" in html

    def test_write_card_states_what_approving_actually_does(self):
        # A read card ends with "What will be provided to Claude"; a write
        # card had nothing naming its own consequence -- only the payload
        # and Claude's reason.
        html = card_builder.build_card_html(
            **self._kwargs(is_read=False, title="Add Gmail Label", tool="gmail_add_label"),
        )
        assert "Effect" in html
        assert "A label is added. Nothing is sent, moved or deleted." in html

    def test_the_effect_row_comes_last_in_the_action_section(self):
        # The payload is read first and the outcome last, which is the
        # order the decision is actually made in.
        html = card_builder.build_card_html(**self._kwargs(
            is_read=False, title="Add Gmail Label", tool="gmail_add_label",
            preview={"From": "a@b.com", "Subject": "Q3"},
        ))
        assert html.index("Subject") < html.index("Effect")

    def test_read_cards_never_get_an_effect_row(self):
        # Their consequence card already exists, and the tool id of a read
        # gate is never in the effect table anyway.
        html = card_builder.build_card_html(**self._kwargs(is_read=True, tool="gmail_get_thread"))
        assert ">Effect<" not in html

    def test_a_write_tool_with_no_sentence_renders_no_row_rather_than_a_vague_one(self):
        html = card_builder.build_card_html(
            **self._kwargs(is_read=False, tool="some_tool_nobody_has_written_copy_for"),
        )
        assert ">Effect<" not in html

    def test_seen_count_zero_still_states_the_frequency(self):
        html = card_builder.build_card_html(**self._kwargs(seen_count=0))
        assert "First time this week" in html

    def test_seen_count_positive_shows_caption(self):
        html = card_builder.build_card_html(**self._kwargs(seen_count=2))
        assert "Seen 2 times this week" in html


class TestAgentDisplayName:
    """Every string on the card that names the caller of this request comes
    from one ``agent_display_name`` -- including the rows connectors write
    with approval_window_html.AGENT_PLACEHOLDER, since connectors never
    learn who is asking. The default (today's "Claude") is pinned by every
    other test in this suite rendering unchanged."""

    _NEW_INFO = {
        "Content returned to {agent}": "None — file bytes are never sent",
        "Note": "More messages exist -- {agent} will see this too",
    }

    def _read_card(self, name: str) -> str:
        return card_builder.build_card_html(
            title="Read Message", preview={"From": "someone@example.com"},
            details_text="The message body.", is_read=True, layout=NARROW,
            claude_reason="to answer the question", visibility={"Body": "block"},
            new_info=self._NEW_INFO, agent_display_name=name,
        )

    def _write_card(self, name: str) -> str:
        return card_builder.build_card_html(
            title="Send Message", preview={"To": "someone@example.com"},
            details_text="Hi.", is_read=False, layout=NARROW,
            claude_reason="the user asked", agent_display_name=name,
        )

    @staticmethod
    def _in_scope(name: str) -> tuple[list[str], list[str]]:
        """(read-card strings, write-card strings), each already escaped the
        way the card renders it."""
        n = html.escape(name)
        read = [
            f"What {n} already knows",
            f"Why {n} needs more data",
            f"{n}’s stated reason · unverified",
            f"What will be provided to {n}",
            f"Content returned to {n}",
            f"More messages exist -- {n} will see this too",
            f"None — not disclosed to {n}",
        ]
        write = [f"Why {n} is doing this", f"{n}’s stated reason · unverified"]
        return read, write

    @staticmethod
    def _without_stylesheet(doc: str) -> str:
        # styles.css's own comments name Claude; they aren't card copy.
        return re.sub(r"<style[^>]*>.*?</style>", "", doc, flags=re.S)

    def test_default_is_todays_copy(self):
        read, _ = self._in_scope("Claude")
        doc = card_builder.build_card_html(
            title="Read Message", preview={"From": "someone@example.com"},
            details_text="The message body.", is_read=True, layout=NARROW,
            claude_reason="to answer the question", visibility={"Body": "block"},
            new_info=self._NEW_INFO,
        )
        for s in read:
            assert s in doc, s

    def test_another_name_changes_every_in_scope_string(self):
        read, write = self._in_scope("Gemini")
        read_html, write_html = self._read_card("Gemini"), self._write_card("Gemini")
        for s in read:
            assert s in read_html, s
        for s in write:
            assert s in write_html, s
        assert "{agent}" not in read_html
        assert "Claude" not in self._without_stylesheet(read_html)
        assert "Claude" not in self._without_stylesheet(write_html)

    def test_a_hostile_name_renders_escaped_in_every_position(self):
        name = "<script>alert(1)</script>"
        read, write = self._in_scope(name)
        read_html, write_html = self._read_card(name), self._write_card(name)
        for s in read:
            assert s in read_html, s
        for s in write:
            assert s in write_html, s
        assert "<script>alert(1)" not in read_html
        assert "<script>alert(1)" not in write_html
