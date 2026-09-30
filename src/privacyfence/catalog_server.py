"""The stdio MCP server that directory listings (Glama) start to read PrivacyFence's tool list.

It holds no credentials, reads and writes no files, makes no network calls, and answers every tool
call with ``CATALOG_CALL_MESSAGE``. It is not a way to run PrivacyFence (ADR 0114). It imports
nothing from the daemon, so nothing here can reach a connector.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

from . import __version__, connector_catalog
from .web import mcp_tools

CATALOG_INSTRUCTIONS = (
    "This server only lists PrivacyFence's tools, for MCP directories. It holds no credentials, "
    "connects to no service, and every tool call returns an error saying so. PrivacyFence itself "
    "runs on the user's own computer or on a server their organization runs: "
    "https://privacyfence.eu/download/"
)

CATALOG_CALL_MESSAGE = (
    "This is PrivacyFence's tool catalog, published so MCP directories can list its tools. It is "
    "not a working PrivacyFence install and cannot reach any service. PrivacyFence runs on your "
    "own computer, or on a server your organization runs: install it from "
    "https://privacyfence.eu/download/ and connect your AI client to that install."
)


def build_catalog_server() -> Server[Any]:
    tools = connector_catalog.catalog_tools()

    async def handle_list_tools(
        ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def handle_call_tool(
        ctx: ServerRequestContext, _params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        return mcp_tools.error_result(CATALOG_CALL_MESSAGE)

    return Server(
        "privacyfence",
        version=__version__,
        instructions=CATALOG_INSTRUCTIONS,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def serve_stdio() -> None:
    server = build_catalog_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    anyio.run(serve_stdio)


if __name__ == "__main__":
    main()
