"""The local file bridge (ADR 0007): the single place that decides how a
local path an agent named in a tool call -- a Drive upload's ``local_path``,
a download's ``destination_dir``, a Gmail/Confluence attachment path -- gets
turned into bytes, or bytes get turned into a file on the user's disk.

Privilege separation (ADR 0003) runs the daemon under its own OS account, so
a plain ``open()`` against a path the agent supplied cannot reach the real
user's home directory. No connector calls ``open()``, ``os.path.isfile``,
``os.path.getsize`` or ``os.path.expanduser`` on an agent-supplied path; each
calls through here instead -- this module decides,
per call, whether the daemon can read/write the path directly (an
unseparated dev checkout or pip/pipx install, where the daemon *is* the
user) or must route the bytes through the ``.mcpb`` shim, which runs as the
user and already sits on the request path of every tool call
(ADR 0007 D1). A client with no shim (Claude Code, any other direct HTTP
client, or an old ``.mcpb``) gets a clearly worded fallback instead of a
bare failure.

State for the current ``tools/call`` dispatch -- whether the calling shim
advertised the bridge, which paths it already uploaded, the bytes claimed
from those uploads, and any downloads staged for the shim to write -- lives
in a contextvar scoped by ``call_context()``, entered once per dispatch in
``web/routes_mcp.py``'s ``handle_call_tool`` exactly like ``principal.
principal_scope``/``gate.reason_scope`` already scope their own per-call
state.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

from . import privilege_separation
from .download_staging import DEFAULT_TTL_SECONDS as _DOWNLOAD_TTL_SECONDS
from .download_staging import get_download_staging_store
from .principal import current_principal
from .upload_staging import DEFAULT_TTL_SECONDS as _UPLOAD_TTL_SECONDS
from .upload_staging import get_upload_staging_store

if TYPE_CHECKING:
    from .principal import Principal

logger = logging.getLogger(__name__)

# Shown verbatim to the model (LocalFileAccessError is a
# ValueError subclass -- see safe_errors.public_message()'s passthrough
# rule) when a tool needs to read a local path but neither a direct read
# nor a bridge handshake is available. A capability upload slot
# (privacyfence_create_upload_slot, ADR 0028) is the way forward for a
# client with no shim at all -- Claude Code, org mode, or an old .mcpb -- so
# this message leads with that rather
# than content_base64, which still works for drive_upload_file alone and
# stays mentioned as the no-round-trip alternative for a small file.
NO_BRIDGE_UPLOAD_MESSAGE = (
    "PrivacyFence runs under its own system account and cannot read files in your home "
    "folder directly. Call privacyfence_create_upload_slot to get a URL you can PUT the "
    "file's bytes to (no PrivacyFence extension needed), then pass its upload_id back to "
    "this tool. For drive_upload_file specifically, passing the file content as "
    "content_base64 also works for a small file."
)

# ADR 0007 SS1.1's one vendor _meta namespace -- the single source of truth
# for this string; web/routes_mcp.py imports it rather than redefining its
# own copy (the shim's own TypeScript side necessarily keeps its own literal,
# see mcpb/shim/src/fileBridge.ts -- there's no shared module for the two
# languages to import from).
META_KEY = "privacyfence.eu/file-bridge"

# deliver_file() is synchronous and fully in memory (both the bridge and
# no-bridge-link paths stage the whole file before anything is written), so
# this is a real memory cap, not just a transfer-size guideline.
# Configurable via settings.yaml's `file_bridge: max_download_bytes:` -- see
# daemon_main.py's own local-mode setup, which
# calls configure_file_bridge() once at startup with whatever that section
# resolves to. 200MB: generous for anything a Drive/Gmail/Confluence tool
# plausibly downloads, small enough that holding one in memory on a
# single-user desktop install is a non-event.
DEFAULT_MAX_DOWNLOAD_BYTES = 200_000_000

# Capability uploads (ADR 0028): a path prefixed this way in any
# require_local_files()/read_local_file()/local_file_size() call is not a
# filesystem path at all -- it names bytes already staged in
# UploadStagingStore by privacyfence_create_upload_slot, waiting to be
# claimed for *this* tool call. Recognized unconditionally, before the
# direct-read/bridge-handshake decision below, so it works in every mode
# (including org mode, where can_access_user_files() is always False and
# there is no bridge at all) -- a capability slot's authorization is the
# token itself, not the daemon's read access to anything.
UPLOAD_REF_PREFIX = "upload:"

# The cap privacyfence_create_upload_slot enforces on a capability
# upload. A slot is created before any connector-specific tool is named, so
# there's no per-tool ceiling to size it against the way
# connectors/drive.py's _UPLOAD_MAX_BYTES sizes drive_upload_file's own
# local_path bridge cap -- reusing that same 50MB figure here isn't a
# coincidence, it's the same "one multi-megabyte file" ceiling applied to
# the one path that has to pick a single number for every tool that might
# later consume an upload_id.
DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES = 50_000_000

_max_download_bytes = DEFAULT_MAX_DOWNLOAD_BYTES


def configure_file_bridge(*, max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES) -> None:
    """Called once by daemon_main.py at startup, mirroring gate.py's
    ``configure_popup_executor`` -- settings.yaml is read once there, not
    re-parsed on every download. Safe to call more than once (e.g. from a
    test); the new value simply replaces the old one."""
    global _max_download_bytes
    _max_download_bytes = max_download_bytes


class LocalFilesNeeded(Exception):  # noqa: N818 -- control flow, not an error the model ever sees
    """Raised by ``require_local_files()`` when one or more paths must be
    fetched through the upload handshake before the tool can proceed.
    ``web/routes_mcp.py``'s ``handle_call_tool`` catches this *before* the
    generic ``except Exception`` and answers with a ``need_uploads``
    response (ADR 0007 §1.1) instead of letting it reach ``safe_errors``."""

    def __init__(self, paths: list[str], max_bytes: int) -> None:
        self.paths = paths
        self.max_bytes = max_bytes
        super().__init__(f"local files needed via the file bridge: {paths!r}")


class LocalFileAccessError(ValueError):
    """Every message this module raises is about the user's own local path
    -- never about connector/OAuth internals -- so it's safe to show
    verbatim. A ``ValueError`` subclass is what makes that happen:
    ``safe_errors.public_message()`` passes a ``ValueError``'s ``str()``
    through unchanged (after secret redaction) instead of replacing it with
    the generic "Tool call failed" message. See safe_errors.py."""


@dataclass
class _CallState:
    bridge_available: bool
    base_url: str
    uploads: dict[str, str]
    resolved: dict[str, bytes] = field(default_factory=dict)
    pending_deliveries: list[dict[str, Any]] = field(default_factory=list)
    staged_download: bool = False


_state_ctx: ContextVar["_CallState | None"] = ContextVar("privacyfence_file_bridge_call", default=None)


@contextmanager
def call_context(*, bridge_available: bool, uploads: dict[str, str], base_url: str = "") -> Iterator[_CallState]:
    """Scopes one ``tools/call`` dispatch's file-bridge state. ``uploads``
    is the ``{declared_path: slot_token}`` map the shim resent on a
    second-round request (empty on a first-round call); ``bridge_available``
    is whether the calling shim advertised ``X-PrivacyFence-File-Bridge`` on
    this request; ``base_url`` is the ``/mcp`` origin, used only to build an
    absolute download URL for the no-bridge fallback -- a bridge-
    capable client never sees it, since the shim already knows its own
    daemon origin."""
    state = _CallState(bridge_available=bridge_available, base_url=base_url, uploads=dict(uploads))
    token = _state_ctx.set(state)
    try:
        yield state
    finally:
        _state_ctx.reset(token)


def _current_state() -> "_CallState | None":
    return _state_ctx.get()


_force_bridge_for_tests = False


def force_bridge_for_tests(enabled: bool) -> None:
    """Test-only: makes ``can_access_user_files()`` return ``False``
    regardless of real privilege-separation state, so an integration test
    can exercise the file-bridge wire protocol (ADR 0007) end to end
    without provisioning a real separated install (which needs root/admin
    and a platform-specific service account -- infeasible in CI). Never
    called from production code -- daemon_main.py has no reference to this
    function. tests/conftest.py resets it to ``False`` between tests, same
    as ``configure_file_bridge()``."""
    global _force_bridge_for_tests
    _force_bridge_for_tests = enabled


def can_access_user_files(download_mode: str) -> bool:
    """True only when the daemon runs as the same OS user as its client:
    local mode (``download_mode != "org"``) and privilege separation is not
    enabled. Dev checkouts and unseparated pip/pipx installs keep direct
    file I/O -- see ADR 0007. Org mode is not a file-bridge case at all: it
    already has its own delivery path (org_mode.DownloadDeliveryConfig /
    staged links), and its daemon does
    not run on the agent's machine in the first place, so a local path
    would be meaningless there regardless of privilege separation."""
    if _force_bridge_for_tests:
        return False
    return download_mode != "org" and not privilege_separation.is_enabled()


def call_produced_deliveries() -> bool:
    """``McpDispatcher.call()`` checks this right after a successful
    dispatch to decide whether the result may be reused from its dedupe
    cache. A result that staged a download (bridge delivery *or* the
    no-bridge link fallback -- either way, a single-use token from
    ``download_staging.DownloadStagingStore``) must never be replayed:
    reusing it would point a second caller at a token the first claim
    already consumed, or hand out a link whose token silently stopped
    working. See mcp_dispatch.py."""
    state = _current_state()
    return bool(state and state.staged_download)


def _expand(path: str) -> str:
    return os.path.expanduser(path.strip())


def _encode_token(token: bytes) -> str:
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")


def _decode_token(raw: str) -> bytes | None:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        token = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return None
    return token if len(token) == 32 else None


def _claim_upload(state: "_CallState", path: str, slot_b64: str) -> None:
    token = _decode_token(slot_b64)
    if token is None:
        raise LocalFileAccessError(f"Could not read {path!r} for upload: invalid upload reference")
    data = get_upload_staging_store().claim(token, current_principal().id)
    if data is None:
        raise LocalFileAccessError(
            f"Could not read {path!r} for upload: the upload expired or was already used. "
            "Call the tool again."
        )
    state.resolved[path] = data


def require_local_files(paths: list[str], *, max_total_bytes: int, download_mode: str) -> None:
    """Called by a connector at the very top of its tool method, before any
    preview/PII-scan/gate work, so a missing or unreadable file is
    reported, or a bridge handshake started, before the human is ever asked
    to approve anything.

    For each path: already claimed from an upload this call -> nothing
    more to do. A capability-slot ``upload:<id>`` reference -> claim it from
    ``UploadStagingStore`` for the current principal now, unconditionally
    -- this bypasses the direct-read/bridge decision entirely (a capability
    slot works in org mode and in every no-bridge case, precisely because
    it needs neither), and a wrong principal gets the exact same
    ``LocalFileAccessError`` an expired or already-claimed slot does (no
    oracle, same as the slot's own 404-shaped HTTP route). The daemon can
    read the user's files directly (``can_access_user_files``) -> nothing
    to do, ``read_local_file``/``local_file_size`` will open it directly. A
    bridge-capable shim is on the other end -> collect the path. None of
    the above -> raise ``LocalFileAccessError`` immediately (shown to
    the model verbatim).

    Raises ``LocalFilesNeeded`` once, listing every path that needs the
    upload handshake at once (not one at a time), if anything was
    collected.
    """
    state = _current_state()
    direct_ok = can_access_user_files(download_mode)
    needed: list[str] = []
    for path in paths:
        if state is not None and path in state.resolved:
            continue
        if path.startswith(UPLOAD_REF_PREFIX):
            if state is None:
                raise LocalFileAccessError(f"Could not read {path!r}: no active call to claim it in")
            _claim_upload(state, path, path[len(UPLOAD_REF_PREFIX):])
            continue
        if state is not None:
            slot_b64 = state.uploads.get(path)
            if slot_b64 is not None:
                _claim_upload(state, path, slot_b64)
                continue
        if direct_ok:
            continue
        if state is not None and state.bridge_available:
            needed.append(path)
            continue
        raise LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)
    if needed:
        raise LocalFilesNeeded(needed, max_total_bytes)


def build_upload_slot(
    principal: "Principal", *, filename: str, size_bytes: int | None, base_url: str,
) -> dict[str, Any]:
    """Handles the ``privacyfence_create_upload_slot`` meta-tool (ADR 0007's
    "Clients without the bridge" section, and ADR 0028): mints an
    ``UploadStagingStore`` slot and returns a capability URL any HTTP
    client can ``PUT`` bytes to directly, with **no bearer header** -- the
    32-byte token embedded in ``upload_url`` is itself the credential,
    which is what makes this reachable from a client that has no way to
    set a custom header at all (a sandboxed agent shelling out to `curl`).
    This is deliberately a different route (``/mcp-files/slots/<token>``,
    unauthenticated) from the shim-only ``/mcp-files/uploads/<slot>``
    (bearer-authenticated, reached only by a shim that already carries the
    daemon's bearer token on every request) -- see web/routes_file_bridge.py.

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


def read_local_file(path: str, *, download_mode: str) -> bytes:
    """``path``'s bytes -- from the per-call upload cache if it was fetched
    through the bridge this call, otherwise a direct read. Only valid to
    call after ``require_local_files([path], ...)`` has already succeeded
    for this path in this call; that guarantees one of the two branches
    below is reachable."""
    state = _current_state()
    if state is not None and path in state.resolved:
        return state.resolved[path]
    if not can_access_user_files(download_mode):
        raise LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)
    expanded = _expand(path)
    try:
        with open(expanded, "rb") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning("local_files: direct read of %r failed: %s", path, exc)
        raise LocalFileAccessError(f"Could not read {path!r}: {exc.strerror or exc}") from exc


def local_file_size(path: str, *, download_mode: str) -> int:
    """Same resolution order as ``read_local_file``, without reading the
    whole file into memory when a direct stat is enough (the preview/stat
    path most callers use before deciding whether to read content at
    all)."""
    state = _current_state()
    if state is not None and path in state.resolved:
        return len(state.resolved[path])
    if not can_access_user_files(download_mode):
        raise LocalFileAccessError(NO_BRIDGE_UPLOAD_MESSAGE)
    expanded = _expand(path)
    if not os.path.isfile(expanded):
        raise LocalFileAccessError(f"Could not read {path!r}: no such file")
    return os.path.getsize(expanded)


def deliver_file(
    dest_dir: str, name: str, data: bytes, mime_type: str, *, download_mode: str,
) -> dict[str, Any]:
    """The write side of the bridge: ``dest_dir``/``name`` are exactly as
    the agent passed them (never daemon-expanded -- the shim, not the
    daemon, resolves ``~`` and writes the file, see ADR 0007 guard G1).
    Returns a dict a connector merges straight into its own
    ``structuredContent``:

    - direct write (``can_access_user_files``) -> ``{"path": <real path>,
      "delivery": "local_disk"}``, unchanged from pre-Phase-1 behaviour.
    - bridge available -> stages ``data`` in ``DownloadStagingStore`` for
      ``current_principal()``, records a pending delivery for
      ``handle_call_tool`` to attach as the ``deliver`` ``_meta`` op, and
      returns ``{"path": None, "delivery": "client_bridge"}`` -- the shim
      rewrites both fields once it has actually written the file.
    - neither -> link fallback: stages the same way, but returns a
      ``download_url`` the caller can ``curl`` directly with its own
      bearer token, since there is no shim to intercept anything.

    Raises ``LocalFileAccessError`` if ``data`` exceeds ``file_bridge.
    max_download_bytes`` (see ``configure_file_bridge``) -- applies to
    every branch, direct writes included, since ``data`` is already fully
    in memory by the time this is called either way.
    """
    if len(data) > _max_download_bytes:
        raise LocalFileAccessError(
            f"This file is {len(data):,} bytes, over PrivacyFence's {_max_download_bytes:,}-byte "
            "file-bridge delivery limit (file_bridge.max_download_bytes in settings.yaml). Ask "
            "for a narrower export or a different way to share it."
        )
    if can_access_user_files(download_mode):
        return _deliver_direct(dest_dir, name, data)
    state = _current_state()
    if state is not None and state.bridge_available:
        return _deliver_bridge(state, dest_dir, name, data, mime_type)
    return _deliver_link(state, name, data, mime_type)


def build_need_uploads_files(principal: "Principal", needed: LocalFilesNeeded) -> list[dict[str, Any]]:
    """Creates one ``upload_staging`` slot per path in ``needed.paths`` and
    returns the wire-format ``files`` list for a ``need_uploads`` response
    (ADR 0007 SS1.1) -- the one place a slot token is turned into the
    opaque ``slot``/``upload_path`` strings the shim sees. Called from
    ``web/routes_mcp.py``'s ``handle_call_tool`` once it catches
    ``LocalFilesNeeded``."""
    store = get_upload_staging_store()
    files: list[dict[str, Any]] = []
    for path in needed.paths:
        token = store.create_slot(principal, path, needed.max_bytes)
        slot = _encode_token(token)
        files.append({
            "path": path,
            "slot": slot,
            "upload_path": f"/mcp-files/uploads/{slot}",
            "max_bytes": needed.max_bytes,
        })
    return files


def _deliver_direct(dest_dir: str, name: str, data: bytes) -> dict[str, Any]:
    expanded_dir = _expand(dest_dir)
    safe_name = os.path.basename(name) or "file"
    os.makedirs(expanded_dir, exist_ok=True)
    target = os.path.join(expanded_dir, safe_name)
    try:
        with open(target, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        logger.warning("local_files: direct write to %r failed: %s", dest_dir, exc)
        raise LocalFileAccessError(f"Could not write to {dest_dir!r}: {exc.strerror or exc}") from exc
    return {"path": target, "name": safe_name, "size_bytes": len(data), "delivery": "local_disk"}


def _deliver_bridge(state: "_CallState", dest_dir: str, name: str, data: bytes, mime_type: str) -> dict[str, Any]:
    principal = current_principal()
    token = get_download_staging_store().stage(principal, data, name, mime_type)
    safe_name = os.path.basename(name) or "file"
    state.pending_deliveries.append({
        "dest_dir": dest_dir,
        "name": safe_name,
        "download_path": f"/mcp-files/downloads/{_encode_token(token)}",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "result_path_pointer": "/path",
    })
    state.staged_download = True
    return {"path": None, "name": safe_name, "size_bytes": len(data), "delivery": "client_bridge"}


def _deliver_link(state: "_CallState | None", name: str, data: bytes, mime_type: str) -> dict[str, Any]:
    """A capability link (``/mcp-files/fetch/<token>``, no bearer
    header needed -- the token in the URL is the credential; ADR 0028)
    rather than the shim's bearer-authenticated
    ``/mcp-files/downloads/<token>``, since the whole point of this branch
    is a caller with no bridge and, frequently, no way to attach a custom
    header either (a sandboxed agent
    `curl`-ing a URL it was handed). See web/routes_file_bridge.py and
    local_files.build_upload_slot's own docstring for the upload-side
    counterpart of this same capability-URL shape."""
    principal = current_principal()
    token = get_download_staging_store().stage(principal, data, name, mime_type)
    base_url = (state.base_url if state is not None else "").rstrip("/")
    download_url = f"{base_url}/mcp-files/fetch/{_encode_token(token)}"
    if state is not None:
        state.staged_download = True
    safe_name = os.path.basename(name) or "file"
    return {
        "path": None,
        "name": safe_name,
        "size_bytes": len(data),
        "delivery": "link",
        "download_url": download_url,
        "expires_at": time.time() + _DOWNLOAD_TTL_SECONDS,
        "note": "Fetch this URL directly -- it's a one-time capability link, no auth header needed.",
    }


__all__ = [
    "DEFAULT_CAPABILITY_UPLOAD_MAX_BYTES",
    "DEFAULT_MAX_DOWNLOAD_BYTES",
    "LocalFileAccessError",
    "LocalFilesNeeded",
    "META_KEY",
    "NO_BRIDGE_UPLOAD_MESSAGE",
    "UPLOAD_REF_PREFIX",
    "build_need_uploads_files",
    "build_upload_slot",
    "call_context",
    "call_produced_deliveries",
    "can_access_user_files",
    "configure_file_bridge",
    "deliver_file",
    "force_bridge_for_tests",
    "local_file_size",
    "read_local_file",
    "require_local_files",
]
