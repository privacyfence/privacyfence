"""List the auto-accept rules a recipe needs, from what the daemon says about each tool step."""

from __future__ import annotations

import json
from typing import Any, TextIO

from .client import SignInError, SyncClient, ToolDenied, ToolFailed
from .preflight import META, _check_args, _connector_of, _not_available_text, _suggestion_text
from .recipe import Recipe, ToolStep

_SESSION_TEMP = "session_temp_accept"
_NO_SUGGESTION_KEY = (
    "NEEDS A RULE: this PrivacyFence does not suggest one; update it, or run once with --attended "
    "and use the card's Always allow"
)
_NO_SUGGESTION = (
    "NEEDS A RULE: PrivacyFence suggests no rule for this call; add one in Settings -> Auto-accept, "
    "or run once with --attended"
)


def _status_of(result: Any, dropped: list[str]) -> str:
    verdict = result.get("verdict") if isinstance(result, dict) else None
    matched = result.get("matched_rule") if isinstance(result, dict) else None
    if isinstance(result, dict) and result.get("gate") == "auto":
        return "auto"
    if verdict == "auto_accept" and matched != _SESSION_TEMP:
        return "covered"
    if dropped:
        return "unknown"
    if verdict == "requires_review" or matched == _SESSION_TEMP:
        return "needs_rule"
    return "unknown"


def _step_text(status: str, matched: Any, dropped: list[str], suggestions: list[dict[str, Any]] | None) -> str:
    if status == "auto":
        return "never asks for approval"
    if status == "covered":
        return f"covered by rule {matched}"
    if status == "needs_rule":
        if suggestions is None:
            return _NO_SUGGESTION_KEY
        if not suggestions:
            return _NO_SUGGESTION
        lines = [f"NEEDS A RULE: {_suggestion_text(item)}" for item in suggestions]
    else:
        note = f" (not checked: {', '.join(dropped)})" if dropped else ""
        lines = [f"depends on what it reaches at run time{note}"]
        lines.extend(f"NEEDS A RULE: {_suggestion_text(item)}" for item in suggestions or [])
    return "\n    ".join(lines)


def _needed(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, tuple[str, ...] | None], dict[str, Any]] = {}
    for step in steps:
        if step["status"] not in ("needs_rule", "unknown"):
            continue
        for item in step["suggested_rules"] or []:
            known = item.get("value_known") is not False and isinstance(item.get("value"), list)
            value = [str(v) for v in item["value"]] if known else None
            key = (item.get("group"), tuple(value) if value is not None else None)
            entry = merged.setdefault(
                key, {"group": item.get("group"), "label": item.get("label"), "value": value, "verbs": set()}
            )
            entry["verbs"].add(item.get("verb"))
    needed = [{**entry, "verbs": sorted(entry["verbs"])} for entry in merged.values()]
    return sorted(needed, key=lambda entry: (str(entry["label"]), entry["value"] or []))


def _need_text(entry: dict[str, Any]) -> str:
    verbs = ", ".join(entry["verbs"])
    if entry["value"] is None:
        return f"  {entry['label']}, naming what it reaches at run time ({verbs})"
    if not entry["value"]:
        return f"  {entry['label']} ({verbs})"
    return f"  {entry['label']} = {', '.join(entry['value'])} ({verbs})"


async def list_permissions(recipe: Recipe, client: SyncClient, *, out: TextIO, as_json: bool) -> None:
    reason = f"privacyfence-sync {recipe.name}"
    try:
        status = await client.call("privacyfence_status", {"reason": f"{reason}: permissions"})
    except ToolDenied:
        raise SignInError("The token was revoked or has expired. Ask its owner for a new one.") from None
    token = status.get("token") if isinstance(status, dict) else None
    scoped = isinstance(token, dict)
    if isinstance(token, dict) and not as_json:
        out.write(
            f'token "{token.get("label")}": {token.get("rules")} rule(s), {token.get("tools")} tool(s), '
            f"expires in {token.get('days_left')} day(s)\n"
        )
    names = await client.list_tool_names()
    if scoped and not names - META:
        raise SignInError(
            "The token sees no tools: its scope is empty, or the connectors it covers are not set up. Ask its owner."
        )

    steps: list[dict[str, Any]] = []
    texts: list[str] = []
    for step in recipe.steps:
        if not isinstance(step, ToolStep):
            continue
        record: dict[str, Any] = {
            "id": step.id,
            "tool": step.tool,
            "status": "not_available",
            "matched_rule": None,
            "dropped": [],
            "suggested_rules": None,
        }
        steps.append(record)
        if step.tool not in names:
            texts.append(_not_available_text(step.tool, scoped))
            continue
        args, dropped = _check_args(step)
        record["dropped"] = dropped
        try:
            result = await client.call(
                "privacyfence_check_policy",
                {
                    "connector": _connector_of(step.tool),
                    "tool": step.tool,
                    "reason": f"{reason}: permissions of step {step.id}",
                    "args": args,
                },
            )
        except ToolFailed as exc:
            record["status"] = "not_checked"
            texts.append(f"NOT CHECKED: {str(exc).split(chr(10), 1)[0]}")
            continue
        raw = result.get("suggested_rules") if isinstance(result, dict) else None
        if isinstance(raw, list):
            record["suggested_rules"] = [item for item in raw if isinstance(item, dict)]
        record["status"] = _status_of(result, dropped)
        if record["status"] == "covered":
            record["matched_rule"] = result.get("matched_rule")
        texts.append(_step_text(record["status"], record["matched_rule"], dropped, record["suggested_rules"]))

    needed = _needed(steps)
    tools = sorted({record["tool"] for record in steps})
    if as_json:
        out.write(
            json.dumps({"version": 1, "recipe": recipe.name, "steps": steps, "needed": needed, "tools": tools}) + "\n"
        )
        return
    for record, text in zip(steps, texts, strict=True):
        out.write(f"[{record['id']}] {record['tool']}: {text}\n")
    out.write("Rules this recipe needs:\n")
    out.write("".join(_need_text(entry) + "\n" for entry in needed) if needed else "  none\n")
    out.write(f"Tools a scoped token for this recipe needs: {', '.join(tools)}\n")
