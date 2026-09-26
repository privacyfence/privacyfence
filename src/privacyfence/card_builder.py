"""Pure translation from gate.py's show_popup/show_read_popup argument
shapes into approval_window_html.build_card_stack_html()'s own argument
shape, for web_approval_ui.py: the reading-time estimate, the "Seen N times
this week" caption, the disclosure card's rows, each accept_all candidate's
"Always allow — {hint}" label, and the pdf/image data-URI precedence for the
WIDE preview pane.
"""
from __future__ import annotations

import base64
from collections.abc import Callable

from . import (
    agent_label, approval_icons, approval_window_html, pdf_render, pii_detector, text_extraction, write_effects,
)
from .agent_identity import UNKNOWN_AGENT, AgentIdentity

# Shown above the button row for operations
# auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS lists.
TEMP_ACCEPT_DISCLOSURE_TEXT = (
    "Approving this also allows further calls like this to the same file "
    "for a few minutes without asking again."
)


def _estimate_reading_seconds(text: str) -> int:
    """~200 words/minute silent-reading estimate, floored at 1 second so an
    empty/tiny body still renders a sane label rather than "~0 sec read"."""
    words = len(text.split())
    return max(1, round(words / 200 * 60))


def _reading_time_label(text: str) -> str:
    seconds = _estimate_reading_seconds(text)
    if seconds < 60:
        return f"~{seconds} sec read"
    return f"~{round(seconds / 60)} min read"


def _seen_count_text(seen_count: int) -> str:
    """The frequency line, rendered on every card rather than only when
    something has been seen before.

    It used to be omitted entirely at ``seen_count == 0``, which made "this
    is the first time Claude has asked for this" look identical to "this
    kind of card doesn't carry this line" -- and absence read as the
    latter. The frequency line is the card's main defence against
    rubber-stamping a request that has quietly become routine, so the
    first-time case is information, not the lack of it."""
    if seen_count <= 0:
        return "First time this week"
    return f"Seen {seen_count} time{'s' if seen_count != 1 else ''} this week"


def _pii_highlighter(
    pii_categories: list[str] | None,
) -> Callable[[str], list[tuple[int, int]]] | None:
    """Marks each PII match where it actually sits in the preview text, so
    the risk card's category tags read as a legend rather than as a search
    task -- "IBAN · National ID" otherwise tells a reviewer that something
    matched and leaves them to find it in a multi-message thread by eye.

    ``None`` unless this card is already declaring a PII match, so a card
    with no risk section does no scanning and gains no marks.

    Scoped to the categories the card names. Highlighting a category the
    card doesn't declare would put a mark on screen that the legend above
    it cannot explain -- and the two are derived from different scans (the
    gate's, over the raw payload; this one, over the rendered preview), so
    they are not guaranteed to agree on their own.

    Nothing new is disclosed by any of this: the text is already the
    contents of the pane, and ``scan_text`` returns positions only, never
    the matched substring (see its own docstring).
    """
    declared = set(pii_categories or [])
    if not declared:
        return None

    def spans(text: str) -> list[tuple[int, int]]:
        return [
            (m.start, m.end) for m in pii_detector.scan_text(text) if m.category in declared
        ]

    return spans


def _disclosure_rows(
    is_read: bool, new_info: dict[str, str] | None, visibility: dict[str, str] | None,
    agent_display_name: str = agent_label.NEUTRAL_SUBJECT,
) -> list[tuple[str, str]]:
    """The disclosure card's rows, on a read gate only: the connector's own
    ``new_info`` pairs first, then the sentences derived from its
    ``visibility`` policy (approval_window_html.disclosure_rows_from_visibility).

    Connectors write ``new_info`` with approval_window_html.AGENT_PLACEHOLDER
    where they mean the caller ("Content returned to {agent}"), since they
    never learn who that is; it is filled in here, raw, and escaped by the
    row renderer like every other row."""
    if not is_read:
        return []
    fill = approval_window_html.fill_agent_placeholder
    rows = [
        (fill(label, agent_display_name), fill(value, agent_display_name))
        for label, value in (new_info or {}).items()
    ]
    if visibility:
        rows += approval_window_html.disclosure_rows_from_visibility(
            visibility, agent_display_name=agent_display_name,
        )
    return rows


