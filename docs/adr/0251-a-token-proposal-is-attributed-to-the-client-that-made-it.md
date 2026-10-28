# ADR 0251: A token proposal is attributed to the client that made it

## Status

Accepted — 2026-10-28. Implemented.
Amends 0006.

## Context

[ADR 0006](0006-attributing-a-request-to-the-ai-system-that-made-it.md) attributes a connector-tool
call to the AI system from the connection, with ranked provenance, and deliberately runs every
meta-tool as `UNKNOWN_AGENT`: a meta-tool call reaches nothing, so naming its caller bought nothing.

`privacyfence_propose_scoped_token` is a meta-tool whose result is a person deciding whether to
delegate access. They need to see which client asked.

## Decision

- A call to `privacyfence_propose_scoped_token` made without a scoped token resolves its agent the way
  a connector-tool call does (`_resolve_agent`). Every other meta-tool stays `UNKNOWN_AGENT`.
- The proposal stores that agent. The review page shows "Proposed by: {label}" and says that the AI
  client reports the name itself and PrivacyFence does not verify it. The audit entries for the
  proposal carry the same agent.
- A scoped token cannot call the tool (0250), so a scoped-token attribution never arises here.

## Alternatives considered

- **Leave the proposing call unattributed under ADR 0006.** Rejected: the person needs to see which
  client asked for the delegation. In local mode every agent reads "Undetected", which is accurate.
- **Present the name as verified.** Rejected: the name is still a claim, and the page says so.

## Consequences

- ADR 0006's "meta-tools are unattributed" now has one exception.
- The label is only as strong as the provenance behind it
  ([ADR 0224](0224-an-org-registration-is-attested-by-its-redirect-uris.md) strengthens it in
  organization mode).

## Verification

`tests/unit/web/test_routes_mcp.py::TestProposeScopedTokenOverTheWire`;
`tests/unit/web/test_routes_mcp_agent_identity.py`; `tests/unit/test_scoped_token_proposals.py`
(`TestReviewData`, `TestRecord`). Code: `src/privacyfence/web/routes_mcp.py`.

## Related

ADR 0006, 0250.
