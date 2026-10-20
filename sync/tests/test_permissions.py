from __future__ import annotations

import io
import json
from typing import Any

import pytest
from conftest import RecipeWriter

from privacyfence_sync.client import SignInError, ToolDenied, ToolFailed
from privacyfence_sync.permissions import list_permissions
from privacyfence_sync.preflight import META
from privacyfence_sync.recipe import load_recipe

TOKEN = {"label": "nightly", "days_left": 30, "expiring_soon": False, "rules": 2, "tools": 3}
FOLDER = {
    "group": "drive.folder",
    "label": "Drive folder",
    "value": ["f1"],
    "value_known": True,
    "verb": "read",
    "operation": "drive.list",
}
SHEET = {**FOLDER, "group": "sheets.file", "label": "Sheet", "value": ["s1", "s2"], "verb": "write"}
RUNTIME = {**FOLDER, "group": "drive.folder", "label": "Drive folder", "value": None, "value_known": False}
REVIEW = {"gate": "review", "verdict": "requires_review", "matched_rule": None}


class FakeClient:
    def __init__(self, names: set[str], results: dict[str, Any] | None = None, status: Any = None) -> None:
        self.names = frozenset(names)
        self.results = results or {}
        self.status = {"token": TOKEN} if status is None else status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tool_names(self) -> frozenset[str]:
        return self.names

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        self.calls.append((tool, args))
        item = self.status if tool == "privacyfence_status" else self.results.get(tool, {})
        if isinstance(item, BaseException):
            raise item
        return item


def step(**kw: Any) -> dict[str, Any]:
    return {"id": "one", "tool": "drive_list_files", **kw}


def src() -> dict[str, Any]:
    return {"id": "src", "tool": "drive_list_files", "save_result": "src.json"}


def names(*extra: str) -> set[str]:
    return {*META, *extra}


async def run(
    write_recipe: RecipeWriter, steps: list[dict[str, Any]], client: FakeClient, as_json: bool = False
) -> str:
    out = io.StringIO()
    await list_permissions(load_recipe(write_recipe(steps)), client, out=out, as_json=as_json)  # type: ignore[arg-type]
    return out.getvalue()


def checked(result: Any, **kw: Any) -> FakeClient:
    return FakeClient(names("drive_list_files"), {"privacyfence_check_policy": result}, **kw)


async def test_auto(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked({"gate": "auto", "verdict": "auto_accept"}))
    assert out == (
        'token "nightly": 2 rule(s), 3 tool(s), expires in 30 day(s)\n'
        "[one] drive_list_files: never asks for approval\n"
        "Rules this recipe needs:\n  none\n"
        "Tools a scoped token for this recipe needs: drive_list_files\n"
    )


async def test_covered_wins_over_dropped_args(write_recipe: RecipeWriter) -> None:
    result = {"gate": "review", "verdict": "auto_accept", "matched_rule": "r1"}
    out = await run(write_recipe, [src(), step(args={"q": "${src:0.name}"})], checked(result))
    assert "[one] drive_list_files: covered by rule r1\n" in out


async def test_session_temp_accept_needs_a_rule(write_recipe: RecipeWriter) -> None:
    result = {"verdict": "auto_accept", "matched_rule": "session_temp_accept", "suggested_rules": [FOLDER]}
    out = await run(write_recipe, [step()], checked(result))
    assert "[one] drive_list_files: NEEDS A RULE: Drive folder = f1 (read)\n" in out


async def test_not_checked(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked(ToolFailed("boom\nmore")))
    assert "[one] drive_list_files: NOT CHECKED: boom\n" in out


async def test_needs_rule_with_known_value_and_extra_suggestions(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked({**REVIEW, "suggested_rules": [SHEET, "junk", FOLDER]}))
    assert (
        "[one] drive_list_files: NEEDS A RULE: Sheet = s1, s2 (write)\n    NEEDS A RULE: Drive folder = f1 (read)\n"
    ) in out


async def test_needs_rule_value_unknown(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked({**REVIEW, "suggested_rules": [RUNTIME]}))
    assert "NEEDS A RULE: Drive folder, naming what it reaches at run time (read)\n" in out
    assert "Rules this recipe needs:\n  Drive folder, naming what it reaches at run time (read)\n" in out


async def test_needs_rule_without_suggested_rules_key(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked(REVIEW))
    assert "this PrivacyFence does not suggest one; update it" in out
    assert "Rules this recipe needs:\n  none\n" in out


async def test_needs_rule_with_empty_suggestions(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked({**REVIEW, "suggested_rules": []}))
    assert "PrivacyFence suggests no rule for this call; add one in Settings -> Auto-accept" in out


