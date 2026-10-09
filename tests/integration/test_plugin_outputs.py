"""Plugin outputs end to end: ``echo`` publishes a file, and the agent lists and reads it through
PrivacyFence's own ``plugin_outputs_*`` tools.

Listing is automatic and audited; reading is a review card unless a folder rule covers the file.
"""
from __future__ import annotations

import hashlib

import pytest

from privacyfence import auto_accept
from privacyfence.plugins import storage
from privacyfence.plugins.constants import OUTPUT_READ_PAGE_BYTES
from privacyfence.principal import LOCAL_PRINCIPAL
from tests.fixtures.plugins.echo.harness import Stack, install_echo, mcp_session, until
from tests.helpers import policy_rules

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]

CSV = "name,total\nada,3\nlin,4\n"


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


async def publish(mcp, name: str = "x", text: str = CSV) -> dict:
    result = await mcp.call("echo_publish", name=name, text=text)
    assert result.is_error is False, result
    return result.structured_content


async def listed(mcp, **args) -> dict:
    result = await mcp.call("plugin_outputs_list", plugin="echo", **args)
    assert result.is_error is False, result
    return result.structured_content


def allow_folder(folder: str) -> None:
    auto_accept.add_policy_v2_rules(policy_rules({
        "plugin_outputs.read": [{"predicate": "plugin:echo:output", "value": folder}],
    }))


class TestPublishAndList:
    async def test_a_published_file_is_listed_without_a_card(self, stack):
        async with mcp_session(stack.server) as mcp:
            assert await publish(mcp) == {"path": "reports/x.csv"}
            cards_before = len(stack.popups.read)

            result = await listed(mcp)

        assert len(stack.popups.read) == cards_before
        [entry] = result["files"]
        assert entry["path"] == "reports/x.csv" and entry["size"] == len(CSV)
        assert entry["mime_type"] == "text/csv" and result["next_cursor"] is None
        assert (storage.output_dir("echo", LOCAL_PRINCIPAL) / "reports" / "x.csv").read_text(encoding="utf-8") == CSV

    async def test_listing_is_audited_as_automatic(self, stack):
        async with mcp_session(stack.server) as mcp:
            await publish(mcp)
            await listed(mcp, prefix="reports/")

        [entry] = [e for e in stack.audit() if e["tool"] == "plugin_outputs_list"]
        assert entry["connector"] == "plugin_outputs" and entry["decision"] == "auto_accepted"
        assert "list echo" in entry["summary"] and "files=1" in entry["summary"]

    async def test_a_prefix_narrows_the_listing(self, stack):
        async with mcp_session(stack.server) as mcp:
            await publish(mcp, "x")

            assert (await listed(mcp, prefix="reports/"))["files"] != []
            assert (await listed(mcp, prefix="other/"))["files"] == []


class TestRead:
    async def test_a_read_shows_a_card_and_releases_the_text(self, stack):
        async with mcp_session(stack.server) as mcp:
            await publish(mcp)
            cards_before = len(stack.popups.read)

            result = await mcp.call("plugin_outputs_read", plugin="echo", path="reports/x.csv")

        assert result.is_error is False, result
        released = result.structured_content
        assert released["text"] == CSV and released["next_offset"] is None
        assert released["sha256"] == hashlib.sha256(CSV.encode()).hexdigest()
        assert len(stack.popups.read) == cards_before + 1
        [entry] = [e for e in stack.audit() if e["decision"] == "plugin_output"]
        assert entry["tool"] == "plugin_outputs_read" and "read reports/x.csv" in entry["summary"]

    async def test_a_denied_card_releases_nothing(self, stack):
        async with mcp_session(stack.server) as mcp:
            await publish(mcp)
            stack.popups.decision = "deny"

            result = await mcp.call("plugin_outputs_read", plugin="echo", path="reports/x.csv")

        assert result.is_error is True
        assert CSV not in str(result.structured_content) and "ada" not in str(result.content)
        assert not [e for e in stack.audit() if e["decision"] == "plugin_output"]

    async def test_offsets_page_a_file_larger_than_one_read(self, stack):
        text = "".join(f"row-{n:06d},{n}\n" for n in range(12_000))   # about 180 KB
        assert len(text.encode()) > 2 * OUTPUT_READ_PAGE_BYTES
        async with mcp_session(stack.server) as mcp:
            await publish(mcp, "big", text)
            pieces: list[str] = []
            offset = 0
            while True:
                result = await mcp.call("plugin_outputs_read", plugin="echo", path="reports/big.csv", offset=offset)
                assert result.is_error is False, result
                page = result.structured_content
                assert page["offset"] == offset and page["length"] <= OUTPUT_READ_PAGE_BYTES
                pieces.append(page["text"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]

        assert len(pieces) == 3 and "".join(pieces) == text
        assert page["sha256"] == hashlib.sha256(text.encode()).hexdigest()


class TestFolderRule:
    async def test_a_folder_rule_releases_that_folder_without_a_card(self, stack):
        allow_folder("reports/")
        async with mcp_session(stack.server) as mcp:
            await publish(mcp)
            cards_before = len(stack.popups.read)

            result = await mcp.call("plugin_outputs_read", plugin="echo", path="reports/x.csv")

        assert result.structured_content["text"] == CSV
        assert len(stack.popups.read) == cards_before
        [entry] = [e for e in stack.audit() if e["tool"] == "plugin_outputs_read" and e["decision"] != "plugin_output"]
        assert entry["decision"] == "auto_accepted"

    async def test_a_rule_for_another_folder_still_shows_a_card(self, stack):
        allow_folder("other/")
        async with mcp_session(stack.server) as mcp:
            await publish(mcp)
            cards_before = len(stack.popups.read)

            result = await mcp.call("plugin_outputs_read", plugin="echo", path="reports/x.csv")

        assert result.structured_content["text"] == CSV
        assert len(stack.popups.read) == cards_before + 1


class TestDisabling:
    async def test_disabling_the_plugin_removes_the_output_tools(self, stack):
        async with mcp_session(stack.server) as mcp:
            assert {"plugin_outputs_list", "plugin_outputs_read", "echo_publish"} <= set(await mcp.tool_names())
            before = mcp.list_changed()

            await stack.run(stack.host.disable("echo"))
            await until(lambda: mcp.list_changed() > before)

            names = set(await mcp.tool_names())

        assert not {"plugin_outputs_list", "plugin_outputs_read", "echo_publish"} & names
