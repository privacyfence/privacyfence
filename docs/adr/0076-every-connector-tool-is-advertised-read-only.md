# ADR 0076: Every connector tool is advertised to MCP clients as read-only

## Status

Accepted (recorded retroactively on 2026-09-25; decided around 2026-07-06 in `29c30572`, "advertise
all tools as read-only to Claude", and carried into `/mcp` by `930b5719`). Implemented. This is a
workaround, not a settled posture. The open follow-up is
[#46](https://github.com/privacyfence/privacyfence/issues/46), which tracks restoring truthful
annotations once the client side allows it.

## Context

MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are hints. The spec says
so, and clients use them only to decide which permission prompts to show. They are not a security
boundary. PrivacyFence's boundary is `gate.py`: the per-tool gate (`auto`/`review`/`popup`),
auto-accept rules and the audit log, all enforced in the daemon before any external read or write.

With truthful annotations, a write tool carries `destructiveHint = true`. On the Team plan, Claude
then prompts on every call to such a tool and greys out "Allow all for this task", and no org-level
pre-approval is available
([anthropics/claude-ai-mcp#491](https://github.com/anthropics/claude-ai-mcp/issues/491)). The user
would confirm the same write twice: once in the client, where the prompt cannot be configured and
shows little, and once in PrivacyFence's own approval, which shows the real content.

## Decision

`web/mcp_tools.py`'s `to_mcp_tool` gives every connector tool `_UNIFORM_READ_ONLY_ANNOTATIONS`
(`read_only_hint=True`, `destructive_hint=False`, `idempotent_hint=True`), whatever its
`ToolSpec.read_only` says. The tool's real nature stays in `spec.read_only` and the gate tables, and
the gate and the audit trail use those. Only what the client is told is overridden.

PrivacyFence's own meta-tools are declared individually in the same module, and none of them is
marked destructive. The read-only ones (`privacyfence_check_policy`, `privacyfence_list_policy`,
`privacyfence_await_approval`, `privacyfence_status`) say so. `privacyfence_propose_policy_change`,
`privacyfence_begin_unattended_session`, `privacyfence_end_unattended_session` and
`privacyfence_create_upload_slot` carry `read_only_hint=False`.

## Alternatives considered

- **Honest annotations** (derive the hints from `spec.read_only`). Accurate, but it puts a
  redundant confirmation in front of PrivacyFence's gate on every write, with no way for an
  organization to turn it off. It also gains no security: the client prompt shows less than the gate
  and enforces nothing the gate does not. This is the target once the client offers org-level
  pre-approval, as tracked in #46.

## Consequences

- A client that trusts the hints treats PrivacyFence's write tools as safe to call without its own
  prompt. That is accurate only because every such call goes through `gate.py`. A connector tool
  exposed without a gate would be both unprompted and ungated. The gate tables
  (`auto_accept.TOOL_TO_GATE`) are the control, and they must stay complete.
- Anyone who reviews the advertised tool list sees write tools labelled read-only. The docs have to
  say that this is deliberate and temporary, so it is not mistaken for a security control.

## Verification

- `tests/unit/web/test_mcp_tools.py`'s
  `test_every_tool_is_advertised_read_only_regardless_of_the_specs_own_flag`.
- `tests/unit/web/test_routes_mcp.py`'s `test_connector_tool_is_advertised_uniformly_read_only`,
  which checks a live `/mcp` `list_tools`.

## Related

- [#46](https://github.com/privacyfence/privacyfence/issues/46): the open follow-up to restore
  truthful annotations.
- The earlier written rationale, "Why every tool is advertised as read-only":
  `git show 6de7f7cd^:docs/TECHNICAL_REFERENCE.md`. Its short successor paragraph is at
  `git show 5deef1d8:docs/TECHNICAL_REFERENCE.md`, section "Meta-tools". That paragraph also says
  the meta-tools carry the uniform annotations, which the code does not do.
