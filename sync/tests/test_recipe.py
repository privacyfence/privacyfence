from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import privacyfence_sync
from privacyfence_sync.recipe import (
    DOWNLOAD_TOOLS,
    EXPORT_TOOLS,
    STOP_EXIT_CODE,
    UPLOAD_TOOLS,
    RecipeError,
    RunStep,
    TemplateStep,
    ToolStep,
    load_recipe,
    produced_files,
)

README = Path(__file__).resolve().parents[1] / "README.md"


def readme_examples() -> list[str]:
    text = README.read_text(encoding="utf-8")
    section = text.split("\n## Examples", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```yaml\n(.*?)```", section, flags=re.DOTALL)
    return [b for b in blocks if b.startswith("version: 1")]


EXPORT = {"id": "exp", "tool": "grist_export_records", "args": {"doc_id": "d", "file_name": "e.csv"}}
RUN = {"id": "r", "run": "scripts/s.py", "outputs": {"o": "plan/o.csv"}, "stdout": "r.json"}
UPLOAD = {"id": "up", "tool": "grist_import_csv", "file": "exp", "args": {"doc_id": "d"}}


def bad(write_recipe: Any, steps: list[dict[str, Any]], message: str, **top: Any) -> None:
    path = write_recipe(steps, **top)
    with pytest.raises(RecipeError, match=message) as info:
        load_recipe(path)
    assert str(info.value).startswith(f"{path}: ")


def test_constants() -> None:
    assert STOP_EXIT_CODE == 10
    assert "jira_export_issues" in EXPORT_TOOLS
    assert "assets_export_objects" in EXPORT_TOOLS
    assert DOWNLOAD_TOOLS == {"drive_download_file"}
    assert "peek_upload_page" in UPLOAD_TOOLS


def test_package_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", missing)
    try:
        assert importlib.reload(privacyfence_sync).__version__ == "0.0.0.dev0"
    finally:
        monkeypatch.undo()
        importlib.reload(privacyfence_sync)


def test_readme_has_examples() -> None:
    assert len(readme_examples()) == 3


@pytest.mark.parametrize("index", [0, 1, 2])
def test_loads_each_readme_example(index: int, tmp_path: Path) -> None:
    text = readme_examples()[index]
    for step in yaml.safe_load(text)["steps"]:
        for key in ("run", "template"):
            if key in step:
                stub = tmp_path / step[key]
                stub.parent.mkdir(parents=True, exist_ok=True)
                stub.touch()
    path = tmp_path / "recipe.yaml"
    path.write_text(text, encoding="utf-8")
    recipe = load_recipe(path)
    assert recipe.steps
    assert recipe.work_dir == tmp_path.resolve() / "work"


def test_loads_all_step_kinds(write_recipe: Any, tmp_path: Path) -> None:
    steps = [
        {**EXPORT, "args": {"doc_id": "d", "file_name": "e.json"}},
        {**RUN, "args": ["{file:exp}", "{output:o}", "{work_dir}", "{recipe_dir}", "${exp:a.0.b}"]},
        {"id": "page", "template": "t/p.html", "data": "r.stdout", "output": "p.html"},
        {**UPLOAD, "id": "up2", "file": "r.o", "skip_if_empty": True, "reason": "why", "save_result": "res.json"},
    ]
    recipe = load_recipe(write_recipe(steps, server="http://x/mcp", work_dir="w"))
    assert recipe.name == "demo"
    assert recipe.server == "http://x/mcp"
    assert recipe.base_dir == tmp_path.resolve()
    assert recipe.work_dir == tmp_path.resolve() / "w"
    export, run, page, upload = recipe.steps
    assert isinstance(export, ToolStep) and export.args["file_name"] == "e.json"
    assert isinstance(run, RunStep)
    assert run.script == tmp_path.resolve() / "scripts/s.py"
    assert run.timeout_seconds == 600
    assert run.args[0] == "{file:exp}"
    assert isinstance(page, TemplateStep) and page.template.name == "p.html"
    assert isinstance(upload, ToolStep) and upload.skip_if_empty and upload.reason == "why"


def test_defaults(write_recipe: Any, tmp_path: Path) -> None:
    recipe = load_recipe(write_recipe([EXPORT]))
    assert recipe.server is None
    assert recipe.work_dir == tmp_path.resolve() / "work"
    step = recipe.steps[0]
    assert isinstance(step, ToolStep)
    assert (step.file, step.save_as, step.save_result, step.skip_if_empty, step.reason) == (
        None,
        None,
        None,
        False,
        None,
    )


def test_produced_files(write_recipe: Any) -> None:
    other = {"id": "list", "tool": "drive_list_files", "save_result": "l.json"}
    plain = {"id": "plain", "tool": "grist_add_records"}
    download = {"id": "dl", "tool": "drive_download_file", "save_as": "w.xlsx"}
    page = {"id": "page", "template": "p.html", "data": "r.stdout", "output": "p.html"}
    recipe = load_recipe(write_recipe([EXPORT, RUN, other, plain, download, page]))
    got = [produced_files(s) for s in recipe.steps]
    assert got == [
        {"exp": "e.csv"},
        {"r.o": "plan/o.csv", "r.stdout": "r.json"},
        {"list": "l.json"},
        {},
        {"dl": "w.xlsx"},
        {"page": "p.html"},
    ]


def test_run_step_without_stdout(write_recipe: Any) -> None:
    step = load_recipe(write_recipe([{"id": "r", "run": "s.py"}])).steps[0]
    assert produced_files(step) == {}


# --- file-level and top-level rules -------------------------------------------------------------


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(RecipeError, match="cannot read"):
        load_recipe(tmp_path / "missing.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "r.yaml"
    path.write_text("a: [", encoding="utf-8")
    with pytest.raises(RecipeError, match="not valid YAML"):
        load_recipe(path)


def test_not_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / "r.yaml"
    path.write_text("- a\n", encoding="utf-8")
    with pytest.raises(RecipeError, match="must be a mapping"):
        load_recipe(path)


def test_unknown_top_level_key(write_recipe: Any) -> None:
    bad(write_recipe, [EXPORT], "unknown top-level key extra", extra=1)


@pytest.mark.parametrize("version", [2, "1", True, 1.0])
def test_bad_version(write_recipe: Any, version: Any) -> None:
    bad(write_recipe, [EXPORT], "version must be 1", version=version)


@pytest.mark.parametrize("name", [None, 5, "", "Upper", "-lead", "a" * 64, "has space"])
def test_bad_name(write_recipe: Any, name: Any, tmp_path: Path) -> None:
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "name": name, "steps": [EXPORT]}), encoding="utf-8")
    with pytest.raises(RecipeError, match="name must be"):
        load_recipe(path)


