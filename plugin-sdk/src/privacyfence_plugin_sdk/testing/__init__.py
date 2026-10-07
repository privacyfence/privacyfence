"""An in-memory PrivacyFence for testing a plugin.

``PluginTestHost`` runs your plugin's real runner over an in-memory stream pair and plays the
daemon's side of the protocol: it checks the tool definitions the way PrivacyFence does, validates
every block and limit, decides each call at the simulated gate (no card, a review card or a popup
card), answers ``source.call`` from fixtures, and releases only what a human would have seen.

A read tool::

    import pytest
    from privacyfence_plugin_sdk import Plugin, Prepared, blocks
    from privacyfence_plugin_sdk.testing import PluginTestHost, samples

    plugin = Plugin(name="today", version="1.0.0")
    plugin.scope_type("calendar", "Calendar id a call reads")

    @plugin.tool("list_events", gate="review", read_only=True, scopes=["calendar"],
                 description="List the day's events.")
    async def list_events(ctx, args):
        result = await ctx.source.call("calendar.list_events", time_min="2026-10-07T00:00:00Z",
                                       time_max="2026-10-08T00:00:00Z")
        titles = [e["title"] for e in result.data]
        return Prepared(
            preview=[blocks.fields({"Events": len(titles)})],
            payload=[blocks.text(", ".join(titles))],
            scopes={"calendar": ["primary"]},
        )

    async def test_the_ai_gets_what_the_card_showed():
        async with PluginTestHost(plugin) as host:
            host.source.load(samples.get("calendar.list_events"))
            outcome = await host.call_tool("list_events", {"reason": "plan the day"})
            assert outcome.card_shown
            assert outcome.released == {"blocks": outcome.card.payload}

Decide a card with ``decide="deny"`` or a function of the card, and let a person's "Always allow"
click skip the card with a scope rule. A rule matches only when every value the call returned is in
the rule::

    async with PluginTestHost(plugin) as host:
        host.source.load(samples.get("calendar.list_events"))
        host.rules.allow_scope("calendar", ["primary"])
        outcome = await host.call_tool("list_events")
        assert not outcome.card_shown and outcome.approval["via"] == "rule"

        denied = await host.call_tool("some_write", decide=lambda card: "deny")

Answer ``source.call`` from the hand-written samples, from your own data, or with a failure. A call
that no fixture answers raises ``SourceFixtureMissing`` out of ``call_tool``::

    host.source.when("jira.search", jql="project = EXAMPLE").returns([])
    host.source.fail("sheets.get_values", "upstream_error", reason="rate_limited")
    host.source.load(samples.drive_download(b"workbook bytes", revision="r1"))
    # after a call_tool: host.source.calls lists every source.call the plugin made

A tool that is on the ``auto`` gate needs the manifest's ``max_gate_floor: auto``. Tell the host
with ``PluginTestHost(plugin, max_gate_floor="auto")``; without it the host raises
``ToolDefinitionError`` when it starts.
"""
from __future__ import annotations

from ._gate import Card, Rules, ToolOutcome
from ._host import PluginTestHost
from ._source import SourceCall, SourceFixtureMissing, SourceFixtures, samples

__all__ = [
    "Card",
    "PluginTestHost",
    "Rules",
    "SourceCall",
    "SourceFixtureMissing",
    "SourceFixtures",
    "ToolOutcome",
    "samples",
]