def build_card_html(
    *,
    title: str,
    preview: dict[str, str],
    details_text: str,
    is_read: bool,
    layout: str,
    accept_all_choices: list[tuple[str, str]] | None = None,
    pii_categories: list[str] | None = None,
    visibility: dict[str, str] | None = None,
    claude_reason: str = "",
    write_content_flags: list[str] | None = None,
    seen_count: int = 0,
    pdf_bytes: bytes = b"",
    connector: str = "",
    preview_bytes: bytes = b"",
    preview_mime_type: str = "",
    new_info: dict[str, str] | None = None,
    preview_tables: list[dict] | None = None,
    preview_blocks: list[dict] | None = None,
    table_only: bool = False,
    upload_forced: bool = False,
    temp_accept_eligible: bool = False,
    tool: str = "",
    agent: AgentIdentity = UNKNOWN_AGENT,
) -> str:
    """Build the full card-stack HTML document for one approval. Nothing here
    sizes a window: the browser lays out a real page.

    ``content_kind`` is accepted by show_read_popup's own signature but
    has no effect on rendering -- see that function's docstring -- so it's
    deliberately not a parameter here.

    ``agent`` is the identity the request's ``PendingApproval`` captured
    (ADR 0006). It is shown in its tier's treatment in the header, and its
    tier decides what the card's copy calls the caller everywhere it names
    one (approval_window_html.AGENT_PLACEHOLDER): the attested name, or "the
    AI system" for a claimed or unknown one -- see agent_label.py. The
    default is unknown, never "Claude".
    """
    label = agent_label.label_for(agent)
    agent_display_name = label.subject
    pdf_data_uri = ""
    pdf_page_uris: list[str] = []
    pdf_page_count = 0
    pdf_fallback_text = ""
    if pdf_bytes:
        pdf_data_uri = f"data:application/pdf;base64,{base64.b64encode(pdf_bytes).decode('ascii')}"
        # The pages a phone shows instead of the <embed> (see build_preview_body_html). A PDF
        # that will not render shows its extracted text there instead, never nothing.
        rendered = pdf_render.render_first_pages(pdf_bytes)
        if rendered is not None:
            pdf_page_uris = [
                f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}" for png in rendered.pages
            ]
            pdf_page_count = rendered.page_count
        else:
            pdf_fallback_text = text_extraction.extract_text(pdf_bytes, "application/pdf")
    image_data_uri = ""
    if not pdf_data_uri and preview_bytes and preview_mime_type.startswith("image/"):
        image_data_uri = (
            f"data:{preview_mime_type};base64,{base64.b64encode(preview_bytes).decode('ascii')}"
        )

    # table_only suppresses details_text only when there's a real table to
    # show instead -- and never when preview_blocks is set, which already
    # controls exactly what renders on its own.
    body_text = "" if table_only and preview_tables and not preview_blocks else details_text
    preview_body_html = approval_window_html.build_preview_body_html(
        body_text, image_data_uri=image_data_uri, pdf_data_uri=pdf_data_uri,
        pdf_page_uris=pdf_page_uris, pdf_page_count=pdf_page_count, pdf_fallback_text=pdf_fallback_text,
        tables=preview_tables, blocks=preview_blocks,
        highlight=_pii_highlighter(pii_categories),
    )

    accept_all_labels = [
        f"Always allow — {hint}" if hint else "Always allow"
        for _rule_name, hint in (accept_all_choices or [])
    ]

    # A read card ends with "What will be provided to Claude"; a write card
    # had nothing that named its own consequence, only the payload and
    # Claude's reason. This is that row -- last in the action card ("Action to perform"),
    # so the payload is read first and the outcome last, which is the order
    # the decision is actually made in. Read gates never get one: their
    # consequence card already exists.
    section_1 = dict(preview or {})
    if not is_read:
        effect = write_effects.effect_for(tool)
        if effect:
            section_1[write_effects.EFFECT_LABEL] = effect

    return approval_window_html.build_card_stack_html(
        layout=layout,
        title=title,
        connector_icon_data_uri=approval_icons.icon_data_uri(approval_icons.connector_icon_path(connector)),
        shield_icon_data_uri=approval_icons.icon_data_uri(approval_icons.shield_icon_path()),
        is_read=is_read,
        seen_count_text=_seen_count_text(seen_count),
        preview=section_1,
        claude_reason=claude_reason or "",
        disclosure_rows=_disclosure_rows(is_read, new_info, visibility, agent_display_name),
        pii_categories=pii_categories or [],
        write_content_flags=write_content_flags or [],
        upload_forced=upload_forced,
        temp_accept_text=TEMP_ACCEPT_DISCLOSURE_TEXT if temp_accept_eligible else "",
        preview_kicker=f"Preview ({_reading_time_label(details_text)})",
        preview_body_html=preview_body_html,
        accept_all_labels=accept_all_labels,
        agent_label=label,
        agent_icon_data_uri=approval_icons.icon_data_uri(approval_icons.agent_icon_path(label.icon_id)),
    )
