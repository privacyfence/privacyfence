"""Peek connector: the HTML pages an account shares through a Peek server.

The connector holds a ``PeekClient`` bound to one server (ADR 0152). Nothing
here logs, audits or previews a Peek token, page content or a
comment body.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from .. import html_to_text, local_files
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..peek_client import (
    PASSWORDS_REMOVED,
    UPLOAD_MAX_BYTES,
    PeekClient,
    PeekClientError,
    normalize_visibility,
    validate_slug,
)
from ..preview_dates import format_preview_datetime

logger = logging.getLogger(__name__)

_SLUG = ToolParam(
    "slug", "str", required=True,
    description=(
        "The page's slug: the slug field from peek_list_pages, or the last part of its "
        "link (…/p/<slug>)."
    ),
)

_VISIBILITY_DESC = (
    "Who can open the page: public (anyone with the link), signed-in (anyone signed in to Peek) "
    "or restricted (only you, Peek admins and the people or groups granted access)."
)

_REASON = ToolParam(
    "reason", "str", required=True,
    description="One sentence: why are you calling this tool right now?",
)


class PeekConnector(Connector):
    def __init__(self, client: PeekClient, download_mode: str = "local") -> None:
        # Only stores its arguments: the tool catalog builds ``cls(None)``.
        self._client = client
        self.download_mode = download_mode

    @property
    def client(self) -> PeekClient:
        return self._client

    @property
    def name(self) -> str:
        return "peek"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="peek_list_pages",
                description=(
                    "List the Peek pages this account can manage, newest first, with each page's "
                    "link and who can open it. An admin account sees every page. Returns a list of "
                    "{slug, name, owner, size_bytes, visibility, url, created_at, version}, where "
                    "version is the page's latest version. Use peek_get_comments to read the "
                    "comments on a page and peek_get_stats to see how often it was opened. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam(
                        "max_results", "int", required=False, default=50,
                        description="Most pages to return, newest first. Default 50, 1 to 200.",
                    ),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="peek_get_comments",
                description=(
                    "Read the reviewers' comments on one Peek page, with what each comment is "
                    "anchored to. Comments belong to one version of the page: the latest unless "
                    "you give version. Returns {slug, name, version, comments: [{id, author, "
                    "verified, body, anchor_kind, selector, element_text, created_at, "
                    "version}]}, where version is the one read and verified is true when a "
                    "signed-in Peek account wrote the comment and false when a viewer typed the "
                    "author name. Use peek_get_stats to see how often the page was opened and "
                    "peek_add_comment to reply. Requires user approval."
                ),
                params=[
                    _SLUG,
                    ToolParam(
                        "version", "int", required=False, default=0,
                        description=(
                            "Version number of the page to read comments of, from "
                            "peek_list_pages' version field (the latest) or an earlier one. "
                            "0, the default, means the latest."
                        ),
                    ),
                    _REASON,
                ],
                read_only=True,
            ),
            ToolSpec(
                name="peek_get_stats",
                description=(
                    "Read how often one Peek page was opened, and the names visitors gave. "
                    "IP addresses and browsers are never returned. Returns {slug, name, "
                    "total_visits, unique_visitors, recent: [{visitor_name, visited_at}]}. "
                    "Use peek_get_comments to read the comments. Requires user approval."
                ),
                params=[_SLUG, _REASON],
                read_only=True,
            ),
            ToolSpec(
                name="peek_upload_page",
                description=(
                    "Publish an HTML page on Peek, or a new version of one of your pages, and "
                    "get its share link. Give the page as html, local_path or upload_id. Give "
                    "slug to replace what an existing page's link shows with this new version; "
                    "its visibility stays as it is. A new page is restricted unless you choose "
                    "otherwise: only you, Peek admins and the people granted access can open it. "
                    "Returns {slug, url, visibility, version, version_url}. Use "
                    "peek_set_visibility to change who can open a page later. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam(
                        "html", "str", required=False, default="",
                        description=(
                            "The page's full HTML source. Give exactly one of html, local_path "
                            "and upload_id; empty otherwise."
                        ),
                    ),
                    ToolParam(
                        "local_path", "str", required=False, default="",
                        description=(
                            "Path of an HTML file on the user's computer, absolute or starting "
                            "with ~/. Not available on an organization-managed install. Give "
                            "exactly one of html, local_path and upload_id; empty otherwise."
                        ),
                    ),
                    ToolParam(
                        "upload_id", "str", required=False, default="",
                        description=(
                            "Id returned by privacyfence_create_upload_slot after you PUT the "
                            "file's bytes to its upload_url. Give exactly one of html, "
                            "local_path and upload_id; empty otherwise."
                        ),
                    ),
                    ToolParam(
                        "name", "str", required=False, default="",
                        description=(
                            "File name Peek shows for the page and builds its link from, such "
                            "as report.html. Empty uses the local file's name, or page.html."
                        ),
                    ),
                    ToolParam(
                        "slug", "str", required=False, default="",
                        description=(
                            "Slug of one of your pages, from peek_list_pages, to publish this "
                            "as its next version under the same link. Empty publishes a new page."
                        ),
                    ),
                    ToolParam(
                        "visibility", "str", required=False, default="",
                        description=(
                            _VISIBILITY_DESC + " Empty means restricted for a new page; leave it "
                            "empty with slug."
                        ),
                    ),
                    _REASON,
                ],
                read_only=False,
            ),
            ToolSpec(
                name="peek_set_visibility",
                description=(
                    "Change who can open a Peek page: anyone with the link, anyone signed in to "
                    "Peek, or only the people granted access (restricted). Returns {slug, "
                    "visibility, notified}, where notified says whether Peek emailed the people "
                    "granted access. Use peek_upload_page to publish a new version. Requires "
                    "user approval."
                ),
                params=[
                    _SLUG,
                    ToolParam("visibility", "str", required=True, description=_VISIBILITY_DESC),
                    _REASON,
                ],
                read_only=False,
            ),
            ToolSpec(
                name="peek_add_comment",
                description=(
                    "Post a comment on a Peek page under your own Peek account name, on the "
                    "latest version or the one you give. Copy selector and element_text from a "
                    "comment returned by peek_get_comments to reply at the same spot. Returns "
                    "{slug, version, comment: {id, author, verified, body, anchor_kind, selector, "
                    "element_text, created_at}}. Use peek_get_comments to read the existing "
                    "comments. Requires user approval."
                ),
                params=[
                    _SLUG,
                    ToolParam(
                        "body", "str", required=True,
                        description="The comment text, 1 to 4000 bytes (UTF-8).",
                    ),
                    ToolParam(
                        "selector", "str", required=False, default="",
                        description=(
                            "CSS selector of the element to pin the comment to, copied from a "
                            "comment's selector in peek_get_comments. Empty comments on the "
                            "whole page."
                        ),
                    ),
                    ToolParam(
                        "element_text", "str", required=False, default="",
                        description=(
                            "Quoted text to anchor the comment to, up to 200 bytes (UTF-8), copied "
                            "from a comment's element_text. Needs selector. Empty anchors to the "
                            "element, or the page."
                        ),
                    ),
                    ToolParam(
                        "version", "int", required=False, default=0,
                        description=(
                            "Version number to comment on, from peek_list_versions; 0, the "
                            "default, means the latest."
                        ),
                    ),
                    _REASON,
                ],
                read_only=False,
            ),
            ToolSpec(
                name="peek_delete_page",
                description=(
                    "Delete a Peek page, with its comments and visit history. Returns "
                    "{deleted: slug}. Use peek_list_pages to find the slug. Requires user approval."
                ),
                params=[_SLUG, _REASON],
                read_only=False,
                destructive=True,
            ),
            ToolSpec(
                name="peek_rename_page",
                description=(
                    "Rename a Peek page; its link may change to match the new name, and old "
                    "links keep redirecting to it in browsers. Use the slug it returns from then "
                    "on; the old slug no longer works in the other Peek tools. Get the current "
                    "slug from peek_list_pages. Returns {old_slug, slug, url, name}. "
                    "Requires user approval."
                ),
                params=[
                    _SLUG,
                    ToolParam(
                        "name", "str", required=True,
                        description=(
                            "The page's new name, 1 to 200 characters, such as Q3 report.html. "
                            "Peek derives the page's link from it."
                        ),
                    ),
                    _REASON,
                ],
                read_only=False,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "peek_list_pages":
            return await self._list_pages(**args)
        if tool == "peek_get_comments":
            return await self._get_comments(**args)
        if tool == "peek_get_stats":
            return await self._get_stats(**args)
        if tool == "peek_upload_page":
            return await self._upload_page(**args)
        if tool == "peek_set_visibility":
            return await self._set_visibility(**args)
        if tool == "peek_add_comment":
            return await self._add_comment(**args)
        if tool == "peek_delete_page":
            return await self._delete_page(**args)
        if tool == "peek_rename_page":
            return await self._rename_page(**args)
        raise ValueError(f"Unknown Peek tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_pages(self, max_results: int = 50) -> Any:
        if not 1 <= max_results <= 200:
            raise ValueError("max_results must be between 1 and 200.")
        t0 = time.time()
        pages = await self._fetch(self._client.list_pages)
        result = [asdict(page) for page in pages[:max_results]]
        self._auto_audit(
            "peek_list_pages", "List Peek Pages",
            f"List pages (max {max_results})", f"{len(result)} page(s)", t0,
        )
        return result

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_comments(self, slug: str, version: int = 0) -> Any:
        _check_slug(slug)
        if version < 0:
            raise ValueError("version must be 0 (the latest) or a version number.")
        page = await self._fetch(self._client.get_page, slug)
        comments = await self._fetch(self._client.get_comments, slug, version)
        shown = version or page.version
        n = len(comments)
        result = {
            "slug": slug,
            "name": page.name,
            "version": shown,
            "comments": [asdict(c) for c in comments],
        }
        lines = [
            f"[{c.created_at}] {c.author} "
            f"({_anchor_text(c.anchor_kind, c.selector, c.element_text)}): {c.body}"
            for c in comments
        ]
        table = {
            "headers": ["Author", "Signed in", "Date", "Anchored to", "Comment"],
            "rows": [
                [
                    c.author,
                    "yes" if c.verified else "no",
                    format_preview_datetime(c.created_at),
                    _anchor_text(c.anchor_kind, c.selector, c.element_text),
                    c.body,
                ]
                for c in comments
            ],
        }
        return await gated_call(
            connector=self.name,
            tool="peek_get_comments",
            tool_name="Read Peek Comments",
            summary=f"{n} comment(s) on {page.name}, version {shown}",
            sender=page.name,
            raw_data=comments,
            filtered_data=result,
            gate="review",
            preview={
                "Server": self._client.host,
                "Page": page.name,
                "Link": page.url,
                "Version": f"{shown} of {page.version}",
            },
            new_info={
                "Comments": str(n),
                "Comment text": "Author, text and anchor of every comment",
            },
            details_text="\n".join(lines) if lines else "(no comments)",
            pii_scan_text="\n".join(
                part for c in comments for part in (c.body, c.element_text) if part
            ),
            preview_tables=[table] if comments else [],
            table_only=True,
            args={"slug": slug, "version": version},
        )

    async def _get_stats(self, slug: str) -> Any:
        _check_slug(slug)
        page = await self._fetch(self._client.get_page, slug)
        stats = await self._fetch(self._client.get_stats, slug)
        result = {
            "slug": slug,
            "name": page.name,
            "total_visits": stats.total_visits,
            "unique_visitors": stats.unique_visitors,
            "recent": [
                {"visitor_name": v.visitor_name, "visited_at": v.visited_at}
                for v in stats.recent
            ],
        }
        lines = [
            f"{v.visited_at} {v.visitor_name or '(no name given)'}" for v in stats.recent
        ]
        table = {
            "headers": ["Visitor", "Time"],
            "rows": [
                [v.visitor_name or "(no name given)", format_preview_datetime(v.visited_at)]
                for v in stats.recent
            ],
        }
        return await gated_call(
            connector=self.name,
            tool="peek_get_stats",
            tool_name="Read Peek Visits",
            summary=f"{stats.total_visits} visit(s) to {page.name}",
            sender=page.name,
            raw_data=stats,
            filtered_data=result,
            gate="review",
            preview={"Server": self._client.host, "Page": page.name, "Link": page.url},
            new_info={
                "Visits": str(stats.total_visits),
                "Unique visitors": str(stats.unique_visitors),
                "Recent visitors": (
                    "Name each visitor gave, and when (no IP address or browser)"
                ),
            },
            details_text="\n".join(lines) if lines else "(no visits)",
            pii_scan_text="\n".join(v.visitor_name for v in stats.recent if v.visitor_name),
            preview_tables=[table] if stats.recent else [],
            table_only=True,
            args={"slug": slug},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _upload_page(
        self,
        html: str = "",
        local_path: str = "",
        upload_id: str = "",
        name: str = "",
        slug: str = "",
        visibility: str = "",
        password: str = "",
    ) -> Any:
        _refuse_password(password)
        sources = (bool(html.strip()), bool(local_path.strip()), bool(upload_id.strip()))
        if sum(sources) != 1:
            raise ValueError("peek_upload_page: give exactly one of html, local_path and upload_id.")
        if local_path.strip() and self.download_mode == "org":
            raise ValueError(
                "local_path is not available on an organization-managed install. "
                "Pass html or upload_id instead."
            )
        if slug:
            _check_slug(slug)
            if visibility:
                raise ValueError(
                    "A new version keeps the page's visibility. Leave visibility empty, "
                    "or change it with peek_set_visibility."
                )
        else:
            visibility = _check_visibility(visibility or "restricted")

        if html.strip():
            data = html.encode("utf-8")
            if len(data) > UPLOAD_MAX_BYTES:
                raise ValueError("peek_upload_page: the page is larger than 10 MB.")
        else:
            source = (
                local_path
                if local_path.strip()
                else f"{local_files.UPLOAD_REF_PREFIX}{upload_id.strip()}"
            )
            local_files.require_local_files(
                [source], max_total_bytes=UPLOAD_MAX_BYTES, download_mode=self.download_mode,
            )
            data = local_files.read_local_file(source, download_mode=self.download_mode)
            if len(data) > UPLOAD_MAX_BYTES:
                raise ValueError("peek_upload_page: the page is larger than 10 MB.")
        if not data:
            raise ValueError("peek_upload_page: the page is empty.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "peek_upload_page: the page is not UTF-8 text. Peek publishes HTML pages only."
            ) from exc

        name = (
            os.path.basename(name.strip())
            or (os.path.basename(local_path.strip()) if local_path.strip() else "")
            or "page.html"
        )
        if not 1 <= len(name) <= 200:
            raise ValueError("name must be 1 to 200 characters.")
        size = f"{len(data):,} bytes"
        if slug:
            page = await self._fetch(self._client.get_page, slug)
            summary = f"New version of {page.name}"
            sender = page.name
            preview = {
                "Server": self._client.host,
                "Page": page.name,
                "Link": page.url,
                "New version": str(page.version + 1),
                "Visibility": f"{page.visibility} (unchanged)",
                "Size": size,
            }
        else:
            summary = f"Publish {name} ({visibility})"
            sender = name
            preview = {
                "Server": self._client.host,
                "Name": name,
                "Visibility": visibility,
                "Size": size,
            }
        details = (
            (html_to_text.html_to_text(text) or "(the page has no visible text)")
            + "\n\n--- HTML source (everything that is published) ---\n"
            + text
        )
        await gated_call(
            connector=self.name,
            tool="peek_upload_page",
            tool_name="Publish Peek Page",
            summary=summary,
            sender=sender,
            raw_data={
                "name": name, "slug": slug, "visibility": visibility, "size_bytes": len(data),
            },
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            upload_pii_scan_text=text,
            args={"name": name, "slug": slug, "visibility": visibility},
        )
        # ADR 0102: the upload slot is consumed only now, once the gate has passed.
        local_files.commit_uploads()
        result = await self._fetch(
            self._client.upload_page, data, name, visibility, slug=slug,
        )
        return asdict(result)

    async def _set_visibility(self, slug: str, visibility: str, password: str = "") -> Any:
        _refuse_password(password)
        _check_slug(slug)
        visibility = _check_visibility(visibility)
        page = await self._fetch(self._client.get_page, slug)
        details = f"Who can open {page.url} changes from {page.visibility} to {visibility}."
        if visibility == "restricted" and page.visibility != "restricted":
            details += (
                "\n\nOnly you, Peek admins and the people granted access can open it. "
                "Peek emails the link to everyone already granted access."
            )
        elif page.visibility == "restricted" and visibility != "restricted":
            details += (
                "\n\nThe page is no longer limited to the people granted access. Their grants "
                "stay and apply again if the page is restricted later."
            )
        await gated_call(
            connector=self.name,
            tool="peek_set_visibility",
            tool_name="Change Peek Page Visibility",
            summary=f"{page.name}: {page.visibility} \u2192 {visibility}",
            sender=page.name,
            raw_data={"slug": slug, "visibility": visibility},
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Page": page.name,
                "Link": page.url,
                "Visibility": f"{page.visibility} \u2192 {visibility}",
            },
            details_text=details,
            args={"slug": slug, "visibility": visibility},
        )
        res = await self._fetch(self._client.set_visibility, slug, visibility)
        return {"slug": slug, "visibility": res.visibility, "notified": res.notified}

    async def _add_comment(
        self, slug: str, body: str, selector: str = "", element_text: str = "", version: int = 0,
    ) -> Any:
        _check_slug(slug)
        if not 1 <= _utf8_len(body) <= 4000:
            raise ValueError("body must be 1 to 4000 bytes (UTF-8).")
        if _utf8_len(selector) > 500:
            raise ValueError("selector must be 500 bytes or fewer (UTF-8).")
        if _utf8_len(element_text) > 200:
            raise ValueError("element_text must be 200 bytes or fewer (UTF-8).")
        if element_text and not selector:
            raise ValueError("element_text needs a selector.")
        if version < 0:
            raise ValueError("version must be 0 (the latest) or a version number.")
        page = await self._fetch(self._client.get_page, slug)
        if version > page.version:
            raise ValueError(f"This page's latest version is {page.version}.")
        shown = version or page.version
        excerpt = body[:80] + ("\u2026" if len(body) > 80 else "")
        kind = "text" if element_text else "element" if selector else "page"
        await gated_call(
            connector=self.name,
            tool="peek_add_comment",
            tool_name="Comment on Peek Page",
            summary=f"Comment on {page.name}: {excerpt}",
            sender=page.name,
            raw_data={"slug": slug, "body": body},
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Page": page.name,
                "Link": page.url,
                "Version": str(shown),
                "Anchored to": _anchor_text(kind, selector, element_text),
            },
            details_text=body,
            args={"slug": slug},
        )
        comment = await self._fetch(
            self._client.add_comment, slug, body, selector, element_text, version,
        )
        return {
            "slug": slug,
            "version": shown,
            "comment": {
                "id": comment.id,
                "author": comment.author,
                "verified": comment.verified,
                "body": comment.body,
                "anchor_kind": comment.anchor_kind,
                "selector": comment.selector,
                "element_text": comment.element_text,
                "created_at": comment.created_at,
            },
        }

    async def _rename_page(self, slug: str, name: str) -> Any:
        _check_slug(slug)
        if not name.strip() or len(name.strip()) > 200:
            raise ValueError("name must be 1 to 200 characters.")
        page = await self._fetch(self._client.get_page, slug)
        await gated_call(
            connector=self.name,
            tool="peek_rename_page",
            tool_name="Rename Peek Page",
            summary=f"Rename {page.name} \u2192 {name}",
            sender=page.name,
            raw_data={"slug": slug, "name": name},
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Page": page.name,
                "New name": name,
                "Link": page.url,
            },
            details_text=(
                f"{page.name} is renamed to {name}. Its link may change to match the new name; "
                f"the old link {page.url} keeps redirecting to the page in browsers, but tools "
                "must use the new slug."
            ),
            args={"slug": slug},
        )
        res = await self._fetch(self._client.rename_page, slug, name)
        return {"old_slug": slug, "slug": res.slug, "url": res.url, "name": res.name}

    async def _delete_page(self, slug: str) -> Any:
        _check_slug(slug)
        page = await self._fetch(self._client.get_page, slug)
        await gated_call(
            connector=self.name,
            tool="peek_delete_page",
            tool_name="Delete Peek Page",
            summary=f"Delete {page.name}",
            sender=page.name,
            raw_data={"slug": slug},
            filtered_data=None,
            gate="popup",
            preview={
                "Server": self._client.host,
                "Page": page.name,
                "Link": page.url,
                "Visibility": page.visibility,
                "Created": page.created_at,
            },
            details_text=f"{page.name} ({page.url}) is deleted with its comments and visit history.",
            args={"slug": slug},
        )
        await self._fetch(self._client.delete_page, slug)
        return {"deleted": slug}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except PeekClientError as exc:
            logger.warning("Peek request to %s failed", self._client.host)
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


def _check_slug(slug: str) -> None:
    try:
        validate_slug(slug)
    except PeekClientError as exc:
        raise ValueError(str(exc)) from exc


def _check_visibility(visibility: str) -> str:
    try:
        return normalize_visibility(visibility)
    except PeekClientError as exc:
        raise ValueError(str(exc)) from exc


def _refuse_password(password: str) -> None:
    # A client that still sends a password must not publish a page it thinks is protected.
    if password:
        raise ValueError(PASSWORDS_REMOVED)


def _utf8_len(value: str) -> int:
    return len(value.strip().encode("utf-8"))


def _anchor_text(anchor_kind: str, selector: str, element_text: str) -> str:
    if anchor_kind == "element":
        return f"Element {selector}"
    if anchor_kind == "text":
        return f"Text \u201c{element_text}\u201d"
    return "Whole page"
