"""Confluence connector."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .. import local_files
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..confluence_client import ConfluenceClient, ConfluenceClientError, resolve_attachment_destination
from ..connector import Connector, ToolParam, ToolSpec
from ..download_staging import get_download_staging_store
from ..gate import current_reason, gated_call
from ..html_to_text import html_to_markdown
from ..org_mode import DownloadDeliveryConfig, base64_length
from ..principal import current_principal
from ..privacy_filter import apply_list, apply_text
from ..text_extraction import extract_text, is_prefetch_worthy, preview_blocks_for

logger = logging.getLogger(__name__)

# Cap on how big an attachment we'll fetch pre-approval -- for a preview
# (images) or a PII scan (text/PDF/DOCX/PPTX). Same reasoning and value as
# gmail.py's _ATTACHMENT_PREFETCH_MAX_BYTES: confluence_download_attachment's
# gate has to fully fetch the attachment to do either at all (no
# partial-fetch API), so this bounds how much we'll pull down before the
# human has decided anything.
_ATTACHMENT_PREFETCH_MAX_BYTES = 5_000_000


class ConfluenceConnector(Connector):
    def __init__(self, client: ConfluenceClient) -> None:
        self._confluence = client
        self.my_email: str = ""
        # See connectors/drive.py's own DriveConnector.__init__ comment.
        self.download_mode: str = "local"
        self.download_config: DownloadDeliveryConfig | None = None
        self.download_base_url: str = ""

    @property
    def client(self) -> ConfluenceClient:
        return self._confluence

    @property
    def name(self) -> str:
        return "confluence"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="confluence_list_spaces",
                description=(
                    "List Confluence spaces the user has access to "
                    "(key, name, type, description). Returns a list of {key, name, "
                    "space_type, description, url}, at most max_results (default 50, "
                    "capped at 250), only the first page the API sends. Use "
                    "confluence_list_pages to list the pages in one of them. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50,
                              description="Maximum number of spaces to return. Default 50, capped at 250."),
                    ToolParam("space_type", "str", required=False, default="",
                              description="Filter to 'global' or 'personal'; "
                                           "omit/empty to return all types"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_search",
                description=(
                    "Full-text search across Confluence content. "
                    "Returns matching pages/blog posts with excerpts, as a list of "
                    "{id, title, content_type, space_key, space_name, excerpt, url}, most "
                    "recently modified first, at most max_results (default 20, capped at "
                    "100); excerpts may be redacted by the user's privacy settings. It "
                    "sends the query as the CQL text ~ \"<query>\" (no operators); use "
                    "confluence_cql_search to filter by space, type, label or date. Get "
                    "the page with confluence_get_page. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str",
                              description="Plain-text words to find in page and blog post text, "
                                          "e.g. 'quarterly roadmap'. Not CQL: it is wrapped in a "
                                          "text-contains clause, so avoid double quotes."),
                    ToolParam("max_results", "int", required=False, default=20,
                              description="Maximum number of results to return. Default 20, capped at 100."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_cql_search",
                description=(
                    "Search Confluence using CQL (Confluence Query Language). "
                    "Sends your CQL exactly as written, so it can filter by space, content "
                    "type, label, creator or date, which confluence_search cannot. Returns "
                    "a list of {id, title, content_type, space_key, space_name, excerpt, "
                    "url} in the order your CQL gives (add 'order by lastmodified desc' "
                    "for newest first), at most max_results (default 20, capped at 100); "
                    "excerpts may be redacted by the user's privacy settings. Use "
                    "confluence_search instead for plain keywords. Auto-approved."
                ),
                params=[
                    ToolParam("cql", "str",
                              description="A CQL query, e.g. \"space = MYSPACE AND type = page AND "
                                          "title ~ 'roadmap' order by lastmodified desc\"."),
                    ToolParam("max_results", "int", required=False, default=20,
                              description="Maximum number of results to return. Default 20, capped at 100."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_list_pages",
                description=(
                    "List pages in a Confluence space (title, id, version). "
                    "Returns a list of {id, title, space_key, space_name, version, author, "
                    "created, updated, url} without page bodies, at most max_results "
                    "(default 20, capped at 200), only the first page the API sends, in the "
                    "API's order. author is an opaque Atlassian account id, not a name. Use "
                    "confluence_search instead to find pages by words, and "
                    "confluence_get_page to read one. Auto-approved."
                ),
                params=[
                    ToolParam("space_key", "str",
                              description="Key of the space, e.g. 'ENG' (not its numeric id or display "
                                          "name), from confluence_list_spaces (its key field)."),
                    ToolParam("max_results", "int", required=False, default=20,
                              description="Maximum number of pages to return. Default 20, capped at 200."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_list_attachments",
                description=(
                    "List attachment names, media types, and sizes for a "
                    "Confluence page. Auto-approved -- metadata only, no "
                    "attachment content is returned. Returns {page_id, attachments: a list "
                    "of {name, media_type, size in bytes}}, at most 50 attachments; names may "
                    "be redacted by the user's privacy settings. Use "
                    "confluence_download_attachment to fetch the actual file."
                ),
                params=[
                    ToolParam("page_id", "str",
                              description="Numeric id of the page, from confluence_list_pages, "
                                          "confluence_search or confluence_get_page (the id field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_download_attachment",
                description=(
                    "Download a Confluence page attachment's content. "
                    "Identify the attachment by the name returned from "
                    "confluence_list_attachments. On a local install: saved "
                    "to destination_dir, which is required (no default): pass "
                    "~/Downloads (or a path the user asked for) for a "
                    "deliverable, or your own scratch directory to just read or "
                    "process it. On an organization-managed install: "
                    "destination_dir is ignored -- a small attachment's bytes come "
                    "back directly in the result, a larger one as a one-time link "
                    "the human opens in their own browser. Returns {path, name, "
                    "size_bytes} on a local install, or the inline or link delivery "
                    "result on an organization-managed one. Requires user approval."
                ),
                params=[
                    ToolParam("page_id", "str",
                              description="Numeric id of the page the attachment is on, from "
                                          "confluence_list_pages or confluence_search."),
                    ToolParam("attachment_name", "str",
                              description="Exact file name of the attachment, as confluence_list_attachments "
                                          "returns it in its name field."),
                    ToolParam(
                        "destination_dir",
                        "str",
                        required=True,
                        description=(
                            "Local install only: where to save the attachment -- required, no default. "
                            "On an organization-managed install it is ignored and nothing is "
                            "saved to it; any value will do. "
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
                name="confluence_get_page",
                description=(
                    "Fetch the full content of a Confluence page by page ID. "
                    "Returns the page body as HTML storage format, inside one page object "
                    "{id, title, space_key, space_name, version, author, created, updated, "
                    "body, url}; author is an opaque Atlassian account id. Use "
                    "confluence_get_page_by_title when you know the space and title but not "
                    "the id. Requires user approval."
                ),
                params=[
                    ToolParam("page_id", "str",
                              description="Numeric id of the page, from confluence_list_pages, "
                                          "confluence_search or confluence_cql_search (the id field)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_get_page_by_title",
                description=(
                    "Fetch a Confluence page by space key and exact title. "
                    "Returns one page in the same shape confluence_get_page returns, body "
                    "included; errors when no page has that title in the space. Use "
                    "confluence_get_page instead when you already have the page id. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("space_key", "str",
                              description="Key of the space the page is in, e.g. 'ENG', from "
                                          "confluence_list_spaces (its key field)."),
                    ToolParam("title", "str",
                              description="Exact title of the page, as shown in Confluence (not a "
                                          "partial match)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_create_page",
                description=(
                    "Create a new Confluence page in the given space. "
                    "Body is HTML storage format. Returns the created page, with its new id, "
                    "in the same shape confluence_get_page returns. Use "
                    "confluence_update_page instead to change a page that already exists. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("space_key", "str",
                              description="Key of the space to create the page in, e.g. 'ENG', from "
                                          "confluence_list_spaces (its key field)."),
                    ToolParam("title", "str", description="Title of the new page, as shown in Confluence."),
                    ToolParam("body", "str",
                              description="Page body in Confluence storage format (XHTML-based HTML), "
                                          "e.g. '<p>Hello</p>'."),
                    ToolParam("parent_id", "str", required=False, default="",
                              description="Id of the page to nest the new page under, from "
                                          "confluence_list_pages. Empty creates it at the top level of the space."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="confluence_update_page",
                description=(
                    "Update the title and/or body of an existing Confluence page. "
                    "Body is HTML storage format. Both title and body replace the current "
                    "ones, so pass the current title to keep it. Returns the updated page, "
                    "with its new version number, in the same shape confluence_get_page "
                    "returns. Call confluence_get_page first to read the current content. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("page_id", "str",
                              description="Numeric id of the page to update, from confluence_list_pages "
                                          "or confluence_search (the id field)."),
                    ToolParam("title", "str",
                              description="Title for the page after the update. Required and not "
                                          "empty: pass the current title to keep it."),
                    ToolParam("body", "str",
                              description="New body in Confluence storage format (XHTML-based HTML), "
                                          "replacing the whole current body."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "confluence_list_spaces":
            return await self._list_spaces(**args)
        if tool == "confluence_search":
            return await self._search(**args)
        if tool == "confluence_cql_search":
            return await self._cql_search(**args)
        if tool == "confluence_list_pages":
            return await self._list_pages(**args)
        if tool == "confluence_list_attachments":
            return await self._list_attachments(**args)
        if tool == "confluence_download_attachment":
            return await self._download_attachment(**args)
        if tool == "confluence_get_page":
            return await self._get_page(**args)
        if tool == "confluence_get_page_by_title":
            return await self._get_page_by_title(**args)
        if tool == "confluence_create_page":
            return await self._create_page(**args)
        if tool == "confluence_update_page":
            return await self._update_page(**args)
        raise ValueError(f"Unknown Confluence tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Always-allowed
    # ------------------------------------------------------------------ #

    async def _list_spaces(self, max_results: int = 50, space_type: str = "") -> Any:
        t0 = time.time()
        spaces = await self._fetch(self._confluence.list_spaces, max_results, space_type)
        data = [asdict(s) for s in spaces]
        self._auto_audit(
            "confluence_list_spaces", "List Confluence Spaces",
            f"List spaces (max {max_results}, type={space_type})",
            f"{len(spaces)} space(s)", t0,
        )
        return data

    async def _search(self, query: str, max_results: int = 20) -> Any:
        t0 = time.time()
        results = await self._fetch(self._confluence.search, query, max_results)
        data = [_redact_excerpt(asdict(r)) for r in results]
        self._auto_audit(
            "confluence_search", "Search Confluence",
            f"Search: {query[:80]}", f"{len(results)} result(s)", t0,
        )
        return data

    async def _cql_search(self, cql: str, max_results: int = 20) -> Any:
        t0 = time.time()
        results = await self._fetch(self._confluence.cql_search, cql, max_results)
        data = [_redact_excerpt(asdict(r)) for r in results]
        self._auto_audit(
            "confluence_cql_search", "CQL Search Confluence",
            f"CQL: {cql[:80]}", f"{len(results)} result(s)", t0,
        )
        return data

    async def _list_pages(self, space_key: str, max_results: int = 20) -> Any:
        t0 = time.time()
        pages = await self._fetch(self._confluence.list_pages_in_space, space_key, max_results)
        data = [asdict(p) for p in pages]
        self._auto_audit(
            "confluence_list_pages", "List Confluence Pages",
            f"List pages in {space_key} (max {max_results})",
            f"{len(pages)} page(s)", t0,
        )
        return data

    async def _list_attachments(self, page_id: str) -> Any:
        t0 = time.time()
        attachments = await self._fetch(self._confluence.list_attachments, page_id)
        data = apply_list(
            "confluence_privacy", "attachments",
            [{"name": a.name, "media_type": a.media_type, "size": a.size} for a in attachments],
        )
        self._auto_audit(
            "confluence_list_attachments", "List Confluence Attachments",
            f"List attachments: page {page_id}", page_id, t0,
        )
        return {"page_id": page_id, "attachments": data}

    # ------------------------------------------------------------------ #
    # Gated
    # ------------------------------------------------------------------ #

    async def _get_page(self, page_id: str) -> Any:
        page = await self._fetch(self._confluence.get_page, page_id)
        data = asdict(page)
        # Title/Space are known for free via either confluence_list_pages OR
        # confluence_search/confluence_cql_search. Author/Last modified are
        # only ever known via confluence_list_pages -- confluence_search's
        # own ConfluenceSearchResult has no author/updated field at all (see
        # 5deef1d8:docs/claude-knowledge-boundary.md) -- so, unlike Drive's "known if that
        # other call happened first" caveat, there's a real, likely path
        # (search) that never surfaces them, and they're treated as new
        # rather than assumed known. Page body has no fixed size, so it gets
        # one fixed summary row rather than a literal (possibly huge) value
        # -- the real content lives in the right-pane preview/details_text.
        preview_fields = {
            "Title": page.title or page_id,
            "Space": page.space_key or "(unknown)",
        }
        new_info = {
            "Author": page.author or "(unknown)",
            "Last modified": page.updated or "(unknown)",
            "Page body": "Full page content",
        }
        body_raw = getattr(page, "body", "") or getattr(page, "body_text", "") or ""
        body_text = html_to_markdown(body_raw)
        return await gated_call(
            connector=self.name,
            tool="confluence_get_page",
            tool_name="Read Confluence Page",
            summary=f"Read \"{page.title}\" ({page.space_key})",
            sender=page.author or page_id,
            raw_data=data,
            filtered_data=data,
            gate="review",
            preview=preview_fields,
            new_info=new_info,
            details_text=body_text,
            pii_scan_text=body_text,
            preview_blocks=[{"type": "markdown", "text": body_text}] if body_text else None,
            my_email=self.my_email,
            args={"page_id": page_id},
        )

    async def _get_page_by_title(self, space_key: str, title: str) -> Any:
        page = await self._fetch(self._confluence.get_page_by_title, space_key, title)
        data = asdict(page)
        # Same knowledge boundary as _get_page above.
        preview_fields = {
            "Title": page.title or title,
            "Space": page.space_key or space_key,
        }
        new_info = {
            "Author": page.author or "(unknown)",
            "Last modified": page.updated or "(unknown)",
            "Page body": "Full page content",
        }
        body_raw = getattr(page, "body", "") or getattr(page, "body_text", "") or ""
        body_text = html_to_markdown(body_raw)
        return await gated_call(
            connector=self.name,
            tool="confluence_get_page_by_title",
            tool_name="Read Confluence Page",
            summary=f"Read \"{page.title}\" ({page.space_key})",
            sender=page.author or space_key,
            raw_data=data,
            filtered_data=data,
            gate="review",
            preview=preview_fields,
            new_info=new_info,
            details_text=body_text,
            pii_scan_text=body_text,
            preview_blocks=[{"type": "markdown", "text": body_text}] if body_text else None,
            my_email=self.my_email,
            args={"space_key": space_key, "title": title},
        )

    async def _download_attachment(
        self, page_id: str, attachment_name: str, destination_dir: str = ""
    ) -> Any:
        page = await self._fetch(self._confluence.get_page, page_id)
        attachments = await self._fetch(self._confluence.list_attachments, page_id)
        attachment = next((a for a in attachments if a.name == attachment_name), None)
        if attachment is None:
            raise RuntimeError(f"No attachment named {attachment_name!r} on page {page_id}")
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
        # comment for the reasoning.
        delivery = (
            ("local_disk" if direct_write else "client_bridge") if self.download_mode != "org"
            else "inline_base64" if cfg.fits_inline(attachment.size, attachment.name, attachment.media_type)
            else "staged_link"
        )
        # Title/Space are known for free via confluence_list_pages/
        # confluence_search; Attachment/Type/Size are known for free via
        # confluence_list_attachments -- same knowledge-boundary reasoning
        # as gmail_download_attachment (see 5deef1d8:docs/claude-knowledge-boundary.md's
        # Gmail worked example). The only genuinely new fact from approving
        # this call is *how* the attachment reaches Claude -- see
        # connectors/drive.py's _download_file for the same mode/delivery-
        # conditional reasoning.
        preview = {
            "Title": page.title or page_id,
            "Space": page.space_key or "(unknown)",
            "Attachment": attachment.name,
            "Type": attachment.media_type,
            "Size": f"{attachment.size:,} bytes",
        }
        if self.download_mode == "org":
            if cfg.fits_inline(attachment.size, attachment.name, attachment.media_type):
                new_info = {
                    "Content returned to {agent}": (
                        f"Yes — file bytes are included in the tool result (attachment is "
                        f"{attachment.size:,} bytes, {base64_length(attachment.size):,} once "
                        f"base64-encoded, within this org's {cfg.inline_max_bytes:,}-byte "
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

        # Confluence's attachment download link has no partial/range fetch --
        # previewing or PII-scanning means fully fetching the attachment
        # before the human has decided anything, same tradeoff
        # gmail_download_attachment makes. Only worth it for types
        # is_prefetch_worthy() recognizes, under a sane size cap; anything
        # else keeps a metadata-only preview and unscanned content.
        preview_bytes = b""
        preview_mime_type = ""
        pii_scan_text = ""
        fetched_bytes: bytes | None = None
        if (
            is_prefetch_worthy(attachment.media_type)
            and 0 < attachment.size <= _ATTACHMENT_PREFETCH_MAX_BYTES
        ):
            try:
                fetched_bytes = await self._fetch(
                    self._confluence.fetch_attachment_bytes, page_id, attachment.attachment_id,
                )
            except RuntimeError:
                # _fetch() already turned the underlying ConfluenceClientError
                # into a RuntimeError and logged it -- this is a best-effort
                # preview/scan, not the actual download, so fall back to
                # today's metadata-only preview instead of failing the call.
                pass
            else:
                if attachment.media_type.startswith("image/"):
                    preview_bytes = fetched_bytes
                    preview_mime_type = attachment.media_type
                # Not an image -- extract_text() below feeds a rich
                # "markdown" preview_blocks entry (see this call's
                # gated_call below) instead of a visual thumbnail.
                pii_scan_text = extract_text(fetched_bytes, attachment.media_type)

        # Gate before touching disk: gated_call raises on denial, and only a
        # decision made here should ever cause the attachment to be written.
        await gated_call(
            connector=self.name,
            tool="confluence_download_attachment",
            tool_name="Download Confluence Attachment",
            summary=f"Download attachment '{attachment.name}' from: {page.title or page_id}",
            sender=page.author or page_id,
            raw_data=asdict(page),
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
            args={"page_id": page_id, "attachment_name": attachment_name},
            delivery=delivery,
        )
        if self.download_mode == "org":
            return await self._deliver_org_attachment(page_id, attachment, fetched_bytes, cfg)
        if direct_write:
            # Unseparated install -- unchanged from before ADR 0007. Reuses
            # the PII-scan prefetch exactly as it always did.
            if fetched_bytes is not None:
                return await self._fetch(
                    self._confluence.save_attachment_bytes, fetched_bytes, attachment.name, destination_dir,
                )
            return await self._fetch(
                self._confluence.download_attachment,
                page_id, attachment.attachment_id, attachment.name, destination_dir,
            )
        # ADR 0007: privilege separation means this process cannot write
        # into the real user's destination_dir -- reuse the bytes the PII
        # scan/preview above already fetched when it had them (prefetch-
        # worthy type, under _ATTACHMENT_PREFETCH_MAX_BYTES), otherwise
        # fetch the full attachment now. local_files.deliver_file stages it
        # for the shim (or, with no bridge-capable client, a one-time link
        # -- ADR 0007 SS1.4). Mirrors connectors/gmail.py's _download_attachment.
        data = fetched_bytes
        mime_type = attachment.media_type or "application/octet-stream"
        if data is None:
            data = await self._fetch(
                self._confluence.fetch_attachment_bytes, page_id, attachment.attachment_id,
            )
        return local_files.deliver_file(
            destination_dir, name, data, mime_type, download_mode=self.download_mode,
        )

    async def _deliver_org_attachment(
        self, page_id: str, attachment: Any, fetched_bytes: bytes | None, cfg: DownloadDeliveryConfig,
    ) -> Any:
        """org mode's own delivery, once approval is granted -- see
        connectors/drive.py's _deliver_org_download for the shared
        inline/staged/refuse shape. ``fetched_bytes`` reuses the pre-
        approval prefetch when it already happened, instead of fetching
        the same attachment from Confluence a second time -- but that
        prefetch cap (5MB) can be larger than inline_max_bytes, so a
        fresh fetch is sometimes needed here, and a prefetched attachment
        can still go out as a link."""
        if not cfg.allow_disk_staging and not cfg.fits_inline(
            attachment.size, attachment.name, attachment.media_type,
        ):
            raise RuntimeError(cfg.over_inline_limit_message("This attachment", attachment.size))

        data = fetched_bytes if fetched_bytes is not None else await self._fetch(
            self._confluence.fetch_attachment_bytes, page_id, attachment.attachment_id,
        )
        size_bytes = len(data)

        inline = cfg.inline_result(attachment.name, attachment.media_type, data)
        if inline is not None:
            return inline

        if not cfg.allow_disk_staging:
            raise RuntimeError(cfg.over_inline_limit_message(f"\"{attachment.name}\"", size_bytes))

        token = await asyncio.to_thread(
            get_download_staging_store().stage, current_principal(), data, attachment.name, attachment.media_type,
            ttl_seconds=cfg.link_ttl_seconds,
        )
        return cfg.link_result(attachment.name, size_bytes, f"{self.download_base_url}{cfg.staged_link_path(token)}")

    async def _create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: str = "",
    ) -> Any:
        preview = {"Space": space_key, "Title": title}
        if parent_id:
            preview["Parent page ID"] = parent_id
        raw = {"space_key": space_key, "title": title, "parent_id": parent_id, "body": body}
        await gated_call(
            connector=self.name,
            tool="confluence_create_page",
            tool_name="Create Confluence Page",
            summary=f"Create \"{title}\" in {space_key}",
            sender=f"space={space_key}",
            raw_data=raw,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=body,
            my_email=self.my_email,
            args={"space_key": space_key, "title": title, "parent_id": parent_id},
        )
        page = await self._fetch(self._confluence.create_page, space_key, title, body, parent_id)
        return asdict(page)

    async def _update_page(self, page_id: str, title: str, body: str) -> Any:
        current = await self._fetch(self._confluence.get_page, page_id)
        preview = {
            "Page ID": page_id,
            "Space": current.space_key or "(unknown)",
            "Title": f"{current.title} → {title}" if title != current.title else title,
        }
        await gated_call(
            connector=self.name,
            tool="confluence_update_page",
            tool_name="Update Confluence Page",
            summary=f"Update \"{title}\"",
            sender=f"page={page_id}",
            raw_data={"page_id": page_id, "space_key": current.space_key, "title": title, "body": body},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=body,
            my_email=self.my_email,
            args={"page_id": page_id, "space_key": current.space_key, "title": title},
        )
        page = await self._fetch(self._confluence.update_page, page_id, title, body)
        return asdict(page)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except ConfluenceClientError as exc:
            logger.error("Confluence fetch failed: %s", exc)
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


def _redact_excerpt(result_dict: dict[str, Any]) -> dict[str, Any]:
    """Apply confluence_privacy's "search_excerpt" category to one
    confluence_search/confluence_cql_search result -- excerpt is a genuine
    content excerpt built to show *why* a page matched (straight from
    Confluence's search API), not structural metadata like title/space/id,
    which have no category of their own."""
    result_dict["excerpt"] = apply_text(
        "confluence_privacy", "search_excerpt", result_dict.get("excerpt", "") or ""
    )
    return result_dict
