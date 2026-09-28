"""A gated call whose approval is still pending releases nothing, and an
approved one is audited end to end under one request_id (ADR 0092).

Before ADR 0092, 5.0.0a1/a2 manual QA recorded this for a gated
``drive_download_file`` from a client *without* the ``.mcpb`` shim (so the
file leaves through a one-time capability link, ADR 0028):

    drive_download_file  approval_pending
    (tool "")            bridge_download_served
    drive_download_file  expired

-- the file handed out by the very call that went pending, because
drive.py discarded gated_call's returned pending result and delivered
anyway. The tests below pin the fixed sequence instead.

Driven through the real stack: a socket-bound local-mode ``WebServer``,
the official ``mcp`` client over ``/mcp`` with a bearer token and no
``X-PrivacyFence-File-Bridge`` header, the real connectors (only their
Google clients are faked), the real ``gate.gated_call`` and the real
``PendingApprovalRegistry``. A short hold window stands in for a human who
has not answered yet, exactly as ``test_deferred_approval_round_trip.py``
does; a decision, where a test makes one, goes through the real decide
route a human's browser posts to.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import MagicMock

import httpx
import httpx2
import pytest

pytest.importorskip("mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'")
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from privacyfence import approval_ui, local_files  # noqa: E402
from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.approvals import PendingApprovalRegistry  # noqa: E402
from privacyfence.audit_log import init_audit_logger  # noqa: E402
from privacyfence.connectors.calendar import CalendarConnector  # noqa: E402
from privacyfence.connectors.drive import DriveConnector  # noqa: E402
from privacyfence.drive_client import DriveFile, DriveFileContent  # noqa: E402
from privacyfence.web.mcp_dispatch import McpDispatcher  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

from .test_deferred_approval_round_trip import _decide, _free_port, _wait_until_connectable  # noqa: E402

pytestmark = [pytest.mark.timeout(30), pytest.mark.integration]

_FILE_BYTES = b"quarterly numbers nobody approved releasing"


def _fake_drive_client() -> MagicMock:
    client = MagicMock()
    client.get_file_metadata.return_value = DriveFile(
        id="f1", name="report.txt", mime_type="text/plain", size=len(_FILE_BYTES),
        modified_time="2026-09-28T00:00:00Z", owners=["alice@example.com"],
    )
    client.get_file_content.return_value = DriveFileContent(
        file=client.get_file_metadata.return_value, content_bytes=_FILE_BYTES, truncated=False,
    )
    return client


@pytest.fixture
def local_server(tmp_path, monkeypatch):
    home = tmp_path / f"pf-{uuid.uuid4().hex[:8]}" / ".privacyfence"
    home.mkdir(parents=True)
    monkeypatch.setattr(paths_module, "data_dir", lambda: home)
    audit_dir = tmp_path / "audit"
    init_audit_logger(str(audit_dir))
    # A privilege-separated local install: the daemon cannot write into the
    # user's destination_dir, so the download goes through the file bridge,
    # and with no shim header on the request, through a capability link.
    local_files.force_bridge_for_tests(True)

    drive = DriveConnector(_fake_drive_client())
    drive.my_email = "me@example.com"
    calendar_client = MagicMock()
    calendar = CalendarConnector(calendar_client)
    calendar.my_email = "me@example.com"
    registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=0.5, ledger_ttl=0.5)
    approval_ui.init_approval_ui(WebApprovalUI(registry=registry))
    dispatcher = McpDispatcher(lambda: {"drive": drive, "calendar": calendar}, registry=registry)

    port = _free_port()
    server = WebServer(approval_ui.get_approval_ui(), host="localhost", port=port, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield server, audit_dir, calendar_client
    finally:
        # Nobody answered: release every card's popup-executor worker so
        # the server can shut down.
        for approval in registry.list_pending():
            registry.finalize(approval.id, "deny")
        server.stop()
        local_files.force_bridge_for_tests(False)


async def _call(server: WebServer, tool: str, args: dict) -> dict:
    headers = {"Authorization": f"Bearer {server.mcp_token}"}
    async with httpx2.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(server.mcp_url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
    return result.structured_content or json.loads(result.content[0].text)


def _rows(audit_dir) -> list[dict]:
    rows = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return rows


def _decisions(audit_dir) -> list[tuple[str, str]]:
    return [(r.get("tool", ""), r.get("decision", "")) for r in _rows(audit_dir)]


_DOWNLOAD_ARGS = {"file_id": "f1", "destination_dir": "~/Downloads", "reason": "check the audit trail"}


async def test_pending_drive_download_hands_out_no_link(local_server):
    """Nobody decides: the call returns approval_pending and nothing to
    fetch, and the card later expires with nothing ever served."""
    server, audit_dir, _calendar_client = local_server

    body = await _call(server, "drive_download_file", _DOWNLOAD_ARGS)

    assert body.get("status") == "approval_pending", body
    assert "download_url" not in body
    # Let the (short) pending TTL lapse, then let the next gated call's
    # opportunistic sweep audit it, exactly as a later call did in QA.
    await asyncio.sleep(0.7)
    await _call(server, "drive_download_file", {**_DOWNLOAD_ARGS, "file_id": "f2"})
    sequence = _decisions(audit_dir)
    assert sequence[:2] == [("drive_download_file", "approval_pending"), ("drive_download_file", "expired")]
    assert ("", "bridge_download_served") not in sequence
    expired = _rows(audit_dir)[1]
    # A card nobody answered: no decision to report.
    assert expired["expired_decision"] == ""
    assert expired["decided_at"] == ""


async def test_approved_download_is_audited_as_approved_under_one_request_id(local_server):
    """The human approves, the agent re-issues the identical call, fetches
    the link: pending, approved and served all carry one request_id and
    the agent, and the approval is collected, so it never expires."""
    server, audit_dir, _calendar_client = local_server

    pending = await _call(server, "drive_download_file", _DOWNLOAD_ARGS)
    assert pending["status"] == "approval_pending"
    response = await _decide(server, pending["approval_id"], "accept")
    assert response.status_code == 200, response.text

    released = await _call(server, "drive_download_file", _DOWNLOAD_ARGS)
    assert released["delivery"] == "link"
    async with httpx.AsyncClient() as http_client:
        served = await http_client.get(released["download_url"])
    assert served.status_code == 200
    assert served.content == _FILE_BYTES

    # Past both TTLs: a sweep must find nothing left to report as expired.
    await asyncio.sleep(0.7)
    await _call(server, "drive_download_file", {**_DOWNLOAD_ARGS, "file_id": "f2"})

    rows = _rows(audit_dir)
    assert [(r["tool"], r["decision"]) for r in rows[:3]] == [
        ("drive_download_file", "approval_pending"),
        ("drive_download_file", "approved"),
        ("", "bridge_download_served"),
    ]
    assert ("drive_download_file", "expired") not in _decisions(audit_dir)
    pending_row, approved_row, served_row = rows[:3]
    assert pending_row["request_id"]
    assert approved_row["request_id"] == served_row["request_id"] == pending_row["request_id"]
    assert approved_row["decided_at"]
    assert served_row["agent_id"]
    assert served_row["agent_id"] == approved_row["agent_id"] == pending_row["agent_id"]


async def test_uncollected_approval_expires_saying_what_was_decided(local_server):
    """The human approves but the agent never comes back for it: the
    "expired" row says it was approved, and when."""
    server, audit_dir, _calendar_client = local_server

    pending = await _call(server, "drive_download_file", _DOWNLOAD_ARGS)
    response = await _decide(server, pending["approval_id"], "accept")
    assert response.status_code == 200, response.text
    await asyncio.sleep(0.7)
    await _call(server, "drive_download_file", {**_DOWNLOAD_ARGS, "file_id": "f2"})

    rows = _rows(audit_dir)
    assert [(r["tool"], r["decision"]) for r in rows[:2]] == [
        ("drive_download_file", "approval_pending"), ("drive_download_file", "expired"),
    ]
    assert rows[1]["request_id"] == rows[0]["request_id"]
    assert rows[1]["expired_decision"] == "approved"
    assert rows[1]["decided_at"]
    assert ("", "bridge_download_served") not in _decisions(audit_dir)


async def test_pending_calendar_write_is_not_performed(local_server):
    """The same shape on the write side: a gated write whose approval is
    still pending must not reach the provider."""
    server, audit_dir, calendar_client = local_server

    body = await _call(server, "calendar_create_event", {
        "calendar_id": "primary", "title": "Unapproved", "start_time": "2026-10-01T10:00:00Z",
        "end_time": "2026-10-01T11:00:00Z", "reason": "reproduce the QA audit trail",
    })

    assert body.get("status") == "approval_pending", (
        f"a still-pending calendar_create_event returned {body!r}; "
        f"provider calls: {calendar_client.create_event.call_args_list}; "
        f"audit sequence: {_decisions(audit_dir)}"
    )
    calendar_client.create_event.assert_not_called()
