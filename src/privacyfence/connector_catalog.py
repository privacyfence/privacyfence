"""Enumerates every connector PrivacyFence ships and every tool it can offer, without any connector
being set up.

Two users: ``catalog_server.py``, and ``scripts/generate_tools_reference.py`` together with the
tests that import it.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil

from mcp import types

from . import connectors as connectors_pkg
from .connector import Connector
from .web import mcp_tools


def connector_classes() -> list[type[Connector]]:
    """Every concrete ``Connector`` subclass defined in ``privacyfence.connectors``."""
    found: dict[str, type[Connector]] = {}
    for module_info in pkgutil.iter_modules(connectors_pkg.__path__):
        module = importlib.import_module(f"{connectors_pkg.__name__}.{module_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Connector) and obj is not Connector and not inspect.isabstract(obj):
                if obj.__module__ == module.__name__:
                    found[obj.__qualname__] = obj
    return [found[key] for key in sorted(found)]


def catalog_tools() -> list[types.Tool]:
    """Every tool PrivacyFence can offer an MCP client: each connector's tools, then the
    ``privacyfence_*`` meta-tools, in the order routes_mcp.handle_list_tools lists them."""
    tools = [
        mcp_tools.to_mcp_tool(spec)
        for cls in connector_classes()
        # tool_specs() is static data and never touches the client, so none is built.
        for spec in cls(None).tool_specs()  # type: ignore[call-arg]
    ]
    tools.extend(mcp_tools.META_TOOLS)
    return tools
