"""Command line entry point: ``privacyfence-sync run|login|check``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx2

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
from .recipe import RecipeError, load_recipe
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
    login = sub.add_parser("login", help="sign in to an organization server", allow_abbrev=False)
    login.add_argument("--server", required=True, help="the PrivacyFence MCP URL")
    login.add_argument(
        "--callback-port", type=int, default=DEFAULT_CALLBACK_PORT, help="loopback port for the browser's return"
    )
    check = sub.add_parser("check", help="validate a recipe without any network call")
    check.add_argument("recipe", type=Path)
    return parser


def _fail(message: str, code: int) -> int:
    print(f"privacyfence-sync: {message}", file=sys.stderr)
    return code


def _resolve_server(flag: str | None, recipe_server: str | None) -> str:
    return flag or recipe_server or os.environ.get("PRIVACYFENCE_MCP_URL") or DEFAULT_SERVER


def _connect(server: str, token_env: str) -> SyncClient:
    token = os.environ.get(token_env)
    if token:
        return connect_token(server, token)
    storage = FileTokenStorage(storage_path(server))
    if not storage.has_tokens():
        raise SignInError(login_hint(server))
    return connect_oauth(server, storage)


async def _run(recipe_path: Path, server_flag: str | None, attended: bool, token_env: str) -> None:
    recipe = load_recipe(recipe_path)
    client = _connect(_resolve_server(server_flag, recipe.server), token_env)
    try:
        async with client:
            await run_recipe(recipe, client, attended=attended, out=sys.stdout)
    except BaseExceptionGroup as group:
        # The MCP transport runs in an anyio task group, which wraps the step's own error.
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


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 1 if exc.code else 0
    try:
        if args.command == "check":
            recipe = load_recipe(args.recipe)
            print(f"{recipe.name}: {len(recipe.steps)} step(s), OK")
            return 0
        if args.command == "login":
            asyncio.run(_login(args.server, args.callback_port))
            return 0
        asyncio.run(_run(args.recipe, args.server, args.attended, args.token_env))
        return 0
    except (RecipeError, TemplateError, RecipeRuntimeError) as exc:
        return _fail(str(exc), 1)
    except ToolDenied as exc:
        return _fail(str(exc), 3)
    except SignInError as exc:
        return _fail(str(exc), 4)
    except (SyncError, ScriptError) as exc:
        return _fail(str(exc), 2)
    except httpx2.HTTPError as exc:
        return _fail(f"cannot reach the server: {exc}", 2)


if __name__ == "__main__":
    raise SystemExit(main())
