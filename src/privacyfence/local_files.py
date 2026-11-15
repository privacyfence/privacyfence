"""How a file an agent names in a tool call gets turned into bytes, and how bytes
an export produced get back to the agent (ADR 0299).

Files reach the server only through an upload slot (``privacyfence_create_upload_slot``, the
``upload:<id>`` reference a tool takes in place of a path), inline bytes, or the one-time link
(ADR 0160) a download comes back as. No connector calls ``open()``, ``os.path.isfile`` or
``os.path.getsize`` on an agent-supplied path; each calls through here instead.

State for the current ``tools/call`` dispatch -- the bytes claimed from upload slots and whether
the call staged a download -- lives in a contextvar scoped by ``call_context()``, entered once
per dispatch in ``web/routes_mcp.py``'s ``handle_call_tool`` exactly like ``principal.
principal_scope``/``gate.reason_scope`` already scope their own per-call state.
"""
from __future__ import annotations

import base64
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

from .download_staging import get_download_staging_store
from .approvals import DEFAULT_LEDGER_TTL_SECONDS, DEFAULT_PENDING_TTL_SECONDS
from .org_mode import DownloadDeliveryConfig
from .principal import current_principal
from .upload_staging import DEFAULT_TTL_SECONDS as _UPLOAD_TTL_SECONDS
from .upload_staging import get_upload_staging_store

if TYPE_CHECKING:
    from .principal import Principal

logger = logging.getLogger(__name__)

# Shown verbatim to the model (LocalFileAccessError is a ValueError subclass -- see
# safe_errors.public_message()'s passthrough rule) when a tool is given a path instead of an
# ``upload:<id>`` reference.
NO_BRIDGE_UPLOAD_MESSAGE = (
    "This tool takes a file through an upload slot. Call privacyfence_create_upload_slot, send "
    "the file to the returned address, and pass the returned upload: reference."
)

# deliver_export() is synchronous and fully in memory (the whole file is staged before anything is
# handed out), so this is a real memory cap, not just a transfer-size guideline.
# Configurable via settings.yaml's `file_bridge: max_download_bytes:` -- daemon_main.py calls
# configure_file_bridge() once at startup with whatever that section resolves to. 200MB:
# generous for anything a Drive/Gmail/Confluence tool plausibly downloads.
DEFAULT_MAX_DOWNLOAD_BYTES = 200_000_000

# Capability uploads (ADR 0028): a path prefixed this way in any
# require_local_files()/read_local_file()/local_file_size() call is not a
# filesystem path at all -- it names bytes already staged in
# UploadStagingStore by privacyfence_create_upload_slot, waiting to be
# claimed for *this* tool call. A capability slot's authorization is the
# token itself, not the daemon's read access to anything.
UPLOAD_REF_PREFIX = "upload:"

# The cap privacyfence_create_upload_slot enforces on a capability
# upload. A slot is created before any connector-specific tool is named, so
# there's no per-tool ceiling to size it against the way
# connectors/drive.py's _UPLOAD_MAX_BYTES sizes drive_upload_file's own
# upload cap -- reusing that same 50MB figure here isn't a coincidence, it's
# the same "one multi-megabyte file" ceiling applied to the one path that has
# to pick a single number for every tool that might later consume an upload_id.
DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES = 50_000_000

_max_download_bytes = DEFAULT_MAX_DOWNLOAD_BYTES


def configure_file_bridge(*, max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES) -> None:
    """Called once by daemon_main.py at startup, mirroring gate.py's
    ``configure_popup_executor`` -- settings.yaml is read once there, not
    re-parsed on every download. Safe to call more than once (e.g. from a
    test); the new value simply replaces the old one."""
    global _max_download_bytes
    _max_download_bytes = max_download_bytes


class LocalFileAccessError(ValueError):
    """Every message this module raises is about the user's own local path
    -- never about connector/OAuth internals -- so it's safe to show
    verbatim. A ``ValueError`` subclass is what makes that happen:
    ``safe_errors.public_message()`` passes a ``ValueError``'s ``str()``
    through unchanged (after secret redaction) instead of replacing it with
    the generic "Tool call failed" message. See safe_errors.py."""


@dataclass
class _CallState:
    base_url: str
    resolved: dict[str, bytes] = field(default_factory=dict)
    staged_download: bool = False
    # Upload slots read (not yet consumed) this call; commit_uploads() consumes them once the gate passes.
    held_uploads: list[bytes] = field(default_factory=list)


_state_ctx: ContextVar["_CallState | None"] = ContextVar("privacyfence_file_bridge_call", default=None)