def test_good_name_limits(write_recipe: Any) -> None:
    assert load_recipe(write_recipe([EXPORT], name="a" * 63)).name == "a" * 63
    assert load_recipe(write_recipe([EXPORT], name="0-a")).name == "0-a"


def test_bad_server(write_recipe: Any) -> None:
    bad(write_recipe, [EXPORT], "server must be a string", server=5)


@pytest.mark.parametrize("work_dir", [5, ""])
def test_bad_work_dir(write_recipe: Any, work_dir: Any) -> None:
    bad(write_recipe, [EXPORT], "work_dir must be", work_dir=work_dir)


def test_steps_missing(tmp_path: Path) -> None:
    path = tmp_path / "r.yaml"
    path.write_text("version: 1\nname: demo\n", encoding="utf-8")
    with pytest.raises(RecipeError, match="1 to 50"):
        load_recipe(path)


def test_steps_empty(write_recipe: Any) -> None:
    bad(write_recipe, [], "1 to 50")


def test_steps_limit(write_recipe: Any) -> None:
    def many(n: int) -> list[dict[str, Any]]:
        return [{"id": f"s{i}", "tool": "x_tool"} for i in range(n)]

    assert len(load_recipe(write_recipe(many(50))).steps) == 50
    bad(write_recipe, many(51), "1 to 50")


