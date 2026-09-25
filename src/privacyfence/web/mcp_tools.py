"""Tool manifest translation for the ``/mcp`` endpoint (routes_mcp.py).

Ports bridge/src/tools.ts's schema mapping (``paramSchema``/
``buildInputShape``/``UNIFORM_READ_ONLY_ANNOTATIONS``/``toCallToolResult``)
from zod/TypeScript into JSON Schema/Python -- a translation of an existing,
shipped mapping, not a redesign.
``ToolSpec.to_dict()`` (connector.py) stays the single source of truth for
what a tool *is*; this module only decides how that shape is presented to an
MCP client.

Also carries PrivacyFence's own meta-tools (privacyfence_check_policy and
friends) -- not sourced from any connector's manifest, ported field-for-field
from bridge/src/tools.ts's ``registerMetaTools`` (same names, same
descriptions, same input shapes) since routes_mcp.py replaces the bridge as
the thing serving them, not what they are (§8.1: "the other three move into
web/routes_mcp.py against the connector registry directly"). One exception has no bridge-era
counterpart: ``PRIVACYFENCE_STATUS_TOOL`` (issue #396), the meta-tool that
tells a client an install is not set up yet. It had a companion --
``GET_SIGN_IN_LINK_TOOL``, added once P10 had left local mode's web UI
headless with no menu bar link of its own -- which minted a live sign-in
link and handed it to the agent. The self-approval plan's Phase 2 retired
it: its own justification ("nothing installs or starts the companion
automatically yet") expired when ADR 0003 made the companion mandatory on
all three platforms, and a session is no longer something to hand the party
it governs (web/session_auth.py's ``PROVENANCE_*``). What is left in its
place is the companion itself, and ``privacyfence-app
--print-sign-in-link`` for a human whose companion menu is out of reach.
"""
from __future__ import annotations

import json
from typing import Any

from mcp import types

from ..connector import ToolSpec

# Same rationale as bridge/src/tools.ts's UNIFORM_READ_ONLY_ANNOTATIONS: MCP
# tool annotations are UI hints, not a security boundary (the spec says so
# explicitly). The real authorization is gate.py's gate -- auto/review/popup,
# auto-accept rules, the audit log -- enforced here in the daemon itself, not
# in the calling client. Advertising every tool uniformly as read-only/
# non-destructive keeps the client from throwing its own redundant
# confirmation prompt in front of gate.py's real one (see
# 5deef1d8:docs/TECHNICAL_REFERENCE.md's "Why every tool is advertised as read-only",
# referenced by §8.1 of the refactor plan).
_UNIFORM_READ_ONLY_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True,
)

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
        annotations=_UNIFORM_READ_ONLY_ANNOTATIONS,
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
# Meta-tools -- ported verbatim (name, description, schema) from
# bridge/src/tools.ts's registerMetaTools(). See routes_mcp.py for the
# handlers dispatching these to gate.py/auto_accept.py.
# --------------------------------------------------------------------------- #

