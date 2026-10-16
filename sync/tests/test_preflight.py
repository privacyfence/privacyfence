from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from conftest import RecipeWriter

from privacyfence_sync.client import SignInError, SyncError, ToolDenied, ToolFailed
from privacyfence_sync.preflight import META, _check_args, _connector_of, preflight_recipe
from privacyfence_sync.recipe import ToolStep, load_recipe
from privacyfence_sync.runner import _UNATTENDED_DISABLED

AUTO = {"gate": "auto", "verdict": "auto_accept", "matched_rule": None, "pii_gate_may_apply": False}
COVERED = {"gate": "review", "verdict": "auto_accept", "matched_rule": "r1", "pii_gate_may_apply": False}
REVIEW = {"gate": "review", "verdict": "requires_review", "matched_rule": None, "pii_gate_may_apply": False}
TOKEN = {"label": "nightly", "days_left": 30, "expiring_soon": False, "rules": 2, "tools": 3}


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


def source() -> dict[str, Any]:
    return {"id": "src", "tool": "drive_list_files", "save_result": "src.json"}


def step(**kw: Any) -> dict[str, Any]:
    return {"id": "one", "tool": "drive_list_files", **kw}


async def run(
    write_recipe: RecipeWriter, steps: list[dict[str, Any]], client: FakeClient, attended: bool = True
) -> tuple[bool, str]:
    out = io.StringIO()
    ok = await preflight_recipe(load_recipe(write_recipe(steps)), client, attended=attended, out=out)  # type: ignore[arg-type]
    return ok, out.getvalue()


def policy(tool: str = "drive_list_files", result: Any = None) -> dict[str, Any]:
    return {"privacyfence_check_policy": result if result is not None else AUTO}


def names(*extra: str) -> set[str]:
    return {*META, *extra}


async def test_all_steps_covered_returns_true(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), policy(result=COVERED))
    ok, out = await run(write_recipe, [step()], client)
    assert ok
    assert out == (
        'token "nightly": 2 rule(s), 3 tool(s), expires in 30 day(s)\n'
        "[one] drive_list_files: ok: covered by rule r1\n"
        "preflight: 1 step(s) checked, 0 problem(s)\n"
    )


async def test_gate_auto_is_ok(write_recipe: RecipeWriter) -> None:
    ok, out = await run(write_recipe, [step()], FakeClient(names("drive_list_files"), policy()))
    assert ok and "ok: never asks for approval" in out


async def test_falsy_matched_rule_omits_the_rule(write_recipe: RecipeWriter) -> None:
    result = {**COVERED, "matched_rule": None}
    ok, out = await run(write_recipe, [step()], FakeClient(names("drive_list_files"), policy(result=result)))
    assert ok and "[one] drive_list_files: ok: covered by rule\n" in out


async def test_step_outside_scope_is_a_problem(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("gmail_list_messages"))
    ok, out = await run(write_recipe, [step()], client)
    assert not ok
    assert "NOT AVAILABLE TO THIS TOKEN: drive_list_files is outside its scope" in out
    assert "1 step(s) checked, 1 problem(s)" in out
    assert [c[0] for c in client.calls] == ["privacyfence_status"]


async def test_unscoped_token_missing_tool_says_not_available(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names(), status={"ok": True})
    ok, out = await run(write_recipe, [step()], client)
    assert not ok
    assert "[one] drive_list_files: NOT AVAILABLE: this server does not offer drive_list_files" in out
    assert "token" not in out.split("\n")[0]


async def test_non_dict_status_is_treated_as_unscoped(write_recipe: RecipeWriter) -> None:
    ok, out = await run(write_recipe, [step()], FakeClient(names("drive_list_files"), policy(), status="up"))
    assert ok and not out.startswith("token")


async def test_requires_review_is_a_problem_unattended_and_attended_wording(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), policy(result=REVIEW))
    ok, out = await run(write_recipe, [step()], client, attended=False)
    assert not ok and out.count("an unattended run would be denied") == 1
    ok, out = await run(write_recipe, [step()], client, attended=True)
    assert not ok and "a run would wait for a card" in out and "denied" not in out


async def test_unknown_verdict_is_ok_with_a_note(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), policy(result={"verdict": "unknown"}))
    ok, out = await run(write_recipe, [step()], client)
    assert ok and "unknown: depends on content PrivacyFence only sees at run time" in out


async def test_unexpected_result_is_unknown(write_recipe: RecipeWriter) -> None:
    for result in ({}, "text", ["x"]):
        client = FakeClient(names("drive_list_files"), policy(result=result))
        ok, out = await run(write_recipe, [step()], client)
        assert ok and "unknown:" in out


async def test_check_policy_failure_is_not_checked(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), policy(result=ToolFailed("boom\nsecond line")))
    ok, out = await run(write_recipe, [step()], client)
    assert not ok
    assert "[one] drive_list_files: NOT CHECKED: boom\n" in out and "second line" not in out


