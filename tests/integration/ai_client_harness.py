"""Shared harness for the T3 real-AI-client contract tests
(``docs/testing-policy.md``, "AI-client contract tests (T3)").

A T3 test runs a real, unmodified AI-client CLI -- today Claude Code
(tests/integration/test_claude_code_contract.py) -- against a real
single-user org ``/mcp`` endpoint and asserts that the client connects. This
module owns the two halves every such test shares, so a second client is a
new test file and a new ``package.json`` entry, not a second copy of either:

- ``org_daemon()`` starts the same in-process single-user org ``WebServer`` that
  test_mcp_daemon_contract.py starts (tests/integration/org_server_harness.py) -- a real uvicorn
  server on a real, free loopback port, with a real ``McpDispatcher`` in front of one real
  connector -- under a throwaway data directory, so nothing it writes touches the developer's
  own data directory. It yields ``(mcp_url, token)``.
- ``client_env()`` builds the environment a client CLI runs under: a
  throwaway ``HOME`` (and ``USERPROFILE``/``XDG_CONFIG_HOME``, so the
  client's config file lands there on every platform) and **no** variable
  that could carry a vendor credential. A T3 test must pass with no API key
  and no signed-in account; stripping ``ANTHROPIC_*``/``CLAUDE*`` here is
  what proves it, even on a machine (or a Claude Code session) that has
  them set.

The client CLIs themselves are pinned, exact-version, in
``tests/integration/ai_clients/package.json`` and its committed
``package-lock.json``, and installed with ``npm ci --prefix
tests/integration/ai_clients`` (integrity-checked against the lockfile).
To bump a pin: ``npm install --prefix tests/integration/ai_clients
--save-exact --save-dev <package>@<version>``, commit both files, and let
the ``test`` job prove the new version still connects. The weekly
``.github/workflows/ai-client-canary.yml`` runs the same tests against
``@latest`` instead, so a breaking client release shows up as an issue
before the pin is bumped into it.
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.web.mcp_dispatch import McpDispatcher
from tests.integration.org_server_harness import single_user_org_server

AI_CLIENTS_DIR = Path(__file__).resolve().parent / "ai_clients"

# Environment variable prefixes a client CLI could read a credential,
# account or config-directory override from. Everything else passes
# through unchanged (PATH, proxy settings, npm's cache location).
_STRIPPED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE")


class HarnessConnector(Connector):
    """One real read-only tool, so the daemon advertises a connector tool
    alongside its ``privacyfence_*`` meta-tools -- the same shape as the
    other contract tests' EchoConnector."""

    @property
    def name(self) -> str:
        return "contract_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="contract_test_echo",
                description="Echoes its arguments back -- used only by the AI-client contract tests.",
                params=[ToolParam("message", "str", required=True)],
                read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        return {"echoed": args}


@contextmanager
def org_daemon(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Run a single-user org ``/mcp`` endpoint whose state lives under ``tmp_path``;
    yield ``(mcp_url, token)``."""
    dispatcher = McpDispatcher(lambda: {"contract_test": HarnessConnector()})
    with single_user_org_server(tmp_path, dispatcher=dispatcher) as server:
        yield server.mcp_url, server.bearer()


def client_env(home: Path) -> dict[str, str]:
    """The environment for a client CLI: ``home`` as its home directory and
    no vendor credential (see the module docstring)."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(_STRIPPED_ENV_PREFIXES)
    }
    # npx (the canary's binary) caches under ~/.npm; keep the real cache
    # rather than downloading the client again into every throwaway HOME.
    env.setdefault("npm_config_cache", str(Path.home() / ".npm"))
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        # Nothing a T3 test does needs the client to phone home.
        DISABLE_AUTOUPDATER="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    return env


def resolve_client_command(override_var: str, pinned_bin: str) -> list[str]:
    """The command line that runs a client CLI.

    ``override_var`` (e.g. ``CLAUDE_CODE_BIN``), when set, is split like a
    shell word list, so the canary can pass ``npx --yes <package>@latest``.
    Setting it also makes the test mandatory: a binary someone named
    explicitly that cannot run is a failure, never a skip. Without it, the
    pinned binary from ``npm ci --prefix tests/integration/ai_clients`` is
    used, and the test skips when node/npm or that install is absent.
    """
    override = os.environ.get(override_var)
    if override:
        command = shlex.split(override, posix=os.name != "nt")
        resolved = shutil.which(command[0])
        if resolved is None:
            pytest.fail(f"{override_var}={override!r}: {command[0]!r} is not on PATH")
        return [resolved, *command[1:]]
    if shutil.which("node") is None or shutil.which("npm") is None:
        pytest.skip("node/npm not on PATH -- the AI-client contract tests run real Node CLIs")
    bin_dir = AI_CLIENTS_DIR / "node_modules" / ".bin"
    resolved = shutil.which(pinned_bin, path=str(bin_dir))
    if resolved is None:
        pytest.skip(
            f"{pinned_bin} not installed -- run `npm ci --prefix tests/integration/ai_clients`"
            f" or set {override_var}"
        )
    return [resolved]
