"""Telegram connector."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..preview_dates import format_preview_datetime
from ..telegram_client import TelegramClientError, TelegramPrivacyFenceClient

logger = logging.getLogger(__name__)


class TelegramConnector(Connector):
    def __init__(self, client: TelegramPrivacyFenceClient) -> None:
        self._telegram = client

    @property
    def client(self) -> TelegramPrivacyFenceClient:
        return self._telegram

    @property
    def name(self) -> str:
        return "telegram"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="telegram_list_chats",
                description=(
                    "List Telegram chats (id, name, type, unread count). Returns a list of "
                    "{id, name, username, type, unread_count, is_self}, most recently active "
                    "first, up to limit (default 50, capped at 200); username is the @handle "
                    "without the @, empty if none; type is 'user', 'group', "
                    "'channel' or 'bot', and is_self marks Saved Messages. Pass an id as "
                    "chat_id to telegram_get_messages or telegram_send_message; call "
                    "telegram_refresh_chat_cache first if a new chat is missing or shows "
                    "no name. Auto-approved."
                ),
                params=[
                    ToolParam(
                        "limit", "int", required=False, default=50,
                        description="Maximum chats to return, most recently active first. Default 50, capped at 200.",
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="telegram_get_messages",
                description=(
                    "Fetch recent messages from a Telegram chat by chat id. "
                    "Returns a list of {id, sender_name, text, date} (date is ISO 8601), "
                    "newest first, up to limit messages (default 50, capped at 200); "
                    "media messages carry only their text or caption, if any. Use "
                    "telegram_search_messages to find messages by keyword across all "
                    "chats; get chat_id from telegram_list_chats. Requires user approval."
                ),
                params=[
                    ToolParam(
                        "chat_id", "int",
                        description=(
                            "Numeric id of the chat, from the id field of telegram_list_chats. "
                            "A chat name or @username is not accepted."
                        ),
                    ),
                    ToolParam(
                        "limit", "int", required=False, default=50,
                        description="Maximum messages to fetch, newest first. Default 50, capped at 200.",
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="telegram_search_messages",
                description=(
                    "Search messages across Telegram chats by keyword. "
                    "Returns a list of {id, chat_id, chat_name, sender_name, text, date} "
                    "(date is ISO 8601), newest first, up to limit messages (default 30, "
                    "capped at 100); id is the message id and chat_id is the value "
                    "telegram_get_messages takes. Use telegram_get_messages to read one chat's recent "
                    "messages in full. Requires user approval."
                ),
                params=[
                    ToolParam(
                        "query", "str",
                        description="Keyword or phrase to search for in message text, across all chats.",
                    ),
                    ToolParam(
                        "limit", "int", required=False, default=30,
                        description="Maximum messages to return, newest first. Default 30, capped at 100.",
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="telegram_refresh_chat_cache",
                description=(
                    "Force an immediate refresh of PrivacyFence's local cache of Telegram "
                    "chat/group/channel names, used to resolve which chat a message belongs "
                    "to in search results and chat history without a per-message lookup. "
                    "Refreshes automatically about once a week; call this after a new chat "
                    "starts so it resolves by name right away. Auto-approved -- refreshes "
                    "name lookups only, reads no message content. Returns "
                    "{cached_chats: <count>}."
                ),
                params=[
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="telegram_send_message",
                description=(
                    "Send a message to a Telegram chat or user by chat id. Returns "
                    "{chat_id, chat_name, msg_id, text} of the sent message. Get chat_id "
                    "from telegram_list_chats. Requires user approval."
                ),
                params=[
                    ToolParam(
                        "chat_id", "int",
                        description=(
                            "Numeric id of the chat or user to send to, from the id field of "
                            "telegram_list_chats. A name or @username is not accepted."
                        ),
                    ),
                    ToolParam("text", "str", description="Message text to send. Must not be empty."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "telegram_list_chats":
            return await self._list_chats(**args)
        if tool == "telegram_get_messages":
            return await self._get_messages(**args)
        if tool == "telegram_search_messages":
            return await self._search_messages(**args)
        if tool == "telegram_refresh_chat_cache":
            return await self._refresh_chat_cache(**args)
        if tool == "telegram_send_message":
            return await self._send_message(**args)
        raise ValueError(f"Unknown Telegram tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_chats(self, limit: int = 50) -> Any:
        t0 = time.time()
        try:
            chats = await self._telegram.list_chats(limit)
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc
        result = [
            {
                "id": c.id,
                "name": c.name,
                "username": c.username or "",
                "type": c.chat_type,
                "unread_count": c.unread_count,
                "is_self": c.is_self,
            }
            for c in chats
        ]
        self._auto_audit("telegram_list_chats", "List Telegram Chats",
                         f"List chats (max {limit})", f"{len(result)} chat(s)", t0)
        return result

    async def _refresh_chat_cache(self) -> Any:
        t0 = time.time()
        try:
            count = await self._telegram.refresh_chat_directory()
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc
        self._auto_audit(
            "telegram_refresh_chat_cache", "Refresh Telegram Chat Cache",
            "Refresh Telegram chat directory cache", f"{count} chat(s)", t0,
        )
        return {"cached_chats": count}

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_messages(self, chat_id: int, limit: int = 50) -> Any:
        try:
            messages = await self._telegram.get_messages(chat_id, limit)
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc
        chat_name = str(chat_id)
        if messages and hasattr(messages[0], "chat_name"):
            chat_name = messages[0].chat_name or chat_name
        n = len(messages)
        # Chat is known via telegram_list_chats; Messages (count) and the
        # actual message content are only learned once this call is
        # approved -- Telegram has no privacy-category schema, so the
        # message content gets one fixed summary row rather than a
        # per-message value (the real text lives in the right-pane preview/
        # details_text instead).
        preview = {"Chat": chat_name}
        new_info = {
            "Messages": str(n),
            "Message text": "Full sender name, text, and date per message",
        }
        lines = [
            f"[{getattr(m, 'date', '')}] {getattr(m, 'sender_name', 'unknown')}: {getattr(m, 'text', '')}"
            for m in messages
        ]
        result = [
            {
                "id": getattr(m, "id", ""),
                "sender_name": getattr(m, "sender_name", ""),
                "text": getattr(m, "text", ""),
                "date": str(getattr(m, "date", "")),
            }
            for m in messages
        ]
        # Built unconditionally, even though the table below is the nicer
        # rendering: this plain-text list is also the PII scan's fallback
        # source (see connectors/salesforce.py's _search for the same
        # reasoning), so it can't be emptied out just because a table is
        # also shown.
        table = {
            "headers": ["Sender", "Date", "Message"],
            "rows": [
                [
                    getattr(m, "sender_name", "unknown"),
                    format_preview_datetime(getattr(m, "date", "")),
                    getattr(m, "text", ""),
                ]
                for m in messages
            ],
        }
        return await gated_call(
            connector=self.name,
            tool="telegram_get_messages",
            tool_name="Read Telegram Chat",
            summary=f"{n} message{'s' if n != 1 else ''} from {chat_name}",
            sender=chat_name,
            raw_data=messages,
            filtered_data=result,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text="\n".join(lines),
            pii_scan_text="\n".join(getattr(m, "text", "") or "" for m in messages),
            preview_tables=[table] if messages else [],
            table_only=True,
            args={"chat_id": chat_id},
        )

    async def _search_messages(self, query: str, limit: int = 30) -> Any:
        try:
            messages = await self._telegram.search_messages(query, limit)
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc
        n = len(messages)
        # Query is Claude's own input (kept in the preview as identifying context);
        # Results (count) and the actual message content are only learned
        # once approved.
        preview = {"Query": query}
        new_info = {
            "Results": str(n),
            "Message text": "Full sender name, text, and date per message",
        }
        lines = [
            f"[{getattr(m, 'chat_name', '')}] {getattr(m, 'sender_name', 'unknown')}: {getattr(m, 'text', '')}"
            for m in messages
        ]
        result = [
            {
                "id": getattr(m, "id", ""),
                "chat_id": getattr(m, "chat_id", 0),
                "chat_name": getattr(m, "chat_name", ""),
                "sender_name": getattr(m, "sender_name", ""),
                "text": getattr(m, "text", ""),
                "date": str(getattr(m, "date", "")),
            }
            for m in messages
        ]
        table = {
            "headers": ["Sender", "Date", "Message"],
            "rows": [
                [
                    getattr(m, "sender_name", "unknown"),
                    format_preview_datetime(getattr(m, "date", "")),
                    getattr(m, "text", ""),
                ]
                for m in messages
            ],
        }
        return await gated_call(
            connector=self.name,
            tool="telegram_search_messages",
            tool_name="Search Telegram",
            summary=f"{n} result{'s' if n != 1 else ''} for \"{query}\"",
            sender=query,
            raw_data=messages,
            filtered_data=result,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text="\n".join(lines),
            pii_scan_text="\n".join(getattr(m, "text", "") or "" for m in messages),
            preview_tables=[table] if messages else [],
            table_only=True,
            args={"query": query},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _send_message(self, chat_id: int, text: str) -> Any:
        resolved_name = await self._telegram.get_chat_name(chat_id)
        chat_display = resolved_name or str(chat_id)
        preview = {"Chat": chat_display}
        await gated_call(
            connector=self.name,
            tool="telegram_send_message",
            tool_name="Send Telegram Message",
            summary=f"To {chat_display}: {text[:80]}{'…' if len(text) > 80 else ''}",
            sender=str(chat_id),
            raw_data={"chat_id": chat_id, "text": text},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=text,
            args={"chat_id": chat_id},
        )
        try:
            return await self._telegram.send_message(chat_id, text)
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

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