async def test_unknown_with_dropped_args_even_when_requires_review(write_recipe: RecipeWriter) -> None:
    result = {**REVIEW, "suggested_rules": [RUNTIME]}
    out = await run(write_recipe, [src(), step(args={"q": "${src:0.name}"})], checked(result))
    assert (
        "[one] drive_list_files: depends on what it reaches at run time (not checked: q)\n"
        "    NEEDS A RULE: Drive folder, naming what it reaches at run time (read)\n"
    ) in out


async def test_unknown_without_dropped_args(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked({"verdict": "unknown"}))
    assert "[one] drive_list_files: depends on what it reaches at run time\n" in out


async def test_non_dict_result_is_unknown(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked("odd"))
    assert "depends on what it reaches at run time\n" in out


async def test_not_available(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names(), status={"ok": True})
    out = await run(write_recipe, [step()], client)
    assert "NOT AVAILABLE: this server does not offer drive_list_files" in out
    assert not [c for c in client.calls if c[0] == "privacyfence_check_policy"]


async def test_not_available_to_scoped_token(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], FakeClient(names("gmail_list_messages")))
    assert "NOT AVAILABLE TO THIS TOKEN: drive_list_files" in out


async def test_summary_merges_verbs_for_same_group_and_value(write_recipe: RecipeWriter) -> None:
    write = {**FOLDER, "verb": "write"}
    steps = [step(id="a"), step(id="b")]
    client = FakeClient(names("drive_list_files"))
    results = iter([{**REVIEW, "suggested_rules": [FOLDER, SHEET]}, {**REVIEW, "suggested_rules": [write]}])

    async def call(tool: str, args: dict[str, Any]) -> Any:
        client.calls.append((tool, args))
        return client.status if tool == "privacyfence_status" else next(results)

    client.call = call  # type: ignore[method-assign]
    out = await run(write_recipe, steps, client)
    assert "Rules this recipe needs:\n  Drive folder = f1 (read, write)\n  Sheet = s1, s2 (write)\n" in out


async def test_tools_line_lists_every_tool_step_sorted(write_recipe: RecipeWriter) -> None:
    steps = [step(id="a", tool="gmail_list_messages"), step(id="b", save_result="x.json"), step(id="c")]
    client = FakeClient(
        names("drive_list_files", "gmail_list_messages"), {"privacyfence_check_policy": {"gate": "auto"}}
    )
    out = await run(write_recipe, steps, client)
    assert out.endswith("needs: drive_list_files, gmail_list_messages\n")


async def test_script_steps_are_skipped(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step(), {"id": "s", "run": "t.py"}], checked({"gate": "auto"}))
    assert "[s]" not in out


async def test_json_output(write_recipe: RecipeWriter) -> None:
    steps = [step(id="a"), step(id="b", tool="gmail_list_messages"), step(id="c", tool="gmail_send")]
    client = FakeClient(names("drive_list_files", "gmail_list_messages"))
    results = iter([{"verdict": "auto_accept", "matched_rule": "r9"}, {**REVIEW, "suggested_rules": [FOLDER]}])

    async def call(tool: str, args: dict[str, Any]) -> Any:
        client.calls.append((tool, args))
        return client.status if tool == "privacyfence_status" else next(results)

    client.call = call  # type: ignore[method-assign]
    doc = json.loads(await run(write_recipe, steps, client, as_json=True))
    assert doc["version"] == 1 and doc["recipe"] == "demo"
    assert [(s["id"], s["status"], s["matched_rule"]) for s in doc["steps"]] == [
        ("a", "covered", "r9"),
        ("b", "needs_rule", None),
        ("c", "not_available", None),
    ]
    assert doc["steps"][0]["suggested_rules"] is None
    assert doc["steps"][1]["suggested_rules"] == [FOLDER]
    assert doc["needed"] == [{"group": "drive.folder", "label": "Drive folder", "value": ["f1"], "verbs": ["read"]}]
    assert doc["tools"] == ["drive_list_files", "gmail_list_messages", "gmail_send"]


async def test_no_unattended_session_is_opened(write_recipe: RecipeWriter) -> None:
    client = checked({"gate": "auto"})
    await run(write_recipe, [step()], client)
    assert not [t for t, _ in client.calls if "unattended" in t]


async def test_status_denied_is_a_sign_in_error(write_recipe: RecipeWriter) -> None:
    with pytest.raises(SignInError, match="revoked or has expired"):
        await run(write_recipe, [step()], FakeClient(names(), status=ToolDenied("no")))


async def test_scoped_token_seeing_no_tools(write_recipe: RecipeWriter) -> None:
    with pytest.raises(SignInError, match="sees no tools"):
        await run(write_recipe, [step()], FakeClient(names()))


async def test_unscoped_token_has_no_token_line(write_recipe: RecipeWriter) -> None:
    out = await run(write_recipe, [step()], checked({"gate": "auto"}, status={"ok": True}))
    assert not out.startswith("token")
