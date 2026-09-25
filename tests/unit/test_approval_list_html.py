"""approval_list_html.py -- the /approvals list page."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from privacyfence import approval_list_html


def _card(**overrides):
    defaults = dict(
        id="abc123", kind="card", connector="gmail", tool="gmail_read_message",
        tool_name="Read Gmail message", summary="", created_at=time.time(),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _real_card(*, dedupe_key="k1", **overrides):
    """A real approvals.PendingApproval (unlike ``_card()``'s duck-typed
    stand-in above) -- used where a test needs is_batchable()/
    blocked_reason() to be the genuine, non-fallback computation."""
    from privacyfence.approvals import PendingApprovalRegistry

    registry = PendingApprovalRegistry()
    kwargs = dict(
        dedupe_key=dedupe_key, connector="gmail", tool="gmail_get_message",
        gate_kind="review", request_id="r1", tool_name="Read Gmail message",
    )
    kwargs.update(overrides)
    approval, _ = registry.register_or_coalesce(**kwargs)
    return approval


class TestRowFromApproval:
    def test_carries_the_display_fields(self):
        row = approval_list_html.row_from_approval(_card())
        assert row["id"] == "abc123"
        assert row["connector"] == "gmail"
        assert row["tool_name"] == "Read Gmail message"
        assert row["created_at"]


class TestBuildListHtml:
    def test_empty_state_when_no_rows(self):
        html = approval_list_html.build_list_html([], csrf="tok")
        assert "Nothing is waiting" in html
        assert "PrivacyFence is watching" in html

    def test_renders_a_row_per_pending_approval(self):
        rows = [approval_list_html.row_from_approval(_card(id="a")), approval_list_html.row_from_approval(_card(id="b"))]
        html = approval_list_html.build_list_html(rows, csrf="tok")
        assert 'data-approval-id="a"' in html
        assert 'data-approval-id="b"' in html
        assert "Read Gmail message" in html

    def test_never_renders_an_allow_button(self):
        # The list's central asymmetry: Deny is on the row, Allow never is.
        rows = [approval_list_html.row_from_approval(_card())]
        html = approval_list_html.build_list_html(rows, csrf="tok")
        assert "data-deny=" in html
        assert ">Allow<" not in html
        assert "allow" not in html.lower().replace("allow once", "").replace("pf-btn-review", "")

    def test_deny_button_and_review_link_are_present(self):
        rows = [approval_list_html.row_from_approval(_card(id="xyz"))]
        html = approval_list_html.build_list_html(rows, csrf="tok")
        assert 'data-deny="xyz"' in html
        assert 'href="/approvals/xyz"' in html

    def test_heading_pluralizes(self):
        html_one = approval_list_html.build_list_html([approval_list_html.row_from_approval(_card())], csrf="t")
        html_two = approval_list_html.build_list_html(
            [approval_list_html.row_from_approval(_card(id="a")), approval_list_html.row_from_approval(_card(id="b"))],
            csrf="t",
        )
        assert "1 approval pending" in html_one
        assert "2 approvals pending" in html_two

    def test_title_and_kicker_are_escaped(self):
        card = _card(tool_name='<script>alert(1)</script>')
        html = approval_list_html.build_list_html([approval_list_html.row_from_approval(card)], csrf="t")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_csrf_is_embedded_for_the_deny_button(self):
        html = approval_list_html.build_list_html([], csrf="my-token-value")
        assert "my-token-value" in html

    def test_confirm_kind_labels_as_confirmation_not_approval(self):
        card = _card(kind="confirm", tool_name="", summary="")
        html = approval_list_html.build_list_html([approval_list_html.row_from_approval(card)], csrf="t")
        assert "Confirmation" in html


class TestRowFromApprovalBatching:
    def test_a_plain_card_is_batchable(self):
        row = approval_list_html.row_from_approval(_real_card())
        assert row["batchable"] is True
        assert row["blocked_reason"] == ""

    def test_a_pii_forced_card_is_not_batchable(self):
        row = approval_list_html.row_from_approval(_real_card(pii_forces_confirmation=True))
        assert row["batchable"] is False
        assert row["blocked_reason"] != ""

    def test_a_confirm_dialog_is_not_batchable(self):
        from privacyfence.approvals import PendingApprovalRegistry

        registry = PendingApprovalRegistry()
        approval = registry.register_confirm()
        row = approval_list_html.row_from_approval(approval)
        assert row["batchable"] is False

    def test_duck_typed_stand_in_without_is_batchable_falls_back_correctly(self):
        # _card() (the plain SimpleNamespace used throughout this file) has
        # no is_batchable()/blocked_reason() of its own -- row_from_approval
        # must still classify it correctly from kind/pii_forces_confirmation.
        row = approval_list_html.row_from_approval(_card())
        assert row["batchable"] is True
        row_confirm = approval_list_html.row_from_approval(_card(kind="confirm"))
        assert row_confirm["batchable"] is False

    def test_operation_key_defaults_to_empty_string(self):
        row = approval_list_html.row_from_approval(_card())
        assert row["operation_key"] == ""


class TestGroupRows:
    def test_batchable_rows_sharing_operation_key_collapse_into_one_group(self):
        rows = [
            approval_list_html.row_from_approval(
                _real_card(dedupe_key="k1", operation_key="drive.read_file_contents"),
            ),
            approval_list_html.row_from_approval(
                _real_card(dedupe_key="k2", operation_key="drive.read_file_contents"),
            ),
        ]
        groups = approval_list_html._group_rows(rows)
        assert len(groups) == 1
        assert len(groups[0]["rows"]) == 2

    def test_different_operation_keys_get_separate_groups(self):
        rows = [
            approval_list_html.row_from_approval(
                _real_card(dedupe_key="k1", operation_key="drive.read_file_contents"),
            ),
            approval_list_html.row_from_approval(
                _real_card(dedupe_key="k2", operation_key="drive.write_file"),
            ),
        ]
        groups = approval_list_html._group_rows(rows)
        assert len(groups) == 2

    def test_non_batchable_rows_never_group_with_anything(self):
        from privacyfence.approvals import PendingApprovalRegistry

        registry = PendingApprovalRegistry()
        confirm_a = registry.register_confirm()
        confirm_b = registry.register_confirm()
        rows = [
            approval_list_html.row_from_approval(confirm_a),
            approval_list_html.row_from_approval(confirm_b),
        ]
        groups = approval_list_html._group_rows(rows)
        assert len(groups) == 2
        assert all(g["key"] is None for g in groups)

    def test_group_header_names_count_and_a_humanized_operation_label(self):
        rows = [
            approval_list_html.row_from_approval(
                _real_card(dedupe_key=f"k{i}", connector="drive", operation_key="drive.read_file_contents"),
            )
            for i in range(3)
        ]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert "Drive" in html
        assert "read file contents" in html
        assert "3" in html

    def test_a_single_row_group_gets_no_group_select_all_header(self):
        rows = [
            approval_list_html.row_from_approval(
                _real_card(dedupe_key="k1", operation_key="drive.read_file_contents"),
            ),
        ]
        # Checked against the group HTML directly, not the whole page: the
        # embedded JS (live re-render) legitimately contains the literal
        # substring 'data-group-select=' as part of *building* that
        # attribute -- see routes_approvals.py's own TestInjectShim for the
        # same "don't grep the whole document" lesson.
        group_html = approval_list_html._group_html(approval_list_html._group_rows(rows)[0])
        assert "data-group-select=" not in group_html


class TestBinderMarkup:
    def test_batchable_rows_get_a_selection_checkbox(self):
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert "data-select=" in html

    def test_non_batchable_rows_get_no_checkbox_but_show_the_reason(self):
        rows = [approval_list_html.row_from_approval(_real_card(pii_forces_confirmation=True))]
        # Checked against the row HTML directly -- the embedded JS legitimately
        # contains the literal substring 'data-select=' while building that
        # attribute for the live re-render path (see the group-header test
        # above for the same reasoning).
        row_html = approval_list_html._row_html(rows[0])
        assert "data-select=" not in row_html
        assert "PII confirmation" in row_html

    def test_toolbar_present_when_rows_exist(self):
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert 'id="pf-approvals-toolbar"' in html
        assert 'id="pf-deny-selected"' in html
        assert 'id="pf-approve-selected"' in html

    def test_toolbar_is_not_hidden_when_rows_exist(self):
        # F576 bug 1: a toolbar that only exists in the DOM when rows exist
        # can never be created by the live re-render -- so it must always
        # exist, and visibility is carried by "hidden" instead.
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert 'id="pf-approvals-toolbar" hidden' not in html

    def test_approve_selected_starts_disabled(self):
        # Like Deny selected, nothing is
        # selected on first paint, so there is nothing to approve yet.
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert 'id="pf-approve-selected" disabled' in html

    def test_toolbar_present_but_hidden_on_the_empty_state(self):
        # Omitted when nothing is pending at first paint, the toolbar
        # could never be created later by window.__pfRenderApprovals's own
        # SSE-driven re-render (that function only ever updates existing
        # elements). It must stay in the DOM, just hidden, so the live re-render can
        # reveal it (see updateToolbar in _JS) the moment something
        # batchable actually arrives.
        html = approval_list_html.build_list_html([], csrf="t")
        assert 'id="pf-approvals-toolbar" hidden' in html
        assert 'id="pf-deny-selected"' in html
        assert 'id="pf-approve-selected"' in html

    def test_select_all_is_disabled_when_nothing_is_batchable(self):
        from privacyfence.approvals import PendingApprovalRegistry

        registry = PendingApprovalRegistry()
        confirm = registry.register_confirm()
        rows = [approval_list_html.row_from_approval(confirm)]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert 'id="pf-select-all-cb" aria-label="Select all batchable approvals" disabled' in html

    def test_select_all_is_enabled_when_something_is_batchable(self):
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert 'id="pf-select-all-cb" aria-label="Select all batchable approvals">' in html

    def test_details_toggle_and_container_are_present_per_row(self):
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert 'data-details="' in html
        assert 'id="pf-details-' in html


class TestRowActionOrder:
    """Deny is last in the cluster, not beside Review -- see the module
    docstring. Asserted on source order in both renderers rather than on a
    CSS ``order`` declaration, because keeping focus order and visual order
    the same thing at every width is the point of doing it this way."""

    def test_first_paint_renders_details_review_deny(self):
        row_html = approval_list_html._row_html(approval_list_html.row_from_approval(_real_card()))
        assert (
            row_html.index("pf-btn-details")
            < row_html.index("pf-btn-review")
            < row_html.index("pf-btn-deny")
        )

    def test_live_rerender_mirrors_the_same_order(self):
        # rowHtml is a hand-kept mirror of _row_html (see the module
        # docstring); an ordering that holds on first paint but not after
        # the first SSE tick would be worse than not doing it at all.
        js = approval_list_html._JS
        body = js[js.index("function rowHtml("):js.index("function groupHtml(")]
        assert body.index("pf-btn-details") < body.index("pf-btn-review") < body.index("pf-btn-deny")


class TestPhoneWidthRules:
    """Without these rules, at phone widths the row's own ``flex-wrap`` never engages, because
    ``.pf-approval-main`` is ``flex:1;min-width:0`` against a
    ``flex-shrink:0`` action cluster -- so the text column shrinks to about
    25px at 393px instead of the row wrapping. These assert the rules that
    make it wrap and give the controls a real target; that the rendered
    result actually follows is covered by the Playwright suite."""

    def test_text_column_gets_a_basis_too_wide_to_sit_beside_the_actions(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert "@media (max-width: 560px)" in html
        assert ".pf-approval-main { flex-basis: calc(100% - 96px); }" in html

    def test_action_strip_goes_full_width(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert ".pf-approval-actions { width: 100%; gap: 10px; margin-top: 12px; }" in html

    def test_row_controls_get_a_real_touch_target(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert "min-height: 44px; padding: 12px 14px; font-size: 13px;" in html

    def test_batch_actions_sit_side_by_side_without_reordering_focus(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert ".pf-btn-approve-selected { grid-column: 1; }" in html
        assert ".pf-btn-deny-selected { grid-column: 2; }" in html


class TestRowNamesItsObject:
    """The row's title is ``summary``, the one fact that decides the
    request, and the raw MCP tool id is only the kicker. ``tool_name`` is
    always populated, so a title that fell back to ``summary`` would never
    show it, and a raw tool id as the headline is what README positions the
    product *against*."""

    def test_title_is_the_summary_and_the_kicker_is_the_tool(self):
        row = approval_list_html.row_from_approval(
            _card(summary='Read "Q3 forecast — legal review"', tool="gmail_get_thread"),
        )
        html = approval_list_html._row_html(row)
        title_at = html.index('class="pf-approval-title"')
        kicker_at = html.index('class="pf-approval-kicker"')
        assert 'Read &quot;Q3 forecast — legal review&quot;' in html[title_at:]
        assert "Read Gmail message" in html[kicker_at:title_at]
        assert "Gmail" in html[kicker_at:title_at]

    def test_raw_tool_id_leaves_the_kicker_and_is_available_to_the_disclosure(self):
        row = approval_list_html.row_from_approval(_card(summary="Read a thing", tool="gmail_get_thread"))
        html = approval_list_html._row_html(row)
        kicker_at = html.index('class="pf-approval-kicker"')
        title_at = html.index('class="pf-approval-title"')
        assert "gmail_get_thread" not in html[kicker_at:title_at]
        # Carried on the row itself, so the Details disclosure can show it
        # on first paint too -- pfLastRows is empty until the first SSE tick.
        assert 'data-tool="gmail_get_thread"' in html

    def test_falls_back_to_tool_name_when_there_is_no_summary(self):
        # A confirm/choice dialog has no summary at all.
        row = approval_list_html.row_from_approval(_card(summary="", tool_name="Read Gmail message"))
        html = approval_list_html._row_html(row)
        title_at = html.index('class="pf-approval-title"')
        assert "Read Gmail message" in html[title_at:]

    def test_live_rerender_uses_the_same_precedence(self):
        js = approval_list_html._JS
        body = js[js.index("function rowHtml("):js.index("function groupHtml(")]
        assert "row.summary || row.tool_name ||" in body


class TestReadWriteDirectionOnTheRow:
    """The card commits hard to read vs write -- a pill in the header and a
    coloured rail down the window edge -- so the row carries the direction
    too, from the ``gate_kind`` that also drives the Approve-selected
    composition label."""

    def test_read_gate_gets_a_read_pill(self):
        row = approval_list_html.row_from_approval(_card(gate_kind="review"))
        assert 'class="pf-approval-pill pf-approval-pill-read">Read<' in approval_list_html._row_html(row)

    def test_write_gate_gets_a_write_pill(self):
        row = approval_list_html.row_from_approval(_card(gate_kind="popup"))
        assert 'class="pf-approval-pill pf-approval-pill-write">Write<' in approval_list_html._row_html(row)

    def test_a_bare_confirm_dialog_has_no_direction_and_no_pill(self):
        row = approval_list_html.row_from_approval(_card(gate_kind=""))
        assert "pf-approval-pill" not in approval_list_html._row_html(row)

    def test_pill_uses_the_same_token_families_as_the_card(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert "var(--color-accent-100)" in html and "var(--color-accent-700)" in html
        assert "var(--color-accent-2-100)" in html and "var(--color-accent-2-700)" in html

    def test_live_rerender_mirrors_the_pill(self):
        js = approval_list_html._JS
        assert "pf-approval-pill-read" in js
        assert "pf-approval-pill-write" in js


class TestHeadingComposition:
    def test_names_the_read_write_split(self):
        rows = [
            approval_list_html.row_from_approval(_card(id="a", gate_kind="review")),
            approval_list_html.row_from_approval(_card(id="b", gate_kind="review")),
            approval_list_html.row_from_approval(_card(id="c", gate_kind="popup")),
        ]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert "3 approvals pending" in html
        assert "2 reads · 1 write" in html

    def test_heading_element_exists_even_with_nothing_pending(self):
        # render() has to be able to reach it on the tick that takes the
        # page from empty to non-empty; an element conditional on the first
        # paint having had rows is one the live re-render cannot update.
        html = approval_list_html.build_list_html([], csrf="t")
        assert 'id="pf-approvals-heading"' in html

    def test_live_rerender_updates_the_heading(self):
        assert "function updateHeading(" in approval_list_html._JS
        assert "updateHeading(pfLastRows)" in approval_list_html._JS


class TestApproveSelectedIsNotTheLoudestControl:
    """Approve-selected is the least-informed action -- select-all plus one
    click, off one-line summaries -- so it must not be as loud (filled
    ``var(--color-accent)``) as Review, the one that opens disclosure."""

    def test_approve_selected_is_an_outline(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert "border: 1px solid var(--color-accent); background: transparent;" in html

    def test_review_keeps_the_fill(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert ".pf-btn-review { background: var(--color-accent); color: #fff; }" in html

    def test_the_composition_guard_on_the_label_is_kept(self):
        # The label naming "9 reads, 3 writes" is what stops an unintended
        # write hiding in a read-shaped batch -- the weight was wrong, not
        # this.
        assert "compositionLabel" in approval_list_html._JS


class TestConnectorIconsSurviveLiveUpdates:
    """The first paint and the live re-render must draw the same brand
    icon; if the re-render fell back to a letter badge, every row would
    silently degrade within one poll interval -- on the page that most
    needs to look trustworthy. The icon lives in one CSS rule per
    connector, which both render paths reach by class name."""

    def _rows(self, *connectors):
        return [
            approval_list_html.row_from_approval(_card(id=f"i{n}", connector=c))
            for n, c in enumerate(connectors)
        ]

    def test_both_render_paths_emit_the_same_icon_element(self):
        rows = self._rows("gmail")
        first_paint = approval_list_html._row_html(rows[0])
        assert 'class="pf-approval-icon pf-approval-icon-img pf-approval-icon-gmail"' in first_paint
        # The JS mirror builds the identical class list, from a name list
        # rather than any image data of its own.
        js = approval_list_html._JS
        assert "pf-approval-icon pf-approval-icon-img pf-approval-icon-" in js
        assert "pfIconConnectors" in js

    def test_the_image_data_appears_once_per_connector_not_once_per_row(self):
        from privacyfence import approval_icons

        html = approval_list_html.build_list_html(self._rows("gmail", "gmail", "gmail"), csrf="t")
        # Every bundled connector's icon is baked in regardless of what's
        # pending (see test_icon_css_present_even_when_nothing_is_pending
        # below), so the count is the whole bundled set's size, not "gmail"
        # alone -- and still exactly once per connector, not once per row.
        # The bundled agent marks ride along the same way, once each.
        assert html.count("data:image/png;base64,") == (
            len(approval_icons.all_connector_icons()) + len(approval_icons.all_agent_icons())
        )

    def test_a_connector_with_no_bundled_icon_still_gets_a_letter_badge(self):
        rows = self._rows("nosuchconnector")
        row_html = approval_list_html._row_html(rows[0])
        assert 'class="pf-approval-icon pf-approval-icon-fallback">N<' in row_html

    def test_the_connector_name_list_is_handed_to_the_page(self):
        # This is every bundled connector, not just the
        # ones with a row on the page right now -- otherwise a connector
        # with nothing pending at first paint would draw a letter badge for
        # any row that arrives for it later, until the next full reload.
        from privacyfence import approval_icons

        html = approval_list_html.build_list_html(self._rows("gmail", "slack"), csrf="t")
        expected = json.dumps(sorted(approval_icons.all_connector_icons()))
        assert f"var pfIconConnectors = {expected}" in html

    def test_a_connector_name_that_is_not_a_safe_css_identifier_gets_no_rule(self):
        # The slug is interpolated into a selector and a class attribute,
        # so it is constrained rather than escaped -- anything else falls
        # through to the letter badge.
        assert approval_list_html._icon_slug("gmail") == "gmail"
        assert approval_list_html._icon_slug('a"};x{y:z') == ""
        assert approval_list_html._icon_slug("") == ""

    def test_icon_css_present_even_when_nothing_is_pending(self):
        # A connector with nothing pending at first paint still needs an
        # icon rule, or a row that arrives for it later draws a letter
        # badge until the next full page load. The bundled icon set is small and fixed, so it's all baked
        # in unconditionally instead.
        from privacyfence import approval_icons

        html = approval_list_html.build_list_html([], csrf="t")
        assert "data:image/png;base64," in html
        expected = json.dumps(sorted(approval_icons.all_connector_icons()))
        assert f"var pfIconConnectors = {expected}" in html


class TestFirstRunEmptyState:
    """"Nothing is waiting. / PrivacyFence is watching." is exactly
    right on a working install and misleading on one where no connector is
    authenticated -- nothing is waiting because nothing *can* wait, and the
    reassurance claims a protection that isn't running."""

    def test_steady_state_copy_when_something_is_authenticated(self):
        html = approval_list_html.build_list_html([], csrf="t", any_authed=True)
        assert "Nothing is waiting." in html
        assert "PrivacyFence is watching." in html
        assert "Nothing is governed yet." not in html

    def test_first_run_copy_and_call_to_action_when_nothing_is(self):
        html = approval_list_html.build_list_html([], csrf="t", any_authed=False)
        assert "Nothing is governed yet." in html
        assert "Nothing is waiting." not in html
        assert 'href="/settings/connectors"' in html

    def test_defaults_to_the_steady_state_copy(self):
        # A caller that cannot determine the answer must never tell someone
        # who is already set up that they aren't.
        assert "Nothing is waiting." in approval_list_html.build_list_html([], csrf="t")

    def test_the_live_rerender_uses_the_same_branch(self):
        # render() writes the empty state too, on the tick that takes the
        # last approval away -- it must not revert to the other copy.
        first_run = approval_list_html.build_list_html([], csrf="t", any_authed=False)
        assert first_run.count("Nothing is governed yet.") == 2  # markup + the JS constant
        assert "Nothing is waiting." not in first_run

    def test_only_the_empty_state_changes_not_a_populated_list(self):
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t", any_authed=False)
        assert "Nothing is governed yet." not in html.split("<script")[0]


