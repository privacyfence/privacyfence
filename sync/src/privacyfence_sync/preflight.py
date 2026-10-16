"""Ask the daemon, with the run's own token, whether each tool step of a recipe would go through."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, TextIO

from . import refs
from .client import SignInError, SyncClient, SyncError, ToolDenied, ToolFailed
from .recipe import Recipe, ToolStep
from .runner import _UNATTENDED_DISABLED, json_text_args

META = frozenset(
    {
        "privacyfence_status",
        "privacyfence_check_policy",
        "privacyfence_create_upload_slot",
        "privacyfence_begin_unattended_session",
        "privacyfence_end_unattended_session",
    }
)
_NEEDS_APPROVAL = "WOULD NEED APPROVAL: no auto-accept rule covers this call; "
_UNKNOWN = (
    "unknown: depends on content PrivacyFence only sees at run time (for example which folder a file is in); "
    "run once with --attended to be sure"
)
_PII_NOTE = (
    "note: reads are also scanned for personal data at run time; an unattended run is denied when some is "
    "found, unless the token allows it for exports, downloads and page uploads"
)


@dataclass(frozen=True)
class Finding:
    step_id: str
    tool: str
    ok: bool
    text: str


def _connector_of(tool: str) -> str:
    return "apps_script" if tool.startswith("apps_script_") else tool.split("_", 1)[0]


def _has_value_ref(value: Any) -> bool:
    if isinstance(value, str):
        return refs.VALUE_REF.search(value) is not None
    if isinstance(value, list):
        return any(_has_value_ref(item) for item in value)
    if isinstance(value, dict):
        return any(_has_value_ref(item) for item in value.values())
    return False


def _check_args(step: ToolStep) -> tuple[dict[str, Any], list[str]]:
    """The step's arguments minus those that only exist at run time, and the names left out."""
    dropped = sorted(key for key, value in step.args.items() if _has_value_ref(value))
    kept = {key: value for key, value in step.args.items() if key not in dropped}
    return json_text_args(kept), dropped


async def preflight_recipe(recipe: Recipe, client: SyncClient, *, attended: bool, out: TextIO) -> bool:
    """True when no step has a problem."""
    session_reason = f"privacyfence-sync {recipe.name}"
    problems = 0
    try:
        status = await client.call("privacyfence_status", {"reason": f"{session_reason}: preflight"})
    except ToolDenied:
        raise SignInError("The token was revoked or has expired. Ask its owner for a new one.") from None
    token = status.get("token") if isinstance(status, dict) else None
    scoped = isinstance(token, dict)
    if isinstance(token, dict):
        out.write(
            f'token "{token.get("label")}": {token.get("rules")} rule(s), {token.get("tools")} tool(s), '
            f"expires in {token.get('days_left')} day(s)\n"
        )
        if token.get("days_left") == 0:
            out.write("PROBLEM: the token has expired; ask its owner for a new one\n")
            problems += 1
        elif token.get("expiring_soon"):
            out.write(f"WARNING: the token expires in {token.get('days_left')} day(s); ask its owner for a new one\n")

    names = await client.list_tool_names()
    if scoped and not names - META:
        raise SignInError(
            "The token sees no tools: its scope is empty, or the connectors it covers are not set up. Ask its owner."
        )

    if not attended:
        try:
            await client.call("privacyfence_begin_unattended_session", {"reason": f"{session_reason}: preflight"})
        except ToolFailed:
            raise SyncError(_UNATTENDED_DISABLED) from None
    pii = False
    checked = 0
    try:
        for step in recipe.steps:
            if not isinstance(step, ToolStep):
                continue
            checked += 1
            finding, may_scan = await _check_step(recipe, step, client, names, scoped, attended)
            pii = pii or may_scan
            problems += not finding.ok
            out.write(f"[{finding.step_id}] {finding.tool}: {finding.text}\n")
    finally:
        if not attended:
            with contextlib.suppress(Exception):
                await client.call(
                    "privacyfence_end_unattended_session", {"reason": f"{session_reason}: preflight done"}
                )
    if pii:
        out.write(_PII_NOTE + "\n")
    out.write(f"preflight: {checked} step(s) checked, {problems} problem(s)\n")
    return problems == 0


async def _check_step(
    recipe: Recipe, step: ToolStep, client: SyncClient, names: frozenset[str], scoped: bool, attended: bool
) -> tuple[Finding, bool]:
    if step.tool not in names:
        if scoped:
            text = f"NOT AVAILABLE TO THIS TOKEN: {step.tool} is outside its scope, or its connector is not set up"
        else:
            text = f"NOT AVAILABLE: this server does not offer {step.tool} (its connector is not set up)"
        return Finding(step.id, step.tool, False, text), False
    args, dropped = _check_args(step)
    try:
        result = await client.call(
            "privacyfence_check_policy",
            {
                "connector": _connector_of(step.tool),
                "tool": step.tool,
                "reason": f"privacyfence-sync {recipe.name}: preflight of step {step.id}",
                "args": args,
            },
        )
    except ToolFailed as exc:
        text = f"NOT CHECKED: {str(exc).split(chr(10), 1)[0]}"
        return Finding(step.id, step.tool, False, text), False
    verdict = result.get("verdict") if isinstance(result, dict) else None
    matched = result.get("matched_rule") if isinstance(result, dict) else None
    may_scan = isinstance(result, dict) and result.get("pii_gate_may_apply") is True
    needs_approval = _NEEDS_APPROVAL + (
        "a run would wait for a card" if attended else "an unattended run would be denied"
    )
    if isinstance(result, dict) and result.get("gate") == "auto":
        finding = Finding(step.id, step.tool, True, "ok: never asks for approval")
    elif verdict == "auto_accept" and matched == "session_temp_accept":
        finding = Finding(step.id, step.tool, False, needs_approval)
    elif verdict == "auto_accept":
        finding = Finding(
            step.id, step.tool, True, f"ok: covered by rule {matched}" if matched else "ok: covered by rule"
        )
    elif verdict == "requires_review" and dropped:
        finding = Finding(step.id, step.tool, True, f"{_UNKNOWN} (not checked: {', '.join(dropped)})")
    elif verdict == "requires_review":
        finding = Finding(step.id, step.tool, False, needs_approval)
    else:
        finding = Finding(step.id, step.tool, True, _UNKNOWN)
    return finding, may_scan
