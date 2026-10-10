"""Tool manifest translation for the ``/mcp`` endpoint (routes_mcp.py).

Ports bridge/src/tools.ts's schema mapping (``paramSchema``/
``buildInputShape``/``toCallToolResult``)
from zod/TypeScript into JSON Schema/Python -- a translation of an existing,
shipped mapping, not a redesign.
``ToolSpec.to_dict()`` (connector.py) stays the single source of truth for
what a tool *is*; this module only decides how that shape is presented to an
MCP client.

Also carries PrivacyFence's own meta-tools (privacyfence_check_policy and
friends) -- not sourced from any connector's manifest, ported field-for-field
from bridge/src/tools.ts's ``registerMetaTools`` (same names, same
descriptions, same input shapes) since routes_mcp.py replaces the bridge as
the thing serving them, not what they are. One exception has no bridge-era
counterpart: ``PRIVACYFENCE_STATUS_TOOL``, the meta-tool that
tells a client an install is not set up yet. There is deliberately no
meta-tool that mints a sign-in link for the agent (ADR 0013): ADR 0003 makes
the companion mandatory on all three platforms, and a session is not
something to hand the party it governs (web/session_auth.py's
``PROVENANCE_*``). What stands in for one is the companion itself, and ``privacyfence-app
--print-sign-in-link`` for a human whose companion menu is out of reach.
"""
from __future__ import annotations

import json
from typing import Any

from mcp import types

from ..connector import ToolSpec

# MCP tool annotations are UI hints, not a security boundary (the spec says so
# explicitly). The real authorization is gate.py's gate -- auto/review/popup,
# auto-accept rules, the audit log -- enforced here in the daemon itself, not
# in the calling client. The hints are still something PrivacyFence *tells* a
# client, so they are always true: a read is read-only and idempotent, a write
# is neither, and only a tool whose ToolSpec says it deletes something is
# destructive. There is no mode that says otherwise (ADR 0089, removing the
# all-read-only option ADR 0086 kept and ADR 0087 shipped; ADR 0076 was the
# original uniform-read-only workaround). A user who does not want the client's
# own confirmation in front of gate.py's always-allows PrivacyFence's tools in
# the client; see https://github.com/privacyfence/privacyfence/issues/46.
_READ_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True,
)
_WRITE_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False,
)
_DESTRUCTIVE_WRITE_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False,
)


def tool_annotations(spec: ToolSpec) -> types.ToolAnnotations:
    if spec.read_only:
        return _READ_ANNOTATIONS
    return _DESTRUCTIVE_WRITE_ANNOTATIONS if spec.destructive else _WRITE_ANNOTATIONS


_JSON_SCHEMA_TYPE = {"int": "integer", "float": "number", "bool": "boolean"}


def _param_schema(annotation: str, description: str) -> dict[str, Any]:
    # Unknown annotation types fall back to string -- mirrors tools.ts's own
    # `case "str": default:` fallthrough.
    schema: dict[str, Any] = {"type": _JSON_SCHEMA_TYPE.get(annotation, "string")}
    if description:
        schema["description"] = description
    return schema


