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
    # pages: page i answers the cursor page i-1 returned, and a cursor from another query is refused
    host.source.when("jira.search", jql="project = EXAMPLE").returns_pages([[{"key": "A-1"}], [{"key": "A-2"}]])
    # or the two hand-written pages of a sample: host.source.load(samples.get("jira.search", page=2))
    # after a call_tool: host.source.calls lists every source.call the plugin made

Pages, confirmations, events, purge and shutdown go through the same host. A page comes back with
the daemon's security headers; a path the daemon refuses never reaches the plugin::

    async with PluginTestHost(plugin) as host:
        page = await host.get("/")
        assert page.status == 200 and page.headers["x-frame-options"] == "DENY"
        assert (await host.request("POST", "/")).status == 405

        await host.emit("connector.state_changed",
                        {"connector": "gmail", "state": "signed_out", "principal": "local"})
        await host.purge()

        # a plugin that asked a human to confirm something: nothing decides the card until you do
        card = host.confirmations[0]
        await host.decide_confirmation(card.approval_id, "approve")  # or "deny", "expire"

A plugin that asks for an approval that stays approved (``ctx.approvals``) gets a card the test
decides, and the daemon's ``check`` semantics once it did. ``revoke_approval`` takes it back and sends
the plugin ``approval.revoked``::

    ticket = ...  # the plugin called ctx.approvals.request(...)
    card = host.approvals[0]
    await host.decide_approval(card.approval_id, "approve")  # or "deny", "expire"
    await host.revoke_approval(card.approval_id)

A plugin with ``outputs: true`` publishes files with ``ctx.outputs``. Start the host with
``PluginTestHost(plugin, outputs=True, output_types=("text/csv",))``, then look at the folder
(``host.output_dir``) or at what an agent could list::

    assert [f.path for f in host.list_outputs("reports/")] == ["reports/today.csv"]

With pytest, ``pytest_plugins = ["privacyfence_plugin_sdk.testing.pytest"]`` provides a
``plugin_host`` fixture that builds the host: ``async with plugin_host(plugin) as host``.

A tool that is on the ``auto`` gate needs the manifest's ``max_gate_floor: auto``. Tell the host
with ``PluginTestHost(plugin, max_gate_floor="auto")``; without it the host raises
``ToolDefinitionError`` when it starts.
"""
from __future__ import annotations

from ._gate import Card, Rules, ToolOutcome
from ._approvals import Approval
from ._confirm import Confirmation
from ._host import PluginTestHost
from ._outputs import OutputFile
from ._pages import PageResponse
from ._source import SourceCall, SourceFixtureMissing, SourceFixtures, samples

__all__ = [
    "Approval",
    "Card",
    "Confirmation",
    "OutputFile",
    "PageResponse",
    "PluginTestHost",
    "Rules",
    "SourceCall",
    "SourceFixtureMissing",
    "SourceFixtures",
    "ToolOutcome",
    "samples",
]