async def test_run_and_template_steps_are_skipped(write_recipe: RecipeWriter) -> None:
    steps = [
        source(),
        {"id": "s", "run": "scripts/x.py"},
        step(),
        {"id": "t", "template": "t.html", "data": "src", "output": "o.html"},
    ]
    ok, out = await run(write_recipe, steps, FakeClient(names("drive_list_files"), policy()))
    assert ok and "2 step(s) checked" in out and "[s]" not in out and "[t]" not in out


async def test_run_time_args_are_dropped_before_check_policy(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), policy())
    steps = [source(), step(args={"folder": "abc", "q": "${src:0.name}", "ids": ["${src:0.id}"], "tags": ["a", "b"]})]
    await run(write_recipe, steps, client)
    sent = [a for t, a in client.calls if t == "privacyfence_check_policy"][-1]
    assert sent["args"] == {"folder": "abc", "tags": '["a", "b"]'}
    assert sent["connector"] == "drive" and sent["tool"] == "drive_list_files"
    assert sent["reason"] == "privacyfence-sync demo: preflight of step one"


async def test_requires_review_with_dropped_keys_is_unknown(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), policy(result=REVIEW))
    ok, out = await run(write_recipe, [source(), step(args={"q": "${src:0.name}", "z": {"k": "${src:0.id}"}})], client)
    assert not ok  # only the source step needs approval
    assert "WOULD NEED APPROVAL" in out.split("\n")[1]
    assert "[one] drive_list_files: unknown:" in out and "(not checked: q, z)" in out


async def test_temp_accept_is_a_problem(write_recipe: RecipeWriter) -> None:
    result = {**COVERED, "matched_rule": "session_temp_accept"}
    ok, out = await run(write_recipe, [step()], FakeClient(names("drive_list_files"), policy(result=result)))
    assert not ok and "WOULD NEED APPROVAL" in out


async def test_empty_scoped_token_raises_sign_in_error(write_recipe: RecipeWriter) -> None:
    with pytest.raises(SignInError, match="sees no tools"):
        await run(write_recipe, [step()], FakeClient(names()))


async def test_expiring_token_warns(write_recipe: RecipeWriter) -> None:
    status = {"token": {**TOKEN, "days_left": 3, "expiring_soon": True}}
    ok, out = await run(write_recipe, [step()], FakeClient(names("drive_list_files"), policy(), status=status))
    assert ok and "WARNING: the token expires in 3 day(s); ask its owner for a new one\n" in out


async def test_expired_token_is_a_problem(write_recipe: RecipeWriter) -> None:
    status = {"token": {**TOKEN, "days_left": 0, "expiring_soon": True}}
    ok, out = await run(write_recipe, [step()], FakeClient(names("drive_list_files"), policy(), status=status))
    assert not ok and "WARNING" not in out
    assert "PROBLEM: the token has expired; ask its owner for a new one\n" in out
    assert "1 step(s) checked, 1 problem(s)" in out


async def test_revoked_token_status_denied_raises_sign_in_error(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names(), status=ToolDenied("Request denied: this token is no longer valid."))
    with pytest.raises(SignInError, match="revoked or has expired"):
        await run(write_recipe, [step()], client)


async def test_unattended_declared_and_ended(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), {**policy(), "privacyfence_end_unattended_session": ToolFailed("x")})
    ok, _ = await run(write_recipe, [step()], client, attended=False)
    assert ok
    assert [t for t, _ in client.calls] == [
        "privacyfence_status",
        "privacyfence_begin_unattended_session",
        "privacyfence_check_policy",
        "privacyfence_end_unattended_session",
    ]


async def test_unattended_disabled_raises_sync_error(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), {"privacyfence_begin_unattended_session": ToolFailed("no")})
    with pytest.raises(SyncError, match="does not allow unattended sessions") as info:
        await run(write_recipe, [step()], client, attended=False)
    assert str(info.value) == _UNATTENDED_DISABLED
    assert "privacyfence_end_unattended_session" not in [t for t, _ in client.calls]


async def test_attended_skips_unattended_calls(write_recipe: RecipeWriter) -> None:
    client = FakeClient(names("drive_list_files"), policy())
    await run(write_recipe, [step()], client, attended=True)
    assert not [t for t, _ in client.calls if "unattended" in t]


async def test_pii_note_printed_once(write_recipe: RecipeWriter) -> None:
    result = {**COVERED, "pii_gate_may_apply": True}
    steps = [step(), step(id="two")]
    ok, out = await run(write_recipe, steps, FakeClient(names("drive_list_files"), policy(result=result)))
    assert ok and out.count("note: reads are also scanned") == 1
    assert out.index("note:") < out.index("preflight:")


def test_connector_of() -> None:
    assert _connector_of("apps_script_run") == "apps_script"
    assert _connector_of("grist_export_records") == "grist"


def test_check_args_keeps_file_placeholders(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    recipe = load_recipe(write_recipe([step(args={"path": "{work_dir}/x", "n": 1})]))
    tool_step = recipe.steps[0]
    assert isinstance(tool_step, ToolStep)
    assert _check_args(tool_step) == ({"path": "{work_dir}/x", "n": 1}, [])
