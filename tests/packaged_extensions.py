"""The two Claude Desktop extensions, as the packaged-artifact smoke tests check them.

``test_macos_packaged_smoke.py`` (the DMG) and ``test_windows_packaged_smoke.py`` (the installer)
both ship ``PrivacyFence.mcpb`` and ``PrivacyFence-no-prompts.mcpb`` (ADR 0087), and both need to
prove the same two things about them against the real packaged daemon: both files are there, and
what the no-prompts one's manifest asks for is what the daemon then advertises. This module is
those checks, so the two platforms name one definition rather than two copies of it.

Standard library only, and no ``privacyfence`` import: the packaged tests test the frozen binary
from outside (``test_macos_packaged_smoke.py``'s own note on its imports).

**What "all read-only" can mean against a packaged daemon.** A freshly installed daemon has no
connector credentials, so its ``tools/list`` holds PrivacyFence's own ``privacyfence_*`` meta-tools
and nothing else, and those keep their declared annotations in every mode (ADR 0086). So the
packaged check is three-part, and each part is one the unit and contract tests cannot make:

1. every non-meta tool the daemon lists through the no-prompts extension is read-only (holds for
   whatever the install happens to have connected);
2. the meta-tools are advertised identically with and without the extension's header;
3. the packaged daemon answers a value it does not know with 400 -- so it really reads the header
   the extension sends, rather than the first two holding only because the header was dropped.

That the header flips a real write tool is ``tests/integration/test_shim_mcp_contract.py``'s
``test_each_extensions_manifest_args_get_the_annotations_it_promises`` (the real shim against the
real ``/mcp``), which a packaged install without credentials cannot show.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_EXTENSION = "PrivacyFence.mcpb"
NO_PROMPTS_EXTENSION = "PrivacyFence-no-prompts.mcpb"

TOOL_ANNOTATIONS_HEADER = "X-PrivacyFence-Tool-Annotations"
_FLAG_PREFIX = "--tool-annotations="
META_TOOL_PREFIX = "privacyfence_"


def read_manifest(mcpb_path: Path) -> dict[str, Any]:
    """The ``manifest.json`` inside a built ``.mcpb`` (a zip archive)."""
    with zipfile.ZipFile(mcpb_path) as archive:
        return json.loads(archive.read("manifest.json"))


def extract(mcpb_path: Path, destination: Path) -> Path:
    """Unpacks ``mcpb_path`` into ``destination`` the way Claude Desktop does before it starts the
    server, and returns ``destination`` (the manifest's ``${__dirname}``)."""
    with zipfile.ZipFile(mcpb_path) as archive:
        archive.extractall(destination)
    return destination


def server_args(manifest: dict[str, Any], dirname: Path) -> list[str]:
    """The manifest's ``server.mcp_config.args``, with ``${__dirname}`` resolved to ``dirname``."""
    return [arg.replace("${__dirname}", str(dirname)) for arg in manifest["server"]["mcp_config"]["args"]]


def tool_annotations_header_value(manifest: dict[str, Any]) -> str | None:
    """The header value the shim will send for this manifest, or None when it sends none."""
    for arg in manifest["server"]["mcp_config"]["args"]:
        if arg.startswith(_FLAG_PREFIX):
            return arg[len(_FLAG_PREFIX):]
    return None


def assert_manifests(default: dict[str, Any], no_prompts: dict[str, Any]) -> None:
    """The two shipped manifests are the two extensions ADR 0087 describes."""
    assert default["name"] == "privacyfence", default["name"]
    assert tool_annotations_header_value(default) is None, default["server"]["mcp_config"]["args"]
    assert no_prompts["name"] == "privacyfence-read-only", no_prompts["name"]
    assert no_prompts["display_name"] == "PrivacyFence (no Claude prompts)", no_prompts.get("display_name")
    assert tool_annotations_header_value(no_prompts) == "all-read-only", no_prompts["server"]["mcp_config"]["args"]
    assert default["version"] == no_prompts["version"], (default["version"], no_prompts["version"])


def _annotations(tool: Any) -> dict[str, Any] | None:
    annotations = getattr(tool, "annotations", None)
    return annotations.model_dump() if annotations is not None else None


def assert_all_read_only(no_prompts_tools: list[Any], default_tools: list[Any]) -> None:
    """Points 1 and 2 of this module's docstring, over two ``tools/list`` results from the same
    daemon: one through the no-prompts extension, one without its header."""
    listed = {tool.name for tool in no_prompts_tools}
    assert listed == {tool.name for tool in default_tools}, "the header changed which tools are listed"
    for tool in no_prompts_tools:
        if tool.name.startswith(META_TOOL_PREFIX):
            continue
        assert tool.annotations is not None and tool.annotations.read_only_hint is True, (
            f"{tool.name} is not advertised read-only through {NO_PROMPTS_EXTENSION}: {_annotations(tool)}"
        )
    default_meta = {t.name: _annotations(t) for t in default_tools if t.name.startswith(META_TOOL_PREFIX)}
    no_prompts_meta = {t.name: _annotations(t) for t in no_prompts_tools if t.name.startswith(META_TOOL_PREFIX)}
    assert default_meta, "no privacyfence_* meta-tool listed at all"
    assert no_prompts_meta == default_meta


def initialize_request() -> dict[str, Any]:
    """A bare JSON-RPC ``initialize``, for point 3's raw POST."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "packaged-extensions-probe", "version": "0"},
        },
    }


def assert_unknown_header_value_is_refused(status_code: int, body: str) -> None:
    """Point 3: the packaged daemon's answer to ``initialize`` with a header value it does not know."""
    assert status_code == 400, (status_code, body)
    assert TOOL_ANNOTATIONS_HEADER in body, body
