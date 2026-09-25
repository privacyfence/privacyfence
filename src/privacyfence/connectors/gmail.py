"""Gmail connector."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import local_files
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..download_staging import get_download_staging_store
from ..gate import current_reason, gated_call
from ..gmail_client import (
    GmailClient,
    GmailClientError,
    resolve_attachment_destination,
    signature_has_content,
    signature_plain_text,
)
from ..html_to_text import html_to_text
from ..org_mode import DownloadDeliveryConfig
from ..principal import current_principal
from ..privacy_filter import apply_list, apply_text, category_policy
from ..text_extraction import extract_text, is_prefetch_worthy, preview_blocks_for

logger = logging.getLogger(__name__)

# Cap on how big an attachment we'll fetch pre-approval -- for a preview
# (images) or a PII scan (text/PDF/DOCX/PPTX). gmail_download_attachment's
# gate has to fully fetch the attachment to do either at all (no
# partial-fetch API), so this bounds how much we'll pull down before the
# human has decided anything.
_ATTACHMENT_PREFETCH_MAX_BYTES = 5_000_000

# ADR 0007: per-path ceiling passed to local_files.require_local_files() for
# the three *_with_attachments tools' attachment paths -- the file bridge
# buffers each upload entirely in memory (upload_staging.UploadStagingStore.
# fill()), unlike the old direct open()/read(), so this is a real per-file
# memory cap, not just a preview-read cap like _ATTACHMENT_PREFETCH_MAX_BYTES
# above (which only bounds the pre-approval preview/PII-scan read, not the
# actual attach). Reuses gmail_client's own outgoing-message cap
# (_MAX_TOTAL_ATTACHMENT_BYTES) as the natural ceiling for any single
# attachment: no one attachment can usefully exceed the whole draft's own
# size limit anyway, and gmail_client._attach_files() still enforces the
# true cross-attachment running total once the bytes are actually read.
_ATTACHMENT_UPLOAD_MAX_BYTES = 18_000_000


def _parse_attachment_paths(value: str) -> list[str]:
    """Parse the ``attachments`` tool argument: a JSON array of local file paths.

    Raises ``ValueError`` (rather than drive.py's ``_parse_json_str_list``
    silent-None-on-invalid-input pattern) because an empty or malformed
    ``attachments`` value on one of these tools means the whole point of
    calling it -- attaching something -- silently didn't happen; that should
    fail loudly, not fall back to a draft with no attachments.
    """
    if not value or not value.strip():
        raise ValueError(
            'attachments: provide a JSON array of local file paths, e.g. ["/path/to/file.pdf"]'
        )
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"attachments: invalid JSON array: {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
        raise ValueError("attachments: must be a JSON array of strings (local file paths)")
    if not parsed:
        raise ValueError("attachments: at least one file path is required")
    return parsed


def _body_params() -> list[ToolParam]:
    """The body/body_markdown ToolParam pair shared by all 6 draft tools."""
    return [
        ToolParam(
            "body", "str", required=False, default="",
            description=(
                "Plain-text body. May be omitted if body_markdown is given -- "
                "the plain-text alternative is then auto-derived from it. At "
                "least one of body/body_markdown is required."
            ),
        ),
        ToolParam(
            "body_markdown", "str", required=False, default="",
            description=(
                "Optional Markdown body for a rich-text draft. Supports "
                "**bold**, *italic*, ==highlight==, [links](url), "
                "bullet/numbered lists, and `# Heading 1`/`## Heading 2` "
                "(rendered as Gmail's Large/Huge font-size presets, not "
                "raw heading tags -- no tables). When given, the draft is "
                "sent as plain text + HTML together, so it renders "
                "formatted in HTML-capable clients and as readable plain "
                "text everywhere else."
            ),
        ),
    ]


def _sender_params() -> list[ToolParam]:
    """The include_signature/send_as ToolParam pair shared by all 6 draft tools."""
    return [
        ToolParam(
            "include_signature", "bool", required=False, default=None,
            description=(
                "Append the user's Gmail signature (the one Gmail stores for "
                "the sending address) to the end of the body. Omit to use the "
                "user's 'Append Gmail signature to drafts' setting. Don't also "
                "write a sign-off block of your own when this is on."
            ),
        ),
        ToolParam(
            "send_as", "str", required=False, default="",
            description=(
                "Send from this Gmail send-as address instead of the account's "
                "default (sets From:, and the signature used is this address's "
                "own). Must be one of the account's configured send-as "
                "addresses -- anything else is rejected."
            ),
        ),
    ]


def _attachments_param() -> ToolParam:
    """The ``attachments`` ToolParam shared by all three
    ``gmail_*_with_attachments`` tools."""
    return ToolParam(
        "attachments", "str",
        description=(
            'JSON array of local file paths to attach, e.g. '
            '["/path/to/report.pdf"] -- each either a path on the '
            "user's computer (where Claude Desktop runs: absolute, or "
            "starting with ~/. Claude's own working or outputs directory "
            "is fine) or 'upload:<upload_id>', the id "
            "privacyfence_create_upload_slot returned after you PUT the "
            "file's bytes to its upload_url -- use that form if a plain "
            "path fails with an error about PrivacyFence being unable to "
            "read files in your home folder directly (e.g. no "
            "PrivacyFence extension is installed, or this is an "
            "organization-managed install: a plain path there is read "
            "from wherever PrivacyFence's own server runs, not the "
            "user's machine -- prefer 'upload:<upload_id>'). At least one "
            "required."
        ),
    )


def _require_body(body: str, body_markdown: str, tool: str) -> None:
    """Reject the call before gating if neither body nor body_markdown was given."""
    if not body.strip() and not body_markdown.strip():
        raise ValueError(f"{tool}: provide body and/or body_markdown -- at least one is required")


def _preview_body_text(body: str, body_markdown: str, signature_html: str = "") -> str:
    """Text shown to the human reviewer for the draft's content.

    Prefers the raw body_markdown source over body when both are given --
    that guarantees what the reviewer approves always matches what actually
    gets rendered as HTML, rather than trusting the two to describe the same
    content independently. The signature, when one is being appended, is
    shown exactly as gmail_client appends it to the plain-text part.
    """
    content = body_markdown if body_markdown.strip() else body
    return content + signature_plain_text(signature_html)


@dataclass
class _DraftSender:
    """What a draft tool resolved, before gating, about who it's from:
    the signature to append (``""`` for none) and the From: header to set
    (``""`` to leave it to Gmail's default)."""

    signature_html: str = ""
    from_header: str = ""
    alias_email: str = ""
    signature_requested: bool = False

    def client_kwargs(self) -> dict[str, str]:
        kwargs = {"signature_html": self.signature_html, "from_header": self.from_header}
        return {k: v for k, v in kwargs.items() if v}

    def preview(self, preview: dict[str, str]) -> dict[str, str]:
        """``preview`` with From/Signature rows added -- metadata only, the
        signature's text itself is in details_text with the body."""
        out = {"From": self.alias_email, **preview} if self.from_header else dict(preview)
        if self.signature_requested:
            if not self.signature_html:
                out["Signature"] = f"None set for {self.alias_email} -- nothing appended"
            elif signature_plain_text(self.signature_html):
                out["Signature"] = f"Appended ({self.alias_email})"
            else:
                out["Signature"] = f"Appended ({self.alias_email}; image only, rich-text drafts only)"
        return out

    def details_text(self, body: str, body_markdown: str) -> str:
        return _preview_body_text(body, body_markdown, self.signature_html)

    def write_content_scan_text(self, body: str, body_markdown: str) -> str | None:
        # The signature is the user's own contact block, not content Claude
        # drafted: scanning it would flag its phone number/address on every
        # draft. See docs/adr/0038-gmail-draft-signature-is-shown-but-not-write-scanned.md.
        return _preview_body_text(body, body_markdown) if self.signature_html else None


class GmailConnector(Connector):
    def __init__(self, client: GmailClient) -> None:
        self._gmail = client
        self.my_email: str = ""
        # See connectors/drive.py's own DriveConnector.__init__ comment.
        self.download_mode: str = "local"
        self.download_config: DownloadDeliveryConfig | None = None
        self.download_base_url: str = ""
        # settings.yaml's gmail.append_signature_to_drafts -- the default
        # for every draft tool's include_signature when a call omits it.
        self.append_signature: bool = False

    @property
    def name(self) -> str:
        return "gmail"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="gmail_list_messages",
                description=(
                    "Search Gmail and return matching message summaries "
                    "(id, thread_id, subject, sender, date). "
                    "Auto-approved — no body content is returned."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=10),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_list_threads",
                description=(
                    "Search Gmail and return matching thread summaries "
                    "(id, snippet). Auto-approved — snippet is a short excerpt of the "
                    "last message's body, subject to the same 'body' privacy category "
                    "as gmail_get_message."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=10),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_get_message",
                description=(
                    "Fetch a single Gmail message by id, including body, metadata, "
                    "and attachment list. Requires user approval."
                ),
                params=[ToolParam("message_id", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_get_thread",
                description=(
                    "Fetch a full Gmail thread by id, including all messages. "
                    "Requires user approval."
                ),
                params=[ToolParam("thread_id", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_list_message_attachments",
                description=(
                    "List attachment names, MIME types, and sizes for a Gmail "
                    "message. Auto-approved — metadata only, no attachment "
                    "content is returned. Use gmail_download_attachment to "
                    "fetch the actual file."
                ),
                params=[ToolParam("message_id", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_download_attachment",
                description=(
                    "Download a Gmail attachment's content. Identify the "
                    "attachment by the name returned from "
                    "gmail_list_message_attachments. On a local install: saved "
                    "to destination_dir, and the saved file path is returned -- "
                    "destination_dir is required, there is no default, so choose "
                    "deliberately: pass ~/Downloads (or another path the user "
                    "asked for) when this attachment is a deliverable the user "
                    "should find afterward, or your own working/scratch "
                    "directory when you're only downloading it to read or "
                    "process it yourself. On an organization-managed install: "
                    "destination_dir is ignored (there is no local filesystem "
                    "you and the human share) -- a small attachment's bytes "
                    "come back directly in this tool's result so you can read "
                    "or hand it to the human yourself; a larger one comes back "
                    "as a one-time link the human opens in their own signed-in "
                    "browser tab instead. Requires user approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
                    ToolParam("attachment_name", "str"),
                    ToolParam(
                        "destination_dir",
                        "str",
                        required=True,
                        description=(
                            "Where to save the attachment -- required, no default. "
                            "Use ~/Downloads (or a path the user specified) if the "
                            "user should find this file afterward; use your own "
                            "working/scratch directory if it's only for you to read "
                            "or process."
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_create_draft",
                description="Create a Gmail draft. Requires user approval.",
                params=[
                    ToolParam("to", "str"),
                    ToolParam("subject", "str"),
                    *_body_params(),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                    *_sender_params(),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_reply_draft",
                description=(
                    "Create a Gmail draft replying to a single message, staying in the "
                    "same thread (sets threadId plus In-Reply-To/References so it "
                    "actually threads, unlike gmail_create_draft). Addressed only to the "
                    "original sender. Requires user approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
                    *_body_params(),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                    *_sender_params(),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_reply_all_draft",
                description=(
                    "Create a Gmail draft replying to all participants of a message "
                    "(original sender plus To/Cc recipients, excluding yourself), "
                    "staying in the same thread. Requires user approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
                    *_body_params(),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                    *_sender_params(),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_create_draft_with_attachments",
                description=(
                    "Create a Gmail draft with one or more local-file attachments. "
                    "Parallel to gmail_create_draft -- use this variant only when "
                    "there is something to attach; use gmail_create_draft when "
                    "there isn't, so a draft doesn't need this tool's extra "
                    "attachments argument for nothing. Requires user approval."
                ),
                params=[
                    ToolParam("to", "str"),
                    ToolParam("subject", "str"),
                    *_body_params(),
                    _attachments_param(),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                    *_sender_params(),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_reply_draft_with_attachments",
                description=(
                    "Create a Gmail draft replying to a single message, staying in "
                    "the same thread, with one or more local-file attachments. "
                    "Parallel to gmail_reply_draft -- use this variant only when "
                    "there is something to attach. Addressed only to the original "
                    "sender. Requires user approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
                    *_body_params(),
                    _attachments_param(),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                    *_sender_params(),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_reply_all_draft_with_attachments",
                description=(
                    "Create a Gmail draft replying to all participants of a message "
                    "(original sender plus To/Cc recipients, excluding yourself), "
                    "staying in the same thread, with one or more local-file "
                    "attachments. Parallel to gmail_reply_all_draft -- use this "
                    "variant only when there is something to attach. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
                    *_body_params(),
                    _attachments_param(),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                    *_sender_params(),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_add_label",
                description="Add a label to a Gmail message. Requires user approval.",
                params=[
                    ToolParam("message_id", "str"),
                    ToolParam("label_name", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_remove_label",
                description="Remove a label from a Gmail message. Requires user approval.",
                params=[
                    ToolParam("message_id", "str"),
                    ToolParam("label_name", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_archive_message",
                description=(
                    "Archive a Gmail message by removing it from the Inbox. "
                    "The message is not deleted and remains searchable. Requires user approval."
                ),
                params=[ToolParam("message_id", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
            ),
            ToolSpec(
                name="gmail_list_filters",
                description=(
                    "List all Gmail filters with their criteria and actions. "
                    "Auto-approved -- filter rules only, no message content is returned."
                ),
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_list_labels",
                description=(
                    "List all Gmail labels (system and user-created). Nested labels "
                    "have a '/' in their name (e.g. 'Work/Projects'). "
                    "Auto-approved -- label metadata only."
                ),
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_create_filter",
                description=(
                    "Create a Gmail filter. Provide at least one criteria field "
                    "(from_address, to_address, subject, query, has_attachment) and "
                    "at least one action (add_label_names, archive, mark_as_read, "
                    "star, forward_to). Requires user approval."
                ),
                params=[
                    ToolParam("from_address", "str", required=False, default=""),
                    ToolParam("to_address", "str", required=False, default=""),
                    ToolParam("subject", "str", required=False, default=""),
                    ToolParam(
                        "query", "str", required=False, default="",
                        description="Gmail search syntax; matches the filter's 'Has the words' field",
                    ),
                    ToolParam("has_attachment", "bool", required=False, default=False),
                    ToolParam(
                        "add_label_names", "str", required=False, default="",
                        description="Comma-separated label names to apply; created if missing",
                    ),
                    ToolParam("archive", "bool", required=False, default=False, description="Skip the Inbox"),
                    ToolParam("mark_as_read", "bool", required=False, default=False),
                    ToolParam("star", "bool", required=False, default=False),
                    ToolParam("forward_to", "str", required=False, default=""),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_update_filter",
                description=(
                    "Replace an existing Gmail filter's criteria and actions, "
                    "identified by filter_id (from gmail_list_filters). Gmail's API "
                    "has no native filter update, so this deletes the filter and "
                    "creates a new one with the given fields, which gets a new id. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("filter_id", "str"),
                    ToolParam("from_address", "str", required=False, default=""),
                    ToolParam("to_address", "str", required=False, default=""),
                    ToolParam("subject", "str", required=False, default=""),
                    ToolParam("query", "str", required=False, default=""),
                    ToolParam("has_attachment", "bool", required=False, default=False),
                    ToolParam("add_label_names", "str", required=False, default=""),
                    ToolParam("archive", "bool", required=False, default=False),
                    ToolParam("mark_as_read", "bool", required=False, default=False),
                    ToolParam("star", "bool", required=False, default=False),
                    ToolParam("forward_to", "str", required=False, default=""),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="gmail_create_label",
                description=(
                    "Create a Gmail label. Use '/' to create nested labels (e.g. "
                    "'Work/Projects' creates 'Projects' nested under 'Work', "
                    "creating 'Work' first if it doesn't already exist). Fails if "
                    "the exact label name already exists. Requires user approval."
                ),
                params=[ToolParam("label_name", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "gmail_list_messages":
            return await self._list_messages(**args)
        if tool == "gmail_list_threads":
            return await self._list_threads(**args)
        if tool == "gmail_get_message":
            return await self._get_message(**args)
        if tool == "gmail_get_thread":
            return await self._get_thread(**args)
        if tool == "gmail_list_message_attachments":
            return await self._list_message_attachments(**args)
        if tool == "gmail_download_attachment":
            return await self._download_attachment(**args)
        if tool == "gmail_create_draft":
            return await self._create_draft(**args)
        if tool == "gmail_reply_draft":
            return await self._reply_draft(**args)
        if tool == "gmail_reply_all_draft":
            return await self._reply_all_draft(**args)
        if tool == "gmail_create_draft_with_attachments":
            return await self._create_draft_with_attachments(**args)
        if tool == "gmail_reply_draft_with_attachments":
            return await self._reply_draft_with_attachments(**args)
        if tool == "gmail_reply_all_draft_with_attachments":
            return await self._reply_all_draft_with_attachments(**args)
        if tool == "gmail_add_label":
            return await self._add_label(**args)
        if tool == "gmail_remove_label":
            return await self._remove_label(**args)
        if tool == "gmail_archive_message":
            return await self._archive_message(**args)
        if tool == "gmail_list_filters":
            return await self._list_filters(**args)
        if tool == "gmail_list_labels":
            return await self._list_labels(**args)
        if tool == "gmail_create_filter":
            return await self._create_filter(**args)
        if tool == "gmail_update_filter":
            return await self._update_filter(**args)
        if tool == "gmail_create_label":
            return await self._create_label(**args)
        raise ValueError(f"Unknown Gmail tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto (no gate)
    # ------------------------------------------------------------------ #

    async def _list_messages(self, query: str, max_results: int = 10) -> Any:
        t0 = time.time()
        summaries = await self._fetch(self._gmail.list_messages, query, max_results)
        self._auto_audit("gmail_list_messages", "List Gmail Messages",
                         f"List messages: {query!r}", f"{len(summaries)} result(s)", t0)
        # Same "metadata" category gmail_get_message/gmail_get_thread already
        # apply to these same fields -- without this, blocking/redacting
        # metadata there had no effect, since search results carried the
        # unfiltered subject/sender/date regardless.
        return [
            {
                **s,
                "subject": apply_text("privacy", "metadata", s.get("subject", "") or ""),
                "sender": apply_text("privacy", "metadata", s.get("sender", "") or ""),
                "date": apply_text("privacy", "metadata", s.get("date", "") or ""),
            }
            for s in summaries
        ]

    async def _list_threads(self, query: str, max_results: int = 10) -> Any:
        t0 = time.time()
        summaries = await self._fetch(self._gmail.list_threads, query, max_results)
        self._auto_audit("gmail_list_threads", "List Gmail Threads",
                         f"List threads: {query!r}", f"{len(summaries)} result(s)", t0)
        # snippet is a genuine excerpt of the last message's body (straight
        # from the Gmail API), not structural metadata -- gate it under the
        # same "body" category gmail_get_message applies to the full body,
        # rather than leaving this the one unfiltered leak of that content.
        return [
            {**s, "snippet": apply_text("privacy", "body", s.get("snippet", "") or "")}
            for s in summaries
        ]

    async def _list_message_attachments(self, message_id: str) -> Any:
        t0 = time.time()
        message = await self._fetch(self._gmail.get_message, message_id)
        attachments = apply_list(
            "privacy", "attachments",
            [
                {"name": att.name, "mime_type": att.mime_type, "size": att.size}
                for att in (message.attachments or [])
            ],
        )
        self._auto_audit(
            "gmail_list_message_attachments", "List Gmail Attachments",
            f"List attachments: {message.subject or '(no subject)'}",
            message.sender or "", t0,
        )
        return {"message_id": message_id, "attachments": attachments}

    async def _list_filters(self) -> Any:
        t0 = time.time()
        filters = await self._fetch(self._gmail.list_filters)
        self._auto_audit("gmail_list_filters", "List Gmail Filters",
                         "List filters", f"{len(filters)} result(s)", t0)
        return filters

    async def _list_labels(self) -> Any:
        t0 = time.time()
        labels = await self._fetch(self._gmail.list_labels)
        self._auto_audit("gmail_list_labels", "List Gmail Labels",
                         "List labels", f"{len(labels)} result(s)", t0)
        return labels

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_message(self, message_id: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        recipients_raw = message.recipients if isinstance(message.recipients, str) else ", ".join(message.recipients or [])
        sender = apply_text("privacy", "metadata", message.sender or "")
        recipients = apply_text("privacy", "metadata", recipients_raw)
        date = apply_text("privacy", "metadata", message.date or "")
        subject = apply_text("privacy", "metadata", message.subject or "")
        raw_body = message.body_text or html_to_text(message.body_html) or ""
        body = apply_text("privacy", "body", raw_body)
        attachments = apply_list("privacy", "attachments", message.attachments or [])
        labels = ", ".join(message.labels or []) if message.labels else ""
        # From/Date/Subject are known for free via gmail_list_messages; To
        # (recipients) and Labels are not returned by any auto tool, so they
        # move to new_info below instead of this preview.
        preview = {
            "From": sender or "(unknown)",
            "Date": date or "(unknown)",
            "Subject": subject or "(no subject)",
        }
        new_info = {
            "To": recipients or "(unknown)",
            "Labels": labels,
        }
        filtered = {
            "id": message.id,
            "thread_id": message.thread_id,
            "subject": subject,
            "sender": sender,
            "recipients": recipients,
            "date": date,
            "body_text": body,
            "attachments": attachments,
            "labels": message.labels,
        }
        return await gated_call(
            connector=self.name,
            tool="gmail_get_message",
            tool_name="Read Email",
            summary=f"Read email: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=body or "(no body)",
            pii_scan_text=body,
            # No "Sender & metadata" row here: From/Date/Subject are already
            # in the preview (known via gmail_list_messages) and To is already
            # a concrete recipient list in new_info above -- an abstract
            # "Full sender & metadata" disclosure sentence would just
            # restate what's already shown as real values, not add
            # information.
            visibility={
                "Message body": category_policy("privacy", "body"),
                "Attachments": category_policy("privacy", "attachments"),
            },
            # No effect on the current rendering (build_preview_body_html has no
            # email special case -- From/Subject/Date are in the preview, To in
            # new_info) -- see gate.py's content_kind docstring.
            content_kind="email",
            my_email=self.my_email,
            args={"message_id": message_id},
        )

    async def _get_thread(self, thread_id: str) -> Any:
        thread = await self._fetch(self._gmail.get_thread, thread_id)
        messages = thread.messages if hasattr(thread, "messages") else []
        all_participants: set[str] = set()
        for m in messages:
            if hasattr(m, "sender") and m.sender:
                all_participants.add(m.sender)
            if hasattr(m, "recipients"):
                recips = m.recipients if isinstance(m.recipients, list) else [m.recipients]
                all_participants.update(r for r in recips if r)
        subject_raw = (messages[0].subject if messages and hasattr(messages[0], "subject") else "") or thread_id
        subject = apply_text("privacy", "metadata", subject_raw)
        participants = apply_text("privacy", "metadata", ", ".join(sorted(all_participants)))
        dates = [m.date for m in messages if hasattr(m, "date") and m.date]
        date_range_raw = f"{dates[0]} – {dates[-1]}" if len(dates) > 1 else (dates[0] if dates else "")
        date_range = apply_text("privacy", "metadata", date_range_raw)
        # gmail_list_threads itself only ever returns id+snippet -- but
        # gmail_list_messages returns thread_id per message, and Gmail
        # convention is that a thread's replies share its subject (often
        # "Re: <subject>") -- so if Claude has already listed even one
        # message belonging to this thread (a common path to learning this
        # thread_id in the first place), it already knows the subject,
        # same "conditionally known via a call that commonly precedes this
        # one" reasoning already applied to Drive's file metadata. Kept in
        # the preview on that basis. Participants/Dates are never sent to
        # Claude at all (computed purely for the human reviewer, never part
        # of filtered_data below) -- kept in the preview anyway as
        # identifying context (same reasoning as Salesforce's own-input
        # record id: useful to the reviewer even though it isn't "Claude
        # already knows this").
        # Messages (count) has no equivalent free source anywhere and
        # stays genuinely new (new_info).
        preview = {
            "Subject": subject,
            "Participants": participants or "(unknown)",
            "Dates": date_range,
        }
        new_info = {
            "Messages": str(len(messages)),
        }
        lines = []
        bodies = []
        filtered_messages = []
        blocks = []
        for i, m in enumerate(messages, 1):
            sender = apply_text("privacy", "metadata", getattr(m, "sender", "") or "")
            date = apply_text("privacy", "metadata", getattr(m, "date", "") or "")
            raw_body = getattr(m, "body_text", "") or html_to_text(getattr(m, "body_html", "") or "") or ""
            # This tool assembles multiple messages into one thread view --
            # "thread_history" is its own documented category (settings.yaml.
            # example), distinct from the single-message "body" category
            # gmail_get_message uses.
            body = apply_text("privacy", "thread_history", raw_body)
            attachments = apply_list("privacy", "attachments", getattr(m, "attachments", None) or [])
            lines.append(f"--- Message {i} ---")
            lines.append(f"From: {sender}")
            lines.append(f"Date: {date}")
            lines.append(body)
            bodies.append(body)
            filtered_messages.append({
                "id": getattr(m, "id", ""),
                "subject": apply_text("privacy", "metadata", getattr(m, "subject", "") or ""),
                "sender": sender,
                "date": date,
                "body_text": body,
                "attachments": attachments,
            })
            # v2's right pane: From/Date as standalone labeled fields (same
            # font as a table header, see approval_window_html.py's
            # _field_block_html), interleaved per message via
            # preview_blocks rather than one flat text blob -- details_text
            # (built from `lines` above) stays a flat string for legacy
            # display and the PII scan's default fallback.
            blocks.append({"type": "heading", "label": f"Message {i}"})
            blocks.append({"type": "field", "label": "From", "value": sender})
            blocks.append({"type": "field", "label": "Date", "value": date})
            blocks.append({"type": "text", "text": body})
        details = "\n".join(lines)
        filtered = {"id": thread.id, "subject": subject, "messages": filtered_messages}
        return await gated_call(
            connector=self.name,
            tool="gmail_get_thread",
            tool_name="Read Email Thread",
            summary=f"Read thread: {subject_raw}",
            sender=subject_raw,
            raw_data=thread,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text="\n".join(bodies),
            # No "Sender & metadata" row here either (see gmail_get_message's
            # same reasoning): Subject/Participants/Dates are already in the
            # preview, and each message's From/Date are already concrete
            # fields in the preview_blocks right pane below -- an abstract
            # policy row would just restate them.
            visibility={
                "Thread messages": category_policy("privacy", "thread_history"),
                "Attachments": category_policy("privacy", "attachments"),
            },
            preview_blocks=blocks,
            my_email=self.my_email,
            args={"thread_id": thread_id},
        )

    async def _download_attachment(
        self, message_id: str, attachment_name: str, destination_dir: str = ""
    ) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        attachment = next(
            (a for a in (message.attachments or []) if a.name == attachment_name), None
        )
        if attachment is None:
            raise RuntimeError(
                f"No attachment named {attachment_name!r} on message {message_id}"
            )
        dest_path = resolve_attachment_destination(attachment.name, destination_dir)
        name = os.path.basename(dest_path)
        # ADR 0007: shown to the human, and handed to local_files.
        # deliver_file(), exactly as the agent typed destination_dir --
        # never the daemon-expanded dest_path above, which mixes in this
        # process's own idea of "~" and is meaningless to a shim writing
        # the file as a different, real user. See connectors/drive.py's
        # _download_file's own displayed_dest.
        displayed_dest = f"{destination_dir.strip().rstrip('/')}/{name}" if destination_dir.strip() else dest_path
        cfg = self.download_config or DownloadDeliveryConfig()
        # ADR 0007: whether this download can still write straight to this
        # process's own filesystem (a dev checkout or an unseparated pip/
        # pipx install, where the daemon *is* the user) or needs the file
        # bridge instead -- see local_files.can_access_user_files's own
        # docstring.
        direct_write = local_files.can_access_user_files(self.download_mode)
        # Audit trail -- see connectors/drive.py's own `delivery`
        # comment for the reasoning; attachment.size here is exact (not an
        # export-size approximation), so this estimate and the eventual
        # actual delivery can only disagree if the prefetch itself failed.
        delivery = (
            ("local_disk" if direct_write else "client_bridge") if self.download_mode != "org"
            else "inline_base64" if cfg.fits_inline(attachment.size)
            else "staged_link"
        )
        # Every one of these is already known for free by the time this
        # gates: From/Subject via gmail_list_messages, Attachment/Type/Size
        # via gmail_list_message_attachments -- see
        # 5deef1d8:docs/claude-knowledge-boundary.md's Gmail worked example ("by the time
        # gmail_download_attachment gates, none of that metadata is new").
        # The only genuinely new fact from approving this call is *how* the
        # attachment reaches Claude -- see connectors/drive.py's
        # _download_file for the same mode/delivery-conditional reasoning.
        preview = {
            "From": message.sender or "(unknown)",
            "Subject": message.subject or "(no subject)",
            "Attachment": attachment.name,
            "Type": attachment.mime_type,
            "Size": f"{attachment.size:,} bytes",
        }
        if self.download_mode == "org":
            if cfg.fits_inline(attachment.size):
                new_info = {
                    "Content returned to {agent}": (
                        f"Yes — file bytes are included in the tool result (attachment is "
                        f"{attachment.size:,} bytes, under this org's {cfg.inline_max_bytes:,}-byte "
                        "inline-delivery limit)"
                    ),
                }
            else:
                new_info = {
                    "Content returned to {agent}": (
                        "None — a one-time link is generated for you to open in your own browser"
                    ),
                }
        else:
            new_info = {
                "Content returned to {agent}": "None — file bytes are never sent",
                "Will save to": displayed_dest,
            }
        details = (
            "The attachment above will be delivered as described above."
            if self.download_mode == "org"
            else "The attachment above will be downloaded to the destination shown."
        )

        # Gmail's attachments().get() has no partial/range fetch -- previewing
        # or PII-scanning means fully fetching the attachment before the
        # human has decided anything, unlike Drive's cheaper thumbnailLink
        # path. Only worth it for types is_prefetch_worthy() recognizes,
        # under a sane size cap; anything else keeps today's metadata-only
        # preview and unscanned content.
        preview_bytes = b""
        preview_mime_type = ""
        pii_scan_text = ""
        fetched_bytes: bytes | None = None
        if (
            is_prefetch_worthy(attachment.mime_type)
            and 0 < attachment.size <= _ATTACHMENT_PREFETCH_MAX_BYTES
        ):
            try:
                fetched_bytes = await self._fetch(
                    self._gmail.fetch_attachment_bytes, message_id, attachment.attachment_id,
                )
            except RuntimeError:
                # _fetch() already turned the underlying GmailClientError into
                # a RuntimeError and logged it -- this is a best-effort
                # preview/scan, not the actual download, so fall back to
                # today's metadata-only preview instead of failing the call.
                pass
            else:
                if attachment.mime_type.startswith("image/"):
                    preview_bytes = fetched_bytes
                    preview_mime_type = attachment.mime_type
                # Not an image -- extract_text() below feeds a rich
                # "markdown" preview_blocks entry (see this call's
                # gated_call below) instead of a visual thumbnail.
                pii_scan_text = extract_text(fetched_bytes, attachment.mime_type)

        # Gate before touching disk: gated_call raises on denial, and only a
        # decision made here should ever cause the attachment to be written.
        await gated_call(
            connector=self.name,
            tool="gmail_download_attachment",
            tool_name="Download Gmail Attachment",
            summary=f"Download attachment '{attachment.name}' from: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text=pii_scan_text,
            preview_bytes=preview_bytes,
            preview_mime_type=preview_mime_type,
            preview_blocks=preview_blocks_for(details, pii_scan_text),
            my_email=self.my_email,
            args={"message_id": message_id, "attachment_name": attachment_name},
            delivery=delivery,
        )
        if self.download_mode == "org":
            return await self._deliver_org_attachment(
                message_id, attachment, fetched_bytes, cfg,
            )
        if direct_write:
            # Unseparated install -- unchanged from before ADR 0007. Reuses
            # the PII-scan prefetch exactly as it always did.
            if fetched_bytes is not None:
                return await self._fetch(
                    self._gmail.save_attachment_bytes, fetched_bytes, attachment.name, destination_dir,
                )
            return await self._fetch(
                self._gmail.download_attachment,
                message_id, attachment.attachment_id, attachment.name, destination_dir,
            )
        # ADR 0007: privilege separation means this process cannot write
        # into the real user's destination_dir -- reuse the bytes the PII
        # scan/preview above already fetched when it had them (prefetch-
        # worthy type, under _ATTACHMENT_PREFETCH_MAX_BYTES), otherwise
        # fetch the full attachment now. local_files.deliver_file stages it
        # for the shim (or, with no bridge-capable client, a one-time link
        # -- ADR 0007 SS1.4). Mirrors connectors/drive.py's _download_file.
        data = fetched_bytes
        mime_type = attachment.mime_type or "application/octet-stream"
        if data is None:
            data = await self._fetch(
                self._gmail.fetch_attachment_bytes, message_id, attachment.attachment_id,
            )
        return local_files.deliver_file(
            destination_dir, name, data, mime_type, download_mode=self.download_mode,
        )

    async def _deliver_org_attachment(
        self, message_id: str, attachment: Any, fetched_bytes: bytes | None, cfg: DownloadDeliveryConfig,
    ) -> Any:
        """org mode's own delivery, once approval is granted -- see
        connectors/drive.py's _deliver_org_download for the shared
        inline/staged/refuse shape. ``fetched_bytes`` reuses the pre-
        approval prefetch (attachment.mime_type prefetch-worthy and under
        _ATTACHMENT_PREFETCH_MAX_BYTES) when it already happened, instead
        of fetching the same attachment from Gmail a second time -- but
        that prefetch cap (5MB) is smaller than a typical inline_max_bytes
        (default 8MB), so a fresh fetch is still sometimes needed here."""
        if not cfg.allow_disk_staging and not cfg.fits_inline(attachment.size):
            raise RuntimeError(
                f"This attachment is {attachment.size:,} bytes, over this organization's "
                f"{cfg.inline_max_bytes:,}-byte inline-delivery limit, and disk staging is disabled "
                "for this organization -- there is no way to deliver it through this tool. Ask the "
                "user for a narrower export or a different way to share it."
            )

        data = fetched_bytes if fetched_bytes is not None else await self._fetch(
            self._gmail.fetch_attachment_bytes, message_id, attachment.attachment_id,
        )
        size_bytes = len(data)

        if cfg.fits_inline(size_bytes):
            return {
                "delivery": "inline",
                "name": attachment.name,
                "mime_type": attachment.mime_type,
                "size_bytes": size_bytes,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }

        if not cfg.allow_disk_staging:
            raise RuntimeError(
                f"\"{attachment.name}\" is {size_bytes:,} bytes, over this organization's "
                f"{cfg.inline_max_bytes:,}-byte inline-delivery limit, and disk staging is disabled "
                "for this organization -- there is no way to deliver it through this tool. Ask the "
                "user for a narrower export or a different way to share it."
            )

        token = await asyncio.to_thread(
            get_download_staging_store().stage, current_principal(), data, attachment.name, attachment.mime_type,
            ttl_seconds=cfg.link_ttl_seconds,
        )
        return {
            "delivery": "link",
            "name": attachment.name,
            "size_bytes": size_bytes,
            "download_url": f"{self.download_base_url}{cfg.staged_link_path(token)}",
            "expires_at": datetime.fromtimestamp(
                time.time() + cfg.link_ttl_seconds, tz=timezone.utc,
            ).isoformat(),
        }

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _resolve_draft_sender(self, include_signature: bool | None, send_as: str) -> _DraftSender:
        """Resolve the signature/From: a draft gets, before gating -- so the
        reviewer approves the exact signature that is saved, and an unknown
        ``send_as`` is rejected without ever raising a popup. Costs no API
        call when neither is wanted."""
        signature_requested = self.append_signature if include_signature is None else bool(include_signature)
        send_as = (send_as or "").strip()
        if not signature_requested and not send_as:
            return _DraftSender()
        alias = await self._fetch(self._gmail.resolve_send_as, send_as)
        has_signature = signature_requested and signature_has_content(alias.signature_html)
        return _DraftSender(
            signature_html=alias.signature_html if has_signature else "",
            from_header=alias.from_header() if send_as else "",
            alias_email=alias.email,
            signature_requested=signature_requested,
        )

    async def _create_draft(
        self, to: str, subject: str, body: str = "", body_markdown: str = "", cc: str = "", bcc: str = "",
        include_signature: bool | None = None, send_as: str = "",
    ) -> Any:
        _require_body(body, body_markdown, "gmail_create_draft")
        sender = await self._resolve_draft_sender(include_signature, send_as)
        preview = {"To": to}
        if cc:
            preview["Cc"] = cc
        if bcc:
            preview["Bcc"] = bcc
        preview["Subject"] = subject
        await gated_call(
            connector=self.name,
            tool="gmail_create_draft",
            tool_name="Create Gmail Draft",
            summary=f"Create draft: {subject}",
            sender=to,
            raw_data={
                "to": to, "subject": subject, "body": body, "body_markdown": body_markdown,
                "cc": cc, "bcc": bcc, "include_signature": sender.signature_requested, "send_as": send_as,
            },
            filtered_data=None,
            gate="popup",
            preview=sender.preview(preview),
            details_text=sender.details_text(body, body_markdown),
            write_content_scan_text=sender.write_content_scan_text(body, body_markdown),
            my_email=self.my_email,
            args={"to": to, "subject": subject},
        )
        return await self._fetch(
            self._gmail.create_draft, to, subject, body, cc, bcc, body_markdown, **sender.client_kwargs()
        )

    async def _reply_draft(
        self, message_id: str, body: str = "", body_markdown: str = "", cc: str = "", bcc: str = "",
        include_signature: bool | None = None, send_as: str = "",
    ) -> Any:
        _require_body(body, body_markdown, "gmail_reply_draft")
        sender = await self._resolve_draft_sender(include_signature, send_as)
        message, preview, to_arg = await self._reply_preview_and_to(message_id, cc, bcc, reply_all=False)
        await gated_call(
            connector=self.name,
            tool="gmail_reply_draft",
            tool_name="Create Gmail Reply Draft",
            summary=f"Reply draft: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data={
                "message_id": message_id, "body": body, "body_markdown": body_markdown,
                "cc": cc, "bcc": bcc, "include_signature": sender.signature_requested, "send_as": send_as,
            },
            filtered_data=None,
            gate="popup",
            preview=sender.preview(preview),
            details_text=sender.details_text(body, body_markdown),
            write_content_scan_text=sender.write_content_scan_text(body, body_markdown),
            my_email=self.my_email,
            args={"message_id": message_id, "to": to_arg},
        )
        return await self._fetch(
            self._gmail.create_reply_draft, message_id, body, False, self.my_email, cc, bcc, body_markdown,
            **sender.client_kwargs(),
        )

    async def _reply_all_draft(
        self, message_id: str, body: str = "", body_markdown: str = "", cc: str = "", bcc: str = "",
        include_signature: bool | None = None, send_as: str = "",
    ) -> Any:
        _require_body(body, body_markdown, "gmail_reply_all_draft")
        sender = await self._resolve_draft_sender(include_signature, send_as)
        message, preview, to_arg = await self._reply_preview_and_to(message_id, cc, bcc, reply_all=True)
        await gated_call(
            connector=self.name,
            tool="gmail_reply_all_draft",
            tool_name="Create Gmail Reply-All Draft",
            summary=f"Reply-all draft: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data={
                "message_id": message_id, "body": body, "body_markdown": body_markdown,
                "cc": cc, "bcc": bcc, "include_signature": sender.signature_requested, "send_as": send_as,
            },
            filtered_data=None,
            gate="popup",
            preview=sender.preview(preview),
            details_text=sender.details_text(body, body_markdown),
            write_content_scan_text=sender.write_content_scan_text(body, body_markdown),
            my_email=self.my_email,
            args={"message_id": message_id, "to": to_arg},
        )
        return await self._fetch(
            self._gmail.create_reply_draft, message_id, body, True, self.my_email, cc, bcc, body_markdown,
            **sender.client_kwargs(),
        )

    async def _create_draft_with_attachments(
        self,
        to: str,
        subject: str,
        body: str = "",
        body_markdown: str = "",
        attachments: str = "",
        cc: str = "",
        bcc: str = "",
        include_signature: bool | None = None,
        send_as: str = "",
    ) -> Any:
        _require_body(body, body_markdown, "gmail_create_draft_with_attachments")
        paths = _parse_attachment_paths(attachments)
        # ADR 0007 and ADR 0028: see _require_attachment_paths' own
        # docstring for why this differs by mode and by an ``upload:``
        # reference's own path shape.
        self._require_attachment_paths(paths)
        attachment_info = self._stat_attachments(paths)
        sender = await self._resolve_draft_sender(include_signature, send_as)
        preview = {"To": to}
        if cc:
            preview["Cc"] = cc
        if bcc:
            preview["Bcc"] = bcc
        preview["Subject"] = subject
        preview["Attachments"] = self._format_attachment_preview(attachment_info)
        await gated_call(
            connector=self.name,
            tool="gmail_create_draft_with_attachments",
            tool_name="Create Gmail Draft with Attachments",
            summary=f"Create draft with {len(paths)} attachment(s): {subject}",
            sender=to,
            raw_data={
                "to": to, "subject": subject, "body": body, "body_markdown": body_markdown,
                "cc": cc, "bcc": bcc, "attachments": paths,
                "include_signature": sender.signature_requested, "send_as": send_as,
            },
            filtered_data=None,
            gate="popup",
            preview=sender.preview(preview),
            details_text=sender.details_text(body, body_markdown),
            write_content_scan_text=sender.write_content_scan_text(body, body_markdown),
            my_email=self.my_email,
            args={"to": to, "subject": subject},
        )
        return await self._fetch(
            self._gmail.create_draft_with_attachments,
            to, subject, body, paths, cc, bcc, body_markdown, self.download_mode,
            **sender.client_kwargs(),
        )

    async def _reply_draft_with_attachments(
        self,
        message_id: str,
        body: str = "",
        body_markdown: str = "",
        attachments: str = "",
        cc: str = "",
        bcc: str = "",
        include_signature: bool | None = None,
        send_as: str = "",
    ) -> Any:
        _require_body(body, body_markdown, "gmail_reply_draft_with_attachments")
        paths = _parse_attachment_paths(attachments)
        self._require_attachment_paths(paths)
        attachment_info = self._stat_attachments(paths)
        sender = await self._resolve_draft_sender(include_signature, send_as)
        message, preview, to_arg = await self._reply_preview_and_to(message_id, cc, bcc, reply_all=False)
        preview["Attachments"] = self._format_attachment_preview(attachment_info)
        await gated_call(
            connector=self.name,
            tool="gmail_reply_draft_with_attachments",
            tool_name="Create Gmail Reply Draft with Attachments",
            summary=f"Reply draft with {len(paths)} attachment(s): {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data={
                "message_id": message_id, "body": body, "body_markdown": body_markdown,
                "cc": cc, "bcc": bcc, "attachments": paths,
                "include_signature": sender.signature_requested, "send_as": send_as,
            },
            filtered_data=None,
            gate="popup",
            preview=sender.preview(preview),
            details_text=sender.details_text(body, body_markdown),
            write_content_scan_text=sender.write_content_scan_text(body, body_markdown),
            my_email=self.my_email,
            args={"message_id": message_id, "to": to_arg},
        )
        return await self._fetch(
            self._gmail.create_reply_draft_with_attachments,
            message_id, body, paths, False, self.my_email, cc, bcc, body_markdown, self.download_mode,
            **sender.client_kwargs(),
        )

    async def _reply_all_draft_with_attachments(
        self,
        message_id: str,
        body: str = "",
        body_markdown: str = "",
        attachments: str = "",
        cc: str = "",
        bcc: str = "",
        include_signature: bool | None = None,
        send_as: str = "",
    ) -> Any:
        _require_body(body, body_markdown, "gmail_reply_all_draft_with_attachments")
        paths = _parse_attachment_paths(attachments)
        self._require_attachment_paths(paths)
        attachment_info = self._stat_attachments(paths)
        sender = await self._resolve_draft_sender(include_signature, send_as)
        message, preview, to_arg = await self._reply_preview_and_to(message_id, cc, bcc, reply_all=True)
        preview["Attachments"] = self._format_attachment_preview(attachment_info)
        await gated_call(
            connector=self.name,
            tool="gmail_reply_all_draft_with_attachments",
            tool_name="Create Gmail Reply-All Draft with Attachments",
            summary=f"Reply-all draft with {len(paths)} attachment(s): {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data={
                "message_id": message_id, "body": body, "body_markdown": body_markdown,
                "cc": cc, "bcc": bcc, "attachments": paths,
                "include_signature": sender.signature_requested, "send_as": send_as,
            },
            filtered_data=None,
            gate="popup",
            preview=sender.preview(preview),
            details_text=sender.details_text(body, body_markdown),
            write_content_scan_text=sender.write_content_scan_text(body, body_markdown),
            my_email=self.my_email,
            args={"message_id": message_id, "to": to_arg},
        )
        return await self._fetch(
            self._gmail.create_reply_draft_with_attachments,
            message_id, body, paths, True, self.my_email, cc, bcc, body_markdown, self.download_mode,
            **sender.client_kwargs(),
        )

    async def _reply_preview_and_to(
        self, message_id: str, cc: str, bcc: str, reply_all: bool
    ) -> tuple[Any, dict[str, str], Any]:
        """Fetch the message being replied to and build the preview dict plus
        the gate's ``args["to"]`` -- shared by gmail_reply_draft/
        gmail_reply_all_draft and their _with_attachments counterparts, since
        only the message construction (plain text vs. multipart) differs
        between them.
        """
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {
            "In reply to": message.subject or "(no subject)",
            "To": message.sender or "(unknown)",
        }
        if not reply_all:
            if cc:
                preview["Cc"] = cc
            if bcc:
                preview["Bcc"] = bcc
            return message, preview, message.sender or ""

        recipients = (
            message.recipients if isinstance(message.recipients, str) else ", ".join(message.recipients or [])
        )
        preview["Also to"] = recipients or "(none)"
        if cc:
            preview["Cc"] = cc
        if bcc:
            preview["Bcc"] = bcc
        # The full expanded audience this reply-all will actually reach —
        # original sender + original recipients + any extra cc — minus
        # ourselves. Auto-accept rules (to_is_myself, approved_recipient_domain)
        # check ALL of these, not just the primary sender, so a rule scoped to
        # a trusted domain can't be satisfied by the sender alone while an
        # external Cc'd participant slips through.
        original_recipients = message.recipients if isinstance(message.recipients, list) else (
            [r.strip() for r in (message.recipients or "").split(",") if r.strip()]
        )
        extra_cc = [r.strip() for r in cc.split(",") if r.strip()] if cc else []
        all_recipients = [message.sender or ""] + list(original_recipients) + extra_cc
        my_email_lower = self.my_email.lower()
        expanded_to = [
            r for r in all_recipients
            if r and (not self.my_email or my_email_lower not in r.lower())
        ]
        return message, preview, expanded_to or [message.sender or ""]

    def _require_attachment_paths(self, paths: list[str]) -> None:
        """Runs local_files.require_local_files() on ``paths`` before
        gating -- see ADR 0007 for why local mode always needs this (the
        bridge handshake) and org mode's own literal filesystem paths never
        do (its daemon runs on a different machine than the user entirely).
        Capability-slot ``upload:`` references (ADR 0028) are the one path
        shape that needs this in *every* mode, org included -- a capability
        slot claim, not a filesystem read, so it's filtered out here rather
        than skipped along with the rest of org mode's paths.
        """
        if self.download_mode != "org":
            local_files.require_local_files(
                paths, max_total_bytes=_ATTACHMENT_UPLOAD_MAX_BYTES, download_mode=self.download_mode,
            )
            return
        upload_refs = [p for p in paths if p.startswith(local_files.UPLOAD_REF_PREFIX)]
        if upload_refs:
            local_files.require_local_files(
                upload_refs, max_total_bytes=_ATTACHMENT_UPLOAD_MAX_BYTES, download_mode=self.download_mode,
            )

    def _stat_attachments(self, paths: list[str]) -> list[dict[str, Any]]:
        """Stat each attachment path so the approval popup shows real
        filenames/sizes before gating -- doesn't read file content, which
        only happens in GmailClient after approval, when the draft is
        actually built.

        ADR 0007: each of this method's three call sites already calls
        local_files.require_local_files() first for local mode, which
        raises before this ever runs if a path can't be reached at all --
        so no os.path.isfile check is needed here, only
        local_files.local_file_size(). Not a ``@staticmethod``, since
        bridge-awareness needs ``self.download_mode``.

        Org mode never goes through require_local_files above for a plain
        filesystem path (its daemon runs on a different machine than the
        user entirely -- the file bridge doesn't apply there, same
        reasoning as connectors/drive.py's _upload_file is_org_local_path
        branch), so it keeps a direct stat here too. A capability-slot
        ``upload:`` reference is never a filesystem path in any mode
        though -- require_local_files() already claimed it above (see this
        method's three call sites), so it always takes the local_files
        branch here, org mode included.
        """
        info = []
        for path in paths:
            if self.download_mode == "org" and not path.startswith(local_files.UPLOAD_REF_PREFIX):
                expanded = os.path.expanduser(path.strip())
                if not os.path.isfile(expanded):
                    raise ValueError(f"attachments: no such file: {path!r}")
                info.append({"name": os.path.basename(expanded), "size_bytes": os.path.getsize(expanded)})
            elif path.startswith(local_files.UPLOAD_REF_PREFIX):
                size_bytes = local_files.local_file_size(path, download_mode=self.download_mode)
                info.append({"name": "attachment", "size_bytes": size_bytes})
            else:
                size_bytes = local_files.local_file_size(path, download_mode=self.download_mode)
                info.append({"name": os.path.basename(path.strip()), "size_bytes": size_bytes})
        return info

    @staticmethod
    def _format_attachment_preview(attachment_info: list[dict[str, Any]]) -> str:
        return ", ".join(f"{a['name']} ({a['size_bytes']:,} bytes)" for a in attachment_info)

    async def _add_label(self, message_id: str, label_name: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {"From": message.sender or "(unknown)", "Subject": message.subject or "(no subject)", "Label": label_name}
        await gated_call(
            connector=self.name,
            tool="gmail_add_label",
            tool_name="Add Gmail Label",
            summary=f"Add label '{label_name}' to: {message.subject or message_id}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Label will be added; no other content changes.",
            my_email=self.my_email,
            args={"message_id": message_id, "label_name": label_name},
        )
        return await self._fetch(self._gmail.add_label, message_id, label_name)

    async def _remove_label(self, message_id: str, label_name: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {"From": message.sender or "(unknown)", "Subject": message.subject or "(no subject)", "Label": label_name}
        await gated_call(
            connector=self.name,
            tool="gmail_remove_label",
            tool_name="Remove Gmail Label",
            summary=f"Remove label '{label_name}' from: {message.subject or message_id}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Label will be removed; no other content changes.",
            my_email=self.my_email,
            args={"message_id": message_id, "label_name": label_name},
        )
        return await self._fetch(self._gmail.remove_label, message_id, label_name)

    async def _archive_message(self, message_id: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {"From": message.sender or "(unknown)", "Subject": message.subject or "(no subject)"}
        details = (
            "Action: Archive (remove from Inbox)\n"
            "The message will remain in All Mail and is not deleted."
        )
        await gated_call(
            connector=self.name,
            tool="gmail_archive_message",
            tool_name="Archive Email",
            summary=f"Archive: {message.subject or '(no subject)'} from {message.sender or message_id}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"message_id": message_id},
        )
        return await self._fetch(self._gmail.archive_message, message_id)

    @staticmethod
    def _filter_preview(
        from_address: str, to_address: str, subject: str, query: str, has_attachment: bool,
        add_label_names: str, archive: bool, mark_as_read: bool, star: bool, forward_to: str,
    ) -> dict[str, str]:
        criteria_parts = []
        if from_address:
            criteria_parts.append(f"from: {from_address}")
        if to_address:
            criteria_parts.append(f"to: {to_address}")
        if subject:
            criteria_parts.append(f"subject: {subject}")
        if query:
            criteria_parts.append(f"has the words: {query}")
        if has_attachment:
            criteria_parts.append("has attachment")
        action_parts = []
        if add_label_names:
            action_parts.append(f"apply label(s): {add_label_names}")
        if star:
            action_parts.append("star it")
        if archive:
            action_parts.append("archive it (skip inbox)")
        if mark_as_read:
            action_parts.append("mark as read")
        if forward_to:
            action_parts.append(f"forward to: {forward_to}")
        return {
            "Criteria": "; ".join(criteria_parts) or "(none)",
            "Actions": "; ".join(action_parts) or "(none)",
        }

    async def _create_filter(
        self,
        from_address: str = "",
        to_address: str = "",
        subject: str = "",
        query: str = "",
        has_attachment: bool = False,
        add_label_names: str = "",
        archive: bool = False,
        mark_as_read: bool = False,
        star: bool = False,
        forward_to: str = "",
    ) -> Any:
        preview = self._filter_preview(
            from_address, to_address, subject, query, has_attachment,
            add_label_names, archive, mark_as_read, star, forward_to,
        )
        await gated_call(
            connector=self.name,
            tool="gmail_create_filter",
            tool_name="Create Gmail Filter",
            summary=f"Create filter — {preview['Criteria']}",
            sender="",
            raw_data=preview,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Filter will be created with the criteria and actions above.",
            my_email=self.my_email,
            args={
                "from_address": from_address, "to_address": to_address, "subject": subject,
                "query": query, "has_attachment": has_attachment, "add_label_names": add_label_names,
                "archive": archive, "mark_as_read": mark_as_read, "star": star, "forward_to": forward_to,
            },
        )
        return await self._fetch(
            self._gmail.create_filter, from_address, to_address, subject, query, has_attachment,
            add_label_names, archive, mark_as_read, star, forward_to,
        )

    async def _update_filter(
        self,
        filter_id: str,
        from_address: str = "",
        to_address: str = "",
        subject: str = "",
        query: str = "",
        has_attachment: bool = False,
        add_label_names: str = "",
        archive: bool = False,
        mark_as_read: bool = False,
        star: bool = False,
        forward_to: str = "",
    ) -> Any:
        preview = {
            "Filter ID": filter_id,
            **self._filter_preview(
                from_address, to_address, subject, query, has_attachment,
                add_label_names, archive, mark_as_read, star, forward_to,
            ),
        }
        details = (
            "Gmail has no filter-update API: this deletes the existing filter "
            f"(id: {filter_id}) and creates a new one with the settings above. "
            "The replacement filter will have a different id."
        )
        await gated_call(
            connector=self.name,
            tool="gmail_update_filter",
            tool_name="Update Gmail Filter",
            summary=f"Update filter {filter_id} — {preview['Criteria']}",
            sender="",
            raw_data=preview,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={
                "filter_id": filter_id, "from_address": from_address, "to_address": to_address,
                "subject": subject, "query": query, "has_attachment": has_attachment,
                "add_label_names": add_label_names, "archive": archive, "mark_as_read": mark_as_read,
                "star": star, "forward_to": forward_to,
            },
        )
        return await self._fetch(
            self._gmail.update_filter, filter_id, from_address, to_address, subject, query,
            has_attachment, add_label_names, archive, mark_as_read, star, forward_to,
        )

    async def _create_label(self, label_name: str) -> Any:
        stripped = label_name.strip("/")
        segments = [s.strip() for s in stripped.split("/") if s.strip()]
        normalized_name = "/".join(segments)
        # Check for a duplicate before gating, not after: create_label() only
        # discovers "label already exists" once it's already past the
        # approval popup, so a doomed duplicate call still cost the user an
        # unnecessary approval decision.
        existing = await self._fetch(self._gmail.list_labels)
        existing_names = {lbl.get("name", "").lower() for lbl in existing}
        if normalized_name.lower() in existing_names:
            raise RuntimeError(f"create_label({normalized_name!r}) failed: label already exists")
        preview = {"Label": label_name}
        details = "Label will be created; no other changes."
        if "/" in stripped:
            parent = stripped.rsplit("/", 1)[0]
            details = f"Nested label — parent '{parent}' will be created too if it doesn't already exist."
        await gated_call(
            connector=self.name,
            tool="gmail_create_label",
            tool_name="Create Gmail Label",
            summary=f"Create label: {label_name}",
            sender="",
            raw_data={"label_name": label_name},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"label_name": label_name},
        )
        return await self._fetch(self._gmail.create_label, label_name)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args, **kwargs) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except GmailClientError as exc:
            logger.error("Gmail fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
