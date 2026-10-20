"""Command line entry point: ``privacyfence-sync run|login|check|preflight|permissions``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx2
from mcp.shared.exceptions import MCPError

from .auth import DEFAULT_CALLBACK_PORT, FileTokenStorage, login_hint, storage_path
from .client import (
    RecipeRuntimeError,
    SignInError,
    SyncClient,
    SyncError,
    ToolDenied,
    connect_oauth,
    connect_token,
)
from .permissions import list_permissions
from .preflight import preflight_recipe
from .recipe import Recipe, RecipeError, load_recipe
from .result import write_result
from .runner import run_recipe
from .template import TemplateError
from .transform import ScriptError

DEFAULT_SERVER = "http://127.0.0.1:8765/mcp"
DEFAULT_TOKEN_ENV = "PRIVACYFENCE_TOKEN"  # nosec B105  # the name of an environment variable, not a secret


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="privacyfence-sync", description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a recipe", allow_abbrev=False)
    run.add_argument("recipe", type=Path)
    run.add_argument("--server", help="the PrivacyFence MCP URL")
    run.add_argument(
        "--attended", action="store_true", help="wait for approvals instead of declaring the run unattended"
    )
    run.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="environment variable holding the token")
    run.add_argument(
        "--result-file", type=Path, default=None, help="write a JSON file saying how the run ended, for a scheduler"
    )
    login = sub.add_parser("login", help="sign in to an organization server", allow_abbrev=False)
    login.add_argument("--server", required=True, help="the PrivacyFence MCP URL")
    login.add_argument(
        "--callback-port", type=int, default=DEFAULT_CALLBACK_PORT, help="loopback port for the browser's return"
    )
    check = sub.add_parser("check", help="validate a recipe without any network call")
    check.add_argument("recipe", type=Path)
    preflight = sub.add_parser(
        "preflight", help="check that your token can run a recipe, without running it", allow_abbrev=False
    )
    preflight.add_argument("recipe", type=Path)
    preflight.add_argument("--server", help="the PrivacyFence MCP URL")
    preflight.add_argument(
        "--attended", action="store_true", help="check for a run that waits for approvals, not an unattended one"
    )
    preflight.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="environment variable holding the token")
    permissions = sub.add_parser(
        "permissions", help="list the auto-accept rules a recipe needs, without running it", allow_abbrev=False
    )
    permissions.add_argument("recipe", type=Path)
    permissions.add_argument("--server", help="the PrivacyFence MCP URL")
    permissions.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="environment variable holding the token")
    permissions.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    return parser


def _fail(message: str, code: int) -> int:
    print(f"privacyfence-sync: {message}", file=sys.stderr)
    return code


def _resolve_server(flag: str | None, recipe_server: str | None) -> str:
    return flag or recipe_server or os.environ.get("PRIVACYFENCE_MCP_URL") or DEFAULT_SERVER


def _connect(server: str, token_env: str) -> SyncClient:
    # The scripts this process runs must never see the bearer token, whatever variable carried it.
    token = os.environ.pop(token_env, None)
    if token:
        return connect_token(server, token)
    storage = FileTokenStorage(storage_path(server))
    if not storage.has_tokens():
        raise SignInError(login_hint(server))
    return connect_oauth(server, storage)


async def _run(recipe: Recipe, server_flag: str | None, attended: bool, token_env: str) -> str | None:
    client = _connect(_resolve_server(server_flag, recipe.server), token_env)
    try:
        async with client:
            return await run_recipe(recipe, client, attended=attended, out=sys.stdout)
    except BaseExceptionGroup as group:
        # The MCP transport runs in an anyio task group, which wraps the step's own error.
        raise _first_leaf(group) from None


async def _preflight(recipe_path: Path, server_flag: str | None, attended: bool, token_env: str) -> bool:
    recipe = load_recipe(recipe_path)
    client = _connect(_resolve_server(server_flag, recipe.server), token_env)
    try:
        async with client:
            return await preflight_recipe(recipe, client, attended=attended, out=sys.stdout)
    except BaseExceptionGroup as group:
        raise _first_leaf(group) from None


async def _permissions(recipe_path: Path, server_flag: str | None, token_env: str, as_json: bool) -> None:
    recipe = load_recipe(recipe_path)
    client = _connect(_resolve_server(server_flag, recipe.server), token_env)
    try:
        async with client:
            await list_permissions(recipe, client, out=sys.stdout, as_json=as_json)
    except BaseExceptionGroup as group:
        raise _first_leaf(group) from None


async def _login(server: str, callback_port: int) -> None:
    client = connect_oauth(
        server, FileTokenStorage(storage_path(server)), interactive=True, callback_port=callback_port
    )
    try:
        async with client:
            await client.call("privacyfence_status", {"reason": "privacyfence-sync login check"})
    except BaseExceptionGroup as group:
        raise _first_leaf(group) from None
    print(f"Signed in to {server}.")


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    first = group.exceptions[0]
    return _first_leaf(first) if isinstance(first, BaseExceptionGroup) else first


def _run_command(args: argparse.Namespace) -> tuple[int, str | None, str | None]:
    """Run a recipe; return (exit code, id of the step that stopped it, recipe name)."""
    recipe_name: str | None = None
    try:
        recipe = load_recipe(args.recipe)
        recipe_name = recipe.name
        stopped_at = asyncio.run(_run(recipe, args.server, args.attended, args.token_env))
        return 0, stopped_at, recipe_name
    except Exception as exc:
        return _exit_code(exc), None, recipe_name


def _exit_code(exc: Exception) -> int:
    if isinstance(exc, (RecipeError, TemplateError, RecipeRuntimeError)):
        return _fail(str(exc), 1)
    if isinstance(exc, ToolDenied):
        return _fail(str(exc), 3)
    if isinstance(exc, SignInError):
        return _fail(str(exc), 4)
    if isinstance(exc, (SyncError, ScriptError)):
        return _fail(str(exc), 2)
    if isinstance(exc, httpx2.HTTPError):
        return _fail(f"cannot reach the server: {exc}", 2)
    if isinstance(exc, MCPError):
        # The MCP client turns an HTTP error on the handshake (a bad or revoked token, a wrong URL,
        # a server error) into this generic error, so name the usual causes.
        return _fail(
            f"the server refused the connection ({exc.message}); check that the token in "
            "$PRIVACYFENCE_TOKEN (or --token-env) is valid and that the server URL is right",
            2,
        )
    raise exc


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 1 if exc.code else 0
    if args.command == "run":
        started_at = datetime.now(timezone.utc)
        code, stopped_at, recipe_name = _run_command(args)
        if args.result_file is not None:
            try:
                write_result(
                    args.result_file,
                    recipe=recipe_name,
                    exit_code=code,
                    stopped_at=stopped_at,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                )
            except OSError as exc:
                print(f"privacyfence-sync: could not write the result file: {exc}", file=sys.stderr)
        return code
    try:
        if args.command == "check":
            recipe = load_recipe(args.recipe)
            print(f"{recipe.name}: {len(recipe.steps)} step(s), OK")
            return 0
        if args.command == "login":
            asyncio.run(_login(args.server, args.callback_port))
            return 0
        if args.command == "permissions":
            asyncio.run(_permissions(args.recipe, args.server, args.token_env, args.json))
            return 0
        ok = asyncio.run(_preflight(args.recipe, args.server, args.attended, args.token_env))
        return 0 if ok else 3
    except Exception as exc:
        return _exit_code(exc)


if __name__ == "__main__":
    raise SystemExit(main())
