# ADR 0122: Plugin tools are gated in two steps, and a read releases the prepared payload

## Status

Accepted — 2026-10-07. Implemented: `src/privacyfence/plugins/connector.py`,
`src/privacyfence/plugins/blocks.py`, `src/privacyfence/plugins/confirm.py`,
`src/privacyfence/plugins/tools.py`, `src/privacyfence/auto_accept.py` (`register_dynamic_tools`),
`src/privacyfence/policy/scopes.py`, `src/privacyfence/policy/propose.py`.
Amended by [ADR 0137](0137-a-plugin-writes-result-is-capped-and-pii-scanned-before-it-reaches-the-ai.md): a write's result is capped and PII-scanned.
Amended by [ADR 0141](0141-plugin-tools-take-files-by-reference-and-privacyfence-passes-the-bytes.md): a tool can take a file by reference.

## Context

A plugin contributes MCP tools, and PrivacyFence must gate them exactly as it gates a connector's:
a human sees what a call would release or do before it happens. Two things are different. The
daemon does not know what a plugin tool does, so the plugin has to say, and a plugin could say one
thing on the card and do another. The tool tables (gates, operations, policy selectors, card
layouts) are static dictionaries built at import, and a plugin's tools exist only at run time.

## Decision

**Two steps.** `tool.prepare` returns typed blocks that describe the call: a preview, and for a
read-only tool the payload it would release, plus the values of each scope type the tool declares.
It has no side effects. The daemon applies the gate with `gated_call`, showing every block on a wide
card, and only after approval sends `tool.execute`.

**Blocks are data, never markup.** The block types are `heading`, `fields`, `table`, `text`, `code`
and `diff`. They are validated on arrival, stripped of control and bidirectional-override
characters, capped in count, size and cell length, and rendered by PrivacyFence's own escaping
renderer. The card's metadata dictionary carries only the plugin's display name and the tool's
title, never plugin content, because notifications read it.

**A read releases the prepared payload.** The human approved exactly those blocks, so whatever
`tool.execute` returns for a read-only tool is ignored and the AI receives the prepared payload. A
plugin that returned something else from `execute` would bypass the gate. The payload's PII scan
runs on its flattened text.

**An approval belongs to one prepared call.** The gate's decision-ledger key carries the prepared
call's id, so the ledger ([ADR 0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md))
replays a decision only to a repeat call that reuses that same prepared call. A fresh `prepare`
always gets its own card, so a released payload is always the one a human saw, also after the
plugin restarts or a tool is removed and added again. A prepared call is kept for the card's pending
lifetime plus the ledger's replay window, both measured from when `prepare` returned; an approved or
denied read is kept for one more replay window after it is decided, so a repeat call in that window
gets the same payload, or the same denial, with no new card. Two identical calls racing share one
`prepare`. An approved write's prepared call is dropped before `execute` is sent, and `execute` is
never retried, so a second identical call prepares afresh and gets its own card.

**Floors are the daemon's.** Tool definitions are validated as a whole and refused on the first
violation, whether they arrive at start or in `tools.changed`: names fit the MCP name limit and do
not collide with a built-in tool; parameters are scalars only (`string`, `integer`, `number`,
`boolean`), because the connector parameter model and the MCP schema builder carry scalars, and
fixed choices go in the description as [ADR 0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md)
decides; a destructive tool must use the `popup` gate; an `auto` tool needs the manifest's
`max_gate_floor: auto` ([ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)).
Gated tools get the same required `reason` parameter every connector tool has.

**Policy tables are filled and emptied at run time.** `register_dynamic_tools` writes a plugin's
tools into the gate, operation, verb, layout, write-effect and scope tables under an owner, refuses
a name that is static or owned by another plugin, and removes exactly what it added when the
plugin's list changes or the plugin stops. Static entries are never touched. Gated plugin tools use
the wide card layout so the preview pane shows the blocks.

**Rules use `plugin:<name>:<scope>` predicates.** A scope rule matches only when every value a call
returned for that scope type is in the rule, and a missing, empty or unlisted value never matches.
A tool with no scope types offers a rule under `plugin:<name>:anything`, never the shared
`always_allow` predicate, which would merge with and be removed together with the Gmail and
Calendar rows. A proposal always covers exactly one operation key. A destructive tool gets no
proposal at all, so there is no one-click "Always allow" for a delete. Plugin scopes are not in the
policy catalogue, so `privacyfence_propose_policy_change` cannot write them; only the card's button
does.

**Confirmations are cards no rule can accept.** `confirm.request` registers a confirm card with the
plugin's blocks, with no operation key, so no rule can accept it. It keeps passkey step-up unless
the plugin turns it off. It is refused while any MCP session is unattended, because the request
comes from the plugin process and the daemon cannot tell which session caused it. The plugin host
finalizes the card when the human answers, which is what `confirm.await` and
`privacyfence_await_approval` read. At most 64 confirmations may be pending, each with a waiting
thread.

## Alternatives considered

- **Gate by the plugin's own declaration with no preview.** Rejected. The card must show what a call
  does, and the plugin is the only party that knows.
- **Trust `tool.execute`'s result for a read.** Rejected. It lets a plugin release anything the
  human did not see.
- **Let plugins send HTML for the card.** Rejected. Typed blocks cannot inject script or restyle
  the approval card, and PrivacyFence escapes every string itself.
- **Arrays, objects and enums in tool parameters.** Rejected. Connector tools carry scalars only;
  structured input is a JSON string the plugin parses.
- **Reuse the shared `always_allow` rule for unscoped tools.** Rejected. Rules would merge across
  plugins and connectors and be removed together.
- **A rule for every tool, destructive ones included.** Rejected. A one-click rule for a delete is
  the wrong default.

## Consequences

- Every plugin read costs two plugin round trips and a card, unless a scope rule or the `auto`
  floor applies.
- A rule written for one plugin tool's scope never covers another operation or plugin.
- A plugin's `effect` sentence for a write is per tool, not per call, as the card builder reads it
  per tool.
- Rule accept is the only way a plugin call skips its card; the daemon reports the approval to the
  plugin as made by the card either way.

## Verification

`tests/unit/plugins/test_connector.py`, `tests/unit/plugins/test_tools.py`,
`tests/unit/plugins/test_blocks.py`, `tests/unit/plugins/test_confirm.py`,
`tests/unit/test_auto_accept.py` (the dynamic tool tables), `tests/unit/policy/test_plugin_scopes.py`
(scope matching and the property that a proposal matches the call it came from and not one more
value), `tests/integration/test_plugin_framework.py`, and the browser tests
`tests/integration/test_plugin_card_escaping_browser.py`.

## Related

- [The tracking issue](https://github.com/privacyfence/privacyfence/issues/846)
- [ADR 0073](0073-an-approved-write-is-single-use-and-an-approved-read-replays.md)
- [ADR 0115](0115-tool-definitions-carry-parameter-return-and-routing-guidance-in-prose.md)
- [ADR 0120](0120-plugins-are-out-of-process-executables-speaking-json-rpc-over-stdio.md)
- [ADR 0121](0121-a-plugin-is-trusted-code-installed-by-an-administrator-into-an-admin-only-directory.md)