# --- step-level rules ---------------------------------------------------------------------------


def test_step_not_a_mapping(write_recipe: Any) -> None:
    bad(write_recipe, ["nope"], "step #1: must be a mapping")  # type: ignore[list-item]


@pytest.mark.parametrize("step_id", [None, 5, "", "Upper", "a" * 41, "has space"])
def test_bad_step_id(write_recipe: Any, step_id: Any) -> None:
    bad(write_recipe, [{"id": step_id, "tool": "x"}], r"step #1: id must be")


def test_duplicate_step_id(write_recipe: Any) -> None:
    bad(write_recipe, [EXPORT, {**EXPORT, "args": {"file_name": "o.csv"}}], "step exp: duplicate id")


@pytest.mark.parametrize(
    "step",
    [
        {"id": "s"},
        {"id": "s", "tool": "x", "run": "a.py"},
        {"id": "s", "tool": "x", "template": "t.html"},
        {"id": "s", "run": "a.py", "template": "t.html"},
    ],
)
def test_exactly_one_kind(write_recipe: Any, step: dict[str, Any]) -> None:
    bad(write_recipe, [step], "step s: needs exactly one of tool, run, template")


@pytest.mark.parametrize(
    "step",
    [
        {"id": "s", "tool": "x", "oops": 1},
        {"id": "s", "run": "a.py", "args": [], "file": "x"},
        {"id": "s", "template": "t.html", "data": "d", "output": "o.html", "args": {}},
    ],
)
def test_unknown_key_per_kind(write_recipe: Any, step: dict[str, Any]) -> None:
    bad(write_recipe, [step], "step s: unknown key (oops|file|args)")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"tool": ""}, "tool must be a string"),
        ({"tool": 5}, "tool must be a string"),
        ({"args": []}, "args must be a mapping"),
        ({"args": {1: 2}}, "args keys must be strings"),
        ({"reason": 5}, "reason must be a string"),
        ({"skip_if_empty": "yes"}, "skip_if_empty must be true or false"),
        ({"save_result": 5}, "save_result must be a string"),
    ],
)
def test_tool_step_types(write_recipe: Any, extra: dict[str, Any], message: str) -> None:
    bad(write_recipe, [{"id": "s", "tool": "x", **extra}], f"step s: {message}")


# --- export and download rules ------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(EXPORT_TOOLS))
def test_export_needs_file_name(write_recipe: Any, tool: str) -> None:
    bad(write_recipe, [{"id": "s", "tool": tool}], f"step s: {tool} needs args.file_name")
    bad(write_recipe, [{"id": "s", "tool": tool, "args": {"file_name": 5}}], "needs args.file_name")


@pytest.mark.parametrize("tool", sorted(EXPORT_TOOLS))
def test_export_rejects_destination_dir_and_save_result(write_recipe: Any, tool: str) -> None:
    args = {"file_name": "a.csv", "destination_dir": "/x"}
    bad(write_recipe, [{"id": "s", "tool": tool, "args": args}], "step s: do not give destination_dir")
    ok = {"file_name": "a.csv"}
    bad(write_recipe, [{"id": "s", "tool": tool, "args": ok, "save_result": "r.json"}], "save_result is not allowed")


def test_download_rules(write_recipe: Any) -> None:
    bad(write_recipe, [{"id": "s", "tool": "drive_download_file"}], "step s: drive_download_file needs save_as")
    bad(
        write_recipe,
        [{"id": "s", "tool": "drive_download_file", "save_as": "a", "args": {"destination_dir": "/x"}}],
        "do not give destination_dir",
    )
    bad(
        write_recipe,
        [{"id": "s", "tool": "drive_download_file", "save_as": "a", "save_result": "r.json"}],
        "save_result is not allowed",
    )


