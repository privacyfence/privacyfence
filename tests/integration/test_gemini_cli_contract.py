"""T3 contract test: does a real, pinned Gemini CLI connect to a real
local-mode ``/mcp`` endpoint?

Gemini CLI connects straight to ``/mcp`` over streamable HTTP (``httpUrl``)
with a bearer header; it never uses the ``.mcpb`` shim. The test writes the
user-level ``~/.gemini/settings.json`` that configuration amounts to, then
runs ``gemini mcp list``, which performs a real MCP ``initialize`` (and a
``ping``) against every configured server and prints a per-server status
line. Everything runs against a throwaway ``HOME`` (the harness's
``client_env``) with no Google credential in the environment: ``mcp list``
needs none, and this test is the proof that it stays that way.

Two things about Gemini CLI shape the test:

- **Folder trust.** In an untrusted working directory, ``mcp list`` reports
  every server -- user-level ones included -- as ``Disabled`` without
  contacting it. The test trusts its throwaway working directory the way a
  user does when they answer Gemini CLI's trust dialog: an entry in
  ``~/.gemini/trustedFolders.json``. (``GEMINI_CLI_TRUST_WORKSPACE`` would do
  the same, but the harness strips every ``GEMINI_*`` variable.)
- **No tool count.** ``mcp list`` stops at ``initialize``/``ping``; it never
  calls ``tools/list``. Listing tools happens only in a model session, and
  that needs a Google auth method, so the ``Connected`` status is as far as a
  credential-free test can see.

The binary is the exact version pinned in
``tests/integration/ai_clients/package.json`` (see
tests/integration/ai_client_harness.py's module docstring for how to bump
it). ``GEMINI_CLI_BIN`` overrides it -- the weekly
``.github/workflows/ai-client-canary.yml`` sets it to ``npx --yes
@google/gemini-cli@latest``.

``gemini mcp list`` exits 0 whether or not a server connected, so the
assertion is on its output line for ``privacyfence``, not on the exit code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.integration.ai_client_harness import client_env, local_daemon, resolve_client_command

# Covers an `npx --yes ...@latest` download on the canary; the pinned binary
# takes a few seconds.
_COMMAND_TIMEOUT_S = 120

pytestmark = [pytest.mark.timeout(_COMMAND_TIMEOUT_S + 30), pytest.mark.integration]


@pytest.fixture
def gemini_cli() -> list[str]:
    return resolve_client_command("GEMINI_CLI_BIN", "gemini")


@pytest.fixture
def throwaway_home():
    # A short fixed-shape path rather than tmp_path, like the other contract
    # tests: the daemon's data directory lives under it too.
    directory = Path(f"/tmp/pf-ai-ct-{uuid.uuid4().hex[:8]}")
    directory.mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _write_gemini_config(home: Path, mcp_url: str, token: str) -> None:
    gemini_dir = home / ".gemini"
    gemini_dir.mkdir()
    settings = {
        "mcpServers": {
            "privacyfence": {"httpUrl": mcp_url, "headers": {"Authorization": f"Bearer {token}"}}
        },
        # Nothing a T3 test does needs the client to phone home.
        "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False},
        "privacy": {"usageStatisticsEnabled": False},
    }
    (gemini_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    # The test runs with `home` as its working directory; see the module
    # docstring, "Folder trust".
    (gemini_dir / "trustedFolders.json").write_text(
        json.dumps({str(home): "TRUST_FOLDER"}), encoding="utf-8"
    )


def test_gemini_cli_connects_to_the_local_daemon_without_any_google_credential(
    gemini_cli, throwaway_home, monkeypatch
):
    with local_daemon(throwaway_home / ".privacyfence", monkeypatch) as (mcp_url, token):
        _write_gemini_config(throwaway_home, mcp_url, token)
        listed = subprocess.run(
            [*gemini_cli, "mcp", "list"],
            cwd=throwaway_home,
            env=client_env(throwaway_home),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_S,
            check=False,
        )

    output = listed.stdout + listed.stderr
    assert listed.returncode == 0, output
    lines = [line for line in output.splitlines() if "privacyfence:" in line]
    assert len(lines) == 1, output
    assert mcp_url in lines[0], output
    assert lines[0].rstrip().endswith("- Connected"), output
    # The bearer token is a secret: Gemini CLI must not echo it.
    assert token not in output