def tool_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """JSON Schema for ``spec``'s params -- the Python-side equivalent of
    tools.ts's ``buildInputShape``. An optional param with a non-null
    default carries it forward as the schema's own ``default`` (informational
    for the client; PrivacyFence still applies the connector's own default
    when the arg is actually omitted)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in spec.params:
        schema = _param_schema(p.annotation, p.description)
        if not p.required and p.default is not None:
            schema["default"] = p.default
        properties[p.name] = schema
        if p.required:
            required.append(p.name)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def to_mcp_tool(spec: ToolSpec) -> types.Tool:
    return types.Tool(
        name=spec.name,
        description=spec.description,
        input_schema=tool_input_schema(spec),
        annotations=tool_annotations(spec),
    )


def to_call_tool_result(value: Any) -> types.CallToolResult:
    """Mirrors tools.ts's ``toCallToolResult``: a string result becomes plain
    text; any other JSON-serializable value is also rendered as text (so
    there's always something readable) and, when it's a plain dict, also
    attached as ``structuredContent`` -- the same "no explicit output schema"
    case fastmcp's own default ``convert_result`` handles that way."""
    if value is None:
        return types.CallToolResult(content=[])
    if isinstance(value, str):
        return types.CallToolResult(content=[types.TextContent(type="text", text=value)])
    text = json.dumps(value, default=str)
    content: list[types.ContentBlock] = [types.TextContent(type="text", text=text)]
    if isinstance(value, dict):
        return types.CallToolResult(content=content, structured_content=value)
    return types.CallToolResult(content=content)


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


# --------------------------------------------------------------------------- #
# Meta-tools. See routes_mcp.py for the handlers dispatching these to
# gate.py/auto_accept.py. Every description stays within 1024 characters,
# the strictest client limit (test_tool_schema_portability.py).
# --------------------------------------------------------------------------- #

CHECK_POLICY_TOOL = types.Tool(
    name="privacyfence_check_policy",
    description=(
        "Before calling a gated tool, ask whether that exact call would auto-accept or need a human. "
        "Pass the connector, tool and args you're about to call. Returns {gate, verdict, "
        "matched_rule, matched_rule_id, reason, pii_gate_may_apply}. verdict is 'auto_accept' (the "
        "real call will pass through identically), 'requires_review' (no configured rule can match "
        "these args), or 'unknown' (it depends on fetched content this can't see in advance). "
        "matched_rule_id is set only for 'auto_accept': the privacyfence_list_policy rule id that "
        "lets this call through, usable as privacyfence_propose_policy_change's rule_id. For "
        "'review'-gated (read) tools pii_gate_may_apply is always true: the PII gate scans real "
        "content and can force a popup even when a rule matches, which can't be predicted. No "
        "external API call, no popup, no side effects -- call it freely while planning, especially "
        "before and during an unattended run. reason: one sentence on why you're checking now "
        "(logged, self-reported, unverified)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "connector": {"type": "string"},
            "tool": {"type": "string"},
            "reason": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["connector", "tool", "reason"],
    },
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)

LIST_POLICY_TOOL = types.Tool(
    name="privacyfence_list_policy",
    description=(
        "List every configured auto-accept rule, plus the scope catalogue "
        "privacyfence_propose_policy_change accepts. Returns {rules, scope_groups}. Each rule has: id "
        "(pass as privacyfence_propose_policy_change's rule_id to update or remove it), sentence "
        "(human-readable), connector, scope_type, value, operations (internal keys, informational), "
        "verbs ({verb, family} pairs; family is 'read', 'write', 'send' or 'destructive'), "
        "conditions, and covered_tools (every tool name the rule can auto-accept). Each scope_groups "
        "entry has an id (pass as group), the verbs that scope can govern (pass a subset as verbs; "
        "any other verb is rejected before a popup), and whether it needs a value. Call this before "
        "proposing a change: an id or group only matches something real if you listed it rather than "
        "guessed. Read-only, no popup. reason: one sentence on why you're listing the policy now "
        "(logged, self-reported; this discloses the full rule set)."
    ),
    input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)

PROPOSE_POLICY_CHANGE_TOOL = types.Tool(
    name="privacyfence_propose_policy_change",
    description=(
        "Propose adding, updating or removing an auto-accept rule (a scope plus allowed verbs). "
        "ALWAYS blocks on a dialog a human must approve. If declined, or in an unattended session, it "
        "errors -- check the result, never assume success. Call privacyfence_list_policy first: "
        "group, verbs and rule_id must be listed, not guessed; a verb not in that group's "
        "scope_groups entry is rejected before any popup.\n\n"
        "operation='add'/'update' need group (a scope_groups id, e.g. 'drive.folder'), value "
        "(resource ids/names the scope matches, e.g. a Drive folder id; omit when the group's "
        "needs_value is false) and verbs (a non-empty subset of the group's verbs, e.g. ['read', "
        "'update']). 'update' also takes rule_id (that rule is removed, then re-added). 'remove' "
        "needs only rule_id.\n\n"
        "Returns {confirmed, changed, description, rule_ids}: changed is false when confirmed but "
        "nothing differed; rule_ids names the rules affected.\n\n"
        "reason: one sentence on why you're proposing this (logged, unverified)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["add", "update", "remove"]},
            "reason": {"type": "string"},
            "rule_id": {"type": "string"},
            "group": {"type": "string"},
            "value": {"type": "array", "items": {"type": "string"}},
            "verbs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["operation", "reason"],
    },
    annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)

BEGIN_UNATTENDED_SESSION_TOOL = types.Tool(
    name="privacyfence_begin_unattended_session",
    description=(
        "Tell PrivacyFence this conversation is an unattended/scheduled Cowork run (e.g. a "
        "Routine firing on a schedule) with no human necessarily watching, for the rest of "
        "this connection. From then on, any gated tool call that isn't already covered by a "
        "configured auto-accept rule is denied immediately with a clear error, instead of "
        "PrivacyFence opening a native approval dialog that nobody will answer. Call this once "
        "at the start of a scheduled run, and pair it with privacyfence_check_policy to plan "
        "which steps are safe to attempt. Never changes what auto-accepts, only what happens "
        "when nothing does. Errors if an administrator hasn't enabled unattended sessions for "
        "this install. Do not call this during a normal interactive conversation -- it makes "
        "denials immediate instead of prompting. reason: one sentence on why this session is "
        "unattended (e.g. the Routine/schedule that triggered it) -- logged in the audit "
        "entry for this session change, since no popup is shown for it to appear in."
    ),
    input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)

AWAIT_APPROVAL_TOOL = types.Tool(
    name="privacyfence_await_approval",
    description=(
        "Long-poll pending approvals from gated calls' {status: 'approval_pending', approval_id, ...} "
        "results; status only, never content. Before the first call for an approval, relay that "
        "result's message and url (binder_url if several) to the user -- never wait silently. If "
        "pending_count > 1, first issue your other ready gated calls, then pass all approval_ids in "
        "one call (one human pass). Keep timeout_seconds under your client's tool-call timeout. "
        "Returns {approval_id: status}: 'pending' (schedule a follow-up if you can, else call again), "
        "'approved' (re-issue the ORIGINAL call with identical arguments -- the only way to get the data), "
        "'denied' (a human said no -- re-issuing will not change that; "
        "don't retry, ask the user how to proceed unless denial_feedback says otherwise), 'expired' "
        "(re-issuing starts a fresh approval) or 'unknown' (no such id here). denial_feedback holds "
        "the user's instruction for a denial: follow it. Returns on any change or at the timeout. "
        "Prefer this over re-issuing the original call to poll."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "approval_ids": {"type": "array", "items": {"type": "string"}},
            "timeout_seconds": {"type": "integer"},
        },
        "required": ["approval_ids"],
    },
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)