def test_save_as_only_on_download(write_recipe: Any) -> None:
    bad(write_recipe, [{"id": "s", "tool": "x", "save_as": "a"}], "step s: save_as is only for download tools")
    bad(write_recipe, [{**EXPORT, "save_as": "a"}], "save_as is only for download tools")


@pytest.mark.parametrize("name", ["a/b.csv", "a\\b.csv", ".hidden.csv", "a\0b.csv", ""])
def test_plain_names(write_recipe: Any, name: str) -> None:
    export = {"id": "s", "tool": "grist_export_records", "args": {"file_name": name}}
    bad(write_recipe, [export], "file_name must be a plain file name")
    download = {"id": "s", "tool": "drive_download_file", "save_as": name}
    bad(write_recipe, [download], "save_as must be a plain file name")
    result = {"id": "s", "tool": "x", "save_result": name}
    bad(write_recipe, [result], "save_result must be a plain file name")
    run = {"id": "s", "run": "a.py", "stdout": name}
    bad(write_recipe, [run], "stdout must be a plain file name")
    template = {"id": "s", "template": "t.html", "data": "d", "output": name}
    bad(write_recipe, [EXPORT, template], "output must be a plain file name")


def test_export_extension(write_recipe: Any) -> None:
    export = {"id": "s", "tool": "grist_export_records", "args": {"file_name": "a.txt"}}
    bad(write_recipe, [export], "file_name must end in .csv or .json")
    for good in ("a.csv", "a.json"):
        export["args"] = {"file_name": good}
        load_recipe(write_recipe([export]))


def test_save_result_extension(write_recipe: Any) -> None:
    bad(write_recipe, [{"id": "s", "tool": "x", "save_result": "r.csv"}], "save_result must end in .json")


# --- upload rules -------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(UPLOAD_TOOLS))
def test_upload_needs_file(write_recipe: Any, tool: str) -> None:
    bad(write_recipe, [EXPORT, {"id": "s", "tool": tool}], f"step s: {tool} needs file")


@pytest.mark.parametrize("key", ["local_path", "upload_id", "html"])
def test_upload_rejects_reserved_args(write_recipe: Any, key: str) -> None:
    bad(write_recipe, [EXPORT, {**UPLOAD, "args": {key: "x"}}], f"step up: do not give {key}")


def test_file_only_on_upload_tools(write_recipe: Any) -> None:
    bad(write_recipe, [EXPORT, {"id": "s", "tool": "x", "file": "exp"}], "step s: file is only for upload tools")


def test_skip_if_empty_only_on_upload_tools(write_recipe: Any) -> None:
    step = {"id": "s", "tool": "x", "skip_if_empty": True}
    bad(write_recipe, [step], "step s: skip_if_empty is only for upload tools")
    load_recipe(write_recipe([{"id": "s", "tool": "x", "skip_if_empty": False}]))


# --- run and template rules ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"args": "a"}, "args must be a list of strings"),
        ({"args": [1]}, "args must be a list of strings"),
        ({"stdout": 5}, "stdout must be a string"),
        ({"outputs": []}, "outputs must be a mapping"),
        ({"outputs": {f"o{i}": f"p{i}" for i in range(21)}}, "outputs must be a mapping"),
        ({"outputs": {"Bad": "p"}}, "invalid output name"),
        ({"outputs": {"a" * 41: "p"}}, "invalid output name"),
        ({"outputs": {1: "p"}}, "invalid output name"),
        ({"outputs": {"o": 5}}, "output o must be a relative path"),
        ({"outputs": {"o": ""}}, "output o must be a relative path"),
        ({"outputs": {"o": "/abs"}}, "output o must be a relative path"),
        ({"outputs": {"o": "a/../b"}}, "output o must be a relative path"),
        ({"outputs": {"o": "a\\b"}}, "output o must be a relative path"),
        ({"outputs": {"o": "a\0b"}}, "output o must be a relative path"),
        ({"stdout": "s.json", "outputs": {"stdout": "p"}}, "output name stdout clashes with stdout"),
        ({"timeout_seconds": 0}, "timeout_seconds must be"),
        ({"timeout_seconds": 3601}, "timeout_seconds must be"),
        ({"timeout_seconds": "5"}, "timeout_seconds must be"),
        ({"timeout_seconds": True}, "timeout_seconds must be"),
    ],
)
def test_run_step_rules(write_recipe: Any, extra: dict[str, Any], message: str) -> None:
    bad(write_recipe, [{"id": "s", "run": "a.py", **extra}], f"step s: {message}")