CHECK_POLICY_TOOL = types.Tool(
    name="privacyfence_check_policy",
    description=(
        "Ask PrivacyFence, before calling a gated tool, whether that specific call would "
        "auto-accept or need a human. Pass the same connector, tool, and args you're about "
        "to call, plus reason: one sentence on why you're checking this right now (logged, "
        "self-reported, unverified -- same as every gated tool's reason param). Returns "
        "{gate, verdict, matched_rule, matched_rule_id, reason, pii_gate_may_apply}, where "
        "verdict is one of: 'auto_accept' (the real call will pass through identically), "
        "'requires_review' (no configured rule can match these arguments, with or without "
        "fetching anything), or 'unknown' (whether it auto-accepts depends on the actual "
        "fetched content, which this can't see in advance). matched_rule_id is null unless "
        "verdict is 'auto_accept': when set, it's the exact rule id privacyfence_list_policy "
        "lists for whatever will let this call through -- pass it straight to "
        "privacyfence_propose_policy_change's rule_id if you also want to narrow or remove that "
        "rule. For 'review'-gated (read) tools, pii_gate_may_apply is always true: PrivacyFence's "
        "PII detection gate scans real content and can force a popup even when a rule matches, "
        "and that can never be predicted ahead of time. This makes no external API call, opens no "
        "popup, and has no side effects -- call it as often as you want while planning a task. "
        "Most useful before and during a scheduled/unattended Cowork run, to plan around steps "
        "that would otherwise need a human who isn't there."
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
        "List every auto-accept rule currently configured under PrivacyFence's policy engine "
        "(the redesigned, single scope+verb vocabulary that replaces the older rule/grant split -- "
        "see privacyfence_propose_policy_change for the write side), plus the scope catalogue that "
        "tool accepts. Returns {rules, scope_groups}. Each entry of rules carries: id (pass this "
        "to privacyfence_propose_policy_change's rule_id to update or remove exactly this rule), "
        "sentence (a human-readable rendering, e.g. \"Drive - folder 1CdeF...: allow read, update, "
        "format\"), connector, scope_type, value, operations (the engine's own internal keys -- "
        "informational only), verbs (the same rule stated as {verb, family} pairs -- family is one "
        "of 'read'/'write'/'send'/'destructive', so a rule granting delete or send is visible "
        "without reading the sentence closely), conditions, and covered_tools (every real tool "
        "name this rule can auto-accept -- one operation, or several, stated plainly rather than "
        "left for you to infer from the operation keys). scope_groups is the catalogue "
        "privacyfence_propose_policy_change's own group/verbs validates against: each entry gives "
        "an id (pass as group), the verbs that scope type can actually govern (pass a subset as "
        "verbs -- naming one this list doesn't include is rejected before any popup is shown), "
        "and whether it needs a value at all. Call this before proposing a change: "
        "an id or group only matches something real if you listed it first rather than guessed. "
        "Read-only, no popup -- reason: one sentence on why you're listing the current policy "
        "right now (logged, self-reported, same as every other gated/meta tool's reason param, "
        "since this discloses the full current rule set)."
    ),
    input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)

