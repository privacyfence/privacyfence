"""A plugin tool's file parameter end to end: the agent uploads a file, PrivacyFence checks and shows it,
and the plugin receives it only after the card is approved."""
from __future__ import annotations

import hashlib

import pytest

from tests.fixtures.plugins.echo.harness import (
    Stack,
    card_pairs,
    install_echo,
    mcp_session,
    upload_file,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch, serve=True)
    install_echo(stack.plugins)
    await stack.start()
    await stack.enable()
    try:
        yield stack
    finally:
        await stack.stop()


def file_card(stack: Stack) -> list[tuple[str, str]]:
    return card_pairs(stack.popups.write[-1][1]["preview_blocks"])


class TestUpload:
    async def test_an_uploaded_file_reaches_the_plugin_after_approval(self, stack):
        async with mcp_session(stack.server) as mcp:
            ref = await upload_file(mcp, b"hello")
            result = await mcp.call("echo_file_put", file=ref)

        sha = hashlib.sha256(b"hello").hexdigest()
        assert result.is_error is False, result
        assert result.structured_content == {"name": "note.txt", "size": 5, "sha256": sha}
        card = file_card(stack)
        assert card[:6] == [
            ("File", "note.txt"), ("Source", "Upload slot"), ("Size", "5 bytes"),
            ("Declared type", "text/plain"), ("Detected type", "text/plain"), ("SHA-256", sha),
        ]
        [row] = [e for e in stack.audit() if e["decision"] == "plugin_file"]
        assert row["connector"] == "plugin:echo"
        assert row["summary"] == f"file: note.txt; bytes=5; sha256={sha}; type=text/plain"

    async def test_a_file_over_the_limit_fails_without_a_card(self, stack):
        async with mcp_session(stack.server) as mcp:
            ref = await upload_file(mcp, b"a" * 70_000)
            result = await mcp.call("echo_file_put", file=ref)

        assert result.is_error is True
        assert "70,000 bytes, over the 65,536-byte limit of File put" in str(result.content)
        assert stack.popups.write == []

    async def test_a_refused_type_fails_without_a_card(self, stack):
        async with mcp_session(stack.server) as mcp:
            ref = await upload_file(mcp, PNG, "pic.png")
            result = await mcp.call("echo_file_put", file=ref)

        assert result.is_error is True
        assert "image/png" in str(result.content) and "accepts only" in str(result.content)
        assert stack.popups.write == []


class TestLocalPath:
    async def test_a_direct_path_works_and_the_card_names_its_basename(self, stack, tmp_path):
        path = tmp_path / "local.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        async with mcp_session(stack.server) as mcp:
            result = await mcp.call("echo_file_put", file=str(path))

        assert result.is_error is False, result
        assert result.structured_content["name"] == "local.json"
        card = dict(file_card(stack))
        assert card["File"] == "local.json" and card["Source"] == str(path)
        assert card["Detected type"] == "application/json"
