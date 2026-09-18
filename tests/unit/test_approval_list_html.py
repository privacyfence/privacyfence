"""approval_list_html.py -- the /approvals list page (docs/approval-list-
ui-ux.md §2, the P1-compatible slice)."""
from __future__ import annotations

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
        # §2.2's central asymmetry: Deny is on the row, Allow never is.
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

    def test_approve_selected_starts_disabled(self):
        # Phase 3 of the binder plan: like Deny selected, nothing is
        # selected on first paint, so there is nothing to approve yet.
        rows = [approval_list_html.row_from_approval(_real_card())]
        html = approval_list_html.build_list_html(rows, csrf="t")
        assert 'id="pf-approve-selected" disabled' in html

    def test_toolbar_absent_on_the_empty_state(self):
        html = approval_list_html.build_list_html([], csrf="t")
        assert 'id="pf-approvals-toolbar"' not in html

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
    """F2/F3: the row's own ``flex-wrap`` never engages, because
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
    """F4: ``tool_name`` was the row's title and ``summary`` only its
    fallback -- and ``tool_name`` is always populated, so a normal row never
    reached the fallback and the one fact that decides the request was never
    on screen. The raw MCP tool id was the kicker instead, which is what
    README positions the product *against*."""

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
    """F6: the card commits hard to read vs write -- a pill in the header
    and a coloured rail down the window edge -- while the row carried
    neither, though ``gate_kind`` was already in the payload and already
    drove the Approve-selected composition label."""

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
    """F5: both Approve-selected and Review were filled
    ``var(--color-accent)``, so the least-informed action -- select-all
    plus one click, off one-line summaries -- was as loud as the one that
    opens disclosure."""

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
