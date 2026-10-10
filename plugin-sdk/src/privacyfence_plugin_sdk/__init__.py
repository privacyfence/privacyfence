"""SDK for writing PrivacyFence plugins.

A plugin is its own executable. PrivacyFence starts it and talks JSON-RPC to it over stdin and
stdout. This package handles the protocol so a plugin only registers tools, pages and handlers.
"""
from __future__ import annotations

from . import blocks
from ._files import IncomingFile, file_param
from .plugin import PROTOCOL_VERSION, Context, PageRequest, Plugin, Prepared, Principal, ToolHandle
from .responses import (
    ApprovalTicket,
    Bytes,
    ConfirmResult,
    DownloadedFile,
    Html,
    PageEntry,
    SourceError,
    SourceResult,
    Text,
    ToolDefinitionError,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ApprovalTicket",
    "Bytes",
    "ConfirmResult",
    "Context",
    "DownloadedFile",
    "Html",
    "IncomingFile",
    "PageEntry",
    "PageRequest",
    "Plugin",
    "Prepared",
    "Principal",
    "SourceError",
    "SourceResult",
    "Text",
    "ToolDefinitionError",
    "ToolHandle",
    "blocks",
    "file_param",
]
