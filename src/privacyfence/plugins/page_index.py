"""The page list a plugin answers ``pages.list`` with, as the page browser shows it.

A plugin that registers no page index answers ``method_not_found``; it gets one entry for its
root page. Any other failure becomes a short error sentence, so the browser can show it next to
the plugin's name without the plugin's own words reaching the page.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from privacyfence.plugins.protocol import PageEntry, PagesListResult, RpcError

logger = logging.getLogger(__name__)

PAGE_INDEX_NO_ANSWER = "The plugin could not list its pages."
PAGE_INDEX_INVALID = "The plugin returned an invalid page list."


@dataclass(frozen=True)
class PageIndex:
    name: str  # plugin name
    display_name: str
    entries: tuple[PageEntry, ...]  # empty when error is set
    error: str = ""  # PAGE_INDEX_NO_ANSWER, PAGE_INDEX_INVALID or ""


def _failed(name: str, display_name: str, error: str, code: str) -> PageIndex:
    logger.warning("Plugin %s page list failed: %s", name, code)
    return PageIndex(name, display_name, (), error)


def index_from_result(
    name: str, display_name: str, result: Any = None, error: RpcError | None = None,
) -> PageIndex:
    if error is not None:
        if error.code == "method_not_found":
            return PageIndex(name, display_name, (PageEntry(path="/", title=display_name),))
        if error.code == "invalid_params":
            return _failed(name, display_name, PAGE_INDEX_INVALID, error.code)
        return _failed(name, display_name, PAGE_INDEX_NO_ANSWER, error.code)
    try:
        parsed = PagesListResult.from_wire(result)
    except RpcError as exc:
        return _failed(name, display_name, PAGE_INDEX_INVALID, exc.code)
    return PageIndex(name, display_name, parsed.pages)