def test_run_step_limits_accepted(write_recipe: Any) -> None:
    outputs = {f"o{i}": f"sub/p{i}.csv" for i in range(20)}
    steps = [{"id": "s", "run": "a.py", "outputs": outputs, "timeout_seconds": 3600}]
    step = load_recipe(write_recipe(steps)).steps[0]
    assert isinstance(step, RunStep) and step.timeout_seconds == 3600 and len(step.outputs) == 20
    steps = [{"id": "s", "run": "a.py", "stdout": "s.txt", "outputs": {"stdout": "x/s.txt"}, "timeout_seconds": 1}]
    with pytest.raises(RecipeError):
        load_recipe(write_recipe(steps))


@pytest.mark.parametrize("key", ["run", "template"])
@pytest.mark.parametrize("value", ["../outside.py", "/etc/passwd", "a/../../x"])
def test_path_outside_recipe_folder(write_recipe: Any, key: str, value: str) -> None:
    step: dict[str, Any] = {"id": "s", key: value}
    if key == "template":
        step.update(data="exp", output="o.html")
    path = write_recipe([EXPORT])
    path.write_text(yaml.safe_dump({"version": 1, "name": "d", "steps": [EXPORT, step]}), encoding="utf-8")
    with pytest.raises(RecipeError, match=f"step s: {key} must be inside the recipe's folder"):
        load_recipe(path)


def test_path_inside_with_dotdot_is_fine(write_recipe: Any) -> None:
    load_recipe(write_recipe([{"id": "s", "run": "a/../b.py"}]))


@pytest.mark.parametrize("value", [None, "", 5])
def test_path_must_be_string(write_recipe: Any, value: Any) -> None:
    path = write_recipe([EXPORT])
    steps = [EXPORT, {"id": "s", "run": value}]
    path.write_text(yaml.safe_dump({"version": 1, "name": "d", "steps": steps}), encoding="utf-8")
    with pytest.raises(RecipeError, match="run must be a path string"):
        load_recipe(path)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"output": "o.html"}, "template needs data"),
        ({"data": 5, "output": "o.html"}, "template needs data"),
        ({"data": "exp"}, "template needs output"),
    ],
)
def test_template_requires_data_and_output(write_recipe: Any, extra: dict[str, Any], message: str) -> None:
    bad(write_recipe, [EXPORT, {"id": "s", "template": "t.html", **extra}], f"step s: {message}")


def test_template_data_must_be_json(write_recipe: Any) -> None:
    step = {"id": "s", "template": "t.html", "data": "exp", "output": "o.html"}
    bad(write_recipe, [EXPORT, step], "step s: data must be a JSON file")
    json_export = {**EXPORT, "args": {"file_name": "e.json"}}
    load_recipe(write_recipe([json_export, step]))


# --- references ---------------------------------------------------------------------------------


def test_file_reference_must_be_earlier(write_recipe: Any) -> None:
    bad(write_recipe, [UPLOAD, EXPORT], "step up: exp is not produced by an earlier step")
    bad(write_recipe, [EXPORT, {**UPLOAD, "file": "nope"}], "step up: nope is not produced")
    template = {"id": "s", "template": "t.html", "data": "nope", "output": "o.html"}
    bad(write_recipe, [EXPORT, template], "step s: nope is not produced")
    run = {"id": "s", "run": "a.py", "args": ["{file:nope}"]}
    bad(write_recipe, [EXPORT, run], "step s: nope is not produced")


