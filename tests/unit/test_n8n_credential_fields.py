"""The n8n node's credential type and the daemon's delivery of it must agree on field names:
``runtime_credentials.credential_data`` writes ``mcpUrl`` and ``token`` into the credential it
creates in n8n, and ``n8n/credentials/PrivacyFenceApi.credentials.ts`` declares the properties n8n
reads back. A rename on either side alone breaks delivery silently."""

from __future__ import annotations

import re
from pathlib import Path

from privacyfence import agent_runtimes, runtime_credentials

CREDENTIAL_FILE = Path(__file__).resolve().parents[2] / "n8n" / "credentials" / "PrivacyFenceApi.credentials.ts"


def test_the_credential_properties_are_the_fields_the_daemon_delivers():
    source = CREDENTIAL_FILE.read_text(encoding="utf-8")
    properties = source[source.index("properties"):]
    declared = set(re.findall(r"^\s*name:\s*'([^']+)'", properties, flags=re.MULTILINE))

    assert declared == set(runtime_credentials.credential_data("privacyFenceApi", "https://x/mcp", "t"))


def test_the_credential_type_name_is_one_the_daemon_knows():
    source = CREDENTIAL_FILE.read_text(encoding="utf-8")
    match = re.search(r"^\s*name\s*=\s*'([^']+)'", source, flags=re.MULTILINE)

    assert match is not None
    assert match.group(1) in agent_runtimes.CREDENTIAL_TYPES
    assert match.group(1) == "privacyFenceApi"
