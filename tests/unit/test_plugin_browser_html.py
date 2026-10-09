"""plugin_browser_html: the /plugin-pages document body."""
from __future__ import annotations

import pytest

from privacyfence.plugin_browser_html import format_timestamp, render
from privacyfence.plugins.page_index import PAGE_INDEX_INVALID, PAGE_INDEX_NO_ANSWER, PageIndex, index_from_result
from privacyfence.plugins.protocol import PageEntry

pytestmark = pytest.mark.unit


def _index(*entries: PageEntry, name: str = "alpha", display: str = "Alpha", error: str = "") -> PageIndex:
    return PageIndex(name, display, tuple(entries), error)


class TestRender:
    def test_title_and_path_are_escaped(self):
        entry = PageEntry(path='/a"b', title="<img src=x>", description="<b>d</b>")
        html = render([_index(entry)], single=False, nonce="n")
        assert "<img src=x>" not in html
        assert "&lt;img src=x&gt;" in html
        assert "<b>d</b>" not in html
        assert 'href="/plugins/alpha/a&quot;b"' in html

    def test_the_link_opens_a_new_tab_and_is_the_plugin_path(self):
        html = render([_index(PageEntry(path="/events?limit=1", title="Events"))], single=False, nonce="n")
        assert '<a href="/plugins/alpha/events?limit=1" target="_blank" rel="noopener">Events</a>' in html

    def test_error_section_shows_the_sentence_only(self):
        html = render([_index(error=PAGE_INDEX_NO_ANSWER)], single=False, nonce="n")
        assert f'<p class="pf-plugin-pages-error">{PAGE_INDEX_NO_ANSWER}</p>' in html
        assert "<table" not in html

    def test_empty_section(self):
        html = render([_index()], single=False, nonce="n")
        assert '<p class="pf-plugin-pages-empty">This plugin lists no pages.</p>' in html

    def test_missing_fields_show_a_dash(self):
        html = render([_index(PageEntry(path="/", title="Home"))], single=False, nonce="n")
        assert html.count("<td>—</td>") == 3
        assert "<th>Title</th><th>Version</th><th>Created</th><th>Updated</th>" in html

    def test_present_fields_are_shown(self):
        entry = PageEntry(path="/", title="Home", version="2", created_at="2026-10-09T12:00:00+02:00",
                          description="About")
        html = render([_index(entry)], single=False, nonce="n")
        assert "<td>2</td>" in html
        assert "2026-10-09 10:00 UTC" in html
        assert '<div class="pf-plugin-pages-desc">About</div>' in html

    @pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
    def test_a_timestamp_that_overflows_in_utc_shows_the_invalid_page_list_message(self, stamp):
        result = {"pages": [{"path": "/", "title": "x", "updated_at": stamp}]}
        html = render([index_from_result("alpha", "Alpha", result)], single=False, nonce="n")
        assert PAGE_INDEX_INVALID in html

    def test_no_script_and_the_style_carries_the_nonce(self):
        html = render([_index(PageEntry(path="/", title="Home"))], single=False, nonce="abc")
        assert "<script" not in html
        assert '<style nonce="abc">' in html

    def test_all_view_has_a_section_per_plugin_with_its_heading(self):
        html = render([_index(), _index(name="beta", display="Beta <i>")], single=False, nonce="n")
        assert "<h1>Plugin pages</h1>" in html
        assert 'data-plugin="alpha"' in html and 'data-plugin="beta"' in html
        assert "<h2>Beta &lt;i&gt;</h2>" in html
        assert "All plugin pages" not in html

    def test_no_plugin(self):
        html = render([], single=False, nonce="n")
        assert '<p id="no-plugins">No plugin with pages is running.</p>' in html

    def test_single_view_names_the_plugin_and_links_back(self):
        html = render([_index(PageEntry(path="/", title="Home"))], single=True, nonce="n")
        assert "<h1>Alpha</h1>" in html
        assert '<p><a href="/plugin-pages">All plugin pages</a></p>' in html
        assert "<h2>" not in html


class TestFormatTimestamp:
    def test_converts_an_offset_to_utc(self):
        assert format_timestamp("2026-10-09T12:30:00+02:00") == "2026-10-09 10:30 UTC"

    def test_z_suffix(self):
        assert format_timestamp("2026-10-09T10:00:00Z") == "2026-10-09 10:00 UTC"

    def test_none_is_empty(self):
        assert format_timestamp(None) == ""

    def test_naive_value_is_taken_as_utc_and_garbage_is_returned_as_is(self):
        assert format_timestamp("2026-10-09T10:00:00") == "2026-10-09 10:00 UTC"
        assert format_timestamp("soon") == "soon"

    @pytest.mark.parametrize("value", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
    def test_value_that_overflows_in_utc_is_returned_as_is(self, value):
        assert format_timestamp(value) == value
