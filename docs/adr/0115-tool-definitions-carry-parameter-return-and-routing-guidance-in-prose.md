# ADR 0115: Tool definitions carry their parameter, return and routing guidance in prose, and a test enforces it

## Status

Accepted — 2026-09-30. Implemented: `tests/helpers.py`'s `assert_tool_definitions_complete`,
`src/privacyfence/connectors/*.py`'s `tool_specs()`.

## Context

Glama rated the v5.3.0 listing **B** on its Tool Definition Quality Score, which measures how well
each tool's description and input schema help an AI agent pick and call the tool. The weakest
averages were Parameters (2.9/5) and Usage Guidelines (3.2/5). The comments repeat three gaps:
parameters with no description, no sentence saying what the tool returns, and no advice on which
similar tool to use instead. AI clients choose tools from these descriptions, so the gaps cost
correct calls everywhere, not only in the score.

## Decision

Every connector tool description keeps its first sentence as the one-line summary, because the
published tools reference and the website render it. After it come one `Returns` sentence naming the
shape the handler returns, the limits, order and paging the code has, one sentence per related tool
the client could confuse it with, and the approval wording its gate implies (`Auto-approved` or
`Requires user approval`). Every parameter except `reason` has a description of at least 20
characters that gives its format, where the value comes from and what empty or the default means.
Fixed choices are listed in the text.

`assert_tool_definitions_complete` checks this. `tests/unit/web/test_tool_schema_portability.py`
runs it for every connector without a sibling map, and each connector's `TestToolDefinitions` runs
it with that connector's sibling map.

## Alternatives considered

- **A JSON Schema `enum` on fixed-choice parameters** — it would help the Parameters score, but it
  changes `ToolParam`'s shape, and a client that enforces `enum` would reject calls that work today,
  such as a color name in `calendar_set_event_color`.
- **One shared `REASON_PARAM` constant** — a fair cleanup, but it does not affect the score and
  would touch every connector file.
- **Rewriting the first sentences** — several are thin, but they are the published tools reference
  and the website's tool table; the added sentences carry the missing information without churn.

## Consequences

Clients get parameter formats, return shapes and routing advice from the definitions themselves. A
new connector cannot ship with undescribed parameters or a description without a `Returns` sentence.
Descriptions are longer, and stay under the 1024-character limit that OpenAI enforces. The check
cannot guarantee an A from Glama, whose grading is a model's judgement, and Glama re-scores only on
the next release.

## Verification

`tests/helpers.py` (`assert_tool_definitions_complete`),
`tests/unit/web/test_tool_schema_portability.py` (`test_every_connector_tool_definition_is_complete`)
and each connector's `TestToolDefinitions` in `tests/unit/connectors/`.

## Related

[ADR 0114](0114-the-glama-listing-runs-a-tool-catalog-not-a-hosted-privacyfence.md),
[ADR 0089](0089-tool-annotations-are-always-truthful.md).
