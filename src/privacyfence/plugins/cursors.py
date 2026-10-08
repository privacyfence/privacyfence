"""The cursor envelope every paged source operation shares (ADR 0128).

A cursor is the base64url text of a small JSON object: the envelope version, the operation, a
digest of the parameters the cursor was issued for, and the operation's own state. Binding it to
the parameters stops a plugin from carrying a cursor over to a different query by mistake. It is
neither secret nor signed, and not a security boundary: the plugin reads with its own rights
either way. Each adapter validates its own state after ``decode`` returns it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json

from privacyfence.plugins.constants import CURSOR_MAX_CHARS

_VERSION = 1
_INVALID = "cursor is not valid"
_FOREIGN = "cursor belongs to a different call"


class CursorError(ValueError):
    """The cursor is malformed or was issued for another call."""


def params_digest(operation: str, bound: dict) -> str:
    raw = json.dumps({"op": operation, "p": bound}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def encode(operation: str, bound: dict, state: dict) -> str:
    envelope = {"v": _VERSION, "op": operation, "d": params_digest(operation, bound), "s": state}
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode(cursor: str, operation: str, bound: dict) -> dict:
    if not isinstance(cursor, str) or not cursor or len(cursor) > CURSOR_MAX_CHARS:
        raise CursorError(_INVALID)
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        envelope = json.loads(raw.decode())
    except (binascii.Error, ValueError):
        raise CursorError(_INVALID) from None
    if (
        not isinstance(envelope, dict)
        or envelope.get("v") != _VERSION
        or not isinstance(envelope.get("op"), str)
        or not isinstance(envelope.get("d"), str)
        or not isinstance(envelope.get("s"), dict)
    ):
        raise CursorError(_INVALID)
    if envelope["op"] != operation or envelope["d"] != params_digest(operation, bound):
        raise CursorError(_FOREIGN)
    return envelope["s"]
