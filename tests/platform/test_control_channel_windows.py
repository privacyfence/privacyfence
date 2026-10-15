"""The Windows named-pipe control channel, with a real peer.

``principal_id_for_peer`` maps a peer to ``os-<sid>`` only on a separated
install, and the SID it maps comes from the pipe's client token. Unit tests
fake the peer; this one connects to a real ``ControlChannelServer`` pipe from
this process, so the SID the server reads off the connection is the one this
process's token carries."""
from __future__ import annotations

import sys

import pytest

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32", reason="named pipes exist only on Windows"),
]


def test_mint_over_the_pipe_binds_the_code_to_the_callers_sid(monkeypatch):
    import win32api
    import win32con
    import win32security

    from privacyfence import privilege_separation
    from privacyfence.web import control_channel
    from privacyfence.web.session_auth import BootstrapStore

    monkeypatch.setattr(privilege_separation, "is_enabled", lambda: True)
    monkeypatch.setattr(privilege_separation, "owner_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(privilege_separation, "windows_channel_trustees", lambda: [])

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.ConvertSidToStringSid(
        win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    )

    bootstrap = BootstrapStore()
    server = control_channel.ControlChannelServer(bootstrap=bootstrap)
    server.start()
    try:
        reply = control_channel.send_line_windows(server.address, "MINT\n", timeout=5)
    finally:
        server.stop()

    assert reply.startswith("OK "), reply
    consumed = bootstrap.consume(reply[len("OK "):].strip())
    assert consumed is not None
    _provenance, principal_id = consumed
    assert principal_id == f"os-{sid}"
