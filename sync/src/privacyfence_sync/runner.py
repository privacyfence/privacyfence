"""Execute a recipe's steps in order."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

import httpx2

from . import refs
from .client import RecipeRuntimeError, SyncClient, SyncError, ToolDenied, ToolFailed
from .recipe import DOWNLOAD_TOOLS, EXPORT_TOOLS, Recipe, RunStep, TemplateStep, ToolStep, produced_files
from .template import render_template
from .transform import StopRun, run_script

_FETCH_PATH = "/mcp-files/fetch/"
_APPROVAL_POLLS = 8
_APPROVAL_POLL_SECONDS = 120
_UNATTENDED_DISABLED = (
    "This PrivacyFence does not allow unattended sessions (unattended_sessions.enabled in "
    "org_config.json). Ask your administrator, or run with --attended."
)


async def run_recipe(recipe: Recipe, client: SyncClient, *, attended: bool, out: TextIO) -> bool:
    """True when every step ran, False when a script step stopped the run (exit 10)."""
    work_dir = recipe.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    session_reason = f"privacyfence-sync {recipe.name}"
    if not attended:
        try:
            await client.call("privacyfence_begin_unattended_session", {"reason": f"{session_reason}: scheduled run"})
        except ToolFailed:
            raise SyncError(_UNATTENDED_DISABLED) from None
    try:
        return await _run_steps(recipe, client, attended, out)
    finally:
        if not attended:
            with contextlib.suppress(Exception):
                await client.call("privacyfence_end_unattended_session", {"reason": f"{session_reason}: run finished"})


async def _run_steps(recipe: Recipe, client: SyncClient, attended: bool, out: TextIO) -> bool:
    work_dir = recipe.work_dir
    files: dict[str, Path] = {}

    def read_json(ref: str) -> Any:
        try:
            return json.loads(files[ref].read_text(encoding="utf-8"))
        except ValueError:
            raise refs.RefError(f"{ref} is not a JSON file") from None

    for step in recipe.steps:
        out.write(f"[{step.id}] ...\n")
        try:
            if isinstance(step, ToolStep):
                await _tool_step(recipe, step, client, files, read_json, attended, out)
            elif isinstance(step, RunStep):
                await _run_step(recipe, step, files, read_json, out)
            else:
                _template_step(recipe, step, files, read_json)
        except StopRun:
            out.write(f"[{step.id}] nothing to do, run stopped\n")
            return False
        except refs.RefError as exc:
            raise RecipeRuntimeError(f"step {step.id}: {exc}") from None
        for ref, relative in produced_files(step).items():
            files[ref] = (work_dir / relative).absolute()
    return True


async def _run_step(recipe: Recipe, step: RunStep, files: dict[str, Path], read_json: Any, out: TextIO) -> None:
    work_dir = recipe.work_dir
    outputs = {name: work_dir / path for name, path in step.outputs.items()}
    argv = [
        refs.expand_run_arg(
            arg,
            files=files,
            outputs=outputs,
            work_dir=work_dir,
            recipe_dir=recipe.base_dir,
            read_json=read_json,
        )
        for arg in step.args
    ]
    await asyncio.to_thread(run_script, step, argv, work_dir)


def _template_step(recipe: Recipe, step: TemplateStep, files: dict[str, Path], read_json: Any) -> None:
    template_text = step.template.read_text(encoding="utf-8")
    page = render_template(template_text, read_json(step.data))
    (recipe.work_dir / step.output).write_text(page, encoding="utf-8")


def _json_text_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in args.items()}


async def _tool_step(
    recipe: Recipe,
    step: ToolStep,
    client: SyncClient,
    files: dict[str, Path],
    read_json: Any,
    attended: bool,
    out: TextIO,
) -> None:
    work_dir = recipe.work_dir
    reason = step.reason or f"privacyfence-sync {recipe.name}: step {step.id}"
    args = _json_text_args(refs.substitute_value_refs(step.args, read_json))
    args["reason"] = reason
    result: Any

    if step.tool in EXPORT_TOOLS:
        args["destination_dir"] = str(work_dir)
        result = await _call(client, step, step.tool, args, attended, out)
        await _receive_file(step, result, work_dir / str(step.args["file_name"]), client, work_dir)
        return
    if step.tool in DOWNLOAD_TOOLS:
        holder = work_dir / f".download-{step.id}"
        holder.mkdir(parents=True, exist_ok=True)
        args["destination_dir"] = str(holder)
        try:
            result = await _call(client, step, step.tool, args, attended, out)
            await _receive_file(step, result, work_dir / str(step.save_as), client, work_dir)
        finally:
            shutil.rmtree(holder, ignore_errors=True)
        return

    if step.file is not None:
        source = files[step.file]
        if step.skip_if_empty and not _has_rows(source):
            out.write(f"[{step.id}] skipped: {source.name} has no rows\n")
            result = {"skipped": True}
            _save_result(work_dir, step, result)
            return
        try:
            args["upload_id"] = await client.upload(source, reason)
        except httpx2.HTTPStatusError as exc:
            raise _transfer_failed(step, exc) from None
    result = await _call(client, step, step.tool, args, attended, out)
    _save_result(work_dir, step, result)


def _save_result(work_dir: Path, step: ToolStep, result: Any) -> None:
    if step.save_result is not None:
        (work_dir / step.save_result).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def _has_rows(path: Path) -> bool:
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8")) != []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.reader(handle)
        next(rows, None)
        return any(any(cell.strip() for cell in row) for row in rows)


def _transfer_failed(step: ToolStep, exc: httpx2.HTTPStatusError) -> ToolFailed:
    return ToolFailed(f"step {step.id}: the file transfer failed with HTTP {exc.response.status_code}")


async def _call(
    client: SyncClient, step: ToolStep, tool: str, args: dict[str, Any], attended: bool, out: TextIO
) -> Any:
    result = await client.call(tool, args)
    if not (attended and isinstance(result, dict) and result.get("status") == "approval_pending"):
        return result
    approval_id = str(result["approval_id"])
    out.write(f"[{step.id}] waiting for approval: {result['url']}\n")
    for _ in range(_APPROVAL_POLLS):
        answer = await client.call(
            "privacyfence_await_approval",
            {"approval_ids": [approval_id], "timeout_seconds": _APPROVAL_POLL_SECONDS},
        )
        status = str(answer.get(approval_id, "unknown"))
        if status != "pending":
            break
    else:
        raise ToolDenied(f"step {step.id}: no decision within {_APPROVAL_POLLS * _APPROVAL_POLL_SECONDS // 60} minutes")
    if status != "approved":
        raise ToolDenied(f"step {step.id}: {status}")
    return await client.call(tool, args)


async def _receive_file(step: ToolStep, result: Any, dest: Path, client: SyncClient, work_dir: Path) -> None:
    delivery = result.get("delivery") if isinstance(result, dict) else None
    if delivery == "local_disk" or (delivery is None and isinstance(result, dict) and result.get("path")):
        if result.get("truncated"):
            raise SyncError(f"step {step.id}: the download was truncated")
        written = Path(result["path"])
        if written.is_symlink() or not written.resolve().is_relative_to(work_dir.resolve()):
            raise SyncError(f"step {step.id}: the server named a file outside the work folder")
        if written != dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(written), dest)
        expected = result.get("sha256")
        if expected is not None and hashlib.sha256(dest.read_bytes()).hexdigest() != expected:
            raise ToolFailed(f"step {step.id}: the file does not match the sha256 the tool reported")
    elif delivery == "link":
        url = result["download_url"]
        if _FETCH_PATH not in urlparse(url).path:
            raise SyncError(
                f"step {step.id}: this server hands out browser-only download links "
                "(download_delivery.agent_links is off), which privacyfence-sync cannot fetch. "
                "Ask your administrator to turn agent_links on."
            )
        try:
            await client.download(url, dest, result.get("sha256"))
        except httpx2.HTTPStatusError as exc:
            raise _transfer_failed(step, exc) from None
    elif delivery == "inline":
        dest.write_bytes(base64.b64decode(result["content_base64"]))
    elif delivery == "none":
        if dest.suffix == ".json":
            dest.write_text("[]", encoding="utf-8")
        else:
            with dest.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(result["columns"])
    else:
        raise SyncError(f"step {step.id}: unexpected delivery {delivery!r}")