PROPOSE_POLICY_CHANGE_TOOL = types.Tool(
    name="privacyfence_propose_policy_change",
    description=(
        "Propose adding, updating, or removing an auto-accept rule under PrivacyFence's policy "
        "engine -- one rule shape (a scope plus the verbs it allows). This ALWAYS blocks on a "
        "native confirmation dialog a human must approve -- there is no way to change this "
        "config without one, even if an identical rule already exists. If declined, or if this "
        "connection is in an unattended session, the call throws "
        "-- never assume success without checking the result. Call privacyfence_list_policy "
        "first: group/verbs/rule_id only match something real if you listed them rather than "
        "guessed, and a verb a scope type cannot govern (one privacyfence_list_policy's own "
        "scope_groups doesn't list for that group) is rejected here, before any popup is shown, "
        "rather than silently stored as a rule nothing could ever render or remove.\n\n"
        "operation='add' or 'update' need group (one of privacyfence_list_policy's scope_groups "
        "ids, e.g. 'drive.folder'), value (a list of resource ids/names the scope matches, e.g. a "
        "Drive folder id or a sender domain -- omit for a value-less scope like \"if I own it\", "
        "which scope_groups' own needs_value tells you), and verbs (a non-empty list from that "
        "group's own verbs, e.g. ['read', 'update']). operation='update' additionally takes "
        "rule_id (the existing rule being replaced -- removed, then re-added under the new "
        "group/value/verbs, since rules are additive by construction). operation='remove' needs "
        "only rule_id.\n\n"
        "Returns {confirmed, changed, description, rule_ids}: changed is false when the human "
        "confirmed but nothing on disk actually differed (e.g. removing a rule id already gone); "
        "rule_ids names the rule(s) actually affected, for a follow-up privacyfence_list_policy "
        "or propose call to target directly.\n\n"
        "reason: one sentence on why you're proposing this change -- logged, self-reported, "
        "unverified, same as every other gated tool's reason param."
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
        "Long-poll one or more pending approvals returned by a gated tool call's "
        "{status: 'approval_pending', approval_id, pending_count, binder_url, url, ...} result, "
        "and report their status -- status only, never "
        "content. Passing every outstanding approval_id in one call is the expected use, not a "
        "fallback for an edge case: if pending_count on the latest pending result is greater than "
        "one, issue whatever other gated calls are independently ready rather than waiting on this "
        "one first, then call this tool once with all of those approval_ids together -- one human "
        "decision pass over the whole batch (the result's binder_url) instead of one prompt per "
        "call. Before your first call to this tool for a given approval, make sure the human has "
        "actually been told: relay that result's own message and url (or binder_url, once there's "
        "more than one) to the user in your reply so they know a decision is waiting on them -- do "
        "not call this (or anything else) silently first. Pass every approval_id "
        "you're waiting on, plus timeout_seconds (how long to wait before returning regardless of "
        "outcome; keep this comfortably under your own client's tool-call timeout -- it's capped "
        "server-side regardless). Returns {approval_id: status}, one of: 'pending' (still "
        "undecided -- if your environment can schedule a follow-up (a reminder, a background check, "
        "a cron-style trigger), schedule one to call this again in a bit rather than blocking the "
        "conversation on a long wait; otherwise call it again with a fresh timeout), 'approved' (a "
        "human said yes -- re-issue the ORIGINAL gated tool call with the exact same arguments to "
        "actually receive the data; this tool never returns content itself, there is no other way "
        "to collect it), 'denied' (a human said no -- re-issuing will not change that), 'expired' "
        "(nobody decided in time -- re-issuing starts a fresh approval, not a retry of the old one), "
        "or 'unknown' (not a real approval_id this connection can see -- wrong id, or it belongs to "
        "a different install). Prefer this over polling the same original tool call repeatedly: it "
        "returns as soon as any status changes, or once timeout_seconds elapses, whichever comes "
        "first."
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
        "Check whether THIS PrivacyFence install is actually set up -- call this before the "
        "first PrivacyFence-governed action in a conversation, or whenever a human asks why a "
        "connector (gmail_*, drive_*, slack_*, ...) isn't available. An empty or partial tool "
        "list from this server means connectors aren't authenticated yet, NOT that PrivacyFence "
        "has nothing to do with the current request -- this is the one tool guaranteed to exist "
        "even when every other tool is missing. Returns {mode, setup_complete, connectors, "
        "next_step, message} and, whenever setup isn't complete, sign_in_url: mode is 'local' or "
        "'org'; connectors is a list of {name, enabled, authenticated, blocked_by} (blocked_by is "
        "null once authenticated or if a human deliberately disabled it, otherwise "
        "'no_org_config' -- never configured -- 'not_authenticated' -- never signed in or the "
        "token expired -- or a short redacted reason); setup_complete is true once at least one "
        "connector is authenticated; next_step and message tell the model what to do next in "
        "plain language -- relay message to the human as-is when setup isn't complete. This tool "
        "never mints a sign-in credential itself, and no tool on this server does any more, so "
        "sign_in_url is always null here. In local mode when un-onboarded, next_step is "
        "'open_privacyfence_companion' -- tell the human to open PrivacyFence's companion app "
        "(the menu-bar/tray icon on macOS and Windows, the PrivacyFence entry in the "
        "applications menu on Linux) and choose Open Settings; you cannot do this for them, and "
        "there is no link for you to hand them. In "
        "org mode it's 'contact_your_administrator' -- org mode signs in through its own IdP and "
        "has no local link to offer at all. Makes no external API call and has no side effects "
        "other than its own audit entry. reason: one sentence on why this is being checked right "
        "now -- logged, self-reported, unverified, same as every other meta tool's reason param, "
        "since this discloses which connectors are authenticated."
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
        "Get a URL to upload a local file's bytes to PrivacyFence directly, for a client with "
        "no PrivacyFence extension (Claude Code, or any other direct HTTP MCP client) -- the "
        "way forward when a tool's local_path/attachments parameter fails with a message about "
        "PrivacyFence being unable to read files in your home folder directly. Returns "
        "{upload_id, upload_url, method: 'PUT', max_bytes, expires_at, example}: PUT the file's "
        "raw bytes to upload_url (e.g. the shown curl -T example) -- no Authorization header or "
        "anything else is needed, the URL itself is the one-time credential. Once the upload "
        "succeeds, pass upload_id back to the tool that needed the file (as its own upload_id "
        "parameter, e.g. drive_upload_file, or as an 'upload:<upload_id>' entry in a tool's "
        "attachments list, e.g. the gmail_*_with_attachments tools) -- do not try to fetch "
        "upload_url yourself, and do not pass it as local_path. The slot is single-use, expires "
        "10 minutes after this call, and can only ever be claimed by the tool call you make "
        "next in this same conversation -- calling this tool does not itself upload, gate, "
        "preview, or approve anything; that all still happens when the file's actual "
        "destination tool runs. reason: one sentence on why this file is needed right now -- "
        "logged, self-reported, unverified, same as every other meta tool's reason param."
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