class TestAgentOnTheRow:
    """Each row shows who is asking in the same tiered form as the
    card (agent_label.py) -- only the attested tier draws the vendor's mark."""

    @staticmethod
    def _row_for(agent):
        return approval_list_html._row_html(approval_list_html.row_from_approval(_card(agent=agent)))

    def test_unattributed_row_is_unknown_not_blank_not_claude(self):
        # Verification 3 (ADR 0006): no usable signal renders as unknown on the list too.
        from privacyfence.agent_identity import UNKNOWN_AGENT, UNRECOGNISED_LABEL

        for card in (_card(), _card(agent=UNKNOWN_AGENT)):
            row_html = approval_list_html._row_html(approval_list_html.row_from_approval(card))
            assert 'data-agent-tier="unknown"' in row_html
            assert f'<span class="pf-approval-agent-name">{UNRECOGNISED_LABEL}</span>' in row_html
            assert "not verified" in row_html
            assert "Claude" not in row_html

    def test_a_row_dict_with_no_agent_field_is_unknown(self):
        row = approval_list_html.row_from_approval(_card())
        del row["agent"]
        assert 'data-agent-tier="unknown"' in approval_list_html._row_html(row)

    def test_an_unexpected_tier_is_treated_as_unknown(self):
        html = approval_list_html._agent_html({"tier": "bogus", "headline": "X", "claim": "", "icon_id": "claude"})
        assert 'data-agent-tier="unknown"' in html
        assert "pf-approval-agent-mark" not in html

    def test_attested_row_draws_the_mark_and_claimed_row_does_not(self):
        from privacyfence.agent_identity import AgentSource, identify

        attested = self._row_for(identify("claude-code", "", AgentSource.OVERRIDE))
        claimed = self._row_for(identify("claude-code", "", AgentSource.CLIENT_INFO))
        assert 'pf-approval-agent-mark pf-approval-agent-mark-claude-code' in attested
        assert '<span class="pf-approval-agent-name">Claude Code</span>' in attested
        assert "not verified" not in attested
        assert "pf-approval-agent-mark" not in claimed
        assert '<span class="pf-approval-agent-name">Says it is Claude Code</span>' in claimed
        assert "not verified" in claimed
        assert attested != claimed

    def test_attested_row_with_no_bundled_mark_draws_none(self):
        html = approval_list_html._agent_html(
            {"tier": "attested", "headline": "Z", "claim": "", "icon_id": "no-such-agent"}
        )
        assert "pf-approval-agent-mark" not in html
        assert "pf-approval-agent-glyph" not in html

    def test_unmatched_claim_is_shown_escaped_with_bidi_stripped(self):
        from privacyfence.agent_identity import AgentSource, identify

        row_html = self._row_for(identify("<b>x</b>‮⁧y", "", AgentSource.CLIENT_INFO))
        assert 'data-agent-tier="unknown"' in row_html
        assert "&lt;b&gt;x&lt;/b&gt;y" in row_html
        assert "<b>" not in row_html
        assert "‮" not in row_html and "⁧" not in row_html

    def test_every_bundled_agent_mark_is_baked_into_the_page_once(self):
        from privacyfence import approval_icons

        html = approval_list_html.build_list_html([], csrf="t")
        for agent_id, uri in approval_icons.all_agent_icons().items():
            assert html.count(uri) == 1, agent_id
            assert f".pf-approval-agent-mark-{agent_id}{{" in html
        assert json.dumps(sorted(approval_icons.all_agent_icons())) in html

    def test_live_rerender_mirrors_the_agent_label(self):
        js = approval_list_html._JS
        assert "function agentHtml(agent)" in js
        assert "agentHtml(row.agent)" in js
        assert "pf-approval-agent-glyph" in js
        assert "pf-approval-agent-unverified" in js

    def test_summary_dict_carries_the_tiered_label(self):
        from privacyfence.agent_identity import AgentSource, agent_scope, identify

        with agent_scope(identify("openai-mcp", "", AgentSource.CLIENT_INFO)):
            approval = _real_card()
        assert approval.to_summary_dict()["agent"] == {
            "tier": "claimed", "headline": "Says it is ChatGPT", "claim": "", "icon_id": "",
        }

    def test_an_agent_icon_with_an_unsafe_id_gets_no_css_rule(self, monkeypatch):
        # The id is interpolated into a CSS selector, so anything that isn't
        # a plain slug is dropped rather than escaped -- same as connectors.
        from privacyfence import approval_icons

        monkeypatch.setattr(approval_icons, "all_agent_icons", lambda: {
            'x"}body{color:red': "data:image/png;base64,AAAA", "claude": "data:image/png;base64,BBBB",
        })
        assert approval_list_html._agent_icon_uris() == {"claude": "data:image/png;base64,BBBB"}
