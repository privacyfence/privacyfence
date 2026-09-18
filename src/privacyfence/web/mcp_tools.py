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
web/routes_mcp.py against the connector registry directly"). Two exceptions
have no bridge-era counterpart, both added once P10 (web/server.py's own
module docstring) had left local mode's web UI headless with no menu bar
link of its own to fall back on: ``GET_SIGN_IN_LINK_TOOL`` and, later
(issue #396), ``PRIVACYFENCE_STATUS_TOOL`` -- the one meta-tool that tells
a client *why* it might need that link in the first place.
"""
from __future__ import annotations

import json
from typing import Any

from mcp import types

from ..connector import ToolSpec
from .session_auth import BOOTSTRAP_TTL_SECONDS

# Same rationale as bridge/src/tools.ts's UNIFORM_READ_ONLY_ANNOTATIONS: MCP
# tool annotations are UI hints, not a security boundary (the spec says so
# explicitly). The real authorization is gate.py's gate -- auto/review/popup,
# auto-accept rules, the audit log -- enforced here in the daemon itself, not
# in the calling client. Advertising every tool uniformly as read-only/
# non-destructive keeps the client from throwing its own redundant
# confirmation prompt in front of gate.py's real one (see
# TECHNICAL_REFERENCE.md's "Why every tool is advertised as read-only",
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


def sign_in_link_result(value: dict[str, str]) -> types.CallToolResult:
    """privacyfence_get_sign_in_link's own result shape -- everything else
    goes through the generic ``to_call_tool_result`` above, whose text
    content is a raw ``json.dumps({"url": ...})`` blob a human has to pick
    the link out of by hand. This tool exists specifically to hand a human a
    link to click, so its text content is a single markdown link instead:
    any client that renders tool text as markdown (most chat clients do)
    shows it as something clickable rather than JSON to copy-paste from. The
    link text itself names the expiry (``BootstrapStore``'s TTL, session_auth.py)
    rather than a second, separate sentence -- one string a human can still
    act on correctly if only the link text survives into a screenshot or a
    shared transcript, instead of a bare URL with no context once separated
    from an explanation next to it. ``structuredContent`` is unchanged --
    still the plain ``{"url": ...}`` dict, for a client that reads that
    instead of the text."""
    url = value["url"]
    minutes = BOOTSTRAP_TTL_SECONDS // 60
    text = f"[Sign in to PrivacyFence]({url}) — one-time link, expires in {minutes} minutes"
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], structured_content=value,
    )


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
        "{gate, verdict, matched_rule, reason, pii_gate_may_apply}, where "
        "verdict is one of: 'auto_accept' (the real call will pass through identically), "
        "'requires_review' (no configured rule can match these arguments, with or without "
        "fetching anything), or 'unknown' (whether it auto-accepts depends on the actual "
        "fetched content, which this can't see in advance). For 'review'-gated (read) tools, "
        "pii_gate_may_apply is always true: PrivacyFence's PII detection gate scans real "
        "content and can force a popup even when a rule matches, and that can never be "
        "predicted ahead of time. This makes no external API call, opens no popup, and has "
        "no side effects -- call it as often as you want while planning a task. Most useful "
        "before and during a scheduled/unattended Cowork run, to plan around steps that would "
        "otherwise need a human who isn't there."
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

LIST_RULES_TOOL = types.Tool(
    name="privacyfence_list_auto_accept_rules",
    description=(
        "List the auto-accept rules and grants currently configured in PrivacyFence's "
        "settings.yaml -- both the auto_accept_rules section (per-operation rule entries) and "
        "the auto_accept_grants section (resource-scoped grants, e.g. a trusted Drive sandbox "
        "folder that covers several sheets.*/drive.* operations at once). Call this before "
        "privacyfence_propose_auto_accept_rule_change: update/remove target an existing entry "
        "by its exact identifying fields (operation_key/rule_name/value for a rule; "
        "connector/config_key/resource_id for a grant), and those fields only match something "
        "if you listed it first rather than guessed. Read-only, no popup -- reason: one "
        "sentence on why you're listing the current rules right now (logged, self-reported, "
        "same as every other gated/meta tool's reason param, since this discloses the full "
        "current rule set)."
    ),
    input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)

PROPOSE_RULE_CHANGE_TOOL = types.Tool(
    name="privacyfence_propose_auto_accept_rule_change",
    description=(
        "Propose adding, updating, or removing an auto-accept rule or grant in PrivacyFence's "
        "settings.yaml. This ALWAYS blocks on a native confirmation dialog a human must "
        "approve -- there is no way to change this config without one, even if an identical "
        "entry already exists. If declined, or if this connection is in an unattended "
        "session, the call throws -- never assume success without checking the result. Call "
        "privacyfence_list_auto_accept_rules first so update/remove target an entry that "
        "actually exists rather than guessing identifiers.\n\n"
        "target='rule' edits the auto_accept_rules section (one list of {rule, value} entries "
        "per operation_key): operation_key (e.g. 'sheets.format_range'), rule_name (e.g. "
        "'trusted_sender_domain' -- must be one of the real rule names PrivacyFence's rule engine "
        "knows, see privacyfence_list_auto_accept_rules' output or the Auto-accept rules tables in "
        "the docs; an unrecognized name is rejected before any popup is shown, not silently "
        "persisted as a dead rule), value (required for add/update -- often a list), old_value "
        "(update only -- the prior value being replaced; omit to add alongside the existing "
        "value instead of replacing it).\n\n"
        "target='grant' edits the auto_accept_grants section (one resource trusted once, "
        "covering several operations at a time -- e.g. a Drive sandbox folder): connector "
        "(e.g. 'drive'), config_key (e.g. 'sandbox_folders'), resource_id (required), name "
        "(optional cosmetic label), tab (no current resource type uses this), capabilities (add/update only -- "
        "a map of capability key, e.g. 'write', to true/false; see "
        "privacyfence_list_auto_accept_rules' auto_accept_grants output for which capability "
        "keys apply to which resource type).\n\n"
        "reason: one sentence on why you're proposing this change -- logged, self-reported, "
        "unverified, same as every other gated tool's reason param."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "enum": ["rule", "grant"]},
            "operation": {"type": "string", "enum": ["add", "update", "remove"]},
            "reason": {"type": "string"},
            "operation_key": {"type": "string"},
            "rule_name": {"type": "string"},
            "value": {},
            "old_value": {},
            "connector": {"type": "string"},
            "config_key": {"type": "string"},
            "resource_id": {"type": "string"},
            "name": {"type": "string"},
            "tab": {"type": "string"},
            "capabilities": {"type": "object", "additionalProperties": {"type": "boolean"}},
        },
        "required": ["target", "operation", "reason"],
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
        "{status: 'approval_pending', approval_id, pending_count, binder_url, url, ...} result "
        "(docs/https-connector-refactor-plan.md §5), and report their status -- status only, never "
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

GET_SIGN_IN_LINK_TOOL = types.Tool(
    name="privacyfence_get_sign_in_link",
    description=(
        "Get a fresh, single-use sign-in link for PrivacyFence's own web UI (Approvals, "
        "Settings, or Settings' Connectors section directly) -- for a human who's locked out of "
        "it and asked you for a link. This process itself has no UI of its own beyond the web "
        "surfaces this mints a link to (local mode is otherwise headless), and its startup log "
        "line for this link is always redacted for security, so that's never usable either. If "
        "PrivacyFence's optional companion app happens to be running (a tray/menu-bar icon on "
        "macOS/Windows, an Applications-menu entry on Linux -- nothing installs or starts it "
        "automatically yet), its own Open Approvals/Open Settings items do the same thing without "
        "you. Returns {url}: open it in a "
        "browser on this same machine within a few minutes, before someone else does -- it's "
        "consumed by the first visit, successful or not, and expires on its own shortly after if "
        "unused. This does not open anything itself; hand the url back to the human so *they* "
        "open it, since this only works from the machine PrivacyFence is actually running on. "
        "Unavailable (errors) in organization mode, which signs in through its own IdP-backed "
        "/login instead. "
        "reason: one sentence on why this link is needed right now -- logged, self-reported, "
        "unverified, same as every other meta tool's reason param, since this hands out a "
        "working (if short-lived) credential for a human-facing surface."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "page": {"type": "string", "enum": ["approvals", "settings", "connectors"], "default": "approvals"},
            "reason": {"type": "string"},
        },
        "required": ["reason"],
    },
    annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False),
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
        "never mints a sign-in credential itself, so sign_in_url is always null here. In local "
        "mode when un-onboarded, next_step is 'ask_for_sign_in_link' -- offer the human a "
        "one-time PrivacyFence sign-in link, and only if they say yes, call "
        "privacyfence_get_sign_in_link with page='connectors' to actually mint and share one; in "
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

META_TOOLS: tuple[types.Tool, ...] = (
    CHECK_POLICY_TOOL,
    LIST_RULES_TOOL,
    PROPOSE_RULE_CHANGE_TOOL,
    BEGIN_UNATTENDED_SESSION_TOOL,
    END_UNATTENDED_SESSION_TOOL,
    AWAIT_APPROVAL_TOOL,
    GET_SIGN_IN_LINK_TOOL,
    PRIVACYFENCE_STATUS_TOOL,
)
META_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in META_TOOLS)
