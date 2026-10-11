"""Peek connector: the HTML pages an account shares through a Peek server.

The connector holds a ``PeekClient`` bound to one server (ADR 0147). Nothing
here logs, audits or previews a Peek token, a page password, page content or a
comment body.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..peek_client import PeekClient, PeekClientError, validate_slug
from ..preview_dates import format_preview_datetime

logger = logging.getLogger(__name__)

_SLUG = ToolParam(
    "slug", "str", required=True,
    description=(
        "The page's slug: the slug field from peek_list_pages, or the last part of its "
        "link (…/p/<slug>)."
    ),
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
                    "body, anchor_kind, selector, element_text, created_at, version}]}, where "
                    "version is the one read. Use peek_get_stats to see how often the page was opened. "
                    "Requires user approval."
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
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "peek_list_pages":
            return await self._list_pages(**args)
        if tool == "peek_get_comments":
            return await self._get_comments(**args)
        if tool == "peek_get_stats":
            return await self._get_stats(**args)
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
            "headers": ["Author", "Date", "Anchored to", "Comment"],
            "rows": [
                [
                    c.author,
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


def _anchor_text(anchor_kind: str, selector: str, element_text: str) -> str:
    if anchor_kind == "element":
        return f"Element {selector}"
    if anchor_kind == "text":
        return f"Text \u201c{element_text}\u201d"
    return "Whole page"