@contextmanager
def call_context(
    *, bridge_available: bool = False, uploads: dict[str, str] | None = None, base_url: str = "",
) -> Iterator[_CallState]:
    """Scopes one ``tools/call`` dispatch's file state. ``base_url`` is the ``/mcp`` origin, used
    to build an absolute one-time download URL. ``bridge_available`` and ``uploads`` are accepted
    and ignored: callers that still pass them are dropped one connector at a time."""
    state = _CallState(base_url=base_url)
    token = _state_ctx.set(state)
    try:
        yield state
    finally:
        _state_ctx.reset(token)


def _current_state() -> "_CallState | None":
    return _state_ctx.get()


def mark_staged_download() -> None:
    """Records that this call staged a one-time download link, so ``call_produced_deliveries()``
    keeps the result out of the dedupe cache: a repeated identical call must not hand back a link
    whose token another caller already consumed, nor mint a second one. Every path that stages a
    download calls it right after ``stage()``."""
    state = _current_state()
    if state is not None:
        state.staged_download = True


def call_produced_deliveries() -> bool:
    """``McpDispatcher.call()`` checks this right after a successful
    dispatch to decide whether the result may be reused from its dedupe
    cache. A result that staged a download (a single-use token from
    ``download_staging.DownloadStagingStore``) must never be replayed: reusing it would hand out
    a link whose token silently stopped working. See mcp_dispatch.py."""
    state = _current_state()
    return bool(state and state.staged_download)


def _encode_token(token: bytes) -> str:
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")


def _decode_token(raw: str) -> bytes | None:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        token = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return None
    return token if len(token) == 32 else None


# How long a slot read by a call outlives that call (ADR 0102): the longest a
# pending approval waits for a human, plus how long its decision waits in the
# ledger for the re-issued call. Without it a slot minted with the default
# 10-minute TTL could expire between the decision and the re-issue.
UPLOAD_HOLD_SECONDS = DEFAULT_PENDING_TTL_SECONDS + DEFAULT_LEDGER_TTL_SECONDS


def _claim_upload(state: "_CallState", path: str, slot_b64: str) -> None:
    """Reads the slot's bytes for this call without consuming it (ADR 0102): the call
    may still stop at a pending approval, and the identical call re-issued once the
    human decides must find the same bytes. ``commit_uploads()`` consumes the slot
    after the gate has passed."""
    token = _decode_token(slot_b64)
    if token is None:
        raise LocalFileAccessError(f"Could not read {path!r} for upload: invalid upload reference")
    data = get_upload_staging_store().peek(
        token, current_principal().id, hold_until=time.time() + UPLOAD_HOLD_SECONDS,
    )
    if data is None:
        raise LocalFileAccessError(
            f"Could not read {path!r} for upload: the upload expired or was already used. "
            "Call the tool again."
        )
    state.resolved[path] = data
    state.held_uploads.append(token)


def commit_uploads() -> None:
    """Consumes every upload slot this call read, once its gate has passed and before
    the write that uses them (ADR 0102). A connector that reads an upload calls this
    right after ``gated_call`` returns, so a slot still backs exactly one approved
    write: of two calls that read the same slot, only the first to get here proceeds,
    and the other fails before it writes anything."""
    state = _current_state()
    if state is None or not state.held_uploads:
        return
    store = get_upload_staging_store()
    principal_id = current_principal().id
    held, state.held_uploads = state.held_uploads, []
    for token in held:
        if store.claim(token, principal_id) is None:
            raise LocalFileAccessError(
                "The uploaded file expired or was already used by another call. "
                "Upload it again with privacyfence_create_upload_slot."
            )


def require_local_files(paths: list[str], *, max_total_bytes: int) -> None:
    """Called by a connector at the very top of its tool method, before any
    preview/PII-scan/gate work, so a missing or unreadable file is reported
    before the human is ever asked to approve anything.

    For each path: already read from an upload this call -> nothing more to do. A
    capability-slot ``upload:<id>`` reference -> read it from ``UploadStagingStore`` for the
    current principal now (without consuming it -- the connector's ``commit_uploads()`` does that
    after its gate, ADR 0102); a wrong principal gets the exact same ``LocalFileAccessError`` an
    expired or already-claimed slot does (no oracle, same as the slot's own 404-shaped HTTP
    route). Anything else -> ``LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)`` (shown to the
    model verbatim).
    """
    state = _current_state()
    for path in paths:
        if state is not None and path in state.resolved:
            continue
        if path.startswith(UPLOAD_REF_PREFIX):
            if state is None:
                raise LocalFileAccessError(f"Could not read {path!r}: no active call to claim it in")
            _claim_upload(state, path, path[len(UPLOAD_REF_PREFIX):])
            continue
        raise LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)


