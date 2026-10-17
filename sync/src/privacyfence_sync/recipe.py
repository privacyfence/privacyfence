"""Load and validate a recipe file. Pure apart from reading the file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from privacyfence_sync.refs import FILE_PLACEHOLDER, VALUE_REF

EXPORT_TOOLS = frozenset({
    "grist_export_records", "grist_export_sql", "salesforce_export_report",
    "jira_export_issues", "assets_export_objects",
})
DOWNLOAD_TOOLS = frozenset({"drive_download_file"})
UPLOAD_TOOLS = frozenset({"grist_import_csv", "grist_update_csv", "peek_upload_page"})
STOP_EXIT_CODE = 10

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_STEP_ID = re.compile(r"[a-z0-9_-]{1,40}")
_OUTPUT_NAME = re.compile(r"[a-z0-9_-]{1,40}")
_VALUE_PATH = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")
_BRACE_GROUP = re.compile(r"(?<!\$)\{([a-z_]+)(?::[^}]*)?\}")
_KNOWN_BRACES = frozenset({"file", "output", "work_dir", "recipe_dir"})

_TOP_KEYS = frozenset({"version", "name", "server", "work_dir", "steps"})
_TOOL_KEYS = frozenset({"id", "tool", "args", "file", "save_as", "save_result", "skip_if_empty", "reason"})
_RUN_KEYS = frozenset({"id", "run", "args", "outputs", "stdout", "timeout_seconds"})
_TEMPLATE_KEYS = frozenset({"id", "template", "data", "output"})
_KIND_KEYS = {"tool": _TOOL_KEYS, "run": _RUN_KEYS, "template": _TEMPLATE_KEYS}


class RecipeError(ValueError):
    """The recipe file is unreadable or breaks a rule."""


@dataclass(frozen=True)
class ToolStep:
    id: str
    tool: str
    args: dict[str, Any]
    file: str | None
    save_as: str | None
    save_result: str | None
    skip_if_empty: bool
    reason: str | None


@dataclass(frozen=True)
class RunStep:
    id: str
    script: Path
    args: tuple[str, ...]
    outputs: dict[str, str]
    stdout: str | None
    timeout_seconds: int


@dataclass(frozen=True)
class TemplateStep:
    id: str
    template: Path
    data: str
    output: str


Step = ToolStep | RunStep | TemplateStep


@dataclass(frozen=True)
class Recipe:
    name: str
    server: str | None
    work_dir: Path
    base_dir: Path
    steps: tuple[Step, ...]


def produced_files(step: Step) -> dict[str, str]:
    """The files a step produces: reference -> path relative to the work folder."""
    if isinstance(step, ToolStep):
        if step.tool in EXPORT_TOOLS:
            return {step.id: str(step.args["file_name"])}
        if step.tool in DOWNLOAD_TOOLS:
            return {step.id: str(step.save_as)}
        if step.save_result is not None:
            return {step.id: step.save_result}
        return {}
    if isinstance(step, RunStep):
        files = {f"{step.id}.{name}": path for name, path in step.outputs.items()}
        if step.stdout is not None:
            files[f"{step.id}.stdout"] = step.stdout
        return files
    return {step.id: step.output}


def load_recipe(path: Path) -> Recipe:
    """Read, parse and validate a recipe file."""

    def fail(message: str) -> RecipeError:
        return RecipeError(f"{path}: {message}")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise fail(f"cannot read the file: {exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise fail(f"not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise fail("a recipe must be a mapping")

    for key in doc:
        if key not in _TOP_KEYS:
            raise fail(f"unknown top-level key {key}")
    if type(doc.get("version")) is not int or doc["version"] != 1:
        raise fail("version must be 1")
    name = doc.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise fail("name must be lowercase letters, digits and dashes, 1 to 63 characters")
    server = doc.get("server")
    if server is not None and not isinstance(server, str):
        raise fail("server must be a string")
    work_dir = doc.get("work_dir", "work")
    if not isinstance(work_dir, str) or not work_dir:
        raise fail("work_dir must be a non-empty string")
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 50:
        raise fail("steps must be a list of 1 to 50 steps")

    base_dir = path.resolve().parent
    steps: list[Step] = []
    produced: dict[str, str] = {}
    paths: set[str] = set()
    seen_ids: set[str] = set()
    for position, raw in enumerate(raw_steps, start=1):
        step = _build_step(raw, position, seen_ids, base_dir, fail)
        _check_references(step, produced, fail)
        for ref, file_path in produced_files(step).items():
            if file_path in paths:
                raise fail(f"step {step.id}: {file_path} is produced by more than one step")
            paths.add(file_path)
            produced[ref] = file_path
        seen_ids.add(step.id)
        steps.append(step)

    return Recipe(
        name=name,
        server=server,
        work_dir=base_dir / work_dir,
        base_dir=base_dir,
        steps=tuple(steps),
    )


def _build_step(raw: Any, position: int, seen_ids: set[str], base_dir: Path, fail: Any) -> Step:
    if not isinstance(raw, dict):
        raise fail(f"step #{position}: must be a mapping")
    step_id = raw.get("id")
    if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
        raise fail(f"step #{position}: id must be 1 to 40 of lowercase letters, digits, - and _")
    if step_id in seen_ids:
        raise fail(f"step {step_id}: duplicate id")
    kinds = [kind for kind in _KIND_KEYS if kind in raw]
    if len(kinds) != 1:
        raise fail(f"step {step_id}: needs exactly one of tool, run, template")
    kind = kinds[0]
    for key in raw:
        if key not in _KIND_KEYS[kind]:
            raise fail(f"step {step_id}: unknown key {key}")
    if kind == "tool":
        return _build_tool_step(step_id, raw, fail)
    if kind == "run":
        return _build_run_step(step_id, raw, base_dir, fail)
    return _build_template_step(step_id, raw, base_dir, fail)


def _optional_str(step_id: str, raw: dict[str, Any], key: str, fail: Any) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise fail(f"step {step_id}: {key} must be a string")
    return value


def _plain_name(step_id: str, key: str, value: str, fail: Any) -> None:
    if not value or "/" in value or "\\" in value or "\0" in value or value.startswith("."):
        raise fail(f"step {step_id}: {key} must be a plain file name")


def _inside(step_id: str, key: str, value: Any, base_dir: Path, fail: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise fail(f"step {step_id}: {key} must be a path string")
    resolved = (base_dir / value).resolve()
    if not resolved.is_relative_to(base_dir):
        raise fail(f"step {step_id}: {key} must be inside the recipe's folder")
    return resolved


def _build_tool_step(step_id: str, raw: dict[str, Any], fail: Any) -> ToolStep:
    tool = raw["tool"]
    if not isinstance(tool, str) or not tool:
        raise fail(f"step {step_id}: tool must be a string")
    args = raw.get("args", {})
    if not isinstance(args, dict):
        raise fail(f"step {step_id}: args must be a mapping")
    if not all(isinstance(key, str) for key in args):
        raise fail(f"step {step_id}: args keys must be strings")
    file = _optional_str(step_id, raw, "file", fail)
    save_as = _optional_str(step_id, raw, "save_as", fail)
    save_result = _optional_str(step_id, raw, "save_result", fail)
    reason = _optional_str(step_id, raw, "reason", fail)
    skip_if_empty = raw.get("skip_if_empty", False)
    if not isinstance(skip_if_empty, bool):
        raise fail(f"step {step_id}: skip_if_empty must be true or false")

    is_export = tool in EXPORT_TOOLS
    is_download = tool in DOWNLOAD_TOOLS
    is_upload = tool in UPLOAD_TOOLS
    if is_export or is_download:
        if "destination_dir" in args:
            raise fail(f"step {step_id}: do not give destination_dir, privacyfence-sync sets it")
        if save_result is not None:
            raise fail(f"step {step_id}: save_result is not allowed on {tool}")
    if is_export:
        file_name = args.get("file_name")
        if not isinstance(file_name, str):
            raise fail(f"step {step_id}: {tool} needs args.file_name")
        _plain_name(step_id, "file_name", file_name, fail)
        if not file_name.endswith((".csv", ".json")):
            raise fail(f"step {step_id}: file_name must end in .csv or .json")
    if is_download and save_as is None:
        raise fail(f"step {step_id}: {tool} needs save_as")
    if save_as is not None:
        if not is_download:
            raise fail(f"step {step_id}: save_as is only for download tools")
        _plain_name(step_id, "save_as", save_as, fail)
    if save_result is not None:
        _plain_name(step_id, "save_result", save_result, fail)
        if not save_result.endswith(".json"):
            raise fail(f"step {step_id}: save_result must end in .json")
    if is_upload:
        if file is None:
            raise fail(f"step {step_id}: {tool} needs file")
        for key in ("local_path", "upload_id", "html"):
            if key in args:
                raise fail(f"step {step_id}: do not give {key}, privacyfence-sync sets it")
    else:
        if file is not None:
            raise fail(f"step {step_id}: file is only for upload tools")
        if skip_if_empty:
            raise fail(f"step {step_id}: skip_if_empty is only for upload tools")

    return ToolStep(step_id, tool, args, file, save_as, save_result, skip_if_empty, reason)


def _build_run_step(step_id: str, raw: dict[str, Any], base_dir: Path, fail: Any) -> RunStep:
    script = _inside(step_id, "run", raw["run"], base_dir, fail)
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise fail(f"step {step_id}: args must be a list of strings")
    stdout = _optional_str(step_id, raw, "stdout", fail)
    if stdout is not None:
        _plain_name(step_id, "stdout", stdout, fail)
    outputs = raw.get("outputs", {})
    if not isinstance(outputs, dict) or len(outputs) > 20:
        raise fail(f"step {step_id}: outputs must be a mapping of at most 20 entries")
    for out_name, out_path in outputs.items():
        if not isinstance(out_name, str) or not _OUTPUT_NAME.fullmatch(out_name):
            raise fail(f"step {step_id}: invalid output name {out_name}")
        if out_name == "stdout" and stdout is not None:
            raise fail(f"step {step_id}: output name stdout clashes with stdout")
        if (
            not isinstance(out_path, str)
            or not out_path
            or "\\" in out_path
            or "\0" in out_path
            or out_path.startswith("/")
            or ".." in out_path.split("/")
        ):
            raise fail(f"step {step_id}: output {out_name} must be a relative path inside the work folder")
    timeout = raw.get("timeout_seconds", 600)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise fail(f"step {step_id}: timeout_seconds must be an integer from 1 to 3600")
    return RunStep(step_id, script, tuple(args), dict(outputs), stdout, timeout)


def _build_template_step(step_id: str, raw: dict[str, Any], base_dir: Path, fail: Any) -> TemplateStep:
    template = _inside(step_id, "template", raw["template"], base_dir, fail)
    data = raw.get("data")
    if not isinstance(data, str):
        raise fail(f"step {step_id}: template needs data")
    output = raw.get("output")
    if not isinstance(output, str):
        raise fail(f"step {step_id}: template needs output")
    _plain_name(step_id, "output", output, fail)
    return TemplateStep(step_id, template, data, output)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def _check_references(step: Step, produced: dict[str, str], fail: Any) -> None:
    def need_file(ref: str) -> None:
        if ref not in produced:
            raise fail(f"step {step.id}: {ref} is not produced by an earlier step")

    def check_value_refs(text: str) -> None:
        for match in VALUE_REF.finditer(text):
            ref, value_path = match.group(1), match.group(2)
            need_file(ref)
            if not produced[ref].endswith(".json"):
                raise fail(f"step {step.id}: {ref} is not a JSON file")
            if not _VALUE_PATH.fullmatch(value_path):
                raise fail(f"step {step.id}: bad path in ${{{ref}:{value_path}}}")

    if isinstance(step, ToolStep):
        if step.file is not None:
            need_file(step.file)
        for text in _strings(step.args):
            check_value_refs(text)
    elif isinstance(step, TemplateStep):
        need_file(step.data)
        if not produced[step.data].endswith(".json"):
            raise fail(f"step {step.id}: data must be a JSON file")
    else:
        for text in step.args:
            check_value_refs(text)
            for match in FILE_PLACEHOLDER.finditer(text):
                if match.group(1) == "file":
                    need_file(match.group(2))
                elif match.group(1) == "output" and match.group(2) not in step.outputs:
                    raise fail(f"step {step.id}: {{output:{match.group(2)}}} is not one of its outputs")
            for brace in _BRACE_GROUP.finditer(text):
                if brace.group(1) not in _KNOWN_BRACES:
                    raise fail(f"step {step.id}: unknown placeholder {brace.group(0)}")
