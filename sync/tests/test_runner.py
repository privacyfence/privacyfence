from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import httpx2
import pytest

from privacyfence_sync.client import RecipeRuntimeError, SyncError, ToolDenied, ToolFailed
from privacyfence_sync.recipe import load_recipe
from privacyfence_sync.runner import run_recipe

from conftest import RecipeWriter

BEGIN = "privacyfence_begin_unattended_session"
END = "privacyfence_end_unattended_session"
AWAIT = "privacyfence_await_approval"


class FakeClient:
    """Stands in for SyncClient: scripted results per tool, every call recorded."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.results = {tool: list(v) if isinstance(v, list) else [v] for tool, v in (results or {}).items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[tuple[str, Path, str | None]] = []
        self.uploads: list[tuple[Path, str, str]] = []
        self.download_error: Exception | None = None
        self.upload_error: Exception | None = None

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        self.calls.append((tool, dict(args)))
        scripted = self.results.get(tool)
        if not scripted:
            return {}
        item = scripted.pop(0) if len(scripted) > 1 else scripted[0]
        if isinstance(item, Exception):
            raise item
        return item(args) if callable(item) else item

    async def download(self, url: str, dest: Path, expected_sha256: str | None) -> None:
        if self.download_error is not None:
            raise self.download_error
        self.downloads.append((url, dest, expected_sha256))
        dest.write_bytes(b"a,b\n1,2\n")

    async def upload(self, path: Path, reason: str) -> str:
        if self.upload_error is not None:
            raise self.upload_error
        upload_id = f"up{len(self.uploads) + 1}"
        self.uploads.append((path, reason, upload_id))
        return upload_id

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def args_of(self, tool: str) -> dict[str, Any]:
        return next(args for name, args in self.calls if name == tool)


def script(tmp_path: Path, name: str, body: str) -> None:
    path = tmp_path / "scripts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def writer_script(tmp_path: Path, name: str, files: dict[str, str]) -> None:
    """A script that writes the given files (relative to its cwd, the work folder)."""
    script(
        tmp_path,
        name,
        f"""
        import pathlib
        for name, text in {files!r}.items():
            path = pathlib.Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        """,
    )


async def run(recipe_path: Path, client: FakeClient, *, attended: bool = False) -> tuple[bool, str]:
    out = io.StringIO()
    done = await run_recipe(load_recipe(recipe_path), client, attended=attended, out=out)  # type: ignore[arg-type]
    return done, out.getvalue()


def export_step(**args: Any) -> dict[str, Any]:
    return {"id": "exp", "tool": "grist_export_records", "args": {"doc_id": "d", "file_name": "t.csv", **args}}


async def test_begins_and_ends_an_unattended_session(write_recipe: RecipeWriter) -> None:
    client = FakeClient()
    recipe = write_recipe([{"id": "one", "tool": "drive_list_files"}])
    done, out = await run(recipe, client)
    assert done is True
    assert client.tools() == [BEGIN, "drive_list_files", END]
    assert client.args_of(BEGIN) == {"reason": "privacyfence-sync demo: scheduled run"}
    assert client.args_of(END)["reason"].startswith("privacyfence-sync demo")
    assert "[one] ..." in out


async def test_end_errors_are_ignored(write_recipe: RecipeWriter) -> None:
    client = FakeClient({END: ToolFailed("gone")})
    done, _ = await run(write_recipe([{"id": "one", "tool": "drive_list_files"}]), client)
    assert done is True


async def test_json_text_in_a_script_argument_passes_unchanged(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    script(
        tmp_path,
        "echo.py",
        "import json, sys, pathlib\npathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]))\n",
    )
    recipe = write_recipe(
        [{"id": "s", "run": "scripts/echo.py", "args": ['{"a": [1, 2]}', "x y"], "outputs": {"argv": "argv.json"}}]
    )
    await run(recipe, FakeClient())
    assert json.loads((tmp_path / "work" / "argv.json").read_text()) == ['{"a": [1, 2]}', "x y"]


async def test_unattended_disabled_gives_the_admin_hint(write_recipe: RecipeWriter) -> None:
    client = FakeClient({BEGIN: ToolFailed("Unattended sessions are disabled")})
    with pytest.raises(SyncError, match="Ask your administrator, or run with --attended"):
        await run(write_recipe([{"id": "one", "tool": "drive_list_files"}]), client)
    assert client.tools() == [BEGIN]


async def test_export_via_link_downloads_into_work_dir(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    link = {"delivery": "link", "download_url": "https://pf.example/mcp-files/fetch/abc", "sha256": "ff"}
    client = FakeClient({"grist_export_records": link})
    await run(write_recipe([export_step()]), client)
    assert client.downloads == [("https://pf.example/mcp-files/fetch/abc", tmp_path / "work" / "t.csv", "ff")]
    assert client.args_of("grist_export_records")["destination_dir"] == str(tmp_path / "work")


async def test_export_on_local_disk_is_copied_and_checked(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.csv"
    elsewhere.write_text("a\n1\n")
    sha = hashlib.sha256(b"a\n1\n").hexdigest()
    client = FakeClient({"grist_export_records": {"delivery": "local_disk", "path": str(elsewhere), "sha256": sha}})
    await run(write_recipe([export_step()]), client)
    assert (tmp_path / "work" / "t.csv").read_text() == "a\n1\n"


async def test_export_written_in_place_is_checked_where_it_is(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    def write(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["destination_dir"]) / "t.csv"
        path.write_text("a\n1\n")
        return {"delivery": "local_disk", "path": str(path), "sha256": hashlib.sha256(b"a\n1\n").hexdigest()}

    await run(write_recipe([export_step()]), FakeClient({"grist_export_records": write}))
    assert (tmp_path / "work" / "t.csv").read_text() == "a\n1\n"


async def test_local_disk_sha_mismatch_fails(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    def write(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["destination_dir"]) / "t.csv"
        path.write_text("a\n1\n")
        return {"delivery": "local_disk", "path": str(path), "sha256": "00"}

    with pytest.raises(ToolFailed, match="step exp: .*sha256"):
        await run(write_recipe([export_step()]), FakeClient({"grist_export_records": write}))


async def test_inline_download_is_decoded(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    inline = {"delivery": "inline", "content_base64": base64.b64encode(b"PK\x03\x04").decode()}
    step = {"id": "wb", "tool": "drive_download_file", "args": {"file_id": "f"}, "save_as": "wb.xlsx"}
    await run(write_recipe([step]), FakeClient({"drive_download_file": inline}))
    assert (tmp_path / "work" / "wb.xlsx").read_bytes() == b"PK\x03\x04"
    assert not (tmp_path / "work" / ".download-wb").exists()


async def test_drive_download_written_in_place_is_moved_to_save_as(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    def write(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["destination_dir"]) / "Cash flow 2026.xlsx"
        path.write_bytes(b"xlsx")
        return {"path": str(path), "name": path.name, "size_bytes": 4, "truncated": False}

    step = {"id": "wb", "tool": "drive_download_file", "args": {"file_id": "f"}, "save_as": "wb.xlsx"}
    client = FakeClient({"drive_download_file": write})
    await run(write_recipe([step]), client)
    assert (tmp_path / "work" / "wb.xlsx").read_bytes() == b"xlsx"
    assert client.args_of("drive_download_file")["destination_dir"] == str(tmp_path / "work" / ".download-wb")
    assert not (tmp_path / "work" / ".download-wb").exists()


async def test_truncated_download_fails(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    def write(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["destination_dir"]) / "big.xlsx"
        path.write_bytes(b"x")
        return {"path": str(path), "truncated": True}

    step = {"id": "wb", "tool": "drive_download_file", "args": {"file_id": "f"}, "save_as": "wb.xlsx"}
    with pytest.raises(SyncError, match="step wb: the download was truncated"):
        await run(write_recipe([step]), FakeClient({"drive_download_file": write}))


async def test_empty_jira_export_writes_a_header_only_csv(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    client = FakeClient({"jira_export_issues": {"delivery": "none", "columns": ["key", "summary"]}})
    step = {"id": "iss", "tool": "jira_export_issues", "args": {"jql": "x", "file_name": "iss.csv"}}
    await run(write_recipe([step]), client)
    assert (tmp_path / "work" / "iss.csv").read_text().strip() == "key,summary"


async def test_empty_jira_export_writes_an_empty_list_for_json(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    client = FakeClient({"jira_export_issues": {"delivery": "none", "columns": ["key"]}})
    step = {"id": "iss", "tool": "jira_export_issues", "args": {"jql": "x", "file_name": "iss.json"}}
    await run(write_recipe([step]), client)
    assert (tmp_path / "work" / "iss.json").read_text() == "[]"


async def test_unexpected_delivery_fails(write_recipe: RecipeWriter) -> None:
    client = FakeClient({"grist_export_records": {"delivery": "carrier_pigeon"}})
    with pytest.raises(SyncError, match="step exp: unexpected delivery 'carrier_pigeon'"):
        await run(write_recipe([export_step()]), client)


async def test_non_dict_export_result_is_an_unexpected_delivery(write_recipe: RecipeWriter) -> None:
    with pytest.raises(SyncError, match="unexpected delivery None"):
        await run(write_recipe([export_step()]), FakeClient({"grist_export_records": "oops"}))


async def test_browser_only_link_gives_the_agent_links_hint(write_recipe: RecipeWriter) -> None:
    link = {"delivery": "link", "download_url": "https://pf.example/mcp-files/downloads/abc"}
    client = FakeClient({"grist_export_records": link})
    with pytest.raises(SyncError, match="agent_links"):
        await run(write_recipe([export_step()]), client)
    assert client.downloads == []


async def test_transfer_status_errors_become_one_line_failures(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    request = httpx2.Request("GET", "https://pf.example/mcp-files/fetch/abc?token=SECRET")
    status_error = httpx2.HTTPStatusError("boom", request=request, response=httpx2.Response(410, request=request))
    link = {"delivery": "link", "download_url": "https://pf.example/mcp-files/fetch/abc?token=SECRET"}
    client = FakeClient({"grist_export_records": link})
    client.download_error = status_error
    with pytest.raises(ToolFailed) as caught:
        await run(write_recipe([export_step()]), client)
    assert str(caught.value) == "step exp: the file transfer failed with HTTP 410"

    writer_script(tmp_path, "mk.py", {"out.csv": "a\n1\n"})
    steps = [
        {"id": "mk", "run": "scripts/mk.py", "outputs": {"csv": "out.csv"}},
        {"id": "load", "tool": "grist_import_csv", "file": "mk.csv", "args": {"doc_id": "d", "table_id": "T"}},
    ]
    upload_client = FakeClient()
    upload_client.upload_error = status_error
    with pytest.raises(ToolFailed, match="step load: the file transfer failed with HTTP 410") as caught:
        await run(write_recipe(steps), upload_client)
    assert "SECRET" not in str(caught.value)


async def test_list_and_mapping_args_are_sent_as_json_text(write_recipe: RecipeWriter) -> None:
    step = {
        "id": "one",
        "tool": "grist_get_records",
        "args": {"filter": {"Stage": ["Won"]}, "fields": ["A", "B"], "limit": 5, "flag": True, "name": "x"},
    }
    client = FakeClient()
    await run(write_recipe([step]), client)
    sent = client.args_of("grist_get_records")
    assert sent["filter"] == '{"Stage": ["Won"]}'
    assert sent["fields"] == '["A", "B"]'
    assert (sent["limit"], sent["flag"], sent["name"]) == (5, True, "x")


def json_source(tmp_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    writer_script(tmp_path, "pick.py", {"pick.json": json.dumps(data)})
    return {"id": "pick", "run": "scripts/pick.py", "outputs": {"out": "pick.json"}}


async def test_value_reference_keeps_json_type_in_tool_args(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    pick = json_source(tmp_path, {"n": 5, "ids": [1, 2], "drive": {"id": "abc"}})
    step = {
        "id": "one",
        "tool": "grist_get_records",
        "args": {
            "limit": "${pick.out:n}",
            "text": "id=${pick.out:drive.id}",
            "ids": "${pick.out:ids}",
            "first": "${pick.out:ids.0}",
        },
    }
    client = FakeClient()
    await run(write_recipe([pick, step]), client)
    sent = client.args_of("grist_get_records")
    assert sent["limit"] == 5
    assert sent["text"] == "id=abc"
    assert sent["ids"] == "[1, 2]"
    assert sent["first"] == 1


async def test_missing_value_reference_fails_before_the_call(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    pick = json_source(tmp_path, {"n": 5})
    step = {"id": "one", "tool": "grist_get_records", "args": {"limit": "${pick.out:nope}"}}
    client = FakeClient()
    with pytest.raises(RecipeRuntimeError, match="step one: nope not found"):
        await run(write_recipe([pick, step]), client)
    assert "grist_get_records" not in client.tools()


async def test_value_reference_to_a_non_json_file_fails(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    writer_script(tmp_path, "pick.py", {"pick.json": "not json"})
    pick = {"id": "pick", "run": "scripts/pick.py", "outputs": {"out": "pick.json"}}
    step = {"id": "one", "tool": "grist_get_records", "args": {"limit": "${pick.out:n}"}}
    with pytest.raises(RecipeRuntimeError, match="step one: pick.out is not a JSON file"):
        await run(write_recipe([pick, step]), FakeClient())


async def test_save_result_writes_the_result(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    step = {"id": "tot", "tool": "salesforce_run_report", "args": {"report_id": "r"}, "save_result": "tot.json"}
    await run(write_recipe([step]), FakeClient({"salesforce_run_report": {"total": "é"}}))
    assert (tmp_path / "work" / "tot.json").read_text(encoding="utf-8") == '{\n "total": "é"\n}'


async def test_upload_step_passes_the_upload_id(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    writer_script(tmp_path, "mk.py", {"out.csv": "k,v\n1,2\n"})
    steps = [
        {"id": "mk", "run": "scripts/mk.py", "outputs": {"csv": "out.csv"}},
        {"id": "load", "tool": "grist_import_csv", "file": "mk.csv", "args": {"doc_id": "d", "table_id": "T"}},
    ]
    client = FakeClient()
    await run(write_recipe(steps), client)
    assert client.uploads == [(tmp_path / "work" / "out.csv", "privacyfence-sync demo: step load", "up1")]
    assert client.args_of("grist_import_csv")["upload_id"] == "up1"


async def test_skip_if_empty_skips_a_header_only_csv_and_writes_skipped(
    write_recipe: RecipeWriter, tmp_path: Path
) -> None:
    writer_script(tmp_path, "mk.py", {"out.csv": "k,v\n,\n"})
    steps = [
        {"id": "mk", "run": "scripts/mk.py", "outputs": {"csv": "out.csv"}},
        {
            "id": "load",
            "tool": "grist_update_csv",
            "file": "mk.csv",
            "skip_if_empty": True,
            "args": {"doc_id": "d", "table_id": "T", "key_column": "k"},
            "save_result": "load.json",
        },
    ]
    client = FakeClient()
    _, out = await run(write_recipe(steps), client)
    assert "[load] skipped: out.csv has no rows" in out
    assert client.uploads == []
    assert "grist_update_csv" not in client.tools()
    assert json.loads((tmp_path / "work" / "load.json").read_text()) == {"skipped": True}


async def test_skip_if_empty_skips_an_empty_json_list(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    writer_script(tmp_path, "mk.py", {"out.json": "[]"})
    steps = [
        {"id": "mk", "run": "scripts/mk.py", "outputs": {"j": "out.json"}},
        {"id": "load", "tool": "grist_import_csv", "file": "mk.j", "skip_if_empty": True, "args": {}},
    ]
    client = FakeClient()
    await run(write_recipe(steps), client)
    assert client.uploads == []


async def test_script_outputs_feed_a_later_upload(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    writer_script(tmp_path, "mk.py", {"plan/a.csv": "k\n1\n", "plan/b.csv": "k\n2\n3\n", "log.txt": "ran"})
    steps = [
        {
            "id": "mk",
            "run": "scripts/mk.py",
            "outputs": {"a": "plan/a.csv", "b": "plan/b.csv"},
            "stdout": "mk.txt",
        },
        {"id": "load", "tool": "grist_import_csv", "file": "mk.b", "args": {}},
    ]
    client = FakeClient()
    await run(write_recipe(steps), client)
    assert [u[0] for u in client.uploads] == [tmp_path / "work" / "plan" / "b.csv"]


async def test_script_exit_10_stops_the_run_and_ends_the_session(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    script(tmp_path, "stop.py", "import sys\nsys.exit(10)\n")
    steps = [
        {"id": "stop", "run": "scripts/stop.py"},
        {"id": "after", "tool": "drive_list_files"},
    ]
    client = FakeClient()
    done, out = await run(write_recipe(steps), client)
    assert done is False
    assert "[stop] nothing to do, run stopped" in out
    assert client.tools() == [BEGIN, END]


async def test_template_step_renders_from_a_json_file(write_recipe: RecipeWriter, tmp_path: Path) -> None:
    writer_script(tmp_path, "data.py", {"data.json": json.dumps({"x": "</script>"})})
    steps = [
        {"id": "data", "run": "scripts/data.py", "outputs": {"json": "data.json"}},
        {"id": "page", "template": "templates/t.html", "data": "data.json", "output": "page.html"},
    ]
    recipe = write_recipe(steps)
    (tmp_path / "templates" / "t.html").write_text("<script>const D = /*PRIVACYFENCE_SYNC_DATA*/null;</script>")
    await run(recipe, FakeClient())
    page = (tmp_path / "work" / "page.html").read_text()
    assert "\\u003c/script\\u003e" in page
    assert "</script>" in page.replace("\\u003c/script\\u003e", "")


async def test_attended_waits_then_repeats_the_call(write_recipe: RecipeWriter) -> None:
    pending = {"status": "approval_pending", "approval_id": "A1", "url": "http://pf/approve/A1"}
    client = FakeClient(
        {
            "drive_list_files": [pending, {"files": []}],
            AWAIT: [{"A1": "pending"}, {"A1": "approved"}],
        }
    )
    _, out = await run(write_recipe([{"id": "one", "tool": "drive_list_files"}]), client, attended=True)
    assert "[one] waiting for approval: http://pf/approve/A1" in out
    assert client.tools() == ["drive_list_files", AWAIT, AWAIT, "drive_list_files"]
    assert client.args_of(AWAIT) == {"approval_ids": ["A1"], "timeout_seconds": 120}
    assert client.calls[0][1] == client.calls[-1][1]


async def test_attended_denial_raises(write_recipe: RecipeWriter) -> None:
    pending = {"status": "approval_pending", "approval_id": "A1", "url": "u"}
    client = FakeClient({"drive_list_files": pending, AWAIT: {"A1": "denied"}})
    with pytest.raises(ToolDenied, match="step one: denied"):
        await run(write_recipe([{"id": "one", "tool": "drive_list_files"}]), client, attended=True)


async def test_attended_gives_up_after_sixteen_minutes(write_recipe: RecipeWriter) -> None:
    pending = {"status": "approval_pending", "approval_id": "A1", "url": "u"}
    client = FakeClient({"drive_list_files": pending, AWAIT: {"A1": "pending"}})
    with pytest.raises(ToolDenied, match="no decision within 16 minutes"):
        await run(write_recipe([{"id": "one", "tool": "drive_list_files"}]), client, attended=True)
    assert client.tools().count(AWAIT) == 8


async def test_default_reason_names_recipe_and_step(write_recipe: RecipeWriter) -> None:
    steps = [
        {"id": "one", "tool": "drive_list_files"},
        {"id": "two", "tool": "drive_list_files", "reason": "custom why"},
    ]
    client = FakeClient()
    await run(write_recipe(steps), client)
    reasons = [args["reason"] for tool, args in client.calls if tool == "drive_list_files"]
    assert reasons == ["privacyfence-sync demo: step one", "custom why"]


def _readme_example(heading: str) -> str:
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    section = text.split(f"### {heading}", 1)[1]
    match = re.search(r"```yaml\n(.*?)```", section, re.S)
    assert match is not None
    return match.group(1)


async def test_readme_salesforce_example_runs_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "recipe.yaml").write_text(_readme_example("A Salesforce report mirrored into Grist"), encoding="utf-8")
    script(
        tmp_path,
        "datalake.py",
        """
        import pathlib, sys
        if sys.argv[1] == "plan":
            out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "upsert.csv").write_text("Opportunity_Id,Amount\\n006A,10\\n")
            (out / "flag_out.csv").write_text("Opportunity_Id,In_Report\\n")
            (out / "summary.json").write_text('{"rows": 1}')
            (out / "sync_log.csv").write_text("Run,Rows\\n1,1\\n")
        """,
    )
    script(tmp_path, "guard.py", "")

    def export(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(args["destination_dir"]) / "opps.csv"
        path.write_text("Opportunity_Id\n006A\n")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"delivery": "local_disk", "path": str(path), "sha256": sha}

    client = FakeClient(
        {
            "salesforce_export_report": export,
            "salesforce_run_report": {"summary": {"rows": 1}},
            "grist_update_csv": {"updated": 1},
        }
    )
    done, out = await run(tmp_path / "recipe.yaml", client)
    assert done is True
    assert client.tools()[0] == BEGIN and client.tools()[-1] == END
    assert [u[0].name for u in client.uploads] == ["upsert.csv", "sync_log.csv"]
    assert "[flag_out] skipped: flag_out.csv has no rows" in out
    assert "[commit] ..." in out
    work = tmp_path / "work"
    assert json.loads((work / "flag_out_result.json").read_text()) == {"skipped": True}
    assert json.loads((work / "upsert_result.json").read_text()) == {"updated": 1}
    assert client.args_of("salesforce_export_report")["file_name"] == "opps.csv"