def build_upload_slot(
    principal: "Principal", *, filename: str, size_bytes: int | None, base_url: str,
) -> dict[str, Any]:
    """Handles the ``privacyfence_create_upload_slot`` meta-tool (ADR 0028): mints an
    ``UploadStagingStore`` slot and returns a capability URL any HTTP
    client can ``PUT`` bytes to directly, with **no bearer header** -- the
    32-byte token embedded in ``upload_url`` is itself the credential,
    which is what makes this reachable from a client that has no way to
    set a custom header at all (a sandboxed agent shelling out to `curl`).
    The route (``/mcp-files/slots/<token>``) is unauthenticated -- see web/routes_file_bridge.py.

    The slot's principal is fixed at creation, to whoever is making *this*
    already-authenticated ``tools/call`` -- a capability token minted for
    one principal can still only be claimed by that principal, since
    ``require_local_files``'s own ``upload:`` handling re-checks it via
    the ordinary, principal-bound ``UploadStagingStore.claim()``. Losing
    the URL therefore lets someone else fill *your* pending upload slot
    with their own bytes, not read or claim anything of yours -- see this
    ADR's own security note.

    ``size_bytes``, when given, is checked against the one fixed cap this
    (connector-agnostic) meta-tool enforces -- raises before a slot is even
    created for a file already known to be too big, rather than minting a
    slot that ``fill()``/``afill_capability()`` would reject mid-stream
    anyway.
    """
    max_bytes = DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES
    if size_bytes is not None and size_bytes > max_bytes:
        raise LocalFileAccessError(
            f"This file is {size_bytes:,} bytes, over PrivacyFence's {max_bytes:,}-byte "
            "upload-slot limit. Ask for a narrower export or a different way to share it."
        )
    token = get_upload_staging_store().create_slot(principal, filename, max_bytes, ttl_seconds=_UPLOAD_TTL_SECONDS)
    slot = _encode_token(token)
    upload_url = f"{base_url.rstrip('/')}/mcp-files/slots/{slot}"
    return {
        "upload_id": slot,
        "upload_url": upload_url,
        "method": "PUT",
        "max_bytes": max_bytes,
        "expires_at": time.time() + _UPLOAD_TTL_SECONDS,
        "example": f"curl -T <file> '{upload_url}'",
    }


def read_local_file(path: str) -> bytes:
    """``path``'s bytes, from the per-call upload cache. Only valid to call after
    ``require_local_files([path], ...)`` has already succeeded for this path in this call."""
    state = _current_state()
    if state is not None and path in state.resolved:
        return state.resolved[path]
    raise LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)


def local_file_size(path: str) -> int:
    """The size of ``path``'s upload, with the same resolution as ``read_local_file``."""
    state = _current_state()
    if state is not None and path in state.resolved:
        return len(state.resolved[path])
    raise LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)


def check_delivery_size(size: int) -> None:
    """Raise ``LocalFileAccessError`` if ``size`` bytes exceed ``file_bridge.
    max_download_bytes``. ``deliver_export`` calls it; a caller that builds the
    data before asking for approval calls it first, so a file that can never
    be delivered does not cost an approval."""
    if size > _max_download_bytes:
        raise LocalFileAccessError(
            f"This file is {size:,} bytes, over PrivacyFence's {_max_download_bytes:,}-byte "
            "file-bridge delivery limit (file_bridge.max_download_bytes in settings.yaml). Ask "
            "for a narrower export or a different way to share it."
        )


def deliver_export(
    name: str, data: bytes, mime_type: str, *,
    download_config: DownloadDeliveryConfig | None, download_base_url: str,
) -> dict[str, Any]:
    """Where an export's file goes after its approval (ADR 0160): a one-time staged link like a
    large Drive download's."""
    check_delivery_size(len(data))
    cfg = download_config or DownloadDeliveryConfig()
    token = get_download_staging_store().stage(
        current_principal(), data, name, mime_type, ttl_seconds=cfg.link_ttl_seconds,
    )
    mark_staged_download()
    return cfg.link_result(name, len(data), f"{download_base_url}{cfg.staged_link_path(token)}")


__all__ = [
    "DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES",
    "DEFAULT_MAX_DOWNLOAD_BYTES",
    "LocalFileAccessError",
    "NO_BRIDGE_UPLOAD_MESSAGE",
    "UPLOAD_REF_PREFIX",
    "build_upload_slot",
    "call_context",
    "call_produced_deliveries",
    "check_delivery_size",
    "configure_file_bridge",
    "deliver_export",
    "commit_uploads",
    "local_file_size",
    "mark_staged_download",
    "read_local_file",
    "require_local_files",
]
