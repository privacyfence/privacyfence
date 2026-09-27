"""T3 contract test: does a real, pinned Claude Code CLI connect to a real
local-mode ``/mcp`` endpoint?

The shape is the one ``docs/install-linux.md``'s "Connect Claude Code"
tells a user to run: ``claude mcp add --transport http --scope user
privacyfence <url> --header "Authorization: Bearer <token>"``, then
``claude mcp list``, which performs a real MCP ``initialize`` against every
configured server and prints a per-server health line. Both run against a
throwaway ``HOME`` (the harness's ``client_env``), so the test writes
Claude Code's user config there and nowhere else, and with no Anthropic
credential in the environment: ``mcp add``/``mcp list`` need none, and this
test is the proof that it stays that way.

The binary is the exact version pinned in
``tests/integration/ai_clients/package.json`` (see
tests/integration/ai_client_harness.py's module docstring for how to bump
it). ``CLAUDE_CODE_BIN`` overrides it -- the weekly
``.github/workflows/ai-client-canary.yml`` sets it to ``npx --yes
@anthropic-ai/claude-code@latest``.

``claude mcp list`` exits 0 whether or not a server connected, so the
assertion is on its output line for ``privacyfence``, not on the exit code.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.integration.ai_client_harness import client_env, local_daemon, resolve_client_command

# Covers an `npx --yes ...@latest` download on the canary; the pinned binary
# takes well under a second per command.
_COMMAND_TIMEOUT_S = 120

pytestmark = [pytest.mark.timeout(2 * _COMMAND_TIMEOUT_S + 30), pytest.mark.integration]


@pytest.fixture
def claude_code() -> list[str]:
    return resolve_client_command("CLAUDE_CODE_BIN", "claude")


@pytest.fixture
def throwaway_home():
    # A short fixed-shape path rather than tmp_path, like the other contract
    # tests: the daemon's data directory lives under it too.
    directory = Path(f"/tmp/pf-ai-ct-{uuid.uuid4().hex[:8]}")
    directory.mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _run(command: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=home,
        env=client_env(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_S,
        check=False,
    )


def test_claude_code_connects_to_the_local_daemon_without_any_anthropic_credential(
    claude_code, throwaway_home, monkeypatch
):
    with local_daemon(throwaway_home / ".privacyfence", monkeypatch) as (mcp_url, token):
        added = _run(
            [
                *claude_code, "mcp", "add", "--transport", "http", "--scope", "user",
                "privacyfence", mcp_url, "--header", f"Authorization: Bearer {token}",
            ],
            throwaway_home,
        )
        assert added.returncode == 0, added.stdout + added.stderr
        # The user-scope config went into the throwaway HOME, nowhere else.
        assert "privacyfence" in (throwaway_home / ".claude.json").read_text(encoding="utf-8")

        listed = _run([*claude_code, "mcp", "list"], throwaway_home)

    output = listed.stdout + listed.stderr
    assert listed.returncode == 0, output
    lines = [line for line in output.splitlines() if line.startswith("privacyfence:")]
    assert len(lines) == 1, output
    assert mcp_url in lines[0], output
    assert "Connected" in lines[0], output
    # The bearer token is a secret: Claude Code must not echo it.
    assert token not in output