PRIVACYFENCE_STATUS_TOOL = types.Tool(
    name="privacyfence_status",
    description=(
        "Check whether THIS PrivacyFence install is set up: call it before the first "
        "PrivacyFence-governed action in a conversation, or when asked why a connector (gmail_*, "
        "...) is missing. An empty or partial tool list means connectors aren't authenticated yet, "
        "NOT that PrivacyFence is irrelevant -- this is the one tool guaranteed to exist even when "
        "every other tool is missing. Returns {mode ('local'/'org'), setup_complete (any connector "
        "authenticated), connectors [{name, enabled, authenticated, blocked_by: null, "
        "'no_org_config', 'not_authenticated' or a reason}], next_step, message, sign_in_url "
        "(always null)}. If setup isn't complete, relay message to the human as-is. next_step "
        "'open_privacyfence_companion' (local): the human opens PrivacyFence's companion app "
        "(menu-bar/tray icon; Linux: applications menu) and chooses Open Settings; you can't, and "
        "have no link. 'contact_your_administrator' (org): sign-in is via the org's IdP. Only side "
        "effect: an audit entry. reason: one sentence on why you're checking now (logged)."
    ),
    input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)

END_UNATTENDED_SESSION_TOOL = types.Tool(
    name="privacyfence_end_unattended_session",
    description=(
        "Clear the unattended-session flag set by privacyfence_begin_unattended_session for "
        "this connection, restoring normal interactive approval behavior. Call this when a "
        "scheduled run finishes. Not strictly required -- the flag also clears automatically "
        "when the connection closes -- but call it if this connection might be reused "
        "afterward for something interactive. reason: one sentence on why the unattended "
        "session is ending now -- logged the same way as privacyfence_begin_unattended_session's."
    ),
    input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)

CREATE_UPLOAD_SLOT_TOOL = types.Tool(
    name="privacyfence_create_upload_slot",
    description=(
        "Get a one-time URL to upload a local file to PrivacyFence, for a client without the "
        "PrivacyFence extension (e.g. Claude Code) -- use it when a tool's local_path/attachments "
        "parameter says PrivacyFence can't read your files directly. Returns {upload_id, upload_url, "
        "method: 'PUT', max_bytes, expires_at, example}: PUT the raw bytes to upload_url (e.g. the "
        "curl -T example); no Authorization header -- the URL is the credential. Then pass upload_id "
        "to the tool that needs the file (its upload_id parameter, e.g. drive_upload_file; an "
        "'upload:<upload_id>' attachments entry, e.g. gmail_*_with_attachments; or "
        "'upload:<upload_id>' as a plugin tool's file parameter). Never fetch "
        "upload_url yourself or pass it as local_path. Single-use, expires in 10 minutes, claimable "
        "only by your next tool call in this conversation. This uploads, gates and approves nothing: "
        "that happens when the destination tool runs. reason: one sentence on why this file is needed "
        "now (logged, self-reported, unverified)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["filename", "reason"],
    },
    annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False),
)

META_TOOLS: tuple[types.Tool, ...] = (
    CHECK_POLICY_TOOL,
    LIST_POLICY_TOOL,
    PROPOSE_POLICY_CHANGE_TOOL,
    BEGIN_UNATTENDED_SESSION_TOOL,
    END_UNATTENDED_SESSION_TOOL,
    AWAIT_APPROVAL_TOOL,
    PRIVACYFENCE_STATUS_TOOL,
    CREATE_UPLOAD_SLOT_TOOL,
)
META_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in META_TOOLS)
