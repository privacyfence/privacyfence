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
from ..gate import current_reason
from ..peek_client import PeekClient, PeekClientError

logger = logging.getLogger(__name__)

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
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "peek_list_pages":
            return await self._list_pages(**args)
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
