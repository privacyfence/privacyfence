"""macOS's ``LOCAL_PEERCRED`` lookup, against a real socket pair.

``_peer_uid_macos`` goes through ``libc`` with a hand-written ``struct
xucred``; only the real kernel can say whether that layout is right."""
from __future__ import annotations

import os
import socket
import sys

import pytest

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERCRED exists only on macOS"),
]


def test_peer_uid_of_a_real_socketpair_is_this_process_uid():
    from privacyfence.web.control_channel import _peer_uid_macos

    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert _peer_uid_macos(a) == os.getuid()
        assert _peer_uid_macos(b) == os.getuid()
    finally:
        a.close()
        b.close()