def test_run_step_cannot_refer_to_its_own_outputs_as_files(write_recipe: Any) -> None:
    run = {"id": "s", "run": "a.py", "outputs": {"o": "o.csv"}, "args": ["{file:s.o}"]}
    bad(write_recipe, [run], "step s: s.o is not produced")


def test_value_reference_checks(write_recipe: Any) -> None:
    json_export = {**EXPORT, "args": {"file_name": "e.json"}}

    def tool(args: Any) -> dict[str, Any]:
        return {"id": "s", "tool": "x", "args": args}

    bad(write_recipe, [EXPORT, tool({"a": "${exp:k}"})], "step s: exp is not a JSON file")
    bad(write_recipe, [tool({"a": "${nope:k}"})], "step s: nope is not produced")
    bad(write_recipe, [json_export, tool({"a": "${exp:a..b}"})], r"bad path in \$\{exp:a\.\.b\}")
    bad(write_recipe, [json_export, tool({"a": "${exp:.a}"})], "bad path")
    # at any depth, in dict values and lists
    bad(write_recipe, [json_export, tool({"a": [{"b": ["x ${nope:k} y"]}]})], "nope is not produced")
    load_recipe(write_recipe([json_export, tool({"a": [{"b": ["x ${exp:a.0.b-c_d}", 3, None]}], "n": 1})]))


def test_value_reference_in_run_args(write_recipe: Any) -> None:
    json_export = {**EXPORT, "args": {"file_name": "e.json"}}
    bad(write_recipe, [EXPORT, {"id": "s", "run": "a.py", "args": ["${exp:k}"]}], "exp is not a JSON file")
    bad(write_recipe, [json_export, {"id": "s", "run": "a.py", "args": ["${exp:k..}"]}], "bad path")
    load_recipe(write_recipe([json_export, {"id": "s", "run": "a.py", "args": ["${exp:k}"]}]))


def test_value_reference_from_run_output(write_recipe: Any) -> None:
    pick = {"id": "pick", "run": "a.py", "stdout": "pick.json"}
    step = {"id": "dl", "tool": "drive_download_file", "args": {"file_id": "${pick.stdout:id}"}, "save_as": "w.xlsx"}
    load_recipe(write_recipe([pick, step]))


def test_output_placeholder_must_be_own_output(write_recipe: Any) -> None:
    run = {"id": "s", "run": "a.py", "outputs": {"o": "o.csv"}, "args": ["{output:other}"]}
    bad(write_recipe, [run], r"step s: \{output:other\} is not one of its outputs")
    run["args"] = ["--out={output:o}"]
    load_recipe(write_recipe([run]))


@pytest.mark.parametrize("typo", ["{fil:x}", "{workdir}", "{recipe}", "{x:1}", "pre{abc}post"])
def test_unknown_brace_group_is_an_error(write_recipe: Any, typo: str) -> None:
    bad(write_recipe, [{"id": "s", "run": "a.py", "args": [typo]}], "step s: unknown placeholder")


@pytest.mark.parametrize(
    "text",
    ['{"json": 1}', "{A}", "{a-b}", "${x}", "plain", "{1}", '{"a": {"b": 2}}', "{}"],
)
def test_other_text_passes_as_written(write_recipe: Any, text: str) -> None:
    load_recipe(write_recipe([{"id": "s", "run": "a.py", "args": [text]}]))


def test_duplicate_produced_path(write_recipe: Any) -> None:
    other = {**EXPORT, "id": "exp2"}
    bad(write_recipe, [EXPORT, other], "step exp2: e.csv is produced by more than one step")
    run_a = {"id": "a", "run": "a.py", "outputs": {"o": "x/o.csv"}}
    run_b = {"id": "b", "run": "a.py", "outputs": {"o": "x/o.csv"}}
    bad(write_recipe, [run_a, run_b], "x/o.csv is produced by more than one step")
    clash = {"id": "c", "run": "a.py", "stdout": "same.json", "outputs": {"o": "same.json"}}
    bad(write_recipe, [clash], "same.json is produced by more than one step")
